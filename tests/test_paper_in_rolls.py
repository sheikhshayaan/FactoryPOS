"""Paper is counted in rolls (meters are worked out from the roll length; see test_ink_litres_paper_meters.py). Every figure below is worked out by hand.

Weighted-average cost per roll = total value / total rolls. An issue takes rolls x average; the last issue takes
whatever value is left, so no paisa is ever stranded and the books always match inventory.
"""
from decimal import Decimal as D

import pytest

from services import accounting_service as acct, auth_service as auth, dashboard_service as ds, inventory_service as inv, job_service as js
from services.units import to_paisa as P


@pytest.fixture
def paper(conn, owner):
    return inv.add_material(conn, owner, "Paper", "paper", "roll", reorder_level="4")


def test_paper_is_always_a_roll_material(conn, owner):
    m = inv.add_material(conn, owner, "Heat Paper", "paper", "m")            # a wrong unit is ignored: paper is rolls
    row = conn.execute("SELECT base_unit, tracks_rolls FROM materials WHERE id=?", (m,)).fetchone()
    assert (row["base_unit"], row["tracks_rolls"]) == ("roll", 0)
    with pytest.raises(inv.InventoryError):
        inv.add_material(conn, owner, "Other Paper", "paper", "m", tracks_rolls=True)
    with pytest.raises(inv.InventoryError):                                  # other materials still need a unit
        inv.add_material(conn, owner, "Binder", "consumable", "  ")


def test_weighted_average_per_roll_hand_calculation(conn, owner, paper):
    inv.receive_stock(conn, owner, material_id=paper, qty="10", total_cost="120000", supplier_id=1)   # Rs 12,000 / roll
    inv.receive_stock(conn, owner, material_id=paper, qty="5", total_cost="75000", supplier_id=1)     # Rs 15,000 / roll
    s = inv.stock_summary(conn, paper)
    assert s["unit"] == "roll" and s["factory_qty"] == D("15") and s["factory_value_paisa"] == P("195000")
    assert s["avg_cost_paisa_per_unit"] == D(P("13000"))                    # 195,000 / 15
    r = inv.issue_stock(conn, owner, material_id=paper, qty="2.5")          # half a roll is allowed
    assert r["cost_paisa"] == P("32500")                                    # 2.5 x 13,000
    s = inv.stock_summary(conn, paper)
    assert s["factory_qty"] == D("12.5") and s["factory_value_paisa"] == P("162500")
    assert s["avg_cost_paisa_per_unit"] == D(P("13000"))                    # issuing never changes the average
    last = inv.issue_stock(conn, owner, material_id=paper, qty="12.5")      # the last issue clears the value exactly
    assert last["cost_paisa"] == P("162500")
    s = inv.stock_summary(conn, paper)
    assert s["factory_qty"] == 0 and s["factory_value_paisa"] == 0


def test_rounding_never_loses_a_paisa(conn, owner, paper):
    inv.receive_stock(conn, owner, material_id=paper, qty="3", total_cost="100", supplier_id=1)       # Rs 100 for 3 rolls = 3,333.33 paisa each
    costs = [inv.issue_stock(conn, owner, material_id=paper, qty="1")["cost_paisa"] for _ in range(3)]
    assert sum(costs) == P("100")                                           # 3333 + 3334 + 3333 in some order: nothing stranded
    assert max(costs) - min(costs) <= 1
    assert inv.stock_summary(conn, paper)["factory_value_paisa"] == 0


def test_books_match_inventory_after_roll_movements(conn, owner, paper):
    inv.receive_stock(conn, owner, material_id=paper, qty="8", total_cost="40000", supplier_id=1)
    inv.issue_stock(conn, owner, material_id=paper, qty="1.5", movement_type="wastage", reason="torn roll")
    inv.issue_stock(conn, owner, material_id=paper, qty="2")
    assert acct.check_integrity(conn) == []
    assert acct.balance(conn, "1200") == P("22500")                         # 4.5 rolls x 5,000
    assert acct.balance(conn, "5300") == P("7500") and acct.balance(conn, "5000") == P("10000")


def test_cannot_use_more_rolls_than_in_stock(conn, owner, paper):
    inv.receive_stock(conn, owner, material_id=paper, qty="2", total_cost="10000", supplier_id=1)
    with pytest.raises(inv.InsufficientStock):
        inv.issue_stock(conn, owner, material_id=paper, qty="2.001")
    assert inv.stock_summary(conn, paper)["factory_qty"] == D("2")


