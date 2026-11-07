"""Business insights: periods, comparisons, exceeding expenses, electricity, payback, inventory use."""
from datetime import date

import pytest

from services import accounting_service as ac, asset_service, inventory_service as inv, job_service as js, insights_service as ins


def test_periods_month_quarter_year_and_custom():
    t = date(2026, 10, 29)
    p = ins.periods("month", t)
    assert (p["cur"]["start"], p["cur"]["end"], p["cur"]["label"]) == ("2026-10-01", "2026-10-31", "Oct 2026")
    assert (p["prev"]["start"], p["prev"]["end"]) == ("2026-09-01", "2026-09-30") and p["cur"]["running"] and p["cur"]["days_elapsed"] == 29
    q = ins.periods("quarter", t, fy_start_month=7)                                   # financial year starts in July: Oct-Dec is the 2nd quarter
    assert (q["cur"]["start"], q["cur"]["end"], q["prev"]["start"]) == ("2026-10-01", "2026-12-31", "2026-07-01")
    y = ins.periods("year", t, fy_start_month=7)
    assert (y["cur"]["start"], y["cur"]["end"], y["prev"]["start"], y["prev"]["end"]) == ("2026-07-01", "2027-06-30", "2025-07-01", "2026-06-30")
    y1 = ins.periods("year", t, fy_start_month=1)
    assert (y1["cur"]["start"], y1["cur"]["end"], y1["cur"]["label"]) == ("2026-01-01", "2026-12-31", "2026")
    back = ins.periods("month", t, offset=-1)                                         # the arrows step a whole period back
    assert (back["cur"]["start"], back["prev"]["start"]) == ("2026-09-01", "2026-08-01")
    c = ins.periods("custom", t, custom=("2026-10-10", "2026-10-19"))                 # 10 days -> the 10 days before
    assert (c["prev"]["start"], c["prev"]["end"]) == ("2026-09-30", "2026-10-09")
    with pytest.raises(ValueError):
        ins.periods("custom", t, custom=("2026-10-10", "2026-10-01"))
    d = ins.periods("month", date(2026, 1, 15))
    assert d["prev"]["start"] == "2025-12-01"                                         # year boundary


@pytest.fixture
def world(conn, owner):
    o = owner
    ac.record_opening_cash(conn, o, "1000000", "2026-01-01")
    cust = inv.add_customer(conn, o, "Ali Traders")
    ink = inv.add_material(conn, o, "Ink", "ink", "L")
    fab = inv.add_material(conn, o, "Kapra", "fabric", "m")
    inv.receive_stock(conn, o, material_id=ink, qty="100", unit_price="1000", received_on="2026-08-01", payment_status="paid")
    inv.receive_stock(conn, o, material_id=fab, qty="500", unit_price="200", received_on="2026-08-01", payment_status="paid")
    j = js.create_job(conn, o, job_no="J1", customer_id=cust, product_type="Bedsheet", fabric_source="factory", order_date="2026-09-02")
    js.set_status(conn, o, j, "approved")
    js.consume_material(conn, o, j, ink, "10", on_date="2026-09-05")                  # Sep: Rs 10,000 of ink
    js.consume_material(conn, o, j, ink, "30", on_date="2026-10-05")                  # Oct: Rs 30,000 of ink
    js.consume_material(conn, o, j, fab, "50", on_date="2026-10-06")                  # Oct: Rs 10,000 of cloth
    ac.issue_invoice(conn, o, invoice_no="I1", customer_id=cust, total="100000", invoice_date="2026-09-10")
    ac.issue_invoice(conn, o, invoice_no="I2", customer_id=cust, total="150000", invoice_date="2026-10-10")
    codes = {r["name"]: r["code"] for r in conn.execute("SELECT code,name FROM accounts WHERE type='expense'")}
    ac.record_expense(conn, o, account_code="5200", amount="8000", on="2026-09-12", description="Electricity Sep")
    ac.record_expense(conn, o, account_code="5200", amount="12000", on="2026-10-12", description="Electricity Oct")
    ac.record_expense(conn, o, account_code="6000", amount="3000", on="2026-09-15", description="Tea")
    ac.record_expense(conn, o, account_code="6000", amount="3100", on="2026-10-15", description="Tea")
    gone = ac.record_expense(conn, o, account_code="6200", amount="9999", on="2026-10-16", description="Typed twice")
    ac.reverse_entry(conn, o, gone, "typed twice")
    return conn, o, codes


