"""Asset register (records only), supplier advance refund, customer stock, and the fabric a job can take from either owner."""
import sqlite3

import pytest

from services import (accounting_service as ac, asset_service as assets, auth_service as auth, inventory_service as inv, job_service as js,
                      queries as q)
from services.units import to_paisa as P


@pytest.fixture
def w(conn, owner):
    auth.create_user(conn, owner, "aud", "auditor-pw-1", "auditor")
    ac.record_opening_cash(conn, owner, "500000", "2026-07-01")
    return dict(owner=owner, aud=auth.authenticate(conn, "aud", "auditor-pw-1"))


def books(conn):
    """Everything an asset record must NOT change: cash, the trial balance, profit, and the number of journal entries."""
    return (ac.balance(conn, "1000"), ac.trial_balance(conn), ac.income_statement(conn, "2026-01-01", "2026-12-31")["net_profit"],
            conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0])


# ---------------------------------------------------------------- assets
def test_an_asset_record_never_touches_the_books(conn, w):
    before = books(conn)
    a = assets.add_asset(conn, w["owner"], name="Rotary printing machine", price="1200000", on="2026-08-01", notes="second hand")
    assert books(conn) == before                                                   # cash, trial balance, profit, journal: all unchanged
    assert assets.total_value(conn) == P("1200000")
    row = next(r for r in q.assets(conn) if r["id"] == a)
    assert row["kind"] == "record" and row["price_paisa"] == P("1200000") and not row["voided"] and row["notes"] == "second hand"
    assets.void_asset(conn, w["owner"], a, "typed twice")
    assert books(conn) == before and assets.total_value(conn) == 0                # voiding does not touch the books either
    assert next(r for r in q.assets(conn) if r["id"] == a)["voided"] == 1


def test_asset_checks(conn, w):
    for kw in (dict(name="", price="100", on="2026-08-01"), dict(name="Press", price="0", on="2026-08-01"), dict(name="Press", price="100", on="01-08-2026")):
        with pytest.raises(assets.AssetError):
            assets.add_asset(conn, w["owner"], **kw)
    with pytest.raises(auth.PermissionDenied):
        assets.add_asset(conn, w["aud"], name="Press", price="100", on="2026-08-01")                       # auditors only look
    a = assets.add_asset(conn, w["owner"], name="Press", price="100", on="2026-08-01")
    with pytest.raises(assets.AssetError):
        assets.void_asset(conn, w["owner"], a, " ")
    assets.void_asset(conn, w["owner"], a, "wrong")
    with pytest.raises(assets.AssetError):
        assets.void_asset(conn, w["owner"], a, "again")


def test_asset_records_are_append_only(conn, w):
    a = assets.add_asset(conn, w["owner"], name="Press", price="100", on="2026-08-01")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM asset_records WHERE id=?", (a,))
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE asset_records SET price_paisa=1 WHERE id=?", (a,))
    assets.void_asset(conn, w["owner"], a, "wrong")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE asset_records SET void_reason='x' WHERE id=?", (a,))                           # a voided record is frozen


def test_old_capex_entries_still_show_in_the_asset_list_and_leave_the_expense_list(conn, w):
    e = ac.record_capex(conn, w["owner"], amount="50000", on="2026-08-02", description="Old compressor")
    assert not any(r["description"] == "Old compressor" for r in q.expenses(conn))
    old = next(r for r in q.assets(conn) if r["kind"] == "books")
    assert old["name"] == "Old compressor" and old["price_paisa"] == P("50000") and not old["voided"]
    cash = ac.balance(conn, "1000")
    ac.reverse_entry(conn, w["owner"], e, "bought by mistake")
    assert next(r for r in q.assets(conn) if r["kind"] == "books")["voided"] == 1 and ac.balance(conn, "1000") == cash + P("50000")


def test_a_reversed_expense_is_flagged(conn, w):
    acct = q.expense_accounts(conn)[0]["code"]
    e1 = ac.record_expense(conn, w["owner"], account_code=acct, amount="1000", on="2026-08-03", description="Tea")
    ac.record_expense(conn, w["owner"], account_code=acct, amount="2000", on="2026-08-03", description="Sugar")
    ac.reverse_entry(conn, w["owner"], e1, "typed twice")
    by = {r["description"]: r["reversed"] for r in q.expenses(conn)}
    assert by == {"Tea": 1, "Sugar": 0}


# ---------------------------------------------------------------- supplier advance back
def hold_advance(conn, owner, paisa=P("5000")):
    """The supplier ends up holding `paisa` of ours (we paid ahead)."""
    ac.post(conn, entry_date="2026-08-01", description="Advance to supplier", user=owner, source_type="test",
            lines=[{"account": "2000", "debit": paisa, "supplier_id": 1}, {"account": "1000", "credit": paisa}])


def test_receiving_the_advance_back_brings_cash_in_and_the_balance_to_zero(conn, w):
    hold_advance(conn, w["owner"])
    assert q.suppliers(conn)[0]["owed"] == -P("5000") and ac.supplier_advance(conn, 1) == P("5000")
    cash, profit = ac.balance(conn, "1000"), ac.income_statement(conn, "2026-01-01", "2026-12-31")["net_profit"]
    ac.receive_supplier_refund(conn, w["owner"], supplier_id=1, amount="2000", received_on="2026-08-05", reference="cheque 7")
    assert q.suppliers(conn)[0]["owed"] == -P("3000") and ac.balance(conn, "1000") == cash + P("2000")
    ac.receive_supplier_refund(conn, w["owner"], supplier_id=1, amount="3000", received_on="2026-08-06")
    assert q.suppliers(conn)[0]["owed"] == 0 and ac.balance(conn, "1000") == cash + P("5000")
    assert ac.income_statement(conn, "2026-01-01", "2026-12-31")["net_profit"] == profit                       # a refund is not income
    assert ac.check_integrity(conn) == []


