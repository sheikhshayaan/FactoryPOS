"""Edit / give-back / delete on Production and Inventory. Ledgers are append-only, so every 'undo' is a reversal:
the books and the stock always agree afterwards. Numbers are hand-worked."""
from decimal import Decimal as D

import pytest

from services import accounting_service as acct, auth_service as auth, inventory_service as inv, job_service as js, queries as q
from services.units import to_paisa as P


@pytest.fixture
def world(conn, owner):
    paper = inv.add_material(conn, owner, "Paper", "paper", "roll", reorder_level="4")
    cust = inv.add_customer(conn, owner, "Ali")
    cust2 = inv.add_customer(conn, owner, "Zed")
    auth.create_user(conn, owner, "sup", "supervisor-pw", "production_supervisor")
    auth.create_user(conn, owner, "aud", "auditor-pw1", "auditor")
    return dict(paper=paper, cust=cust, cust2=cust2, sup=auth.authenticate(conn, "sup", "supervisor-pw"), aud=auth.authenticate(conn, "aud", "auditor-pw1"))


def entry(conn, owner, w, no="P-1", **kw):
    args = dict(job_no=no, customer_id=w["cust"], product_type="Bedsheet", fabric_source="factory", order_date="2026-07-03", quantity_pieces=50, status="in_progress")
    args.update(kw)
    return js.create_job(conn, owner, **args)


def stock(conn, w):
    s = inv.stock_summary(conn, w["paper"])
    return s["factory_qty"], s["factory_value_paisa"]


# ------------------------------------------------------------- give stock back
def test_return_stock_restores_quantity_value_and_books(conn, owner, world):
    inv.receive_stock(conn, owner, material_id=world["paper"], qty="8", total_cost="40000", supplier_id=1)
    j = entry(conn, owner, world)
    js.consume_material(conn, owner, j, world["paper"], "2", on_date="2026-07-05")
    assert stock(conn, world) == (D("6"), P("30000")) and acct.balance(conn, "5000") == P("10000")
    line = [l for l in js.job_summary(conn, j)["lines"]][0]["id"]
    r = js.return_material(conn, owner, j, line, "entered on the wrong entry", on_date="2026-07-06")
    assert r["cost_paisa"] == P("10000")
    assert stock(conn, world) == (D("8"), P("40000"))
    assert acct.balance(conn, "1200") == P("40000") and acct.balance(conn, "5000") == 0
    s = js.job_summary(conn, j)
    assert s["total_paisa"] == 0 and s["cost_per_piece_paisa"] == 0
    assert s["lines"][0]["returned"] and s["lines"][1]["cost_paisa"] == -P("10000") and s["lines"][1]["qty_milli"] == -2000
    assert acct.check_integrity(conn) == []
    with pytest.raises(js.JobError):                                         # only once
        js.return_material(conn, owner, j, line, "again")


def test_return_goes_back_at_the_original_cost_even_if_the_average_moved(conn, owner, world):
    inv.receive_stock(conn, owner, material_id=world["paper"], qty="8", total_cost="40000", supplier_id=1)       # Rs 5,000 each
    j = entry(conn, owner, world)
    js.consume_material(conn, owner, j, world["paper"], "2")                                                    # leaves at 10,000
    inv.receive_stock(conn, owner, material_id=world["paper"], qty="4", total_cost="60000", supplier_id=1)       # Rs 15,000 each
    assert stock(conn, world) == (D("10"), P("90000"))
    js.return_material(conn, owner, j, js.job_summary(conn, j)["lines"][0]["id"], "mistake")
    assert stock(conn, world) == (D("12"), P("100000"))                                                         # 90,000 + the 10,000 it left with
    assert acct.check_integrity(conn) == []


def test_return_needs_a_reason_and_the_right_kind_of_line(conn, owner, world):
    inv.receive_stock(conn, owner, material_id=world["paper"], qty="8", total_cost="40000", supplier_id=1)
    j = entry(conn, owner, world)
    js.consume_material(conn, owner, j, world["paper"], "2")
    lid = js.job_summary(conn, j)["lines"][0]["id"]
    with pytest.raises(js.JobError):
        js.return_material(conn, owner, j, lid, "  ")
    with pytest.raises(js.JobError):
        js.return_material(conn, owner, j, 9999, "x")
    js.return_material(conn, world["sup"], j, lid, "supervisor can undo their own entry")        # consume_stock permission is enough
    with pytest.raises(auth.PermissionDenied):
        js.return_material(conn, world["aud"], j, lid, "x")


