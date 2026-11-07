from datetime import date

import pytest

from services import accounting_service as ac, auth_service as auth, dashboard_service as ds
from services import inventory_service as inv, job_service as js, payroll_service as pr, profit_service as ps
from services.units import to_paisa as P


# ---------- ranges ----------
def test_ranges():
    t = date(2026, 10, 17)   # Saturday
    assert ds.resolve_range("today", t) == ("2026-10-17", "2026-10-17")
    assert ds.resolve_range("this_week", t) == ("2026-10-12", "2026-10-18")
    assert ds.resolve_range("this_month", t) == ("2026-10-01", "2026-10-31")
    assert ds.resolve_range("this_quarter", t) == ("2026-10-01", "2026-12-31")          # FY Jul: Q2
    assert ds.resolve_range("this_year", t) == ("2026-07-01", "2027-06-30")
    assert ds.resolve_range("this_year", date(2027, 2, 1)) == ("2026-07-01", "2027-06-30")
    assert ds.resolve_range("this_quarter", date(2027, 6, 30)) == ("2027-04-01", "2027-06-30")
    assert ds.resolve_range("this_year", t, fy_start_month=1) == ("2026-01-01", "2026-12-31")
    assert ds.resolve_range("custom", t, custom=("2026-01-05", "2026-01-09")) == ("2026-01-05", "2026-01-09")
    with pytest.raises(ds.RangeError):
        ds.resolve_range("custom", t, custom=("2026-02-01", "2026-01-01"))


# ---------- empty database: zeros, nothing invented ----------
def test_empty_database_shows_zeros(conn):
    s = ds.snapshot(conn, "2026-07-01", "2026-07-31")
    assert all(v == 0 for k, v in s["performance"].items() if not k.endswith("_bps")) and all(v == 0 for v in s["position"].values())
    assert s["performance"]["net_margin_bps"] is None and s["performance"]["gross_margin_bps"] is None
    assert s["inventory"]["items"] == [] and s["receivables"] == [] and s["partners"] == []
    assert s["jobs"]["active"] == 0
    assert "No successful backup has been made yet." in s["warnings"]
    assert [m["machine"] for m in s["machines"]] == ["Heating Machine", "Printing Machine"]


# ---------- known scenario, hand-computed ----------
@pytest.fixture
def scenario(conn, owner):
    auth.create_user(conn, owner, "acc", "accountant-pw", "accountant")
    acc = auth.authenticate(conn, "acc", "accountant-pw")
    conn.execute("INSERT INTO suppliers(id,name,created_at) VALUES (2,'Paper House','x')")
    a = ps.add_partner(conn, owner, "A"); b = ps.add_partner(conn, owner, "B")
    ps.record_capital(conn, owner, a, "300000", "2026-07-01"); ps.record_capital(conn, owner, b, "100000", "2026-07-01")
    ps.set_profit_shares(conn, owner, "2026-07-01", {a: 5000, b: 5000}, confirmed=True)
    cust = inv.add_customer(conn, owner, "Ali Traders"); cust2 = inv.add_customer(conn, owner, "Zed")
    paper = inv.add_material(conn, owner, "Paper", "paper", "roll", reorder_level="4")
    fab = inv.add_material(conn, owner, "Kapra", "fabric", "m")
    inv.receive_stock(conn, owner, material_id=paper, qty="8", supplier_id=2, total_cost="40000", received_on="2026-07-02")   # 8 rolls, Rs 5,000 each
    inv.receive_stock(conn, owner, material_id=fab, qty="200", ownership="customer", customer_id=cust)
    j = js.create_job(conn, owner, job_no="J1", customer_id=cust, product_type="b", fabric_source="customer", order_date="2026-07-03")
    js.set_status(conn, owner, j, "approved")
    js.consume_material(conn, owner, j, paper, "2", on_date="2026-07-05")   # 2 rolls = Rs 10,000
    # sales: 60,000 invoice, 20,000 received; second customer invoice 10,000 unpaid; 5,000 advance from Zed
    i1 = ac.issue_invoice(conn, acc, invoice_no="I1", customer_id=cust, total="60000", invoice_date="2026-07-10")
    ac.receive_payment(conn, acc, customer_id=cust, amount="20000", paid_on="2026-07-11", invoice_id=i1)
    ac.issue_invoice(conn, acc, invoice_no="I2", customer_id=cust2, total="10000", invoice_date="2026-07-12")
    ac.receive_payment(conn, acc, customer_id=cust2, amount="5000", paid_on="2026-07-13")
    ac.record_expense(conn, acc, account_code="5200", amount="3000", on="2026-07-14", description="electricity")
    ac.record_expense(conn, acc, account_code="6000", amount="2000", on="2026-07-15", description="office")
    e = pr.add_employee(conn, owner, emp_code="E1", name="Bilal", monthly_salary="30000", effective_from="2026-01-01")
    s = pr.accrue_salary(conn, acc, e, "2026-07")
    pr.pay_salary(conn, acc, s, "10000", "2026-08-01")
    ac.pay_supplier(conn, acc, supplier_id=2, amount="15000", paid_on="2026-07-20")
    return dict(a=a)


