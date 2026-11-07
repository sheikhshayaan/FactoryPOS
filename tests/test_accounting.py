import sqlite3

import pytest

from services import accounting_service as ac, auth_service as auth, inventory_service as inv
from services import job_service as js, profit_service as ps
from services.units import to_paisa as P


@pytest.fixture
def w(conn, owner):
    auth.create_user(conn, owner, "acc", "accountant-pw", "accountant")
    acc = auth.authenticate(conn, "acc", "accountant-pw")
    cust = inv.add_customer(conn, owner, "Ali Traders")
    fab = inv.add_material(conn, owner, "Kapra", "fabric", "m")
    return dict(owner=owner, acc=acc, cust=cust, fab=fab)


def cash(conn): return ac.balance(conn, "1000")


def test_every_posted_entry_balances_and_is_immutable(conn, w):
    ac.record_opening_cash(conn, w["owner"], "100000", "2026-07-01")
    ac.record_expense(conn, w["acc"], account_code="6000", amount="500", on="2026-07-02", description="stationery")
    assert ac.check_integrity(conn) == []
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE journal_lines SET debit_paisa=1")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM journal_entries")


def test_unbalanced_entry_rejected_nothing_posted(conn, w):
    n = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
    with pytest.raises(ac.AccountingError, match="balance"):
        ac.post(conn, entry_date="2026-07-01", description="x", lines=[
            {"account": "1000", "debit": 100}, {"account": "4000", "credit": 90}])
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == n


def test_advance_is_liability_not_revenue(conn, w):
    ac.receive_payment(conn, w["acc"], customer_id=w["cust"], amount="10000", paid_on="2026-07-05")
    assert cash(conn) == P("10000") and ac.balance(conn, "2200") == P("10000")
    assert ac.income_statement(conn, "2026-07-01", "2026-07-31")["revenue"] == 0
    inv_id = ac.issue_invoice(conn, w["acc"], invoice_no="INV-1", customer_id=w["cust"], total="30000",
                              invoice_date="2026-07-10")
    assert ac.income_statement(conn, "2026-07-01", "2026-07-31")["revenue"] == P("30000")
    ac.apply_advance(conn, w["acc"], customer_id=w["cust"], invoice_id=inv_id, amount="10000", on="2026-07-10")
    assert ac.balance(conn, "2200") == 0 and ac.invoice_outstanding(conn, inv_id) == P("20000")
    ac.receive_payment(conn, w["acc"], customer_id=w["cust"], amount="20000", paid_on="2026-07-20", invoice_id=inv_id)
    assert ac.invoice_outstanding(conn, inv_id) == 0 and ac.balance(conn, "1100") == 0
    assert ac.income_statement(conn, "2026-07-01", "2026-07-31")["revenue"] == P("30000")   # unchanged by cash
    assert ac.check_integrity(conn) == []


def test_overpayment_and_overapplication_rejected(conn, w):
    i = ac.issue_invoice(conn, w["acc"], invoice_no="I", customer_id=w["cust"], total="100", invoice_date="2026-07-01")
    with pytest.raises(ac.AccountingError):
        ac.receive_payment(conn, w["acc"], customer_id=w["cust"], amount="101", paid_on="2026-07-02", invoice_id=i)
    with pytest.raises(ac.AccountingError):
        ac.apply_advance(conn, w["acc"], customer_id=w["cust"], invoice_id=i, amount="50", on="2026-07-02")


def test_purchase_is_asset_until_consumed(conn, w):
    ac.record_opening_cash(conn, w["owner"], "1000000", "2026-07-01")
    inv.receive_stock(conn, w["owner"], material_id=w["fab"], qty="100", supplier_id=1, total_cost="10000",
                      received_on="2026-07-03")
    assert ac.balance(conn, "1200") == P("10000") and ac.balance(conn, "2000", supplier_id=1) == P("10000")
    assert ac.income_statement(conn, "2026-07-01", "2026-07-31")["net_profit"] == 0     # not an expense yet
    inv.issue_stock(conn, w["owner"], material_id=w["fab"], qty="40", on_date="2026-07-10")
    assert ac.balance(conn, "1200") == P("6000")
    st = ac.income_statement(conn, "2026-07-01", "2026-07-31")
    assert st["direct_production"] == P("4000") and st["net_profit"] == -P("4000")
    ac.pay_supplier(conn, w["acc"], supplier_id=1, amount="10000", paid_on="2026-07-15")
    assert ac.balance(conn, "2000") == 0 and cash(conn) == P("990000")
    with pytest.raises(ac.AccountingError):
        ac.pay_supplier(conn, w["acc"], supplier_id=1, amount="1", paid_on="2026-07-16")   # nothing owed
    assert ac.check_integrity(conn) == []


