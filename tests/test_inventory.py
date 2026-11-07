import sqlite3
from decimal import Decimal as D

import pytest

from services import auth_service as auth, inventory_service as inv
from services.units import UnitError, to_milli, to_paisa


@pytest.fixture
def users(conn, owner):
    auth.create_user(conn, owner, "inv", "inventory-pw", "accountant")
    auth.create_user(conn, owner, "sup", "supervisor-pw", "auditor")
    auth.create_user(conn, owner, "aud", "auditor-pw1", "auditor")
    return {"owner": owner,
            "inv": auth.authenticate(conn, "inv", "inventory-pw"),
            "sup": auth.authenticate(conn, "sup", "supervisor-pw"),
            "aud": auth.authenticate(conn, "aud", "auditor-pw1")}


@pytest.fixture
def paper(conn, owner):
    # Roll-by-roll length tracking is a generic engine feature kept for non-paper materials; real paper is counted in rolls
    # (see test_paper_in_rolls.py), so this fixture is a "consumable" on a roll.
    return inv.add_material(conn, owner, "Transfer Paper", "consumable", "m", tracks_rolls=True, reorder_level="1000")


@pytest.fixture
def fabric(conn, owner):
    return inv.add_material(conn, owner, "Kapra", "fabric", "m")


def snap(conn):
    return ([tuple(r) for r in conn.execute("SELECT * FROM material_stock")],
            [tuple(r) for r in conn.execute("SELECT id,qty_remaining_milli FROM stock_lots")],
            [tuple(r) for r in conn.execute("SELECT id,remaining_milli FROM stock_rolls")],
            conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0])


def test_units_exact():
    assert to_milli("1000") == 1_000_000 and to_paisa("12.345") == 1235 and to_paisa("0.005") == 1
    with pytest.raises(UnitError):
        to_milli("1.0005")
    with pytest.raises(UnitError):
        to_milli(0.1)


def test_20_rolls_of_1000m_then_consume_750(conn, owner, paper):
    lot = inv.receive_stock(conn, owner, material_id=paper, rolls=["1000"] * 20, supplier_id=1, total_cost="400000")
    s = inv.stock_summary(conn, paper)
    assert s["factory_qty"] == D("20000") and s["factory_rolls_total"] == 20 and s["factory_rolls_full"] == 20
    r = inv.issue_stock(conn, owner, material_id=paper, qty="750", ref_type="job", ref_id="J1")
    s = inv.stock_summary(conn, paper)
    assert s["factory_qty"] == D("19250")
    assert s["factory_rolls_total"] == 20 and s["factory_rolls_full"] == 19
    assert s["factory_rolls_partial"][0][1] == D("250")
    # cost = 750 m * (Rs 400000/20000 m = Rs 20/m) = Rs 15000 = 1,500,000 paisa
    assert r["cost_paisa"] == 1_500_000
    assert s["factory_value_paisa"] == 40_000_000 - 1_500_000
    assert lot > 0


def test_mixed_roll_lengths_fifo_across_rolls(conn, owner, paper):
    inv.receive_stock(conn, owner, material_id=paper, rolls=["300", "500", "450"], supplier_id=1, total_cost="1250")
    inv.issue_stock(conn, owner, material_id=paper, qty="400")  # 300 full + 100 of next
    s = inv.stock_summary(conn, paper)
    assert s["factory_qty"] == D("850") and s["factory_rolls_total"] == 2
    assert s["factory_rolls_full"] == 1 and s["factory_rolls_partial"][0][1] == D("400")


def test_specific_roll_issue(conn, owner, paper):
    inv.receive_stock(conn, owner, material_id=paper, rolls=["300", "500"], supplier_id=1, total_cost="800")
    rid = conn.execute("SELECT id FROM stock_rolls WHERE length_milli=500000").fetchone()[0]
    inv.issue_stock(conn, owner, material_id=paper, qty="120", roll_id=rid)
    assert conn.execute("SELECT remaining_milli FROM stock_rolls WHERE id=?", (rid,)).fetchone()[0] == 380_000
    with pytest.raises(inv.InsufficientStock):
        inv.issue_stock(conn, owner, material_id=paper, qty="400", roll_id=rid)


