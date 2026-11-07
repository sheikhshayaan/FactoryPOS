"""Double-entry accounting on integer paisa.

Recognition rules
- Revenue is recognised when an invoice is issued (Dr Receivable / Cr Sales).
- Customer money received before invoicing is a LIABILITY (Customer Advances), not revenue.
- Stock purchases are ASSETS (Inventory); cost reaches the P&L only when consumed (Dr 5000/5300/5900).
- Capital contributions and partner payments never touch revenue/expense.
- Job-level electricity/labor estimates are management figures: they are NOT posted. The real
  electricity bill / salary expense is what the ledger records, so nothing is counted twice.
Entries are immutable; corrections are reversals.
"""
from __future__ import annotations

import sqlite3
from datetime import date as _date
from typing import Iterable

from database.connection import claim_idempotency_key, transaction, utc_now
from services import audit_service
from services.auth_service import require_permission
from services.units import to_paisa


class AccountingError(Exception):
    pass


class InsufficientCash(AccountingError):
    pass


class DuplicateSubmission(AccountingError):
    pass


INVENTORY_MANAGED_SOURCES = {"stock"}      # must be corrected through inventory, not reversed here


def _valid_date(d: str) -> str:
    try:
        _date.fromisoformat(d)
    except (ValueError, TypeError) as e:
        raise AccountingError(f"Invalid date: {d!r} (use YYYY-MM-DD)") from e
    return d


def _acct(conn, code):
    r = conn.execute("SELECT * FROM accounts WHERE code=? AND is_active=1", (code,)).fetchone()
    if not r:
        raise AccountingError(f"Unknown or inactive account {code}")
    return r


def post(conn: sqlite3.Connection, *, entry_date: str, description: str, lines: Iterable[dict],
         user=None, source_type: str | None = None, source_id=None,
         reverses_entry_id: int | None = None) -> int:
    """Internal: post a balanced entry inside the caller's transaction (no permission check).
    line = {account, debit?, credit?, customer_id?, supplier_id?, partner_id?, employee_id?, memo?} in paisa."""
    _valid_date(entry_date)
    lines = list(lines)
    if len(lines) < 2:
        raise AccountingError("An entry needs at least two lines.")
    dr = cr = 0
    prepared = []
    for ln in lines:
        d, c = int(ln.get("debit", 0)), int(ln.get("credit", 0))
        if (d > 0) == (c > 0) or d < 0 or c < 0:
            raise AccountingError("Each line must have exactly one positive debit or credit.")
        a = _acct(conn, ln["account"])
        dr += d
        cr += c
        prepared.append((a["id"], d, c, ln))
    if dr != cr:
        raise AccountingError(f"Entry does not balance: debits {dr} != credits {cr}")
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO journal_entries(entry_date,description,source_type,source_id,reverses_entry_id,posted_by,posted_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (entry_date, description, source_type, None if source_id is None else str(source_id),
             reverses_entry_id, user["id"] if user else None, utc_now()))
        eid = cur.lastrowid
        for aid, d, c, ln in prepared:
            conn.execute(
                "INSERT INTO journal_lines(entry_id,account_id,debit_paisa,credit_paisa,customer_id,supplier_id,"
                "partner_id,employee_id,memo) VALUES (?,?,?,?,?,?,?,?,?)",
                (eid, aid, d, c, ln.get("customer_id"), ln.get("supplier_id"), ln.get("partner_id"),
                 ln.get("employee_id"), ln.get("memo")))
        if conn.execute("SELECT value FROM app_settings WHERE key='allow_negative_cash'").fetchone()[0] != "1":
            if balance(conn, "1000") < 0:
                raise InsufficientCash("Not enough cash for this payment.")
        return eid


