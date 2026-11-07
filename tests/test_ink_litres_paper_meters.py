"""Ink is counted in litres. Paper is received in rolls and can be used in rolls OR meters (roll length per material, usually 300 m).
Every figure is worked out by hand in the comments."""
import sqlite3
from decimal import Decimal as D

import pytest

from database import migrations
from services import accounting_service as acct, dashboard_service as ds, inventory_service as inv, job_service as js
from services.units import to_paisa as P


@pytest.fixture
def paper(conn, owner):
    m = inv.add_material(conn, owner, "Sublimation Paper", "paper", "roll")
    inv.receive_stock(conn, owner, material_id=m, qty="10", unit_price="12000", supplier_id=1, received_on="2026-07-01")   # 10 rolls x Rs 12,000
    return m


@pytest.fixture
def job(conn, owner):
    c = inv.add_customer(conn, owner, "Ali")
    return js.create_job(conn, owner, job_no="P-1", customer_id=c, product_type="Bedsheet", fabric_source="factory",
                         order_date="2026-07-03", quantity_pieces=50, status="in_progress")


# ------------------------------------------------------------------ ink in litres
def test_ink_is_always_litres(conn, owner):
    m = inv.add_material(conn, owner, "Cyan Ink", "ink", "ml")                        # a wrong unit is ignored
    row = conn.execute("SELECT base_unit, roll_length_milli FROM materials WHERE id=?", (m,)).fetchone()
    assert (row["base_unit"], row["roll_length_milli"]) == ("L", None)


def test_ink_cost_in_litres_hand_calculation(conn, owner):
    m = inv.add_material(conn, owner, "Cyan Ink", "ink", "L")
    inv.receive_stock(conn, owner, material_id=m, qty="20", unit_price="900", supplier_id=1)       # 20 L x Rs 900 = Rs 18,000
    assert inv.stock_summary(conn, m)["factory_value_paisa"] == P("18000")
    r = inv.issue_stock(conn, owner, material_id=m, qty="2.5")                          # 2.5 L x 900 = Rs 2,250
    assert r["cost_paisa"] == P("2250")
    s = inv.stock_summary(conn, m)
    assert s["unit"] == "L" and s["factory_qty"] == D("17.5") and s["factory_value_paisa"] == P("15750")
    assert acct.check_integrity(conn) == []


def test_one_millilitre_is_still_exact(conn, owner):
    m = inv.add_material(conn, owner, "Black Ink", "ink", "L")
    inv.receive_stock(conn, owner, material_id=m, qty="1", unit_price="1000", supplier_id=1)       # Rs 1,000 per litre = Rs 1 per ml
    assert inv.issue_stock(conn, owner, material_id=m, qty="0.001")["cost_paisa"] == P("1")        # 1 ml
    with pytest.raises(Exception):
        inv.issue_stock(conn, owner, material_id=m, qty="0.0005")                                 # half a ml cannot be recorded


# ------------------------------------------------------------------ paper: rolls and meters
def test_new_paper_defaults_to_300_m_and_can_be_changed(conn, owner):
    m = inv.add_material(conn, owner, "Paper A", "paper", "roll")
    assert conn.execute("SELECT roll_length_milli FROM materials WHERE id=?", (m,)).fetchone()[0] == 300000
    m2 = inv.add_material(conn, owner, "Paper B", "paper", "roll", roll_length="250")
    assert conn.execute("SELECT roll_length_milli FROM materials WHERE id=?", (m2,)).fetchone()[0] == 250000
    with pytest.raises(inv.InventoryError):
        inv.add_material(conn, owner, "Paper C", "paper", "roll", roll_length="0")
    with pytest.raises(inv.InventoryError):
        inv.add_material(conn, owner, "Paper D", "paper", "roll", roll_length="abc")
    assert conn.execute("SELECT roll_length_milli FROM materials WHERE name='Paper A'").fetchone()[0] == 300000
    inv.update_material(conn, owner, m, name="Paper A", category="paper", base_unit="roll", roll_length="320")
    assert conn.execute("SELECT roll_length_milli FROM materials WHERE id=?", (m,)).fetchone()[0] == 320000


def test_other_materials_have_no_roll_length(conn, owner):
    m = inv.add_material(conn, owner, "Cloth", "fabric", "m", roll_length="300")
    assert conn.execute("SELECT roll_length_milli FROM materials WHERE id=?", (m,)).fetchone()[0] is None