def test_customer_owned_fabric_creates_no_entries(conn, w):
    n = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
    inv.receive_stock(conn, w["owner"], material_id=w["fab"], qty="300", ownership="customer", customer_id=w["cust"])
    inv.issue_stock(conn, w["owner"], material_id=w["fab"], qty="100", ownership="customer", customer_id=w["cust"])
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == n
    assert ac.balance(conn, "1200") == 0


def test_job_consumption_posts_once_and_sub_ledger_matches(conn, w):
    inv.receive_stock(conn, w["owner"], material_id=w["fab"], qty="100", supplier_id=1, total_cost="10000")
    j = js.create_job(conn, w["owner"], job_no="J1", customer_id=w["cust"], product_type="b", fabric_source="factory",
                      order_date="2026-07-01")
    js.set_status(conn, w["owner"], j, "approved")
    js.consume_material(conn, w["owner"], j, w["fab"], "25")
    js.consume_material(conn, w["owner"], j, w["fab"], "5", wastage=True, reason="cut error")
    assert ac.balance(conn, "5000") == P("2500") and ac.balance(conn, "5300") == P("500")
    assert ac.check_integrity(conn) == []


def test_cash_cannot_go_negative(conn, w):
    ac.record_opening_cash(conn, w["owner"], "100", "2026-07-01")
    with pytest.raises(ac.InsufficientCash):
        ac.record_expense(conn, w["acc"], account_code="6000", amount="101", on="2026-07-02", description="x")
    assert cash(conn) == P("100")


def test_duplicate_payment_blocked(conn, w):
    ac.receive_payment(conn, w["acc"], customer_id=w["cust"], amount="500", paid_on="2026-07-01", idem_key="r1")
    with pytest.raises(ac.DuplicateSubmission):
        ac.receive_payment(conn, w["acc"], customer_id=w["cust"], amount="500", paid_on="2026-07-01", idem_key="r1")
    assert cash(conn) == P("500")


def test_capex_and_capital_not_income_or_expense(conn, w):
    pid = ps.add_partner(conn, w["owner"], "Faizan")
    ps.record_capital(conn, w["owner"], pid, "500000", "2026-07-01")
    ac.record_capex(conn, w["acc"], amount="200000", on="2026-07-02", description="printing machine")
    st = ac.income_statement(conn, "2026-07-01", "2026-07-31")
    assert st["revenue"] == 0 and st["net_profit"] == 0
    assert ac.balance(conn, "1500") == P("200000") and cash(conn) == P("300000")
    assert ac.balance_sheet(conn, "2026-07-31")["balanced"]


def test_reverse_entry(conn, w):
    ac.record_opening_cash(conn, w["owner"], "1000", "2026-07-01")
    e = ac.record_expense(conn, w["acc"], account_code="6000", amount="100", on="2026-07-02", description="oops")
    ac.reverse_entry(conn, w["acc"], e, "entered twice")
    assert ac.balance(conn, "6000") == 0 and cash(conn) == P("1000")
    with pytest.raises(ac.AccountingError):
        ac.reverse_entry(conn, w["acc"], e, "again")
    with pytest.raises(ac.AccountingError):
        ac.reverse_entry(conn, w["acc"], e, "")


def test_stock_postings_cannot_be_reversed(conn, w):
    inv.receive_stock(conn, w["owner"], material_id=w["fab"], qty="1", supplier_id=1, total_cost="10")
    eid = conn.execute("SELECT id FROM journal_entries WHERE source_type='stock'").fetchone()[0]
    with pytest.raises(ac.AccountingError):
        ac.reverse_entry(conn, w["owner"], eid, "x")


def test_auditor_cannot_post(conn, owner):
    auth.create_user(conn, owner, "aud", "auditor-pw1", "auditor")
    aud = auth.authenticate(conn, "aud", "auditor-pw1")
    with pytest.raises(auth.PermissionDenied):
        ac.record_expense(conn, aud, account_code="6000", amount="1", on="2026-07-01", description="x")


# ------------------------------------------------------------------ partners / profit
@pytest.fixture
def firm(conn, w):
    a = ps.add_partner(conn, w["owner"], "A"); b = ps.add_partner(conn, w["owner"], "B"); c = ps.add_partner(conn, w["owner"], "C")
    ps.record_capital(conn, w["owner"], a, "600000", "2026-07-01")
    ps.record_capital(conn, w["owner"], b, "300000", "2026-07-01")
    ps.record_capital(conn, w["owner"], c, "100000", "2026-07-01")
    ps.set_profit_shares(conn, w["owner"], "2026-07-01", {a: 5000, b: 3000, c: 2000}, confirmed=True)
    return dict(a=a, b=b, c=c)