def balance(conn, code: str, *, as_of: str | None = None, start: str | None = None, customer_id=None,
            supplier_id=None, partner_id=None) -> int:
    """Natural-side balance in paisa (assets/expenses: debit-credit; others: credit-debit)."""
    a = conn.execute("SELECT id,type FROM accounts WHERE code=?", (code,)).fetchone()
    if not a:
        raise AccountingError(f"Unknown account {code}")
    q = ("SELECT COALESCE(SUM(l.debit_paisa),0), COALESCE(SUM(l.credit_paisa),0) FROM journal_lines l "
         "JOIN journal_entries e ON e.id=l.entry_id WHERE l.account_id=?")
    args: list = [a["id"]]
    for col, v in (("l.customer_id", customer_id), ("l.supplier_id", supplier_id), ("l.partner_id", partner_id)):
        if v is not None:
            q += f" AND {col}=?"
            args.append(v)
    if as_of:
        q += " AND e.entry_date<=?"; args.append(as_of)
    if start:
        q += " AND e.entry_date>=?"; args.append(start)
    d, c = conn.execute(q, args).fetchone()
    return d - c if a["type"] in ("asset", "expense") else c - d


# ------------------------------------------------------------------ public operations
def _guard(conn, key, op):
    if key and not claim_idempotency_key(conn, key, op):
        raise DuplicateSubmission("This transaction was already recorded.")


def record_opening_cash(conn, actor, amount, on: str) -> int:
    require_permission(actor, "post_journal")
    p = to_paisa(amount)
    with transaction(conn):
        e = post(conn, entry_date=on, description="Opening cash balance", user=actor, source_type="opening",
                 lines=[{"account": "1000", "debit": p}, {"account": "3500", "credit": p}])
        audit_service.log(conn, "acct.opening_cash", user=actor, entity="journal", entity_id=e, details={"paisa": p})
        return e