def test_dashboard_counts_rolls_and_uses_the_movement_date(conn, owner, paper):
    cust = inv.add_customer(conn, owner, "Ali")
    inv.receive_stock(conn, owner, material_id=paper, qty="8", total_cost="40000", supplier_id=1, received_on="2026-07-02")
    j = js.create_job(conn, owner, job_no="P-1", customer_id=cust, product_type="Bedsheet", fabric_source="factory",
                      order_date="2026-07-03", quantity_pieces=50, status="in_progress")
    js.consume_material(conn, owner, j, paper, "1.5", on_date="2026-07-05")       # entered later, but dated 5 July
    jul = ds.snapshot(conn, "2026-07-01", "2026-07-31")["inventory"]
    aug = ds.snapshot(conn, "2026-08-01", "2026-08-31")["inventory"]
    assert jul["paper_rolls_on_hand"] == D("6.5") and jul["paper_rolls_used_in_period"] == D("1.5")
    assert aug["paper_rolls_used_in_period"] == 0 and aug["paper_rolls_on_hand"] == D("6.5")
    assert jul["paper_meters_on_hand"] == D("1950")                                # 6.5 rolls x 300 m


def test_production_entry_calculation(conn, owner, paper):
    cust = inv.add_customer(conn, owner, "Ali")
    ink = inv.add_material(conn, owner, "Ink", "ink", "g")
    inv.receive_stock(conn, owner, material_id=paper, qty="8", total_cost="40000", supplier_id=1)         # Rs 5,000 / roll
    inv.receive_stock(conn, owner, material_id=ink, qty="1000", total_cost="3000", supplier_id=1)         # Rs 3 / g
    j = js.create_job(conn, owner, job_no="P-1", customer_id=cust, product_type="Bedsheet", fabric_source="factory",
                      order_date="2026-07-03", quantity_pieces=40, status="in_progress")
    js.consume_material(conn, owner, j, paper, "1.5", on_date="2026-07-05")        # 7,500
    js.consume_material(conn, owner, j, ink, "250", on_date="2026-07-06")          # 750
    s = js.job_summary(conn, j)
    assert s["stock_cost_paisa"] == P("8250") and s["total_paisa"] == P("8250") and s["other_cost_paisa"] == 0
    assert s["cost_per_piece_paisa"] == P("206.25")                               # 8,250 / 40
    paper_line, ink_line = s["lines"]
    assert (paper_line["date"], paper_line["unit"], paper_line["qty_milli"], paper_line["rate_paisa_per_unit"]) == ("2026-07-05", "roll", 1500, P("5000"))
    assert (ink_line["date"], ink_line["rate_paisa_per_unit"]) == ("2026-07-06", P("3"))
    assert acct.balance(conn, "5000") == P("8250")                                 # same figure reaches the books


def test_cost_per_piece_edge_cases():
    assert js.cost_per_piece(1000, None) is None and js.cost_per_piece(1000, 0) is None
    assert js.cost_per_piece(1000, 3) == 333 and js.cost_per_piece(2000, 3) == 667       # half-up, whole paisa
    assert js.cost_per_piece(-1000, 3) == -333


def test_customer_fabric_has_no_cost_in_the_calculation(conn, owner):
    cust = inv.add_customer(conn, owner, "Ali")
    fab = inv.add_material(conn, owner, "Kapra", "fabric", "m")
    inv.receive_stock(conn, owner, material_id=fab, qty="200", ownership="customer", customer_id=cust)
    j = js.create_job(conn, owner, job_no="P-2", customer_id=cust, product_type="Bedsheet", fabric_source="customer",
                      order_date="2026-07-03", quantity_pieces=10, status="in_progress")
    js.consume_material(conn, owner, j, fab, "30")
    s = js.job_summary(conn, j)
    assert s["total_paisa"] == 0 and s["lines"][0]["customer_owned"] and s["lines"][0]["rate_paisa_per_unit"] is None
    assert s["cost_per_piece_paisa"] == 0


def test_new_entry_must_start_open(conn, owner):
    cust = inv.add_customer(conn, owner, "Ali")
    with pytest.raises(js.JobError):
        js.create_job(conn, owner, job_no="X", customer_id=cust, product_type="b", fabric_source="factory", order_date="2026-07-01", status="finalized")