def make_profit(conn, w, amount="100000"):
    i = ac.issue_invoice(conn, w["acc"], invoice_no="P1", customer_id=w["cust"], total=amount, invoice_date="2026-08-01")
    ac.receive_payment(conn, w["acc"], customer_id=w["cust"], amount=amount, paid_on="2026-08-02", invoice_id=i)


def test_ownership_from_capital_and_shares_differ(conn, w, firm):
    o = ps.ownership(conn)
    assert [o[firm[k]]["ownership_bps"] for k in "abc"] == [6000, 3000, 1000]
    assert ps.shares_effective(conn, "2026-08-01") == {firm["a"]: 5000, firm["b"]: 3000, firm["c"]: 2000}


def test_shares_must_total_100_and_need_confirmation(conn, w, firm):
    with pytest.raises(ps.ProfitError):
        ps.set_profit_shares(conn, w["owner"], "2026-09-01", {firm["a"]: 5000, firm["b"]: 3000, firm["c"]: 1000}, confirmed=True)
    with pytest.raises(ps.ProfitError):
        ps.set_profit_shares(conn, w["owner"], "2026-09-01", {firm["a"]: 4000, firm["b"]: 3000, firm["c"]: 3000}, confirmed=False)


def test_allocations_equal_approved_amount(conn, w, firm):
    make_profit(conn, w, "100000.01")
    d = ps.approve_distribution(conn, w["owner"], period_start="2026-08-01", period_end="2026-08-31",
                                amount="100000.01", on="2026-08-31")
    lines = conn.execute("SELECT amount_paisa FROM distribution_lines WHERE distribution_id=?", (d,)).fetchall()
    assert sum(r[0] for r in lines) == P("100000.01")
    assert ac.balance(conn, "2400") == P("100000.01") and ac.check_integrity(conn) == []
    for n in (1, 7, 99999, 10001):
        assert sum(ps.allocate(n, {1: 3333, 2: 3333, 3: 3334}).values()) == n


def test_only_owner_approves_and_loss_not_distributed(conn, w, firm):
    make_profit(conn, w)
    with pytest.raises(auth.PermissionDenied):
        ps.approve_distribution(conn, w["acc"], period_start="2026-08-01", period_end="2026-08-31", amount="1", on="2026-08-31")
    with pytest.raises(ps.ProfitError):      # more than profit
        ps.approve_distribution(conn, w["owner"], period_start="2026-08-01", period_end="2026-08-31", amount="100001", on="2026-08-31")
    ac.record_expense(conn, w["acc"], account_code="6000", amount="150000", on="2026-09-05", description="loss month")
    with pytest.raises(ps.ProfitError):
        ps.approve_distribution(conn, w["owner"], period_start="2026-09-01", period_end="2026-09-30", amount="1", on="2026-09-30")


def test_distribution_not_double_counted_and_payment_is_cash_only(conn, w, firm):
    make_profit(conn, w)
    d = ps.approve_distribution(conn, w["owner"], period_start="2026-08-01", period_end="2026-08-31", amount="60000", on="2026-08-31")
    with pytest.raises(ps.ProfitError):      # remaining only 40,000
        ps.approve_distribution(conn, w["owner"], period_start="2026-08-01", period_end="2026-08-31", amount="40000.01", on="2026-08-31")
    before = ac.income_statement(conn, "2026-08-01", "2026-08-31")["net_profit"]
    ps.pay_distribution(conn, w["owner"], distribution_id=d, partner_id=firm["a"], amount="30000", on="2026-09-01")
    assert ac.income_statement(conn, "2026-08-01", "2026-09-30")["net_profit"] == before     # P&L untouched by payout
    with pytest.raises(ps.ProfitError):
        ps.pay_distribution(conn, w["owner"], distribution_id=d, partner_id=firm["a"], amount="0.01", on="2026-09-02")  # A fully paid
    assert ac.balance(conn, "2400") == P("30000")


def test_historical_shares_preserved(conn, w, firm):
    ps.set_profit_shares(conn, w["owner"], "2026-10-01", {firm["a"]: 4000, firm["b"]: 4000, firm["c"]: 2000}, confirmed=True)
    assert ps.shares_effective(conn, "2026-09-30")[firm["a"]] == 5000
    assert ps.shares_effective(conn, "2026-10-01")[firm["a"]] == 4000
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE partner_profit_shares SET share_bps=1")


def test_drawings_not_expense(conn, w, firm):
    ps.record_drawing(conn, w["owner"], firm["a"], "1000", "2026-07-10")
    assert ac.income_statement(conn, "2026-07-01", "2026-07-31")["net_profit"] == 0
    assert ps.ownership(conn)[firm["a"]]["capital_paisa"] == P("599000")


def test_trial_balance_totals_equal(conn, w, firm):
    make_profit(conn, w)
    tb = ac.trial_balance(conn)
    assert sum(r["debit"] for r in tb) == sum(r["credit"] for r in tb)
