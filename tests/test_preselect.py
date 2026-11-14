"""A form opened after clicking a row starts on that row (material, partner, machine, employee) - nothing is forced when nothing is clicked."""
import pytest

pytest.importorskip("tkinter")
from tests.test_ui_smoke import (app, populated, no_blocking_confirm, login, dialogs, _select)  # noqa: E402,F401
from services import inventory_service as inv, payroll_service as pr  # noqa: E402


def _label(d, key):
    f = next(f for f in d.fields if f.key == key)
    return next((l for l, v in f.choices if v == d.values()[key]), None)


def test_receive_and_wastage_start_on_the_selected_material(app):
    login(app); app.show("inventory"); app.update()
    page = app.screen.page_cache["inventory"]
    page.receive(); app.update()
    d = dialogs(app)[-1]
    assert d.values()["kind"] == "fabric"; d.destroy()                         # nothing selected: the first kind, as before
    inv.add_material(app.ctx.conn, app.ctx.user, "Pigment ink", "ink", "L"); page.refresh()
    _select(page, page.t, "name", "Pigment ink", app)
    for opener, kind in ((page.receive, "Ink"), (page.issue, "Ink")):
        opener(); app.update()
        d = dialogs(app)[-1]
        assert d.values()["kind"] == "ink" and _label(d, "mat").startswith("Pigment ink"), (opener.__name__, d.values())
        d.destroy()
    _select(page, page.t, "name", "Kapra", app)
    page.receive(); app.update()
    d = dialogs(app)[-1]
    assert d.values()["kind"] == "fabric" and _label(d, "mat").startswith("Kapra")
    d.destroy()


def test_partner_click_selects_and_capital_form_starts_on_him(app):
    login(app); app.show("partners"); app.update()
    page = app.screen.page_cache["partners"]
    page.capital(); app.update(); d = dialogs(app)[-1]
    assert _label(d, "p") == "A"; d.destroy()                                   # nothing clicked: the first partner
    second = page.t.rows[1]
    page._choose(second); app.update()
    for opener in (page.capital, page.draw):
        opener(); app.update(); d = dialogs(app)[-1]
        assert _label(d, "p") == second["name"]; d.destroy()
    page.refresh(); app.update()
    assert page.sel_id == second["id"]                                          # survives a refresh


def test_past_day_starts_on_the_selected_employee(app):
    login(app)
    c, u = app.ctx.conn, app.ctx.user
    pr.add_employee(c, u, emp_code="E2", name="Zain", monthly_salary="20000", effective_from="2026-01-01")
    app.show("labor"); app.update()
    page = app.screen.page_cache["labor"]
    page.refresh(); app.update()
    _select(page, page.emp, "name", "Zain", app)
    page.add_day(); app.update(); d = dialogs(app)[-1]
    assert _label(d, "e").endswith("Zain"); d.destroy()
