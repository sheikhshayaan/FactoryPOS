"""A stock return to a supplier must show on that supplier's row, matching the Dashboard payables."""
from services import accounting_service as acct, inventory_service as inv, profit_service as ps, queries as q


def _owed(conn, name):
    return {r["name"]: r["owed"] for r in q.suppliers(conn)}[name]


def _setup(conn, owner, status):
    pid = ps.add_partner(conn, owner, "Faizan")
    ps.record_capital(conn, owner, pid, "1000000", "2026-07-01")
    a = inv.add_supplier(conn, owner, "Alpha")
    b = inv.add_supplier(conn, owner, "Beta")
    m = inv.add_material(conn, owner, "Kapra", "fabric", "m")
    inv.receive_stock(conn, owner, material_id=m, qty="1000", total_cost="5000", supplier_id=b, payment_status=status)
    return a, b, m


def test_return_shows_on_supplier_and_dashboard(conn, owner):
    a, b, m = _setup(conn, owner, "unpaid")
    assert _owed(conn, "Beta") == 500000
    inv.issue_stock(conn, owner, material_id=m, qty="150", movement_type="supplier_return", supplier_id=b, reason="bad")
    assert _owed(conn, "Beta") == 500000 - 75000
    assert _owed(conn, "Alpha") == 0
    assert acct.balance(conn, "2000") == 425000


def test_return_after_payment_is_an_advance(conn, owner):
    a, b, m = _setup(conn, owner, "paid")
    inv.issue_stock(conn, owner, material_id=m, qty="150", movement_type="supplier_return", supplier_id=b, reason="bad")
    assert _owed(conn, "Beta") == -75000
    assert acct.balance(conn, "2000") == -75000


# ---- through the real Wastage / return form and the real Suppliers page
import pytest

pytest.importorskip("tkinter")
from tests.test_ui_smoke import (app, populated, no_blocking_confirm, login, dialogs, fill, submit, _find, _select, pick)  # noqa: E402,F401


def test_return_form_lands_on_the_chosen_supplier_row(app):
    login(app); conn = app.ctx.conn; u = app.ctx.user
    b = inv.add_supplier(conn, u, "Zed Traders")
    m = inv.add_material(conn, u, "Return Kapra", "fabric", "m")
    inv.receive_stock(conn, u, material_id=m, qty="1000", total_cost="5000", supplier_id=b, payment_status="paid")
    app.show("inventory"); app.update()
    page = app.screen.page_cache["inventory"]; page.refresh()
    page.issue(); app.update()
    d = dialogs(app)[-1]
    d.widgets["type"].set("Return to supplier"); d._changed(); app.update()
    pick(d, m); app.update()
    d.widgets["sup"].set("Zed Traders"); d._changed(); app.update()
    fill(d, qty="150", reason="damaged"); submit(app)
    app.show("suppliers"); app.update()
    rows = {r["name"]: r["owed"] for r in app.screen.page_cache["suppliers"].t.rows}
    assert rows["Zed Traders"] == -75000


def test_suppliers_page_flags_a_balance_not_tied_to_a_supplier(app):
    login(app); conn = app.ctx.conn; u = app.ctx.user
    acct.post(conn, entry_date="2026-08-01", description="Loose", user=u, source_type="test",
              lines=[{"account": "2000", "debit": 75000}, {"account": "1000", "credit": 75000}])
    app.show("suppliers"); app.update()
    page = app.screen.page_cache["suppliers"]
    assert "not tied to any supplier" in page.note.cget("text")