# ------------------------------------------------------------- edit entry
def test_edit_entry_details_and_locks(conn, owner, world):
    j = entry(conn, owner, world)
    js.update_job(conn, owner, j, customer_id=world["cust2"], product_type="Quilt cover", order_date="2026-07-04", quantity_pieces=60, specs="blue", fabric_source="customer")
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (j,)).fetchone()
    assert (row["customer_id"], row["product_type"], row["quantity_pieces"], row["fabric_source"], row["job_no"]) == (world["cust2"], "Quilt cover", 60, "customer", "P-1")
    inv.receive_stock(conn, owner, material_id=world["paper"], qty="8", total_cost="40000", supplier_id=1)
    js.consume_material(conn, owner, j, world["paper"], "1")
    js.update_job(conn, owner, j, customer_id=world["cust2"], product_type="Quilt cover", order_date="2026-07-04", quantity_pieces=40)    # pieces still editable
    assert js.job_summary(conn, j)["cost_per_piece_paisa"] == P("125")                                                                  # 5,000 / 40
    with pytest.raises(js.JobError):
        js.update_job(conn, owner, j, customer_id=world["cust"], product_type="x", order_date="2026-07-04", quantity_pieces=40)
    with pytest.raises(js.JobError):
        js.update_job(conn, owner, j, customer_id=world["cust2"], product_type="x", order_date="2026-07-04", quantity_pieces=0)
    with pytest.raises(auth.PermissionDenied):
        js.update_job(conn, world["aud"], j, customer_id=world["cust2"], product_type="x", order_date="2026-07-04", quantity_pieces=40)


# ------------------------------------------------------------- delete entry
def test_delete_entry_rules(conn, owner, world):
    inv.receive_stock(conn, owner, material_id=world["paper"], qty="8", total_cost="40000", supplier_id=1)
    empty = entry(conn, owner, world, "E-1")
    assert js.delete_job(conn, owner, empty) == "deleted"                                       # nothing ever recorded: really gone
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE job_no='E-1'").fetchone()[0] == 0
    entry(conn, owner, world, "E-1")                                                            # the number is free again
    used = entry(conn, owner, world, "E-2")
    js.consume_material(conn, owner, used, world["paper"], "2")
    with pytest.raises(js.JobError):
        js.delete_job(conn, owner, used)                                                        # stock still out
    js.return_material(conn, owner, used, js.job_summary(conn, used)["lines"][0]["id"], "oops")
    assert js.delete_job(conn, owner, used) == "cancelled"                                      # history kept, hidden from the list
    assert "E-2" not in [r["job_no"] for r in q.jobs(conn)] and conn.execute("SELECT status FROM jobs WHERE id=?", (used,)).fetchone()[0] == "cancelled"
    with pytest.raises(js.JobError):
        js.delete_job(conn, owner, used)
    assert stock(conn, world) == (D("8"), P("40000")) and acct.check_integrity(conn) == []


def test_cannot_delete_an_entry_that_has_an_invoice(conn, owner, world):
    auth.create_user(conn, owner, "acc", "accountant-pw", "accountant")
    j = entry(conn, owner, world)
    acct.issue_invoice(conn, auth.authenticate(conn, "acc", "accountant-pw"), invoice_no="I1", customer_id=world["cust"], total="1000", invoice_date="2026-07-10", job_id=j)
    with pytest.raises(js.JobError):
        js.delete_job(conn, owner, j)


# ------------------------------------------------------------- materials
def test_edit_material(conn, owner, world):
    ink = inv.add_material(conn, owner, "Ink", "ink", "g")                                                              # asked for g: ink is always litres
    assert conn.execute("SELECT base_unit FROM materials WHERE id=?", (ink,)).fetchone()[0] == "L"
    inv.update_material(conn, owner, ink, name="Reactive Ink", category="ink", base_unit="L", reorder_level="500")
    m = conn.execute("SELECT * FROM materials WHERE id=?", (ink,)).fetchone()
    assert (m["name"], m["base_unit"], m["reorder_level_milli"]) == ("Reactive Ink", "L", 500000)
    inv.receive_stock(conn, owner, material_id=ink, qty="100", total_cost="500", supplier_id=1)
    inv.update_material(conn, owner, ink, name="Reactive Ink 2", category="ink", base_unit="L", reorder_level="200")      # name / reorder always fine
    inv.update_material(conn, owner, ink, name="Reactive Ink 2", category="ink", base_unit="g", reorder_level="200")      # ink is always L: the unit cannot be changed
    assert conn.execute("SELECT base_unit FROM materials WHERE id=?", (ink,)).fetchone()[0] == "L"
    with pytest.raises(inv.InventoryError):
        inv.update_material(conn, owner, ink, name="Reactive Ink 2", category="consumable", base_unit="g", reorder_level="200")  # category locked once stock exists
    with pytest.raises(inv.InventoryError):
        inv.update_material(conn, owner, ink, name="Paper", category="ink", base_unit="ml")                              # duplicate name
    inv.update_material(conn, owner, world["paper"], name="Paper", category="paper", base_unit="m", reorder_level="4")    # paper stays rolls
    assert conn.execute("SELECT base_unit FROM materials WHERE id=?", (world["paper"],)).fetchone()[0] == "roll"


