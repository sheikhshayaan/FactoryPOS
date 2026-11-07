"""Simpler forms: meters produced / price per meter, material forms that show only what the kind needs, no Position column, clean expense categories."""
import pytest

pytest.importorskip("tkinter")
from tests.test_ui_smoke import (app, populated, no_blocking_confirm, login, dialogs, fill, submit, _find, _select, pick)  # noqa: E402,F401
from services import accounting_service as ac, inventory_service as inv, queries as q  # noqa: E402


def _inventory(app):
    login(app); app.show("inventory"); app.update()
    return app.screen.page_cache["inventory"]


def _mid(app, name):
    return next(m["id"] for m in q.materials(app.ctx.conn) if m["name"] == name)


def test_receive_form_has_no_clutter_and_follows_the_kind(app):
    page = _inventory(app)
    inv.add_material(app.ctx.conn, app.ctx.user, "Pigment ink", "ink", "L"); page.refresh()
    page.receive(); app.update()
    d = dialogs(app)[-1]
    keys = [f.key for f in d.fields]
    assert not {"inv", "ref", "loc", "reason", "type"} & set(keys) and d.more_btn is None                               # supplier invoice, purchase ref, storage place, reason are gone
    assert keys.index("own") < keys.index("qty") and keys.index("cus") < keys.index("qty")   # whose stock is it: up with the material
    labels = lambda: [l for l, _v in next(f for f in d.fields if f.key == "mat").choices]
    d.widgets["kind"].set("Ink"); d._changed(); app.update()
    assert labels() == ["Pigment ink  (litre)"] and "rl" in d._hidden                      # only ink; meters-in-a-roll is for paper
    d.widgets["kind"].set("Paper"); d._changed(); app.update()
    assert labels() == ["Paper  (roll)"] and "rl" not in d._hidden
    d.widgets["kind"].set("Fabric (kapra)"); d._changed(); app.update()
    assert labels() == ["Kapra  (meter)"] and "rl" in d._hidden
    assert "cus" in d._hidden and "price" not in d._hidden and "sup" not in d._hidden
    d.set_value("own", "customer"); d._changed(); app.update()
    assert "cus" not in d._hidden and {"price", "cost", "sup", "pay"} <= d._hidden        # his own stock: no price, supplier or payment
    d.destroy()


def test_receive_customers_own_stock_saves_without_price(app):
    page = _inventory(app)
    page.receive(); app.update()
    cust = next(c["id"] for c in q.customers(app.ctx.conn))
    kap = _mid(app, "Kapra")
    own = lambda: app.ctx.conn.execute("SELECT COALESCE(SUM(qty_remaining_milli),0) FROM stock_lots WHERE material_id=? AND ownership='customer'", (kap,)).fetchone()[0]
    before = own()
    submit(app, mat=kap, own="customer", cus=cust, qty="50", on="2026-08-01")
    assert own() - before == 50000


def test_wastage_form_shows_only_what_the_choices_need(app):
    page = _inventory(app)
    page.issue(); app.update()
    d = dialogs(app)[-1]
    assert {"cus", "sup"} <= d._hidden
    d.widgets["type"].set("Return to supplier"); d._changed(); app.update()
    assert "sup" not in d._hidden
    d.widgets["own"].set("Customer's own"); d._changed(); app.update()
    assert "cus" not in d._hidden
    pick(d, _mid(app, "Kapra")); app.update()
    assert "unit" in d._hidden                                                          # rolls / meters switch is for paper only
    pick(d, _mid(app, "Paper")); app.update()
    assert "unit" not in d._hidden
    d.destroy()


def test_new_material_asks_only_what_the_kind_needs(app):
    page = _inventory(app)
    page.new_material(); app.update()
    d = dialogs(app)[-1]
    assert {"rl", "unit"} <= d._hidden                                                  # fabric: unit is meters, no roll length
    d.widgets["cat"].set("Paper  -  counted in rolls and meters"); d._changed(); app.update()
    assert "rl" not in d._hidden and "unit" in d._hidden
    d.widgets["cat"].set("Other (pieces, kg ...)"); d._changed(); app.update()
    assert "unit" not in d._hidden and "rl" in d._hidden
    d.destroy()


def test_suppliers_have_no_position_column_and_customer_stock_is_short(app):
    login(app); app.show("suppliers"); app.update()
    assert [c.key for c in app.screen.page_cache["suppliers"].t.columns] == ["name", "phone", "address", "owed"]
    from ui.pages.people_pages import CustomersPage
    items = [dict(material=n, left_milli=m, base_unit=u, received_milli=m, roll_length_milli=None) for n, m, u in
             (("Kapra", 200000, "m"), ("Paper", 8000, "roll"), ("Ink", 5000, "L"))]
    text = CustomersPage._stock_text(items)
    assert text == "Kapra 200 m;  Paper 8 rolls;  +1 more" and len(text) < 45 and CustomersPage._stock_text([]) == "-"