def test_dashboard_matches_hand_calculation(conn, scenario):
    s = ds.snapshot(conn, "2026-07-01", "2026-07-31")
    p = s["performance"]
    # paper cost: 2 of 8 rolls @ Rs5,000 = 10,000 ; direct labour 30,000 ; electricity 3,000 ; admin 2,000
    assert p["sales_paisa"] == P("70000")
    assert p["direct_production_paisa"] == P("10000") + P("30000")
    assert p["factory_overhead_paisa"] == P("3000") and p["production_cost_paisa"] == P("43000")
    assert p["gross_profit_paisa"] == P("27000") and p["operating_expenses_paisa"] == P("2000")
    assert p["net_profit_paisa"] == P("25000")
    pos = s["position"]
    # cash: +400,000 capital +20,000 +5,000 -3,000 -2,000 -15,000 supplier ; salary paid 10,000 is dated Aug 1 (after period end)
    assert pos["cash_paisa"] == P("400000") + P("20000") + P("5000") - P("3000") - P("2000") - P("15000")
    assert pos["inventory_value_paisa"] == P("30000")                        # 6 rolls * Rs5,000
    assert pos["receivables_paisa"] == P("50000") and pos["customer_advances_paisa"] == P("5000")
    assert pos["payables_paisa"] == P("25000") and pos["salaries_payable_paisa"] == P("30000")   # payment is after end
    assert pos["partner_capital_paisa"] == P("400000")
    assert s["receivables"][0] == {"customer": "Ali Traders", "outstanding_paisa": P("40000")}
    assert {r["customer"]: r["outstanding_paisa"] for r in s["receivables"]} == {"Ali Traders": P("40000"), "Zed": P("10000")}
    assert s["payables"] == [{"supplier": "Paper House", "owed_paisa": P("25000")}]
    assert str(s["inventory"]["paper_rolls_on_hand"]) == "6.000" and str(s["inventory"]["paper_rolls_used_in_period"]) == "2.000"
    assert s["jobs"]["entries_in_period"] == 1
    kap = [i for i in s["inventory"]["items"] if i["material"] == "Kapra"][0]
    assert kap["value_paisa"] == 0 and str(kap["customer_owned_qty"]) == "200.000"       # customer property, no value
    assert s["jobs"]["active"] == 1
    assert [x["material"] for x in s["inventory"]["low_stock"]] == []                     # 6 rolls > reorder level 4
    assert s["warnings"] == ["No successful backup has been made yet."]                  # books consistent


def test_period_boundaries_and_position_as_of(conn, scenario):
    s = ds.snapshot(conn, "2026-08-01", "2026-08-31")
    assert s["performance"]["sales_paisa"] == 0 and s["performance"]["net_profit_paisa"] == 0   # nothing in Aug
    assert s["position"]["salaries_payable_paisa"] == P("20000")                          # position carries forward
    assert s["unpaid_salaries"][0]["status"] == "partial" and s["unpaid_salaries"][0]["due_paisa"] == P("20000")