def test_negative_stock_rejected_state_unchanged(conn, owner, paper):
    inv.receive_stock(conn, owner, material_id=paper, rolls=["100"], supplier_id=1, total_cost="100")
    before = snap(conn)
    with pytest.raises(inv.InsufficientStock):
        inv.issue_stock(conn, owner, material_id=paper, qty="100.001")
    assert snap(conn) == before


def test_failed_transaction_leaves_inventory_unchanged(conn, owner, paper, monkeypatch):
    inv.receive_stock(conn, owner, material_id=paper, rolls=["1000"], supplier_id=1, total_cost="1000")
    before = snap(conn)

    def boom(*a, **k):
        raise RuntimeError("audit write failed")
    monkeypatch.setattr(inv.audit_service, "log", boom)
    with pytest.raises(RuntimeError):
        inv.issue_stock(conn, owner, material_id=paper, qty="500")
    assert snap(conn) == before


def test_weighted_average_and_final_issue_clears_value(conn, owner, fabric):
    inv.receive_stock(conn, owner, material_id=fabric, qty="100", supplier_id=1, total_cost="1000")   # Rs10/m
    inv.receive_stock(conn, owner, material_id=fabric, qty="100", supplier_id=1, total_cost="2000")   # Rs20/m
    r = inv.issue_stock(conn, owner, material_id=fabric, qty="50")                     # avg Rs15
    assert r["cost_paisa"] == 75_000
    r2 = inv.issue_stock(conn, owner, material_id=fabric, qty="150")
    assert r2["cost_paisa"] == 225_000
    s = inv.stock_summary(conn, fabric)
    assert s["factory_qty"] == 0 and s["factory_value_paisa"] == 0


def test_rounding_never_leaks_value(conn, owner, fabric):
    inv.receive_stock(conn, owner, material_id=fabric, qty="3", supplier_id=1, total_cost="10")  # 1000 paisa / 3 m
    total = 0
    for _ in range(3):
        total += inv.issue_stock(conn, owner, material_id=fabric, qty="1")["cost_paisa"]
    assert total == 1000 and inv.stock_summary(conn, fabric)["factory_value_paisa"] == 0


def test_customer_fabric_separate_and_unvalued(conn, owner, fabric):
    cid = inv.add_customer(conn, owner, "Ali Traders")
    inv.receive_stock(conn, owner, material_id=fabric, qty="200", supplier_id=1, total_cost="5000")
    inv.receive_stock(conn, owner, material_id=fabric, qty="300", ownership="customer", customer_id=cid)
    s = inv.stock_summary(conn, fabric)
    assert s["factory_qty"] == D("200") and s["factory_value_paisa"] == 500_000
    assert s["customer_owned"] == {cid: D("300")}
    r = inv.issue_stock(conn, owner, material_id=fabric, qty="120", ownership="customer", customer_id=cid)
    assert r["cost_paisa"] == 0
    s = inv.stock_summary(conn, fabric)
    assert s["factory_qty"] == D("200") and s["customer_owned"] == {cid: D("180")}
    with pytest.raises(inv.InsufficientStock):   # customer stock can't be used beyond what they supplied
        inv.issue_stock(conn, owner, material_id=fabric, qty="181", ownership="customer", customer_id=cid)


def test_customer_stock_cannot_carry_cost(conn, owner, fabric):
    cid = inv.add_customer(conn, owner, "C")
    with pytest.raises(inv.InventoryError):
        inv.receive_stock(conn, owner, material_id=fabric, qty="1", supplier_id=1, total_cost="5",
                          ownership="customer", customer_id=cid)