def test_delete_material_rules(conn, owner, world):
    unused = inv.add_material(conn, owner, "Spare", "consumable", "pcs")
    assert inv.delete_material(conn, owner, unused) == "deleted"
    assert conn.execute("SELECT COUNT(*) FROM materials WHERE name='Spare'").fetchone()[0] == 0
    inv.receive_stock(conn, owner, material_id=world["paper"], qty="3", total_cost="9000", supplier_id=1)
    with pytest.raises(inv.InventoryError):
        inv.delete_material(conn, owner, world["paper"])                                        # stock on hand
    inv.issue_stock(conn, owner, material_id=world["paper"], qty="3", movement_type="wastage", reason="damp")
    assert inv.delete_material(conn, owner, world["paper"]) == "archived"                       # has history: kept, hidden
    assert "Paper" not in [m["name"] for m in q.materials(conn)]
    with pytest.raises(inv.InventoryError):
        inv.receive_stock(conn, owner, material_id=world["paper"], qty="1", total_cost="1", supplier_id=1)
    assert acct.check_integrity(conn) == []


# ------------------------------------------------------------- undo receipt
def test_undo_credit_purchase(conn, owner, world):
    lot = inv.receive_stock(conn, owner, material_id=world["paper"], qty="10", total_cost="120000", supplier_id=1)
    assert acct.balance(conn, "2000") == P("120000") and acct.balance(conn, "1200") == P("120000")
    inv.undo_receipt(conn, owner, lot, "typed the wrong quantity", on_date="2026-07-07")
    assert stock(conn, world) == (D("0"), 0)
    assert acct.balance(conn, "1200") == 0 and acct.balance(conn, "2000") == 0
    assert [r for r in q.stock_receipts(conn) if r["id"] == lot][0]["undone"] == 1
    assert acct.check_integrity(conn) == []
    with pytest.raises(inv.InventoryError):
        inv.undo_receipt(conn, owner, lot, "again")


def test_undo_cash_purchase_returns_the_cash(conn, owner, world):
    acct.record_opening_cash(conn, owner, "200000", "2026-07-01")
    lot = inv.receive_stock(conn, owner, material_id=world["paper"], qty="10", total_cost="120000", payment_status="paid")
    assert acct.balance(conn, "1000") == P("80000")
    inv.undo_receipt(conn, owner, lot, "wrong supplier")
    assert acct.balance(conn, "1000") == P("200000") and stock(conn, world) == (D("0"), 0)


def test_undo_is_refused_when_unsafe(conn, owner, world):
    a = inv.receive_stock(conn, owner, material_id=world["paper"], qty="10", total_cost="120000", supplier_id=1)
    inv.issue_stock(conn, owner, material_id=world["paper"], qty="1")
    with pytest.raises(inv.InventoryError):                                                   # partly used
        inv.undo_receipt(conn, owner, a, "x")
    b = inv.receive_stock(conn, owner, material_id=world["paper"], qty="5", total_cost="75000", supplier_id=1)
    inv.issue_stock(conn, owner, material_id=world["paper"], qty="1")                          # something left the material after b arrived
    with pytest.raises(inv.InventoryError):
        inv.undo_receipt(conn, owner, b, "x")
    with pytest.raises(inv.InventoryError):
        inv.undo_receipt(conn, owner, 9999, "x")
    with pytest.raises(inv.InventoryError):
        inv.undo_receipt(conn, owner, b, "")
    with pytest.raises(auth.PermissionDenied):
        inv.undo_receipt(conn, world["sup"], b, "x")
    assert acct.check_integrity(conn) == []


def test_undo_customer_property_receipt(conn, owner, world):
    fab = inv.add_material(conn, owner, "Kapra", "fabric", "m")
    lot = inv.receive_stock(conn, owner, material_id=fab, qty="200", ownership="customer", customer_id=world["cust"])
    inv.undo_receipt(conn, owner, lot, "wrong customer")
    assert inv.stock_summary(conn, fab)["customer_owned"] == {}