def test_refund_checks(conn, w):
    with pytest.raises(ac.AccountingError):                                                                    # nothing held yet
        ac.receive_supplier_refund(conn, w["owner"], supplier_id=1, amount="1", received_on="2026-08-05")
    hold_advance(conn, w["owner"])
    for bad in ("0", "5000.01", "-1"):
        with pytest.raises(ac.AccountingError):
            ac.receive_supplier_refund(conn, w["owner"], supplier_id=1, amount=bad, received_on="2026-08-05")
    with pytest.raises(auth.PermissionDenied):
        ac.receive_supplier_refund(conn, w["aud"], supplier_id=1, amount="100", received_on="2026-08-05")
    with pytest.raises(ac.AccountingError):
        ac.receive_supplier_refund(conn, w["owner"], supplier_id=99, amount="100", received_on="2026-08-05")


def test_a_refund_can_be_reversed(conn, w):
    hold_advance(conn, w["owner"])
    e = ac.receive_supplier_refund(conn, w["owner"], supplier_id=1, amount="5000", received_on="2026-08-05")
    ac.reverse_entry(conn, w["owner"], e, "cheque bounced")
    assert q.suppliers(conn)[0]["owed"] == -P("5000")


# ---------------------------------------------------------------- customer stock + fabric from either owner
@pytest.fixture
def mill(conn, w):
    o = w["owner"]
    ali, bashir = inv.add_customer(conn, o, "Ali Traders"), inv.add_customer(conn, o, "Bashir Cloth")
    fab = inv.add_material(conn, o, "Kapra", "fabric", "m")
    inv.receive_stock(conn, o, material_id=fab, qty="500", ownership="customer", customer_id=ali, received_on="2026-08-01")
    inv.receive_stock(conn, o, material_id=fab, qty="100", ownership="customer", customer_id=bashir, received_on="2026-08-02")
    inv.receive_stock(conn, o, material_id=fab, qty="200", supplier_id=1, total_cost="40000", received_on="2026-08-03")        # the factory's own: Rs 200 a metre
    j = js.create_job(conn, o, job_no="J1", customer_id=ali, product_type="Bedsheet", fabric_source="customer", order_date="2026-08-04")
    js.set_status(conn, o, j, "approved")
    return dict(owner=o, ali=ali, bashir=bashir, fab=fab, job=j)


def test_customer_stock_is_per_customer(conn, mill):
    got = {r["customer"]: (r["received_milli"], r["left_milli"]) for r in q.customer_stock(conn)}
    assert got == {"Ali Traders": (500_000, 500_000), "Bashir Cloth": (100_000, 100_000)}
    js.consume_material(conn, mill["owner"], mill["job"], mill["fab"], "120", on_date="2026-08-05")                       # the job's default: Ali's own cloth
    assert [(r["received_milli"], r["left_milli"]) for r in q.customer_stock(conn, mill["ali"])] == [(500_000, 380_000)]
    assert [(r["received_milli"], r["left_milli"]) for r in q.customer_stock(conn, mill["bashir"])] == [(100_000, 100_000)]
    lots = q.customer_lots(conn, mill["ali"])
    assert len(lots) == 1 and lots[0]["qty_remaining_milli"] == 380_000 and not lots[0]["undone"]


def test_an_undone_receipt_is_not_counted_as_given(conn, mill):
    lot = q.customer_lots(conn, mill["bashir"])[0]["id"]
    inv.undo_receipt(conn, mill["owner"], lot, "typed for the wrong customer")
    assert q.customer_stock(conn, mill["bashir"]) == []
    assert q.customer_lots(conn, mill["bashir"])[0]["undone"] == 1


def test_a_customer_job_can_also_take_the_factorys_own_fabric(conn, mill):
    o, job, fab = mill["owner"], mill["job"], mill["fab"]
    res = js.consume_materials(conn, o, job, [(fab, "50", None, "factory")], on_date="2026-08-05")
    assert res["cost_paisa"] == P("10000")                                                                   # 50 m x Rs 200 from the factory's stock
    assert [r["left_milli"] for r in q.customer_stock(conn, mill["ali"])] == [500_000]                       # Ali's cloth untouched
    res = js.consume_materials(conn, o, job, [(fab, "30", None, "customer")], on_date="2026-08-05")
    assert res["cost_paisa"] == 0 and q.customer_stock(conn, mill["ali"])[0]["left_milli"] == 470_000        # his own cloth costs nothing
    assert next(m for m in q.materials(conn) if m["id"] == fab)["qty_milli"] == 150_000                      # the factory kept 150 of its 200 m


def test_a_job_can_only_take_its_own_customers_cloth(conn, mill):
    with pytest.raises(js.JobError):                                                                          # Ali has 500 m, not 600
        js.consume_materials(conn, mill["owner"], mill["job"], [(mill["fab"], "600", None, "customer")], on_date="2026-08-05")
    assert q.customer_stock(conn, mill["ali"])[0]["left_milli"] == 500_000
    assert q.customer_stock(conn, mill["bashir"])[0]["left_milli"] == 100_000
