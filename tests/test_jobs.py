from decimal import Decimal as D

import pytest

from services import auth_service as auth, costing_service as cs, inventory_service as inv, job_service as js
from services.units import to_paisa


@pytest.fixture
def world(conn, owner):
    auth.create_user(conn, owner, "sup", "supervisor-pw", "production_supervisor")
    sup = auth.authenticate(conn, "sup", "supervisor-pw")
    paper = inv.add_material(conn, owner, "Paper", "consumable", "m", tracks_rolls=True)   # roll-by-roll engine fixture (see test_paper_in_rolls.py)
    ink = inv.add_material(conn, owner, "Ink", "ink", "ml")
    fabric = inv.add_material(conn, owner, "Kapra", "fabric", "m")
    cust = inv.add_customer(conn, owner, "Ali Traders")
    inv.receive_stock(conn, owner, material_id=paper, rolls=["1000"] * 2, supplier_id=1, total_cost="40000")  # Rs20/m
    inv.receive_stock(conn, owner, material_id=ink, qty="10000", supplier_id=1, total_cost="50000")           # Rs5/ml
    inv.receive_stock(conn, owner, material_id=fabric, qty="500", supplier_id=1, total_cost="50000")          # Rs100/m
    inv.receive_stock(conn, owner, material_id=fabric, qty="300", ownership="customer", customer_id=cust)
    return dict(owner=owner, sup=sup, paper=paper, ink=ink, fabric=fabric, cust=cust)


def mk(conn, w, no, src):
    jid = js.create_job(conn, w["owner"], job_no=no, customer_id=w["cust"], product_type="Bedsheet",
                        fabric_source=src, order_date="2026-10-17", fabric_length="100")
    js.set_status(conn, w["owner"], jid, "approved")
    return jid


def test_scenario_a_factory_fabric_costed(conn, world):
    w = world; j = mk(conn, w, "J-A", "factory")
    js.consume_material(conn, w["sup"], j, w["fabric"], "100")
    js.consume_material(conn, w["sup"], j, w["paper"], "100")
    js.consume_material(conn, w["sup"], j, w["ink"], "200")
    c = js.costing(conn, j)
    assert c.by_kind["material"] == to_paisa("10000") + to_paisa("2000") + to_paisa("1000")
    assert conn.execute("SELECT status FROM jobs WHERE id=?", (j,)).fetchone()[0] == "in_progress"


def test_scenario_b_customer_fabric_zero_cost_and_separate(conn, world):
    w = world; j = mk(conn, w, "J-B", "customer")
    r = js.consume_material(conn, w["sup"], j, w["fabric"], "100")
    assert r["cost_paisa"] == 0
    js.consume_material(conn, w["sup"], j, w["paper"], "100")
    s = inv.stock_summary(conn, w["fabric"])
    assert s["factory_qty"] == D("500") and s["customer_owned"] == {w["cust"]: D("200")}
    assert s["factory_value_paisa"] == to_paisa("50000")
    assert js.costing(conn, j).by_kind["material"] == to_paisa("2000")      # paper only


def test_scenario_b_with_extra_factory_fabric_is_charged(conn, world):
    w = world; j = mk(conn, w, "J-B2", "customer")
    js.consume_material(conn, w["sup"], j, w["fabric"], "20", ownership="factory")
    assert js.costing(conn, j).by_kind["material"] == to_paisa("2000")


def test_stock_failure_rolls_back_job_cost(conn, world):
    w = world; j = mk(conn, w, "J-F", "factory")
    with pytest.raises(inv.InsufficientStock):
        js.consume_material(conn, w["sup"], j, w["paper"], "5000")
    assert js.costing(conn, j).total_paisa == 0
    assert inv.stock_summary(conn, w["paper"])["factory_qty"] == D("2000")