def issue_invoice(conn, actor, *, invoice_no, customer_id, total, invoice_date, job_id=None, idem_key=None,
                  pieces=None, rate_paisa=None) -> int:
    require_permission(actor, "manage_invoices")
    p = to_paisa(total)
    if p <= 0:
        raise AccountingError("Invoice total must be positive.")
    with transaction(conn):
        _guard(conn, idem_key, "issue_invoice")
        e = post(conn, entry_date=invoice_date, description=f"Invoice {invoice_no}", user=actor,
                 source_type="invoice", source_id=invoice_no,
                 lines=[{"account": "1100", "debit": p, "customer_id": customer_id},
                        {"account": "4000", "credit": p, "customer_id": customer_id}])
        try:
            cur = conn.execute("INSERT INTO invoices(invoice_no,customer_id,job_id,invoice_date,total_paisa,entry_id,"
                               "created_by,created_at,pieces,rate_paisa) VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (invoice_no, customer_id, job_id, invoice_date, p, e, actor["id"], utc_now(), pieces, rate_paisa))
        except sqlite3.IntegrityError as ex:
            raise AccountingError(f"Cannot create invoice: {ex}") from ex
        audit_service.log(conn, "invoice.issue", user=actor, entity="invoice", entity_id=cur.lastrowid,
                          details={"paisa": p})
        return cur.lastrowid


def invoice_outstanding(conn, invoice_id) -> int:
    r = conn.execute("SELECT total_paisa - COALESCE((SELECT SUM(amount_paisa) FROM invoice_allocations WHERE invoice_id=i.id),0) "
                     "FROM invoices i WHERE id=?", (invoice_id,)).fetchone()
    if r is None:
        raise AccountingError("Invoice not found.")
    return r[0]


def receive_payment(conn, actor, *, customer_id, amount, paid_on, method="cash", reference="",
                    invoice_id=None, idem_key=None) -> int:
    """Customer cash in. Against an invoice -> reduces receivable. Without -> customer ADVANCE (liability)."""
    require_permission(actor, "record_payment")
    p = to_paisa(amount)
    if p <= 0:
        raise AccountingError("Amount must be positive.")
    with transaction(conn):
        _guard(conn, idem_key, "receive_payment")
        if invoice_id is not None:
            inv = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
            if not inv or inv["customer_id"] != customer_id:
                raise AccountingError("Invoice not found for this customer.")
            if p > invoice_outstanding(conn, invoice_id):
                raise AccountingError("Payment exceeds the invoice balance; record the excess as an advance.")
            credit_acct = "1100"
        else:
            credit_acct = "2200"
        e = post(conn, entry_date=paid_on, description=f"Receipt {reference}".strip(), user=actor,
                 source_type="receipt", source_id=reference or None,
                 lines=[{"account": "1000", "debit": p},
                        {"account": credit_acct, "credit": p, "customer_id": customer_id}])
        if invoice_id is not None:
            conn.execute("INSERT INTO invoice_allocations(invoice_id,amount_paisa,kind,method,reference,paid_on,entry_id) "
                         "VALUES (?,?,?,?,?,?,?)", (invoice_id, p, "payment", method, reference, paid_on, e))
        audit_service.log(conn, "payment.receive", user=actor, entity="customer", entity_id=customer_id,
                          details={"paisa": p, "invoice": invoice_id, "advance": invoice_id is None})
        return e


def apply_advance(conn, actor, *, customer_id, invoice_id, amount, on, idem_key=None) -> int:
    require_permission(actor, "record_payment")
    p = to_paisa(amount)
    with transaction(conn):
        _guard(conn, idem_key, "apply_advance")
        inv = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if not inv or inv["customer_id"] != customer_id:
            raise AccountingError("Invoice not found for this customer.")
        if p <= 0 or p > invoice_outstanding(conn, invoice_id):
            raise AccountingError("Amount must be positive and not exceed the invoice balance.")
        if p > balance(conn, "2200", customer_id=customer_id):
            raise AccountingError("Customer has insufficient advance balance.")
        e = post(conn, entry_date=on, description="Apply customer advance", user=actor, source_type="advance_applied",
                 lines=[{"account": "2200", "debit": p, "customer_id": customer_id},
                        {"account": "1100", "credit": p, "customer_id": customer_id}])
        conn.execute("INSERT INTO invoice_allocations(invoice_id,amount_paisa,kind,paid_on,entry_id) VALUES (?,?,?,?,?)",
                     (invoice_id, p, "advance_applied", on, e))
        return e


def pay_supplier(conn, actor, *, supplier_id, amount, paid_on, reference="", idem_key=None) -> int:
    require_permission(actor, "record_payment")
    p = to_paisa(amount)
    with transaction(conn):
        _guard(conn, idem_key, "pay_supplier")
        if p <= 0 or p > balance(conn, "2000", supplier_id=supplier_id):
            raise AccountingError("Amount must be positive and not exceed what is owed to this supplier.")
        e = post(conn, entry_date=paid_on, description=f"Supplier payment {reference}".strip(), user=actor,
                 source_type="supplier_payment", source_id=reference or None,
                 lines=[{"account": "2000", "debit": p, "supplier_id": supplier_id}, {"account": "1000", "credit": p}])
        audit_service.log(conn, "payment.supplier", user=actor, entity="supplier", entity_id=supplier_id,
                          details={"paisa": p})
        return e


def supplier_advance(conn, supplier_id) -> int:
    """Money the supplier holds for us (we paid ahead, or returned goods worth more than we owed). 0 when we owe him or are square."""
    return max(0, -balance(conn, "2000", supplier_id=supplier_id))


def receive_supplier_refund(conn, actor, *, supplier_id, amount, received_on, reference="", idem_key=None) -> int:
    """The supplier gives back money he is holding for us (a negative 'We owe'). Cash comes in and his balance moves toward zero.
    This is NOT income: it only clears the advance, so profit is untouched."""
    require_permission(actor, "record_payment")
    p = to_paisa(amount)
    with transaction(conn):
        _guard(conn, idem_key, "supplier_refund")
        if conn.execute("SELECT 1 FROM suppliers WHERE id=?", (supplier_id,)).fetchone() is None:
            raise AccountingError("Supplier not found.")
        held = supplier_advance(conn, supplier_id)
        if held <= 0:
            raise AccountingError("This supplier is not holding any advance of ours, so there is nothing to receive back.")
        if p <= 0 or p > held:
            raise AccountingError("Amount must be positive and not more than the advance the supplier is holding.")
        e = post(conn, entry_date=received_on, description=f"Supplier refund {reference}".strip(), user=actor,
                 source_type="supplier_refund", source_id=reference or None,
                 lines=[{"account": "1000", "debit": p}, {"account": "2000", "credit": p, "supplier_id": supplier_id}])
        audit_service.log(conn, "refund.supplier", user=actor, entity="supplier", entity_id=supplier_id, details={"paisa": p})
        return e


def record_expense(conn, actor, *, account_code, amount, on, description, supplier_id=None, on_credit=False,
                   idem_key=None) -> int:
    """Operating/overhead expense (cash, or on credit to a supplier). Only expense accounts allowed."""
    require_permission(actor, "record_expense")
    p = to_paisa(amount)
    with transaction(conn):
        _guard(conn, idem_key, "record_expense")
        if p <= 0:
            raise AccountingError("Amount must be positive.")
        if _acct(conn, account_code)["type"] != "expense":
            raise AccountingError("Not an expense account.")
        if account_code in ("5000", "5300", "5900"):
            raise AccountingError("That account is posted automatically from inventory.")
        if account_code in ("5100", "6100"):
            raise AccountingError("Salaries are booked on the Attendance & Salaries page, not as an expense.")
        if on_credit and not supplier_id:
            raise AccountingError("A supplier is required for expenses on credit.")
        e = post(conn, entry_date=on, description=description, user=actor, source_type="expense",
                 lines=[{"account": account_code, "debit": p},
                        {"account": "2000" if on_credit else "1000", "credit": p,
                         "supplier_id": supplier_id if on_credit else None}])
        audit_service.log(conn, "expense.record", user=actor, entity="journal", entity_id=e,
                          details={"account": account_code, "paisa": p})
        return e


def record_capex(conn, actor, *, amount, on, description, supplier_id=None, on_credit=False) -> int:
    """Capital expenditure -> asset (not an expense)."""
    require_permission(actor, "record_expense")
    p = to_paisa(amount)
    if p <= 0 or (on_credit and not supplier_id):
        raise AccountingError("Positive amount required; supplier required on credit.")
    with transaction(conn):
        return post(conn, entry_date=on, description=description, user=actor, source_type="capex",
                    lines=[{"account": "1500", "debit": p},
                           {"account": "2000" if on_credit else "1000", "credit": p,
                            "supplier_id": supplier_id if on_credit else None}])


def reverse_entry(conn, actor, entry_id: int, reason: str, on: str | None = None) -> int:
    require_permission(actor, "post_journal")
    if not reason:
        raise AccountingError("A reason is required.")
    with transaction(conn):
        e = conn.execute("SELECT * FROM journal_entries WHERE id=?", (entry_id,)).fetchone()
        if not e:
            raise AccountingError("Entry not found.")
        if e["source_type"] in INVENTORY_MANAGED_SOURCES:
            raise AccountingError("Stock postings are corrected through inventory adjustments, not reversed here.")
        if e["reverses_entry_id"]:
            raise AccountingError("A reversal cannot itself be reversed; post a new entry.")
        if conn.execute("SELECT 1 FROM journal_entries WHERE reverses_entry_id=?", (entry_id,)).fetchone():
            raise AccountingError("This entry was already reversed.")
        if conn.execute("SELECT 1 FROM invoice_allocations WHERE entry_id=?", (entry_id,)).fetchone() or \
           conn.execute("SELECT 1 FROM invoices WHERE entry_id=?", (entry_id,)).fetchone() or \
           e["source_type"] in ("distribution", "distribution_payment"):
            raise AccountingError("This entry is linked to an invoice/distribution record; correct it with a new "
                                  "transaction (credit note / refund) instead.")
        lines = conn.execute("SELECT j.*, a.code FROM journal_lines j JOIN accounts a ON a.id=j.account_id "
                             "WHERE entry_id=?", (entry_id,)).fetchall()
        rid = post(conn, entry_date=on or e["entry_date"], description=f"REVERSAL of #{entry_id}: {reason}",
                   user=actor, source_type="reversal", source_id=entry_id, reverses_entry_id=entry_id,
                   lines=[{"account": l["code"], "debit": l["credit_paisa"], "credit": l["debit_paisa"],
                           "customer_id": l["customer_id"], "supplier_id": l["supplier_id"],
                           "partner_id": l["partner_id"], "employee_id": l["employee_id"]} for l in lines])
        audit_service.log(conn, "journal.reverse", user=actor, entity="journal", entity_id=entry_id, reason=reason)
        return rid


# ------------------------------------------------------------------ reports
def trial_balance(conn, as_of: str | None = None) -> list[dict]:
    out = []
    for a in conn.execute("SELECT * FROM accounts ORDER BY code"):
        q = ("SELECT COALESCE(SUM(l.debit_paisa),0), COALESCE(SUM(l.credit_paisa),0) FROM journal_lines l "
             "JOIN journal_entries e ON e.id=l.entry_id WHERE l.account_id=?")
        args = [a["id"]]
        if as_of:
            q += " AND e.entry_date<=?"; args.append(as_of)
        d, c = conn.execute(q, args).fetchone()
        if d or c:
            out.append({"code": a["code"], "name": a["name"], "debit": d, "credit": c})
    return out


def income_statement(conn, start: str, end: str) -> dict:
    rows = conn.execute(
        "SELECT a.type, a.category, a.code, a.name, COALESCE(SUM(l.debit_paisa),0) d, COALESCE(SUM(l.credit_paisa),0) c "
        "FROM journal_lines l JOIN journal_entries e ON e.id=l.entry_id JOIN accounts a ON a.id=l.account_id "
        "WHERE e.entry_date BETWEEN ? AND ? AND a.type IN ('revenue','expense') GROUP BY a.id", (start, end)).fetchall()
    revenue = sum(r["c"] - r["d"] for r in rows if r["type"] == "revenue")
    cat = {k: sum(r["d"] - r["c"] for r in rows if r["type"] == "expense" and r["category"] == k)
           for k in ("direct_production", "factory_overhead", "selling_admin")}
    cop = cat["direct_production"] + cat["factory_overhead"]
    gross = revenue - cop
    return {"revenue": revenue, "direct_production": cat["direct_production"], "factory_overhead": cat["factory_overhead"],
            "cost_of_production": cop, "gross_profit": gross, "selling_admin": cat["selling_admin"],
            "net_profit": gross - cat["selling_admin"]}


def balance_sheet(conn, as_of: str) -> dict:
    tot = {t: 0 for t in ("asset", "liability", "equity", "revenue", "expense")}
    for a in conn.execute("SELECT code,type FROM accounts"):
        tot[a["type"]] += balance(conn, a["code"], as_of=as_of)
    earnings = tot["revenue"] - tot["expense"]
    return {"assets": tot["asset"], "liabilities": tot["liability"], "equity_contributed": tot["equity"],
            "retained_earnings_to_date": earnings,
            "balanced": tot["asset"] == tot["liability"] + tot["equity"] + earnings}


def check_integrity(conn) -> list[str]:
    """Problems found (empty list = consistent): unbalanced entries, inventory/AR/AP sub-ledger mismatches."""
    p = []
    for r in conn.execute("SELECT entry_id, SUM(debit_paisa) d, SUM(credit_paisa) c FROM journal_lines GROUP BY entry_id HAVING d<>c"):
        p.append(f"Entry {r['entry_id']} unbalanced")
    inv_sub = conn.execute("SELECT COALESCE(SUM(value_paisa),0) FROM material_stock").fetchone()[0]
    if inv_sub != balance(conn, "1200"):
        p.append(f"Inventory sub-ledger {inv_sub} != GL 1200 {balance(conn, '1200')}")
    ar_sub = conn.execute("SELECT COALESCE(SUM(total_paisa),0) - COALESCE((SELECT SUM(amount_paisa) FROM invoice_allocations),0) FROM invoices").fetchone()[0]
    if ar_sub != balance(conn, "1100"):
        p.append(f"Receivables sub-ledger {ar_sub} != GL 1100 {balance(conn, '1100')}")
    sal_sub = conn.execute("SELECT COALESCE(SUM(net_paisa),0) - COALESCE((SELECT SUM(amount_paisa) FROM payslip_payments),0) FROM payslips").fetchone()[0]
    if sal_sub != balance(conn, "2300"):
        p.append(f"Salaries payable sub-ledger {sal_sub} != GL 2300 {balance(conn, '2300')}")
    if conn.execute("SELECT COALESCE(SUM(debit_paisa),0)-COALESCE(SUM(credit_paisa),0) FROM journal_lines WHERE supplier_id IS NULL AND account_id=(SELECT id FROM accounts WHERE code='2000')").fetchone()[0] != 0:
        p.append("Accounts payable has lines without a supplier")
    return p
