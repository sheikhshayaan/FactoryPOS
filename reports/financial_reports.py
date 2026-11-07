"""Report definitions: (headers, rows, subtitle). Money columns are Decimal rupees. Read-only."""
from __future__ import annotations

from decimal import Decimal

from services import accounting_service as acct, dashboard_service as ds, queries as q
from services.units import from_milli, from_paisa


def _r(p):
    return from_paisa(int(p))


def trial_balance(conn, start, end):
    rows = [[t["code"], t["name"], _r(t["debit"]), _r(t["credit"])] for t in acct.trial_balance(conn, end)]
    rows.append(["", "TOTAL", sum((r[2] for r in rows), Decimal(0)), sum((r[3] for r in rows), Decimal(0))])
    return ["Code", "Account", "Debit", "Credit"], rows, f"As of {end}"


def income_statement(conn, start, end):
    s = acct.income_statement(conn, start, end)
    rows = [["Sales revenue", _r(s["revenue"])], ["Direct production costs", _r(s["direct_production"])], ["Factory overheads", _r(s["factory_overhead"])],
            ["Total cost of production", _r(s["cost_of_production"])], ["GROSS PROFIT", _r(s["gross_profit"])], ["Operating expenses (Chai Paani, office, other)", _r(s["selling_admin"])], ["NET PROFIT", _r(s["net_profit"])]]
    return ["Item", "Amount (Rs)"], rows, f"{start} to {end}. Accounting profit - not the same as cash."


def balance_sheet(conn, start, end):
    b = acct.balance_sheet(conn, end)
    rows = [["Assets", _r(b["assets"])], ["Liabilities", _r(b["liabilities"])], ["Partner capital & equity", _r(b["equity_contributed"])], ["Profit retained to date", _r(b["retained_earnings_to_date"])],
            ["Balanced", "yes" if b["balanced"] else "NO - investigate"]]
    return ["Item", "Amount (Rs)"], rows, f"As of {end}"


def stock_report(conn, start, end):
    rows = []
    for m in q.materials(conn):
        avg = (Decimal(m["value_paisa"]) / 100 / (Decimal(m["qty_milli"]) / 1000)) if m["qty_milli"] else None
        rows.append([m["name"], m["category"], m["base_unit"], from_milli(m["qty_milli"]), m["rolls"] if m["tracks_rolls"] else "", None if avg is None else round(avg, 2),
                     _r(m["value_paisa"]), from_milli(m["cust_milli"])])
    return ["Material", "Category", "Unit", "Factory stock", "Rolls", "Avg cost / unit", "Value (Rs)", "Customer property (no value)"], rows, "Weighted-average valuation of factory-owned stock"


def receivables(conn, start, end):
    return ["Customer", "Outstanding (Rs)", "Advance held (Rs)"], [[c["name"], _r(c["outstanding"]), _r(c["advance"])] for c in q.customers(conn) if c["outstanding"] or c["advance"]], "Current balances"


def payables(conn, start, end):
    return ["Supplier", "We owe (Rs)"], [[s["name"], _r(s["owed"])] for s in q.suppliers(conn) if s["owed"]], "Current balances"


def salaries_due(conn, start, end):
    return ["Month", "Employee", "Net pay", "Still due"], [[p["period"], p["name"], _r(p["net_paisa"]), _r(p["due_paisa"])] for p in q.payslips(conn) if p["due_paisa"] > 0], "Unpaid and partly paid salaries"


def job_profitability(conn, start, end):
    rows = []
    for j in q.jobs(conn):
        price = j["final_price_paisa"] if j["final_price_paisa"] is not None else j["quoted_price_paisa"]
        profit = None if price is None else _r(price - j["cost_paisa"])
        margin = None if not price else round(Decimal(price - j["cost_paisa"]) * 100 / price, 2)
        rows.append([j["job_no"], j["customer"], j["fabric_source"], j["status"], _r(j["cost_paisa"]), None if price is None else _r(price), profit, margin])
    return ["Job", "Customer", "Fabric from", "Status", "Cost (Rs)", "Price (Rs)", "Profit (Rs)", "Margin %"], rows, "Price = final if finalised, else quoted. Costs may include labelled estimates."


def partner_statement(conn, start, end):
    rows = []
    for p in q.partners(conn):
        d = conn.execute("SELECT COALESCE(SUM(dl.amount_paisa),0), COALESCE(SUM(dl.paid_paisa),0) FROM distribution_lines dl WHERE dl.partner_id=?", (p["id"],)).fetchone()
        rows.append([p["name"], _r(p["capital_paisa"]), Decimal(p["ownership_bps"]) / 100, None if p["share_bps"] is None else Decimal(p["share_bps"]) / 100, _r(d[0]), _r(d[1]), _r(d[0] - d[1])])
    return ["Partner", "Capital (Rs)", "Ownership %", "Profit share %", "Distributions approved", "Paid", "Unpaid"], rows, "Ownership from capital; profit share as agreed"


def stock_ledger(conn, start, end):
    rows = [[r["created_at"][:10], r["material"], r["movement_type"], r["ownership"], from_milli(r["qty_milli"]), _r(r["value_paisa"]), f"{r['ref_type'] or ''} {r['ref_id'] or ''}".strip(), r["reason"] or "", r["user"] or ""]
            for r in q.movements(conn, limit=100000) if start <= r["created_at"][:10] <= end]
    return ["Date", "Material", "Type", "Owner", "Qty", "Value (Rs)", "Reference", "Reason", "By"], rows, f"{start} to {end}"


def expenses_report(conn, start, end):
    rows = [[e["entry_date"], e["account"], e["description"], _r(e["amount_paisa"])] for e in q.expenses(conn, 100000) if start <= e["entry_date"] <= end]
    return ["Date", "Category", "Description", "Amount (Rs)"], rows, f"{start} to {end}"


REPORTS = {
    "Trial balance": trial_balance, "Income statement": income_statement, "Balance sheet": balance_sheet, "Stock report": stock_report,
    "Customer receivables": receivables, "Supplier payables": payables, "Salaries due": salaries_due, "Job profitability": job_profitability,
    "Partner statement": partner_statement, "Stock ledger": stock_ledger, "Expenses": expenses_report,
}