def test_duplicate_submission_blocked(conn, owner, fabric):
    inv.receive_stock(conn, owner, material_id=fabric, qty="10", supplier_id=1, total_cost="100", idem_key="rcpt-1")
    with pytest.raises(inv.DuplicateSubmission):
        inv.receive_stock(conn, owner, material_id=fabric, qty="10", supplier_id=1, total_cost="100", idem_key="rcpt-1")
    assert inv.stock_summary(conn, fabric)["factory_qty"] == D("10")
    inv.issue_stock(conn, owner, material_id=fabric, qty="1", idem_key="use-1")
    with pytest.raises(inv.DuplicateSubmission):
        inv.issue_stock(conn, owner, material_id=fabric, qty="1", idem_key="use-1")
    assert inv.stock_summary(conn, fabric)["factory_qty"] == D("9")


def test_movements_append_only(conn, owner, fabric):
    inv.receive_stock(conn, owner, material_id=fabric, qty="10", supplier_id=1, total_cost="100")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM stock_movements")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE stock_movements SET qty_milli=1")


def test_ledger_reconciles_with_balance(conn, owner, paper):
    inv.receive_stock(conn, owner, material_id=paper, rolls=["500", "400"], supplier_id=1, total_cost="900")
    inv.issue_stock(conn, owner, material_id=paper, qty="123.456")
    inv.issue_stock(conn, owner, material_id=paper, qty="10", movement_type="wastage", reason="torn")
    q, v = conn.execute("SELECT SUM(qty_milli), SUM(value_paisa) FROM stock_movements WHERE ownership='factory'").fetchone()
    ms = conn.execute("SELECT qty_milli, value_paisa FROM material_stock").fetchone()
    assert (q, v) == (ms[0], ms[1])
    assert conn.execute("SELECT SUM(remaining_milli) FROM stock_rolls").fetchone()[0] == q


def test_reason_required_for_wastage_and_adjustment(conn, owner, fabric):
    inv.receive_stock(conn, owner, material_id=fabric, qty="10", supplier_id=1, total_cost="100")
    with pytest.raises(inv.InventoryError):
        inv.issue_stock(conn, owner, material_id=fabric, qty="1", movement_type="wastage")
    with pytest.raises(inv.InventoryError):
        inv.receive_stock(conn, owner, material_id=fabric, qty="1", movement_type="adjustment_in")


def test_permissions(conn, users, fabric):
    for who in ("aud", "sup"):                                   # auditors only look: they cannot receive, use, waste or adjust stock
        with pytest.raises(auth.PermissionDenied):
            inv.receive_stock(conn, users[who], material_id=fabric, qty="1", supplier_id=1, total_cost="1")
    inv.receive_stock(conn, users["inv"], material_id=fabric, qty="10", supplier_id=1, total_cost="100")      # the accountant can
    with pytest.raises(auth.PermissionDenied):
        inv.issue_stock(conn, users["sup"], material_id=fabric, qty="1")
    inv.issue_stock(conn, users["inv"], material_id=fabric, qty="1")                                           # the accountant can use stock
    inv.issue_stock(conn, users["inv"], material_id=fabric, qty="1", movement_type="wastage", reason="x")     # and book wastage
    with pytest.raises(auth.PermissionDenied):
        inv.issue_stock(conn, users["sup"], material_id=fabric, qty="1", movement_type="wastage", reason="x")


def test_low_stock_alert(conn, owner, paper):
    inv.receive_stock(conn, owner, material_id=paper, rolls=["1500"], supplier_id=1, total_cost="100")
    assert inv.low_stock_materials(conn) == []
    inv.issue_stock(conn, owner, material_id=paper, qty="600")
    assert [r["name"] for r in inv.low_stock_materials(conn)] == ["Transfer Paper"]


def test_roll_material_requires_rolls(conn, owner, paper):
    with pytest.raises(inv.InventoryError):
        inv.receive_stock(conn, owner, material_id=paper, qty="100", supplier_id=1, total_cost="1")