def test_comparison_numbers_come_from_the_books(world):
    conn, o, _ = world
    r = ins.compare(conn, date(2026, 10, 29), "month", fy_start_month=7)
    assert r["cur"]["sales_paisa"] == 15_000_000 and r["prev"]["sales_paisa"] == 10_000_000
    assert r["change"]["sales_paisa"]["delta"] == 5_000_000 and r["change"]["sales_paisa"]["pct_bps"] == 5000          # +50%
    rows = {x["name"]: x for x in r["expense_rows"]}
    assert rows["Electricity & Utilities"]["status"] == "exceeding" and rows["Electricity & Utilities"]["pct_bps"] == 5000
    assert rows["Chai Paani Refreshment"]["status"] == "higher" and "Other Expenses" not in rows              # +3.3% only; the reversed entry is not counted
    assert r["electricity"]["delta"] == 400_000 and "more" in r["story"]["electricity"][1]
    assert "Exceeding" in r["story"]["expenses"][1] and "Electricity" in r["story"]["expenses"][1]
    assert r["cur"]["expenses_total_paisa"] == 1_510_000 and r["prev"]["expenses_total_paisa"] == 1_100_000
    inv_cur = r["inventory"]["cur"]
    assert [m["name"] for m in inv_cur["used"]] == ["Ink", "Kapra"] and inv_cur["used"][0]["cost_paisa"] == 3_000_000
    assert inv_cur["used"][0]["share_bps"] == 7500 and r["inventory"]["prev"]["used_total_paisa"] == 1_000_000
    assert r["story"]["verdict"][0] in ("good", "bad", "neutral") and "Oct 2026" in r["story"]["verdict"][1]
    assert sum(x["bps"] for x in r["sales_split"]) <= 10000 + 3


def test_net_profit_matches_the_dashboard_statement(world):
    conn, o, _ = world
    st = ac.income_statement(conn, "2026-10-01", "2026-10-31")
    r = ins.compare(conn, date(2026, 10, 29), "month")
    assert r["cur"]["net_profit_paisa"] == st["net_profit"]


def test_payback_speed_and_states(world):
    conn, o, _ = world
    t = date(2026, 10, 29)
    r0 = ins.compare(conn, t, "month")
    assert r0["payback"]["state"] == "no_assets" and "No assets" in r0["story"]["payback"][1]
    asset_service.add_asset(conn, o, name="Printing machine", price="500000", on="2026-09-01")
    r = ins.compare(conn, t, "month")
    p = r["payback"]
    assert p["assets_paisa"] == 50_000_000 and r["assets"]["count"] == 1
    net = r["cur"]["net_profit_paisa"]
    if net > 0:
        assert p["state"] in ("recovering", "recovered") and p["per_month_paisa"] > 0
        if p["state"] == "recovering":
            assert p["days_to_recover"] > 0 and p["recover_on"] > "2026-10-29"
    else:
        assert p["state"] in ("not_recovering", "recovered")
    asset_service.void_asset(conn, o, 1, "wrong")
    assert ins.compare(conn, t, "month")["payback"]["state"] == "no_assets"                            # a voided asset does not count


def test_a_loss_period_and_an_empty_database(conn, owner):
    r = ins.compare(conn, date(2026, 10, 29), "month")
    assert r["cur"]["sales_paisa"] == 0 and r["expense_rows"] == [] and r["inventory"]["cur"]["used"] == []
    assert r["story"]["verdict"][0] == "neutral"
    ac.record_opening_cash(conn, owner, "100000", "2026-01-01")
    ac.record_expense(conn, owner, account_code="5400", amount="5000", on="2026-10-03", description="Repair")
    r = ins.compare(conn, date(2026, 10, 29), "month")
    assert r["cur"]["net_profit_paisa"] < 0 and r["story"]["verdict"][0] == "bad" and "LOSS" in r["story"]["verdict"][1]
    assert [x["status"] for x in r["expense_rows"]] == ["new"]


def test_insights_expenses_are_only_what_the_expenses_page_adds_up(conn, owner, world):
    from services import payroll_service as pr, queries as qq
    pr.add_employee(conn, owner, emp_code="S1", name="Sam", monthly_salary="30000", effective_from="2026-01-01", joined_on="2026-01-01")
    conn.execute("UPDATE employees SET created_at='2026-01-02T00:00:00Z'")
    pr.auto_accrue_month(conn, owner, "2026-09")                                          # a salary is booked in September's books
    ac.record_expense(conn, owner, account_code="5200", amount="7000", on="2026-10-12", description="Bijli bill")
    ac.record_expense(conn, owner, account_code="6200", amount="1000", on="2026-10-13", description="Misc")
    o = ins.compare(conn, date(2026, 10, 29))
    total, _n = qq.expense_total(conn, "2026-10-01", "2026-10-31")
    assert o["cur"]["expenses_total_paisa"] == total == 1_510_000 + 800_000           # the world's Oct expenses (12,000 + 3,100) + the two added here
    assert set(o["cur"]["expenses"]) == {"Electricity & Utilities", "Chai Paani Refreshment", "Other Expenses"}
    assert not any("Salar" in k or "Materials" in k for k in o["cur"]["expenses"])
    assert o["prev"]["expenses_total_paisa"] == 1_100_000                              # September's 8,000 + 3,000: the salary booked in September is not in it