def test_stock_summary_shows_rolls_and_meters(conn, owner, paper):
    s = inv.stock_summary(conn, paper)
    assert s["factory_qty"] == D("10") and s["roll_length_m"] == D("300") and s["factory_meters"] == D("3000")   # 10 x 300


def test_using_one_roll_takes_one_roll_and_its_full_cost(conn, owner, paper, job):
    r = js.consume_material(conn, owner, job, paper, "1", unit="roll")
    assert r["cost_paisa"] == P("12000")
    s = inv.stock_summary(conn, paper)
    assert s["factory_qty"] == D("9") and s["factory_meters"] == D("2700")


def test_using_150_meters_takes_half_a_roll_and_half_its_cost(conn, owner, paper, job):
    r = js.consume_material(conn, owner, job, paper, "150", unit="m")          # 150 / 300 = 0.5 roll -> 0.5 x 12,000
    assert r["cost_paisa"] == P("6000") and r["qty_milli"] == 500
    s = inv.stock_summary(conn, paper)
    assert s["factory_qty"] == D("9.5") and s["factory_meters"] == D("2850") and s["factory_value_paisa"] == P("114000")


def test_meters_that_are_not_a_whole_part_of_a_roll_are_rounded_to_0_001_roll(conn, owner, paper, job):
    r = js.consume_material(conn, owner, job, paper, "100", unit="m")          # 100 / 300 = 0.3333 -> 0.333 roll; cost 0.333 x 12,000 = 3,996
    assert r["qty_milli"] == 333 and r["cost_paisa"] == P("3996")
    with pytest.raises(inv.InventoryError, match="less than 0.001"):
        js.consume_material(conn, owner, job, paper, "0.1", unit="m")


def test_cannot_use_more_meters_than_in_stock(conn, owner, paper, job):
    with pytest.raises(inv.InsufficientStock):
        js.consume_material(conn, owner, job, paper, "3000.5", unit="m")      # only 3,000 m exist
    assert inv.stock_summary(conn, paper)["factory_qty"] == D("10")


def test_all_of_it_in_meters_clears_the_value_exactly(conn, owner, paper, job):
    r = js.consume_material(conn, owner, job, paper, "3000", unit="m")
    assert r["cost_paisa"] == P("120000")
    s = inv.stock_summary(conn, paper)
    assert s["factory_qty"] == 0 and s["factory_value_paisa"] == 0 and acct.check_integrity(conn) == []


def test_a_batch_mixes_units_and_is_all_or_nothing(conn, owner, paper, job):
    ink = inv.add_material(conn, owner, "Cyan Ink", "ink", "L")
    inv.receive_stock(conn, owner, material_id=ink, qty="10", unit_price="1000", supplier_id=1)
    r = js.consume_materials(conn, owner, job, [(paper, "150", "m"), (ink, "2")])             # Rs 6,000 + 2 L x 1,000 = Rs 8,000
    assert r["cost_paisa"] == P("8000")
    with pytest.raises(js.JobError):
        js.consume_materials(conn, owner, job, [(paper, "1", "roll"), (ink, "99")])           # ink fails -> the paper must not leave either
    assert inv.stock_summary(conn, paper)["factory_qty"] == D("9.5")
    assert inv.stock_summary(conn, ink)["factory_qty"] == D("8")


def test_wrong_unit_for_a_material_is_refused(conn, owner, paper, job):
    with pytest.raises(inv.InventoryError):
        js.consume_material(conn, owner, job, paper, "1", unit="kg")
    ink = inv.add_material(conn, owner, "Cyan Ink", "ink", "L")
    inv.receive_stock(conn, owner, material_id=ink, qty="1", unit_price="10", supplier_id=1)
    with pytest.raises(inv.InventoryError):
        js.consume_material(conn, owner, job, ink, "5", unit="m")                           # only paper has a meters choice


def test_wastage_and_adjustment_can_also_be_in_meters(conn, owner, paper):
    r = inv.issue_stock(conn, owner, material_id=paper, qty="60", unit="m", movement_type="wastage", reason="torn")   # 0.2 roll x 12,000
    assert r["qty_milli"] == 200 and r["cost_paisa"] == P("2400")


def test_receiving_can_set_the_roll_length_and_it_is_audited(conn, owner, paper):
    inv.receive_stock(conn, owner, material_id=paper, qty="2", unit_price="12000", supplier_id=1, roll_length="250")
    s = inv.stock_summary(conn, paper)
    assert s["roll_length_m"] == D("250") and s["factory_meters"] == D("3000")               # 12 rolls x 250
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='material.roll_length'").fetchone()[0] == 1