def test_expense_categories_have_no_codes_and_include_other(app):
    login(app); app.show("expenses"); app.update()
    page = app.screen.page_cache["expenses"]
    page.expense(); app.update()
    d = dialogs(app)[-1]
    names = [l for l, _v in next(f for f in d.fields if f.key == "a").choices]
    assert "Other Expenses" in names and not any(ch.isdigit() for n in names for ch in n)
    submit(app, a="6200", amt="750", d="Misc", on="2026-10-20")
    row = next(r for r in q.expenses(app.ctx.conn) if r["description"] == "Misc")
    assert row["amount_paisa"] == 75000


def test_production_says_meters(app):
    login(app); app.show("jobs"); app.update()
    page = app.screen.page_cache["jobs"]
    assert any(c.title == "Meters" for c in page.t.columns)
    page.new_job(); app.update()
    d = dialogs(app)[-1]
    assert d.labels["pcs"].cget("text") == "Meters produced"
    d.destroy()


def test_use_stock_lists_the_customers_own_ink_and_paper_too(app):
    from ui.pages.production_pages import StockUseDialog
    login(app); conn, o = app.ctx.conn, app.ctx.user
    cust = next(c["id"] for c in q.customers(conn))
    ink = inv.add_material(conn, o, "Pigment ink", "ink", "L")
    paper = _mid(app, "Paper")
    inv.receive_stock(conn, o, material_id=ink, qty="12", ownership="customer", customer_id=cust)
    inv.receive_stock(conn, o, material_id=paper, qty="2", ownership="customer", customer_id=cust, roll_length="300")
    app.show("jobs"); app.update()
    page = app.screen.page_cache["jobs"]
    _select(page, page.t, "job_no", "J1", app)
    page.consume(); app.update()
    d = _find(app, StockUseDialog)[-1]
    for name in ("Pigment ink", "Paper"):
        opts = [m["own"] for l, m in d.mats.items() if m["name"] == name]
        assert opts == ["customer", "factory"], name                                   # his own first, then the factory's
    own_ink = next(l for l, m in d.mats.items() if m["name"] == "Pigment ink" and m["own"] == "customer")
    assert "12 L (customer's own)" in own_ink
    r = d.rows[0]
    r["mat"].set(own_ink); d._material_changed(r); r["qty"].insert(0, "5"); d._calc()
    assert r["cost"].cget("text") in ("Rs 0.00", "-", "customer's")  or "0" in r["cost"].cget("text")
    d.submit(); app.update()
    left = conn.execute("SELECT SUM(qty_remaining_milli) FROM stock_lots WHERE material_id=? AND ownership='customer'", (ink,)).fetchone()[0]
    assert left == 7000


def test_main_lists_show_their_entries_in_bold(app):
    login(app)
    for key, names in (("customers", ["t"]), ("suppliers", ["t"]), ("inventory", ["t", "rec", "led"]), ("jobs", ["t", "r"]), ("expenses", ["t", "assets"])):
        app.show(key); app.update()
        page = app.screen.page_cache[key]
        for n in names:
            assert str(getattr(page, n).tree.cget("style")) == "FMS.Bold.Treeview", (key, n)
    from tkinter import ttk, font as tkfont
    f = ttk.Style().lookup("FMS.Bold.Treeview", "font")
    assert tkfont.Font(name=f, exists=True).actual()["weight"] == "bold"                  # the font really exists (it used to be garbage-collected, so nothing was bold)


def test_extra_backup_folder_is_chosen_with_a_button_not_typed(app, tmp_path, monkeypatch):
    login(app); app.show("settings"); app.update()
    page = app.screen.page_cache["settings"]
    assert page.choose_btn.cget("text") == "Choose folder..."
    usb = tmp_path / "USB drive"; usb.mkdir()
    monkeypatch.setattr("ui.pages.admin_pages.filedialog.askdirectory", lambda **k: str(usb))
    page.choose_btn.invoke(); app.update()
    assert page.e["backup_dir"].get().endswith("USB drive") and "Press Save settings" in page.msg.cget("text")
    page.save(); app.update()
    from services import settings_service as st
    assert st.get(app.ctx.conn, "backup_dir").endswith("USB drive")
    monkeypatch.setattr("ui.pages.admin_pages.filedialog.askdirectory", lambda **k: "")             # cancelling the window changes nothing
    page.choose_btn.invoke(); app.update()
    assert page.e["backup_dir"].get().endswith("USB drive")
    page.clear_folder_btn.invoke(); page.save(); app.update()
    assert st.get(app.ctx.conn, "backup_dir") == ""