def test_cost_line_failure_rolls_back_stock(conn, world, monkeypatch):
    w = world; j = mk(conn, w, "J-R", "factory")
    monkeypatch.setattr(js, "_add_cost", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        js.consume_material(conn, w["sup"], j, w["paper"], "100")
    assert inv.stock_summary(conn, w["paper"])["factory_qty"] == D("2000")
    assert conn.execute("SELECT COUNT(*) FROM stock_movements WHERE movement_type='production'").fetchone()[0] == 0


def test_hourly_electricity_is_estimate_and_flagged(conn, world):
    w = world; j = mk(conn, w, "J-E", "factory")
    pm = conn.execute("SELECT id FROM machines WHERE kind='printing'").fetchone()[0]
    r = js.record_machine_run(conn, w["sup"], j, pm, "2026-10-17T09:00", "2026-10-17T11:00")
    assert r["cost"] is None                       # no rate configured: NOT invented
    c = js.costing(conn, j)
    assert "electricity" not in c.by_kind and any("no electricity cost" in x for x in c.warnings)
    conn.execute("UPDATE machines SET hourly_rate_paisa=? WHERE id=?", (to_paisa("300"), pm))
    r = js.record_machine_run(conn, w["sup"], j, pm, "2026-10-17T13:00", "2026-10-17T15:30", downtime_minutes=30)
    assert r["minutes"] == 120 and r["cost"].paisa == to_paisa("600") and r["cost"].is_estimate
    c = js.costing(conn, j)
    assert c.includes_estimates and any("ESTIMATED" in x for x in c.warnings)


def test_meter_based_electricity_actual(conn, world):
    w = world; j = mk(conn, w, "J-M", "factory")
    pm = conn.execute("SELECT id FROM machines WHERE kind='printing'").fetchone()[0]
    r = js.record_machine_run(conn, w["sup"], j, pm, "2026-10-17T09:00", "2026-10-17T10:00",
                              kwh_start="100", kwh_end="110", bill_total="100000", bill_units_kwh="5000")
    assert r["cost"].paisa == to_paisa("200") and not r["cost"].is_estimate


def test_wastage_counted_once_not_double(conn, world):
    w = world; j = mk(conn, w, "J-W", "factory")
    js.consume_material(conn, w["sup"], j, w["paper"], "100")
    with pytest.raises(auth.PermissionDenied):
        js.consume_material(conn, w["sup"], j, w["paper"], "10", wastage=True, reason="torn")
    js.consume_material(conn, w["owner"], j, w["paper"], "10", wastage=True, reason="torn")
    c = js.costing(conn, j)
    assert c.by_kind == {"material": to_paisa("2000"), "wastage": to_paisa("200")} and c.total_paisa == to_paisa("2200")


def test_finalize_requires_owner_and_completed_and_locks(conn, world):
    w = world; j = mk(conn, w, "J-X", "factory")
    js.consume_material(conn, w["sup"], j, w["paper"], "100")
    with pytest.raises(js.JobError):
        js.finalize_job(conn, w["owner"], j, "5000")          # not completed
    js.set_status(conn, w["sup"], j, "completed")
    with pytest.raises(auth.PermissionDenied):
        js.finalize_job(conn, w["sup"], j, "5000")
    out = js.finalize_job(conn, w["owner"], j, "5000")
    assert out["cost_paisa"] == to_paisa("2000") and out["profit_paisa"] == to_paisa("3000")
    with pytest.raises(js.JobError):
        js.consume_material(conn, w["sup"], j, w["paper"], "1")
    with pytest.raises(js.JobError):
        js.set_status(conn, w["owner"], j, "in_progress")


def test_estimate_vs_actual(conn, world):
    w = world; j = mk(conn, w, "J-V", "factory")
    js.add_estimate(conn, w["owner"], j, "material", amount="2500")
    js.consume_material(conn, w["sup"], j, w["paper"], "100")
    assert js.estimate_vs_actual(conn, j)["material"] == {"estimated": 250000, "actual": 200000, "variance": -50000}


def test_cost_lines_append_only(conn, world):
    import sqlite3
    w = world; j = mk(conn, w, "J-L", "factory")
    js.add_cost(conn, w["owner"], j, "labor", "500")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM job_costs")
    with pytest.raises(js.JobError):
        js.add_cost(conn, w["owner"], j, "labor", "-100")        # correction needs a reason
    js.add_cost(conn, w["owner"], j, "labor", "-100", description="entered twice")
    assert js.costing(conn, j).by_kind["labor"] == to_paisa("400")


# ---------- pricing ----------
def test_markup_vs_margin_distinct():
    mk_ = cs.price_cost_plus(10000, "25")        # price 12500, profit 2500
    tm = cs.price_target_margin(10000, "25")     # price 13333.33 -> margin 25%
    assert mk_.price_paisa == 12500 and cs.markup_pct(10000, 12500) == D(25) and cs.margin_pct(10000, 12500) == D(20)
    assert tm.price_paisa == 13333 and abs(cs.margin_pct(10000, tm.price_paisa) - D(25)) < D("0.01")
    assert "1 - " in tm.explanation


def test_target_margin_validation():
    cs.price_target_margin(100, "0")
    for bad in ("100", "-1", "150"):
        with pytest.raises(cs.PricingError):
            cs.price_target_margin(100, bad)


def test_per_meter_fixed_manual():
    assert cs.price_per_meter(0, "100", "45.5").price_paisa == to_paisa("4550")
    assert cs.price_fixed(500, 700).profit_paisa == 200
    assert cs.price_manual(500, 400).profit_paisa == -100