def test_dashboard_shows_paper_in_rolls_and_meters(conn, owner, paper, job):
    js.consume_material(conn, owner, job, paper, "150", unit="m", on_date="2026-07-05")
    jul = ds.snapshot(conn, "2026-07-01", "2026-07-31")["inventory"]
    assert jul["paper_rolls_on_hand"] == D("9.5") and jul["paper_meters_on_hand"] == D("2850")
    assert jul["paper_rolls_used_in_period"] == D("0.5") and jul["paper_meters_used_in_period"] == D("150")


# ------------------------------------------------------------------ migration of existing data
def _v10_db(tmp_path):
    c = sqlite3.connect(tmp_path / "old.db"); c.row_factory = sqlite3.Row
    migrations.migrate(c, migrations=[m for m in migrations.load_migrations() if m.version < 11])
    c.execute("INSERT INTO materials(id,name,category,base_unit,reorder_level_milli,created_at) VALUES (1,'Cyan','ink','ml',2000000,'x')")
    c.execute("INSERT INTO materials(id,name,category,base_unit,created_at) VALUES (2,'Paper','paper','roll','x')")
    c.execute("INSERT INTO materials(id,name,category,base_unit,created_at) VALUES (3,'Cloth','fabric','m','x')")
    c.execute("INSERT INTO material_stock(material_id,qty_milli,value_paisa) VALUES (1,3500000,350000),(2,4000,480000),(3,100000,100000)")   # 3,500 ml ink
    c.execute("INSERT INTO stock_lots(material_id,ownership,received_on,qty_received_milli,qty_remaining_milli,total_cost_paisa,created_at) "
              "VALUES (1,'factory','2026-07-01',5000000,3500000,500000,'x')")                                                           # 5,000 ml received
    c.execute("INSERT INTO stock_movements(material_id,ownership,movement_type,qty_milli,value_paisa,created_at) VALUES (1,'factory','purchase',5000000,500000,'x')")
    c.execute("INSERT INTO stock_movements(material_id,ownership,movement_type,qty_milli,value_paisa,created_at) VALUES (1,'factory','production',-1500000,-150000,'x')")
    c.commit()
    return c


def test_migration_turns_existing_ml_into_the_same_amount_of_litres(tmp_path):
    c = _v10_db(tmp_path)
    migrations.migrate(c)
    ink = c.execute("SELECT base_unit, reorder_level_milli FROM materials WHERE id=1").fetchone()
    assert (ink["base_unit"], ink["reorder_level_milli"]) == ("L", 2000)                    # 2,000 ml = 2 L = 2000 milli-L
    assert c.execute("SELECT qty_milli, value_paisa FROM material_stock WHERE material_id=1").fetchone()[:] == (3500, 350000)   # 3.5 L, value unchanged
    assert c.execute("SELECT qty_received_milli, qty_remaining_milli FROM stock_lots").fetchone()[:] == (5000, 3500)
    assert [r[0] for r in c.execute("SELECT qty_milli FROM stock_movements ORDER BY id")] == [5000, -1500]
    assert c.execute("SELECT SUM(qty_milli) FROM stock_movements").fetchone()[0] == c.execute("SELECT qty_milli FROM material_stock WHERE material_id=1").fetchone()[0]
    assert c.execute("SELECT qty_milli FROM material_stock WHERE material_id=3").fetchone()[0] == 100000            # other materials untouched
    assert c.execute("SELECT qty_milli FROM material_stock WHERE material_id=2").fetchone()[0] == 4000


def test_migration_gives_old_paper_300_m_and_nothing_else(tmp_path):
    c = _v10_db(tmp_path)
    migrations.migrate(c)
    assert [r[0] for r in c.execute("SELECT roll_length_milli FROM materials ORDER BY id")] == [None, 300000, None]


def test_migration_keeps_movements_append_only(tmp_path):
    c = _v10_db(tmp_path)
    migrations.migrate(c)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE stock_movements SET qty_milli=1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM stock_movements")


def test_migration_refuses_a_fraction_of_a_millilitre_and_changes_nothing(tmp_path):
    c = _v10_db(tmp_path)
    c.execute("UPDATE material_stock SET qty_milli=3500500 WHERE material_id=1")            # 3,500.5 ml
    c.commit()
    with pytest.raises(Exception):
        migrations.migrate(c)
    assert migrations.current_version(c) == 10
    assert c.execute("SELECT base_unit FROM materials WHERE id=1").fetchone()[0] == "ml"
    assert c.execute("SELECT qty_milli FROM material_stock WHERE material_id=1").fetchone()[0] == 3500500