def test_partners_section(conn, scenario):
    s = ds.snapshot(conn, "2026-07-01", "2026-07-31")
    assert {p["partner"]: p["capital_paisa"] for p in s["partners"]} == {"A": P("300000"), "B": P("100000")}


def test_distribution_shows_unpaid(conn, owner, scenario):
    ps.approve_distribution(conn, owner, period_start="2026-07-01", period_end="2026-07-31", amount="20000", on="2026-07-31")
    s = ds.snapshot(conn, "2026-07-01", "2026-07-31")
    assert s["position"]["distributions_payable_paisa"] == P("20000")
    assert sum(p["distributions_unpaid_paisa"] for p in s["partners"]) == P("20000")


def test_chart_series_sums_to_period_total(conn, scenario):
    ser = ds.monthly_series(conn, "2026-07-01", "2026-09-30")
    assert [m["month"] for m in ser] == ["2026-07", "2026-08", "2026-09"]
    total = ac.income_statement(conn, "2026-07-01", "2026-09-30")
    assert sum(m["revenue"] for m in ser) == total["revenue"] and sum(m["net_profit"] for m in ser) == total["net_profit"]


def test_machine_usage_and_missing_rate_warning(conn, owner, scenario):
    sup_job = conn.execute("SELECT id FROM jobs").fetchone()[0]
    pm = conn.execute("SELECT id FROM machines WHERE kind='printing'").fetchone()[0]
    js.record_machine_run(conn, owner, sup_job, pm, "2026-07-06T09:00", "2026-07-06T12:00", downtime_minutes=15, meters="250")
    s = ds.snapshot(conn, "2026-07-01", "2026-07-31")
    m = [x for x in s["machines"] if x["machine"] == "Printing Machine"][0]
    assert m["run_minutes"] == 165 and m["downtime_minutes"] == 15 and str(m["meters"]) == "250.000"
    assert any("Electricity cost unknown" in w for w in s["warnings"])


def test_salary_list_is_as_of_period_end(conn, scenario):
    """Salary list must agree with the Salaries payable card for the same period (payment dated 1 Aug is not in July)."""
    s = ds.snapshot(conn, "2026-07-01", "2026-07-31")
    assert s["unpaid_salaries"][0]["status"] == "unpaid" and s["unpaid_salaries"][0]["due_paisa"] == P("30000")
    for rng in (("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31")):
        s = ds.snapshot(conn, *rng)
        assert sum(x["due_paisa"] for x in s["unpaid_salaries"]) == s["position"]["salaries_payable_paisa"]


def test_cards_reconcile_with_their_lists(conn, scenario):
    s = ds.snapshot(conn, "2026-07-01", "2026-07-31")
    assert sum(x["outstanding_paisa"] for x in s["receivables"]) == s["position"]["receivables_paisa"]
    assert sum(x["owed_paisa"] for x in s["payables"]) == s["position"]["payables_paisa"]
    assert sum(x["value_paisa"] for x in s["inventory"]["items"]) == s["position"]["inventory_value_paisa"]
    assert sum(x["capital_paisa"] for x in s["partners"]) == s["position"]["partner_capital_paisa"]
    p = s["performance"]
    assert p["gross_profit_paisa"] == p["sales_paisa"] - p["production_cost_paisa"]
    assert p["net_profit_paisa"] == p["gross_profit_paisa"] - p["operating_expenses_paisa"]
    assert p["production_cost_paisa"] == p["direct_production_paisa"] + p["factory_overhead_paisa"]


def test_margins_in_basis_points(conn, scenario):
    p = ds.snapshot(conn, "2026-07-01", "2026-07-31")["performance"]
    assert p["gross_margin_bps"] == 3857 and p["net_margin_bps"] == 3571      # 27000/70000, 25000/70000 (rounded)
    assert ds.snapshot(conn, "2026-08-01", "2026-08-31")["performance"]["net_margin_bps"] is None   # no sales
    assert ds._bps(-1, 3) == -3333 and ds._bps(1, 3) == 3333
