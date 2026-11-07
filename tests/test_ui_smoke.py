"""Drives the real CustomTkinter UI (needs a display; run under xvfb on Linux). Skipped if Tk is unavailable."""
import calendar
import os
from datetime import date, datetime, timedelta
import sys

import pytest

tk = pytest.importorskip("tkinter")
try:
    _r = tk.Tk(); _r.destroy()
except Exception:                                   # no display
    pytest.skip("no display available for Tk", allow_module_level=True)

import customtkinter as ctk  # noqa: E402

from config.paths import get_paths  # noqa: E402
from services import (accounting_service as ac, auth_service as auth, inventory_service as inv, job_service as js,  # noqa: E402
                      payroll_service as pr, profit_service as ps, queries as q)
from services.startup import initialize_new_database  # noqa: E402
from ui import app as appmod, widgets  # noqa: E402
from ui.widgets import FormDialog  # noqa: E402


@pytest.fixture(autouse=True)
def no_blocking_confirm(monkeypatch):
    monkeypatch.setattr("ui.base.confirm", lambda *a, **k: True)
    monkeypatch.setattr("ui.widgets.confirm", lambda *a, **k: True)


@pytest.fixture
def populated(tmp_path):
    paths = get_paths(tmp_path / "app"); paths.ensure_dirs()
    c = initialize_new_database(paths)
    auth.create_user(c, None, "boss", "correct-horse", "owner", "The Boss")
    o = auth.authenticate(c, "boss", "correct-horse")
    for n, r in (("acc", "accountant"), ("aud", "auditor")):
        auth.create_user(c, o, n, "password-123", r)
    from services import settings_service
    settings_service.set_values(c, o, {"factory_name": "Test Mills"})
    c.execute("INSERT INTO suppliers(id,name,created_at) VALUES (2,'Paper House','x')")
    ac.record_opening_cash(c, o, "500000", "2026-07-01")
    a = ps.add_partner(c, o, "A"); b = ps.add_partner(c, o, "B")
    ps.record_capital(c, o, a, "300000", "2026-07-01"); ps.record_capital(c, o, b, "100000", "2026-07-01")
    ps.set_profit_shares(c, o, "2026-07-01", {a: 5000, b: 5000}, confirmed=True)
    cust = inv.add_customer(c, o, "Ali Traders")
    paper = inv.add_material(c, o, "Paper", "paper", "roll", reorder_level="4")
    fab = inv.add_material(c, o, "Kapra", "fabric", "m")
    inv.receive_stock(c, o, material_id=paper, qty="8", supplier_id=2, total_cost="40000", received_on="2026-07-02")
    inv.receive_stock(c, o, material_id=fab, qty="200", ownership="customer", customer_id=cust)
    j = js.create_job(c, o, job_no="J1", customer_id=cust, product_type="Bedsheet", fabric_source="customer", order_date="2026-07-03")
    js.set_status(c, o, j, "approved")
    js.consume_material(c, o, j, paper, "2", on_date="2026-07-05")
    i1 = ac.issue_invoice(c, o, invoice_no="I1", customer_id=cust, total="60000", invoice_date="2026-07-10", job_id=j)
    ac.receive_payment(c, o, customer_id=cust, amount="20000", paid_on="2026-07-11", invoice_id=i1)
    e = pr.add_employee(c, o, emp_code="E1", name="Bilal", monthly_salary="30000", effective_from="2026-01-01")
    pr.accrue_salary(c, o, e, "2026-07")
    c.close()
    return paths


@pytest.fixture
def app(populated):
    a = appmod.App(populated)
    a.update()
    yield a
    try:
        a.on_close()
    except Exception:
        pass


def login(app, user="boss", pw="correct-horse"):
    s = app.screen
    s.u.delete(0, "end"); s.p.delete(0, "end")
    s.u.insert(0, user); s.p.insert(0, pw); s.go(); app.update()
    return app


def dialogs(app):
    out = []
    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, FormDialog):
                out.append(c)
            walk(c)
    walk(app)
    return out


def _pick_kind(dlg, mat_id):
    """Material forms ask for the kind first and then list only that kind's materials; do the same here before choosing the material."""
    kind = dlg.master.ctx.conn.execute("SELECT category FROM materials WHERE id=?", (mat_id,)).fetchone()[0]
    f = next(f for f in dlg.fields if f.key == "kind")
    dlg.widgets["kind"].set(next(l for l, v in f.choices if v == kind)); dlg._changed()


def pick(dlg, mat_id):
    _pick_kind(dlg, mat_id)
    dlg.set_value("mat", mat_id); dlg._changed()


def fill(dlg, **vals):
    if "mat" in vals and "kind" in dlg.widgets and "kind" not in vals:
        _pick_kind(dlg, vals["mat"])
    for k, v in vals.items():
        w = dlg.widgets[k]
        f = next(f for f in dlg.fields if f.key == k)
        if f.kind == "choice":
            label = next(l for l, val in f.choices if val == v or l == v)
            w.set(label)
        elif f.kind == "multiline":
            w.delete("1.0", "end"); w.insert("1.0", v)
        else:
            w.configure(state="normal"); w.delete(0, "end"); w.insert(0, v)


def submit(app, **vals):
    d = dialogs(app)[-1]
    fill(d, **vals)
    d.submit(); app.update()
    return d


def _find(app, cls):
    found = []

    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, cls):
                found.append(c)
            walk(c)
    walk(app)
    return found


def use_stock(app, lines, on="2026-08-02"):
    """Fill the 'Use stock' dialog: lines = [(material name, quantity), ...], then press the button."""
    from ui.pages.production_pages import StockUseDialog
    d = _find(app, StockUseDialog)[-1]
    for i, line in enumerate(lines):
        name, qty = line[0], line[1]
        if i >= len(d.rows):
            d.add_row()
        r = d.rows[i]
        r["mat"].set(next(l for l, m in d.mats.items() if m["name"] == name))
        d._material_changed(r)                                    # what picking the material in the list does
        if len(line) > 2:
            r["unit_seg"].set(line[2])                            # paper: "Rolls" or "Meters"
        r["qty"].delete(0, "end"); r["qty"].insert(0, qty)
    d._calc(); d.on.set_date(on)
    d.submit(); app.update()
    return d


def test_login_wrong_then_right(app):
    s = app.screen
    s.u.insert(0, "boss"); s.p.insert(0, "nope-nope"); s.go(); app.update()
    assert "Invalid" in s.err.cget("text") and isinstance(app.screen, appmod.LoginScreen)
    login(app)
    assert isinstance(app.screen, appmod.MainScreen)


def test_every_page_opens_for_owner(app):
    login(app)
    keys = [k for k, *_ in app.screen.nav]
    assert len(keys) == 16 and "costing" in keys and "insights" in keys and "machines" not in keys and "sales" not in keys and "payroll" not in keys
    for k in keys:
        app.show(k); app.update()


def test_dashboard_numbers_come_from_database(app):
    login(app)
    dash = app.screen.page_cache["dashboard"]
    dash.kind.set("this_year"); dash.refresh(); app.update()
    # hand-check: revenue 60,000 ; direct materials 500m*Rs20 = 10,000 ; direct labour 30,000
    assert dash.cards["sales"].value.cget("text") == "Rs 60,000.00"
    assert dash.cards["prod"].value.cget("text") == "Rs 40,000.00"
    assert dash.cards["net"].value.cget("text") == "Rs 20,000.00"
    assert dash.cards["net"].kind == "good"                                  # profit -> green box
    dash.kind.set("this_month"); dash.refresh(); app.update()
    assert dash.cards["net"].kind in ("good", "bad", "neutral")
    dash.kind.set("this_year"); dash.refresh(); app.update()
    assert dash.cards["cash"].value.cget("text") == "Rs 920,000.00"       # 500k opening + 400k capital + 20k received


ACCOUNTANT_PAGES = {"customers", "suppliers", "inventory", "jobs", "labor", "costing", "expenses"}
AUDITOR_PAGES = ACCOUNTANT_PAGES | {"partners", "profit", "reports", "audit", "backup"}


def test_role_based_navigation_and_disabled_actions(populated):
    for user, pages, landing in (("acc", ACCOUNTANT_PAGES, "jobs"), ("aud", AUDITOR_PAGES, "jobs")):
        a = appmod.App(populated); a.update()
        try:
            login(a, user, "password-123")
            visible = {k for k, *_ in a.screen.nav}
            assert visible == pages, (user, visible ^ pages)
            assert not (visible & {"dashboard", "insights", "settings", "users"})                 # neither of them ever sees these
            assert a.screen.key == landing                                                         # lands on the first page they may open, not on a Dashboard they cannot see
        finally:
            a.on_close()
    a = appmod.App(populated); a.update()
    try:
        login(a, "boss", "correct-horse")
        assert len(a.screen.nav) == 16 and a.screen.key == "dashboard"
    finally:
        a.on_close()
    a = appmod.App(populated); a.update()
    try:
        login(a, "aud", "password-123")
        a.show("jobs"); a.update()
        state = {t: enabled for t, _b, enabled in a.screen.page_cache["jobs"].buttons}
        assert state["Create invoice"] is False and state["Receive payment"] is False and state["+ New entry"] is False
        assert state["Invoice PDF"] is True                       # read-only users may still print
        a.show("partners"); a.update()
        assert not any(en for t, _b, en in a.screen.page_cache["partners"].buttons)
    finally:
        a.on_close()


def test_add_customer_via_dialog(app):
    login(app); app.show("customers"); app.update()
    page = app.screen.page_cache["customers"]
    page.add(); app.update()
    submit(app, name="Zed Textiles", phone="0300", address="Faisalabad")
    assert "Zed Textiles" in [r["name"] for r in q.customers(app.ctx.conn)]
    assert any(r["name"] == "Zed Textiles" for r in page.t.rows)


def test_error_in_dialog_keeps_it_open(app):
    login(app); app.show("customers"); app.update()
    page = app.screen.page_cache["customers"]
    page.add(); app.update()
    d = submit(app, name="Ali Traders")            # duplicate
    assert d.winfo_exists() and "already exists" in d.err.cget("text")
    assert d.submit_btn.cget("state") == "normal"


def test_receive_paper_in_rolls_then_ledger(app):
    login(app); app.show("inventory"); app.update()
    page = app.screen.page_cache["inventory"]
    page.receive(); app.update()
    pid = next(m["id"] for m in q.materials(app.ctx.conn) if m["name"] == "Paper")
    submit(app, mat=pid, qty="3", price="5000", own="factory", sup=2, pay="unpaid", on="2026-08-01")
    s = inv.stock_summary(app.ctx.conn, pid)
    assert s["unit"] == "roll" and str(s["factory_qty"]) == "9.000"          # 6 left + 3 received
    assert s["factory_value_paisa"] == 4500000                                # Rs 30,000 left + Rs 15,000 bought
    app.update()
    assert len(page.led.rows) >= 2
    assert list(page.tabs._tab_dict) == ["Stock", "Receipts", "Ledger"]       # no per-roll / meters tab any more


def test_production_entry_use_stock_and_calculation(app):
    login(app); app.show("jobs"); app.update()
    page = app.screen.page_cache["jobs"]
    assert [t for t, _b, _e in page.buttons] == ["+ New entry", "Use stock", "Calculation", "Edit", "Return stock", "Delete", "Create invoice", "Receive payment", "Invoice PDF"]
    page.new_job(); app.update()
    cust = next(c["id"] for c in q.customers(app.ctx.conn))
    submit(app, no="P-0002", od="2026-08-02", cus=cust, prod="Bedsheet", pcs="50", src="factory", notes="")
    page.refresh(); app.update()
    _select(page, page.t, "job_no", "P-0002", app)
    page.consume(); app.update()
    use_stock(app, [("Paper", "1.5")])
    page.refresh(); app.update()
    row = next(r for r in page.t.rows if r["job_no"] == "P-0002")
    assert row["cost_paisa"] == 750000 and row["bill_status"] == "none"          # 1.5 rolls x Rs 5,000 = Rs 7,500
    assert js.cost_per_piece(row["cost_paisa"], row["quantity_pieces"]) == 15000  # / 50 pieces = Rs 150.00
    _select(page, page.t, "job_no", "P-0002", app)
    page.details(); app.update()
    from ui.pages.production_pages import CalculationDialog
    found = _find(app, CalculationDialog)
    assert found and found[0].winfo_exists()
    found[0].destroy()


def test_use_several_materials_at_once_and_all_or_nothing(app):
    login(app); app.show("jobs"); app.update()
    page = app.screen.page_cache["jobs"]
    conn = app.ctx.conn
    ink = inv.add_material(conn, app.ctx.user, "Ink", "ink", "L")
    inv.receive_stock(conn, app.ctx.user, material_id=ink, qty="1", unit_price="500", supplier_id=2)       # 1 L at Rs 500
    page.new_job(); app.update()
    cust = next(c["id"] for c in q.customers(conn))
    submit(app, no="P-0002", od="2026-08-02", cus=cust, prod="Bedsheet", pcs="10", src="factory", notes="")
    page.refresh(); _select(page, page.t, "job_no", "P-0002", app)
    page.consume(); app.update()
    d = use_stock(app, [("Paper", "1"), ("Ink", "5")])                                                     # ink: only 1 L exists
    assert d.winfo_exists() and "Line 2" in d.err.cget("text")
    assert inv.stock_summary(conn, next(m["id"] for m in q.materials(conn) if m["name"] == "Paper"))["factory_qty"] == 6   # nothing was taken
    d.rows[1]["qty"].delete(0, "end"); d.rows[1]["qty"].insert(0, "0.2")
    d.go_btn.configure(state="normal"); d.submit(); app.update()
    page.refresh(); app.update()
    row = next(r for r in page.t.rows if r["job_no"] == "P-0002")
    assert row["cost_paisa"] == 500000 + 10000                                                               # 1 roll Rs 5,000 + 0.2 L x Rs 500 = Rs 100


def test_create_invoice_applies_the_customers_advance(app):
    login(app); app.show("jobs"); app.update()
    page = app.screen.page_cache["jobs"]
    conn = app.ctx.conn
    page.new_job(); app.update()
    cust = next(c["id"] for c in q.customers(conn))
    submit(app, no="P-0002", od="2026-08-02", cus=cust, prod="Bedsheet", pcs="50", src="factory", notes="")
    page.refresh(); _select(page, page.t, "job_no", "P-0002", app)
    page.consume(); app.update(); use_stock(app, [("Paper", "1.5")])                                       # cost Rs 7,500
    app.show("customers"); app.update()
    cp = app.screen.page_cache["customers"]
    _select(cp, cp.t, "name", "Ali Traders", app)
    cp.advance(); app.update(); submit(app, a="5000", on="2026-08-03", m="cash", ref="adv")
    app.show("jobs"); app.update()
    _select(page, page.t, "job_no", "P-0002", app)
    page.invoice(); app.update()
    from ui.pages.production_pages import InvoiceDialog
    d = _find(app, InvoiceDialog)[-1]
    d.rate.insert(0, "400"); d._from_rate(); app.update()
    assert d.total.get() == "20000.00"                                                                      # 50 x Rs 400
    assert "12,500.00" in d.profit.cget("text")                                                             # 20,000 - 7,500
    assert "5,000.00" in d.adv.cget("text") and "15,000.00" in d.adv.cget("text")
    d.submit(); app.update()
    row = next(r for r in page.t.rows if r["job_no"] == "P-0002")
    assert (row["invoice_paisa"], row["paid_paisa"], row["due_paisa"], row["bill_status"]) == (2000000, 500000, 1500000, "part")
    assert conn.execute("SELECT invoice_no FROM invoices WHERE job_id=?", (row["id"],)).fetchone()[0] == "P-0002"   # invoice number = entry number
    _select(page, page.t, "job_no", "P-0002", app)
    page.payment(); app.update()
    d2 = dialogs(app)[-1]
    assert d2.widgets["a"].get() == "15000.00"                                                              # the balance is filled in for him
    d2.submit(); app.update()
    page.refresh(); app.update()
    assert next(r for r in page.t.rows if r["job_no"] == "P-0002")["bill_status"] == "paid"


def test_calendar_picks_a_date_without_typing(app):
    login(app); app.show("dashboard"); app.update()
    dash = app.screen.page_cache["dashboard"]
    dash._pick("Custom"); app.update()
    from datetime import date as _d
    from ui.widgets import CalendarPopup
    dash.c_from.open(); app.update()
    pop = _find(app, CalendarPopup)[-1]
    pop._shift(-1); app.update()                                                                             # previous month
    y, m = pop.year, pop.month
    next(b for i, b in enumerate(pop.cells) if b.cget("text") == "5" and i < 14).invoke(); app.update()
    assert dash.c_from.get() == f"{y}-{m:02d}-05" and not _find(app, CalendarPopup)
    assert dash.period_lbl.cget("text").startswith(f"{y}-{m:02d}-05")                                          # the figures followed the calendar
    d = _d.today()
    assert dash.c_to.get() == d.isoformat()


def test_every_date_in_a_form_is_a_calendar(app):
    login(app); app.show("customers"); app.update()
    page = app.screen.page_cache["customers"]
    _select(page, page.t, "name", "Ali Traders", app)
    page.advance(); app.update()
    from ui.widgets import DateField
    assert isinstance(dialogs(app)[-1].widgets["on"], DateField)


def test_new_material_unit_follows_the_kind(app):
    login(app); app.show("inventory"); app.update()
    page = app.screen.page_cache["inventory"]
    page.new_material(); app.update()
    d = dialogs(app)[-1]
    assert d.maps["unit"] and d.widgets["unit"].get() == "Meter (m)"                                         # fabric is the first kind: meters
    for cat, label in (("ink", "Litre (L)"), ("paper", "Roll"), ("fabric", "Meter (m)")):
        d.widgets["cat"].set(next(l for l, v in d.fields[1].choices if v == cat)); d._changed()
        assert d.widgets["unit"].get() == label and d.widgets["unit"].cget("state") == "disabled"
    d.widgets["cat"].set(next(l for l, v in d.fields[1].choices if v == "consumable")); d._changed()
    assert d.widgets["unit"].cget("state") != "disabled"                                                    # only 'Other' chooses a unit
    d.destroy()


def test_receive_stock_total_is_worked_out_live(app):
    login(app); app.show("inventory"); app.update()
    page = app.screen.page_cache["inventory"]
    page.receive(); app.update()
    d = dialogs(app)[-1]
    pid = next(m["id"] for m in q.materials(app.ctx.conn) if m["name"] == "Paper")
    pick(d, pid)
    d.set_value("qty", "12.5"); d.set_value("price", "38500"); d._changed()
    assert d.widgets["cost"].get() == "Rs 481,250.00"                                                        # 12.5 rolls x Rs 38,500
    assert "rolls" in d.labels["qty"].cget("text") and "roll" in d.labels["price"].cget("text")
    d.set_value("own", "customer"); d._changed()
    assert "cost" in d._hidden and "price" in d._hidden and "cus" not in d._hidden           # the customer's own stock has no price
    d.destroy()


def test_customers_edit_and_delete_buttons(app, monkeypatch):
    monkeypatch.setattr("ui.pages.people_pages.show_info", lambda *a, **k: None)
    login(app); app.show("customers"); app.update()
    page = app.screen.page_cache["customers"]
    page.add(); app.update(); submit(app, name="Temp Co", phone="1", address="x")
    _select(page, page.t, "name", "Temp Co", app)
    page.edit(); app.update(); submit(app, name="Temp Company", phone="2")
    assert "Temp Company" in [r["name"] for r in page.t.rows]
    _select(page, page.t, "name", "Temp Company", app)
    page.delete(); app.update()
    assert "Temp Company" not in [r["name"] for r in page.t.rows]
    assert [r for r in page.t.rows if r["name"] == "Ali Traders"][0]["billed"] == 6000000                    # the old invoice, Rs 60,000


def test_receive_payment_and_invoice_pdf(app, tmp_path, monkeypatch):
    login(app); app.show("jobs"); app.update()
    page = app.screen.page_cache["jobs"]
    _select(page, page.t, "job_no", "J1", app)                                                               # the old invoice (I1: Rs 60,000, Rs 20,000 paid)
    page.payment(); app.update()
    d = dialogs(app)[-1]
    fill(d, a="10000", on="2026-07-20", m="cash", ref="R2")
    d.submit(); app.update()
    assert q.invoices(app.ctx.conn)[0]["due_paisa"] == 3000000
    out = tmp_path / "inv.pdf"
    monkeypatch.setattr("ui.pages.production_pages.filedialog.asksaveasfilename", lambda **k: str(out))
    monkeypatch.setattr("ui.pages.production_pages.show_info", lambda *a, **k: None)
    _select(page, page.t, "job_no", "J1", app); page.pdf(); app.update()
    assert out.read_bytes()[:4] == b"%PDF"


def _labor(app):
    login(app); app.show("labor"); app.update()
    return app.screen.page_cache["labor"]


def _emp_id(app, name="Bilal"):
    return app.ctx.conn.execute("SELECT id FROM employees WHERE name=?", (name,)).fetchone()[0]


def _salary_row(page, name="Bilal"):
    return next(r for r in pr.month_overview(page.conn, page.period) if r["name"] == name)


def _state(page, eid):
    return next(r for r in page._board if r["id"] == eid)


def _enabled(page, eid, key):
    return page.btns[(eid, key)].cget("state") == "normal"


def _senabled(page, eid, key):
    return page.sbtns[(eid, key)].cget("state") == "normal"


def test_one_page_for_attendance_and_salaries(app):
    page = _labor(app)
    names = page.tabs._name_list if hasattr(page.tabs, "_name_list") else list(page.tabs._tab_dict)
    assert list(names) == ["Today", "Month & Salaries", "Records", "Employees"]
    keys = [k for k, *_ in app.screen.nav]
    assert "labor" in keys and "payroll" not in keys


def test_today_board_in_out_buttons_stamp_the_time(app):
    page = _labor(app); eid = _emp_id(app)
    assert _state(page, eid)["state"] == "idle"
    assert _enabled(page, eid, "in") and not _enabled(page, eid, "out") and _enabled(page, eid, "absent") and _enabled(page, eid, "leave")
    page.press(eid, "in"); app.update()
    r = _state(page, eid)
    assert r["state"] == "in" and r["clock_in"][:10] == date.today().isoformat()          # stamped with the current time, nothing typed
    assert not _enabled(page, eid, "in") and _enabled(page, eid, "out") and not _enabled(page, eid, "absent") and _enabled(page, eid, "undo")
    # pretend IN was 3h 20m ago, then press OUT
    ci = (datetime.now() - timedelta(minutes=200)).replace(second=0, microsecond=0).isoformat(timespec="minutes")
    app.ctx.conn.execute("UPDATE attendance SET clock_in=? WHERE employee_id=?", (ci, eid))
    page.press(eid, "out"); app.update()
    r = _state(page, eid)
    assert r["state"] == "done" and r["minutes"] == 200 and not r["auto_out"]
    assert not any(_enabled(page, eid, k) for k in ("in", "out", "absent", "leave"))


def test_today_board_absent_leave_and_undo(app):
    page = _labor(app); eid = _emp_id(app)
    page.press(eid, "absent"); app.update()
    assert _state(page, eid)["state"] == "absent" and _enabled(page, eid, "leave") and _enabled(page, eid, "in") and not _enabled(page, eid, "absent")
    page.press(eid, "leave"); app.update()
    assert _state(page, eid)["state"] == "leave"
    page.press(eid, "undo"); app.update()
    assert _state(page, eid)["state"] == "idle"


def test_forgotten_out_closes_after_ten_hours_with_a_warning(app):
    page = _labor(app); eid = _emp_id(app)
    page.press(eid, "in"); app.update()
    old = (datetime.now() - timedelta(hours=11)).replace(second=0, microsecond=0).isoformat(timespec="minutes")
    app.ctx.conn.execute("UPDATE attendance SET clock_in=? WHERE employee_id=?", (old, eid))
    page._housekeeping(); app.update()                                                     # the once-a-minute check
    r = _state(page, eid)
    assert r["auto_out"] == 1 and r["minutes"] == 600 and r["state"] == "done"
    assert page.notice_box.winfo_manager() and "closed automatically" in page.notice.cget("text")
    page.refresh(); app.update()
    assert page.att.rows and any(x["auto_out"] for x in page.att.rows)


def test_buttons_are_locked_for_roles_that_only_look(populated):
    a = appmod.App(populated); a.update()
    try:
        login(a, "aud", "password-123"); a.show("labor"); a.update()
        page = a.screen.page_cache["labor"]
        eid = _emp_id(a)
        assert not any(_enabled(page, eid, k) for k in ("in", "out", "absent", "leave"))
        row = _salary_row(page)
        assert not _senabled(page, row["id"], "accrue") and not _senabled(page, row["id"], "pay")
    finally:
        a.on_close()


def test_accountant_uses_the_gate_and_the_salaries(populated):
    a = appmod.App(populated); a.update()
    try:
        login(a, "acc", "password-123"); a.show("labor"); a.update()
        page = a.screen.page_cache["labor"]
        assert {"Today", "Month & Salaries", "Records", "Employees"} <= set(page.tabs._tab_dict)
        eid = _emp_id(a)
        assert _enabled(page, eid, "in")
        page.press(eid, "in"); a.update()
        assert _state(page, eid)["state"] == "in"
    finally:
        a.on_close()


def test_month_sheet_and_pay_salary_with_the_row_button(app, tmp_path, monkeypatch):
    page = _labor(app)
    page.period = "2026-07"; page._reload_months(); app.update()
    r = _salary_row(page)
    assert r["status"] == "unpaid" and _senabled(page, r["id"], "pay") and not _senabled(page, r["id"], "accrue") and not _senabled(page, r["id"], "edit") is False
    page.pay(r); app.update()
    submit(app, a="10000", on="2026-08-01", ref="x")
    r = _salary_row(page)
    assert r["status"] == "partial" and r["due_paisa"] == 2000000 and q.payslips(app.ctx.conn, "2026-07")[0]["due_paisa"] == 2000000
    page._load_month(); app.update()
    assert not _senabled(page, r["id"], "edit")                       # a part-paid payslip can no longer be edited
    out = tmp_path / "slip.pdf"
    monkeypatch.setattr("ui.pages.labor_pages.filedialog.asksaveasfilename", lambda **k: str(out))
    monkeypatch.setattr("ui.pages.labor_pages.show_info", lambda *a, **k: None)
    page.slip(r); app.update()
    assert out.read_bytes()[:4] == b"%PDF"


def test_accrue_by_row_button_then_edit_it(app):
    page = _labor(app)
    page.period = "2026-08"; page._reload_months(); app.update()
    r = _salary_row(page)
    assert r["status"] == "not accrued" and _senabled(page, r["id"], "accrue") and not _senabled(page, r["id"], "pay")
    page.accrue(r); app.update()
    submit(app, al="0", de="0", dn="", ar="0")
    r = _salary_row(page)
    assert r["status"] == "unpaid" and r["net_paisa"] == 3000000
    page._load_month(); app.update()
    assert _senabled(page, r["id"], "edit") and not _senabled(page, r["id"], "accrue")
    page.revise(r); app.update()
    submit(app, ba="30000", al="0", de="1000", dn="2 days absent", ar="0", why="attendance checked")
    assert _salary_row(page)["net_paisa"] == 2900000
    assert ac.balance(app.ctx.conn, "2300") == 3000000 + 2900000           # July payslip + revised August payslip


def test_accrue_all_and_the_auto_accrue_switch_are_back(app, monkeypatch):
    page = _labor(app)
    texts = [t for t, _b, _e in page.buttons]
    assert "Accrue all salaries" in texts and any(t.startswith("Auto-accrue") for t in texts) and "Pay salary" not in texts
    app.ctx.conn.execute("UPDATE employees SET created_at='2026-01-02T00:00:00Z'")        # long-standing staff, so the month is not a 'by hand' case
    page.period = "2026-08"; page._reload_months(); app.update()
    monkeypatch.setattr("ui.pages.labor_pages.confirm", lambda *a, **k: True)
    monkeypatch.setattr("ui.pages.labor_pages.show_info", lambda *a, **k: None)
    page.accrue_all(); app.update()
    assert _salary_row(page)["status"] == "unpaid"
    page.toggle_auto(); app.update()
    assert page.auto_btn.cget("text") == "Auto-accrue: OFF"


def test_month_estimate_uses_absent_and_leave_marks(app):
    page = _labor(app); eid = _emp_id(app)
    this = date.today().strftime("%Y-%m")
    for d in (1, 2):
        pr.record_attendance(app.ctx.conn, app.ctx.user, eid, f"{this}-{d:02d}", "absent")
    page.period = this; page._reload_months(); app.update()
    r = _salary_row(page)
    dim = calendar.monthrange(int(this[:4]), int(this[5:]))[1]
    assert r["absent"] == 2 and r["est_deduction_paisa"] == (2 * 3000000 * 2 + dim) // (2 * dim)       # 2 days of the month, half-up


def test_add_a_past_day_with_calendar_and_time_lists(app):
    page = _labor(app); eid = _emp_id(app)
    page.add_day(); app.update()
    submit(app, e="E1 - Bilal", d="2026-07-06", st="Present", **{"in": "09:00", "out": "17:30"}, nx="The same day", sh="Day", br="30", nt="")
    r = app.ctx.conn.execute("SELECT * FROM attendance WHERE employee_id=? AND work_date='2026-07-06'", (eid,)).fetchone()
    assert r["clock_in"] == "2026-07-06T09:00" and r["clock_out"] == "2026-07-06T17:30" and r["worked_minutes"] == 8 * 60       # 8h30 - 30 min break
    page.add_day(); app.update()
    d = submit(app, e="E1 - Bilal", d="2026-07-07", st="Present", **{"in": "09:00", "out": ""}, nx="The same day", sh="Day", br="0", nt="")
    assert "both" in d.err.cget("text")                              # one time without the other is refused
    d.destroy()


def test_correct_an_auto_out_record_removes_the_warning(app):
    page = _labor(app); eid = _emp_id(app)
    started = datetime.combine((datetime.now() - timedelta(days=1)).date(), datetime.min.time()).replace(hour=8)      # yesterday 08:00 -> auto OUT at 18:00, whatever time the test runs
    pr.clock_in(app.ctx.conn, app.ctx.user, eid, started)
    pr.auto_close_open(app.ctx.conn)
    page.period = started.strftime("%Y-%m"); page._reload_months(); app.update()
    idx = next(i for i, x in enumerate(page.att.rows) if x["auto_out"])
    page.att.tree.selection_set(str(idx)); app.update()
    page.correct(); app.update()
    base = page.att.rows[idx]["clock_in"][:11]
    submit(app, st="Present", **{"in": page.att.rows[idx]["clock_in"][11:16], "out": "16:00"}, nx="The same day", br="0", why="gate register says 16:00")
    row = app.ctx.conn.execute("SELECT auto_out, clock_out FROM attendance WHERE employee_id=?", (eid,)).fetchone()
    assert row["auto_out"] == 0 and row["clock_out"] == base + "16:00"


def test_reports_run_and_export_all(app, tmp_path, monkeypatch):
    login(app); app.show("reports"); app.update()
    page = app.screen.page_cache["reports"]
    monkeypatch.setattr("ui.pages.admin_pages.show_info", lambda *a, **k: None)
    from reports.financial_reports import REPORTS
    for name in REPORTS:
        page.sel.set(name); page.a.delete(0, "end"); page.a.insert(0, "2026-07-01"); page.b.delete(0, "end"); page.b.insert(0, "2026-12-31")
        page.run(); app.update()
        assert page.result and page.result[0] == name
        for ext in (".xlsx", ".csv", ".pdf"):
            f = tmp_path / f"{name.replace(' ', '_')}{ext}"
            monkeypatch.setattr("ui.pages.admin_pages.filedialog.asksaveasfilename", lambda f=f, **k: str(f))
            page.export(ext); app.update()
            assert f.exists() and f.stat().st_size > 10


def test_backup_now_runs_in_background_and_records(app, monkeypatch):
    login(app); app.show("backup"); app.update()
    page = app.screen.page_cache["backup"]
    monkeypatch.setattr("ui.pages.admin_pages.show_info", lambda *a, **k: None)
    page.backup_now()
    for _ in range(100):
        app.update()
        if q.backups(app.ctx.conn):
            break
        app.after(50); import time; time.sleep(0.05)
    rows = q.backups(app.ctx.conn)
    assert rows and rows[0]["status"] == "ok" and os.path.exists(rows[0]["path"])


def test_users_page_add_user(app):
    login(app); app.show("users"); app.update()
    page = app.screen.page_cache["users"]
    page.add(); app.update()
    submit(app, u="newacc", n="New", r="accountant", p="longenough1", sa="Lahore")
    assert "newacc" in [u["username"] for u in q.users(app.ctx.conn)]


def test_users_page_requires_security_answer(app):
    login(app); app.show("users"); app.update()
    page = app.screen.page_cache["users"]
    page.add(); app.update()
    submit(app, u="noanswer", n="X", r="accountant", p="longenough1", sa="")
    assert "noanswer" not in [u["username"] for u in q.users(app.ctx.conn)]
    for d in _find(app, __import__("ui.widgets", fromlist=["FormDialog"]).FormDialog):
        d.destroy()


def test_forgot_password_screen_resets_and_signs_in(app):
    """Boss has no question yet in the fixture: set one, sign out, use 'Forgot password?', sign in with the new password."""
    from services import auth_service as auth
    boss = app.ctx.conn.execute("SELECT * FROM users WHERE username='boss'").fetchone()
    auth.set_security_question(app.ctx.conn, boss, boss["id"], "What was the name of your first school?", "  City   Model ", "correct-horse")
    app._swap(appmod.ForgotPasswordScreen(app, app, "boss")); app.update()
    f = app.screen
    f.show_question(); app.update()
    assert "first school" in f.q_label.cget("text")
    f.a.insert(0, "wrong"); f.p1.insert(0, "brand-new-pass1"); f.p2.insert(0, "brand-new-pass1"); f.reset(); app.update()
    assert "not correct" in f.err.cget("text") and isinstance(app.screen, appmod.ForgotPasswordScreen)
    f.a.insert(0, "city model"); f.p1.delete(0, "end"); f.p2.delete(0, "end")
    f.p1.insert(0, "brand-new-pass1"); f.p2.insert(0, "different-pass1"); f.reset(); app.update()
    assert "do not match" in f.err.cget("text")
    f.p2.delete(0, "end"); f.p2.insert(0, "brand-new-pass1")
    import ui.app as ua
    shown = []
    ua.show_info = lambda *a, **k: shown.append(a)           # the confirmation pop-up would block the test
    f.reset(); app.update()
    assert shown and isinstance(app.screen, appmod.LoginScreen)
    user = auth.authenticate(app.ctx.conn, "boss", "brand-new-pass1")
    assert user["username"] == "boss"


def test_forgot_password_unknown_user_gets_generic_message(app):
    app._swap(appmod.ForgotPasswordScreen(app, app, "nobody")); app.update()
    f = app.screen
    f.show_question(); app.update()
    assert "not set up" in f.err.cget("text") and not f.step2.winfo_ismapped()


def test_login_screen_has_forgot_link(app):
    texts = []

    def walk(w):
        for c in w.winfo_children():
            try:
                texts.append(c.cget("text"))
            except Exception:  # noqa: BLE001
                pass
            walk(c)
    walk(app.screen)
    assert "Forgot password?" in texts


def test_settings_save(app):
    login(app); app.show("settings"); app.update()
    page = app.screen.page_cache["settings"]
    page.e["factory_name"].delete(0, "end"); page.e["factory_name"].insert(0, "New Name")
    page.save(); app.update()
    assert page.msg.cget("text") == "Saved."


def test_first_run_creates_empty_db_and_owner(tmp_path):
    paths = get_paths(tmp_path / "fresh")
    a = appmod.App(paths); a.update()
    try:
        assert isinstance(a.screen, appmod.FirstRunScreen) and not paths.db.exists()
        s = a.screen
        s.fn.insert(0, "Fresh Mills"); s.un.insert(0, "owner1"); s.full.insert(0, "O"); s.pw.insert(0, "longenough1"); s.pw2.insert(0, "longenough1")
        s.sq_answer.insert(0, "Faisalabad")
        s.go(); a.update()
        assert isinstance(a.screen, appmod.LoginScreen)
        import sqlite3
        c = sqlite3.connect(paths.db)
        assert c.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == 0 and c.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        sq, sh = c.execute("SELECT security_question, security_answer_hash FROM users").fetchone()
        assert sq and sh and "faisalabad" not in sh.lower()               # question stored, answer only as a hash
        c.close()
    finally:
        a.on_close()


def test_first_run_needs_security_answer_and_leaves_no_database(tmp_path):
    paths = get_paths(tmp_path / "fresh3")
    a = appmod.App(paths); a.update()
    try:
        s = a.screen
        s.fn.insert(0, "X"); s.un.insert(0, "o"); s.pw.insert(0, "longenough1"); s.pw2.insert(0, "longenough1")
        s.go(); a.update()
        assert not paths.db.exists() and "answer" in s.err.cget("text").lower()
        s.sq_combo.set("Write my own question"); s._sq_changed("Write my own question")
        s.sq_custom.insert(0, "My pet's name?"); s.sq_answer.insert(0, "Moti"); s.go(); a.update()
        assert isinstance(a.screen, appmod.LoginScreen)                  # a custom question works too
    finally:
        a.on_close()


def test_first_run_password_mismatch_leaves_no_database(tmp_path):
    paths = get_paths(tmp_path / "fresh2")
    a = appmod.App(paths); a.update()
    try:
        s = a.screen
        s.fn.insert(0, "X"); s.un.insert(0, "o"); s.pw.insert(0, "longenough1"); s.pw2.insert(0, "different11")
        s.go(); a.update()
        assert not paths.db.exists() and "match" in s.err.cget("text")
    finally:
        a.on_close()


def test_corrupt_database_shows_recovery_not_overwrite(populated):
    populated.db.write_bytes(b"garbage" * 500)
    a = appmod.App(populated); a.update()
    try:
        assert isinstance(a.screen, appmod.RecoveryScreen)
        assert populated.db.read_bytes().startswith(b"garbage")
    finally:
        a.on_close()


def _select(page, table, key, value, app):
    idx = next(i for i, r in enumerate(table.rows) if r[key] == value)
    table.tree.selection_set(str(idx)); app.update()


def test_production_edit_return_and_delete_entry(app, monkeypatch):
    monkeypatch.setattr("ui.base.confirm", lambda *a, **k: True)
    login(app); app.show("jobs"); app.update()
    page = app.screen.page_cache["jobs"]
    conn = app.ctx.conn
    pid = next(m["id"] for m in q.materials(conn) if m["name"] == "Paper")
    page.new_job(); app.update()
    cust = next(c["id"] for c in q.customers(conn))
    submit(app, no="P-0003", od="2026-08-03", cus=cust, prod="Bedsheet", pcs="20", src="factory", notes="")
    _select(page, page.t, "job_no", "P-0003", app)
    page.edit(); app.update()
    submit(app, prod="Quilt cover", pcs="25")                                             # Update
    assert conn.execute("SELECT product_type, quantity_pieces FROM jobs WHERE job_no='P-0003'").fetchone()[:] == ("Quilt cover", 25)
    _select(page, page.t, "job_no", "P-0003", app)
    page.consume(); app.update()
    use_stock(app, [("Paper", "1")], on="2026-08-03")
    _select(page, page.t, "job_no", "P-0003", app)
    before = inv.stock_summary(conn, pid)["factory_qty"]
    page.delete(); app.update()                                                            # stock still out -> refused, nothing deleted
    assert conn.execute("SELECT status FROM jobs WHERE job_no='P-0003'").fetchone()[0] == "in_progress"
    _select(page, page.t, "job_no", "P-0003", app)
    page.give_back(); app.update()
    submit(app, reason="wrong entry", on="2026-08-03")                                    # Return stock
    assert inv.stock_summary(conn, pid)["factory_qty"] == before + 1
    _select(page, page.t, "job_no", "P-0003", app)
    page.delete(); app.update()                                                            # Delete
    assert conn.execute("SELECT status FROM jobs WHERE job_no='P-0003'").fetchone()[0] == "cancelled"
    assert "P-0003" not in [r["job_no"] for r in page.t.rows]


def test_same_customer_many_entries_and_number_never_clashes(app, monkeypatch):
    monkeypatch.setattr("ui.base.confirm", lambda *a, **k: True)
    login(app); app.show("jobs"); app.update()
    page = app.screen.page_cache["jobs"]
    conn = app.ctx.conn
    cust = next(c["id"] for c in q.customers(conn))
    for _ in range(3):                                                                     # three entries, same customer
        no = page._next_no()
        page.new_job(); app.update()
        submit(app, no=no, od="2026-08-10", cus=cust, prod="Bedsheet", pcs="5", src="factory", notes="")
    nos = [r[0] for r in conn.execute("SELECT job_no FROM jobs WHERE customer_id=? ORDER BY id", (cust,))]
    assert len(set(nos)) == len(nos) >= 3
    _select(page, page.t, "job_no", nos[-2], app)
    page.delete(); app.update()                                                            # remove one in the middle
    assert page._next_no() not in [r[0] for r in conn.execute("SELECT job_no FROM jobs")]


def test_inventory_edit_delete_material_and_undo_receipt(app, monkeypatch):
    monkeypatch.setattr("ui.base.confirm", lambda *a, **k: True)
    monkeypatch.setattr("ui.pages.inventory_page.show_info", lambda *a, **k: None)
    login(app); app.show("inventory"); app.update()
    page = app.screen.page_cache["inventory"]
    conn = app.ctx.conn
    page.new_material(); app.update()
    submit(app, name="Binder", cat="consumable", unit="kg", reorder="5")                  # Create
    _select(page, page.t, "name", "Binder", app)
    page.edit_material(); app.update()
    submit(app, name="Binder X", reorder="8")                                              # Update
    m = conn.execute("SELECT name, reorder_level_milli FROM materials WHERE name='Binder X'").fetchone()
    assert m[1] == 8000
    page.refresh(); _select(page, page.t, "name", "Binder X", app)
    page.receive(); app.update()
    mid = conn.execute("SELECT id FROM materials WHERE name='Binder X'").fetchone()[0]
    submit(app, mat=mid, qty="10", price="100", own="factory", sup=2, pay="unpaid", on="2026-08-05")
    page.refresh(); app.update()
    _select(page, page.t, "name", "Binder X", app)
    page.delete_material(); app.update()                                                   # stock on hand -> refused, still listed
    assert "Binder X" in [r["name"] for r in page.t.rows]
    _select(page, page.rec, "material", "Binder X", app)
    page.undo_receipt(); app.update()
    submit(app, on="2026-08-05", reason="typed twice")                                    # undo receipt
    assert inv.stock_summary(conn, mid)["factory_qty"] == 0
    assert next(r for r in page.rec.rows if r["material"] == "Binder X")["state"] == "Undone"
    _select(page, page.t, "name", "Binder X", app)
    page.delete_material(); app.update()                                                   # Delete (archived: it has history)
    assert "Binder X" not in [r["name"] for r in page.t.rows]


# ---------------------------------------------------------------- ink in litres, paper in rolls AND meters
def _new_entry(app, no="P-0009"):
    login(app); app.show("jobs"); app.update()
    page = app.screen.page_cache["jobs"]
    page.new_job(); app.update()
    cust = next(c["id"] for c in q.customers(app.ctx.conn))
    submit(app, no=no, od="2026-08-02", cus=cust, prod="Bedsheet", pcs="10", src="factory", notes="")
    page.refresh(); _select(page, page.t, "job_no", no, app)
    return page


def test_use_stock_paper_in_meters_costs_the_matching_part_of_a_roll(app):
    page = _new_entry(app)
    page.consume(); app.update()
    d = use_stock(app, [("Paper", "150", "Meters")])                         # 150 m of a 300 m roll = 0.5 roll = Rs 2,500 (a roll costs Rs 5,000)
    page.refresh(); app.update()
    assert next(r for r in page.t.rows if r["job_no"] == "P-0009")["cost_paisa"] == 250000
    pid = next(m["id"] for m in q.materials(app.ctx.conn) if m["name"] == "Paper")
    s = inv.stock_summary(app.ctx.conn, pid)
    assert s["factory_qty"] == 5.5 and s["factory_meters"] == 1650            # 6 - 0.5 rolls; 5.5 x 300 m


def test_use_stock_one_roll_takes_one_roll(app):
    page = _new_entry(app)
    page.consume(); app.update()
    use_stock(app, [("Paper", "1", "Rolls")])
    pid = next(m["id"] for m in q.materials(app.ctx.conn) if m["name"] == "Paper")
    assert inv.stock_summary(app.ctx.conn, pid)["factory_qty"] == 5


def test_use_stock_shows_the_live_conversion_and_the_switch_only_for_paper(app):
    page = _new_entry(app)
    ink = inv.add_material(app.ctx.conn, app.ctx.user, "Ink", "ink", "L")
    inv.receive_stock(app.ctx.conn, app.ctx.user, material_id=ink, qty="4", unit_price="500", supplier_id=2)
    page.consume(); app.update()
    from ui.pages.production_pages import StockUseDialog
    d = _find(app, StockUseDialog)[-1]
    r = d.rows[0]
    d._material_changed(r)
    labels = list(d.mats)
    paper_label = next(l for l, m in d.mats.items() if m["name"] == "Paper")
    assert "rolls (1,800 m)" in paper_label                                  # the stock list already shows rolls and meters
    r["mat"].set(paper_label); d._material_changed(r); app.update()
    assert r["unit_seg"].winfo_ismapped() and not r["unit_lbl"].winfo_ismapped()
    r["unit_seg"].set("Meters"); r["qty"].insert(0, "150"); d._calc(); app.update()
    assert "0.5 rolls (150 m)" in r["note"].cget("text") and r["cost"].cget("text") == "Rs 2,500.00"
    r["qty"].delete(0, "end"); r["qty"].insert(0, "99999"); d._calc()
    from ui import theme as T
    assert "Only" in r["note"].cget("text") and r["note"].cget("text_color") == T.BAD
    r["mat"].set(next(l for l in labels if l.startswith("Ink"))); d._material_changed(r); app.update()
    assert not r["unit_seg"].winfo_ismapped() and r["unit_lbl"].cget("text") == "litres (L)"
    d.destroy()


def test_inventory_lists_paper_in_rolls_and_meters(app):
    login(app); app.show("inventory"); app.update()
    page = app.screen.page_cache["inventory"]
    page.refresh(); app.update()
    assert next(r for r in page.t.rows if r["name"] == "Paper")["qty"] == "6 rolls (1,800 m)"


def test_receive_paper_asks_meters_per_roll_and_shows_the_total(app):
    login(app); app.show("inventory"); app.update()
    page = app.screen.page_cache["inventory"]
    page.receive(); app.update()
    d = dialogs(app)[-1]
    pid = next(m["id"] for m in q.materials(app.ctx.conn) if m["name"] == "Paper")
    pick(d, pid)
    assert d.widgets["rl"].get() == "300" and d.widgets["rl"].cget("state") != "disabled"
    d.set_value("qty", "10"); d._changed()
    assert "10 rolls x 300 m = 3,000 m" in d.hints["rl"].cget("text")
    d.set_value("rl", "250"); d.set_value("price", "5000"); fill(d, pay="paid"); d._changed()
    assert "2,500 m" in d.hints["rl"].cget("text") and d.widgets["cost"].get() == "Rs 50,000.00"
    d.submit(); app.update()
    s = inv.stock_summary(app.ctx.conn, pid)
    assert s["roll_length_m"] == 250 and s["factory_qty"] == 16 and s["factory_meters"] == 4000


def test_meters_per_roll_is_only_for_paper_in_the_material_form(app):
    login(app); app.show("inventory"); app.update()
    page = app.screen.page_cache["inventory"]
    page.new_material(); app.update()
    d = dialogs(app)[-1]
    assert d.widgets["rl"].cget("state") == "disabled"                         # fabric is the first kind
    d.widgets["cat"].set(next(l for l, v in d.fields[1].choices if v == "paper")); d._changed()
    assert d.widgets["rl"].cget("state") != "disabled" and d.widgets["rl"].get() == "300"
    d.widgets["cat"].set(next(l for l, v in d.fields[1].choices if v == "ink")); d._changed()
    assert d.widgets["rl"].cget("state") == "disabled" and d.widgets["rl"].get() == ""
    assert d.widgets["unit"].get() == "Litre (L)"
    d.destroy()


def test_wastage_form_counts_paper_in_meters(app):
    login(app); app.show("inventory"); app.update()
    page = app.screen.page_cache["inventory"]
    page.issue(); app.update()
    d = dialogs(app)[-1]
    pid = next(m["id"] for m in q.materials(app.ctx.conn) if m["name"] == "Paper")
    pick(d, pid)
    assert d.widgets["unit"].cget("state") != "disabled"
    fill(d, type="wastage", unit="m", qty="60", reason="torn"); d._changed()
    assert "0.2 rolls (60 m)" in d.hints["qty"].cget("text")
    d.submit(); app.update()
    assert inv.stock_summary(app.ctx.conn, pid)["factory_qty"] == __import__("decimal").Decimal("5.8")


# ---------------------------------------------------------------- partners: automatic / manual profit sharing, profit distribution page
def _partners(app):
    login(app); app.show("partners"); app.update()
    return app.screen.page_cache["partners"]


def test_partners_page_switches_between_manual_and_automatic(app):
    page = _partners(app)
    conn = app.ctx.conn
    assert "Manual" in page.mode_lbl.cget("text")                                       # the fixture typed 50 / 50
    assert {r["name"]: r["share_bps"] for r in page.t.rows} == {"A": 5000, "B": 5000}
    page.use_auto(); app.update()
    assert "Automatic" in page.mode_lbl.cget("text")
    assert {r["name"]: r["share_bps"] for r in page.t.rows} == {"A": 7500, "B": 2500}   # capital 300,000 : 100,000
    ps.record_drawing(conn, app.ctx.user, next(r["id"] for r in page.t.rows if r["name"] == "B"), "50000", "2026-08-01")
    page.refresh(); app.update()
    assert {r["name"]: r["share_bps"] for r in page.t.rows} == {"A": 8571, "B": 1429}   # 300,000 : 50,000 - follows the withdrawal by itself


def test_manual_dialog_shows_the_live_total_and_switches_to_manual(app):
    page = _partners(app)
    page.use_auto(); app.update()
    page.shares(); app.update()
    d = dialogs(app)[-1]
    ids = [k for k in d.widgets if k.startswith("p")]
    fill(d, **{ids[0]: "60", ids[1]: "30"}); d._changed()
    assert "90" in d.widgets["total"].get() and "exactly 100%" in d.hints["total"].cget("text")
    fill(d, **{ids[1]: "40"}); d._changed()
    assert "Total is 100%" in d.hints["total"].cget("text")
    d.submit(); app.update()
    assert "Manual" in page.mode_lbl.cget("text") and sorted(r["share_bps"] for r in page.t.rows) == [4000, 6000]


def _profit_page(app, upto="2026-07-31"):
    login(app); app.show("profit"); app.update()
    page = app.screen.page_cache["profit"]
    page.s.set_date("2026-07-01"); page.e.set_date(upto)
    page.refresh(); app.update()
    return page


def test_profit_approval_shows_each_partner_with_percentage_amount_and_pay_buttons(app):
    page = _profit_page(app)
    conn = app.ctx.conn
    avail = ps.distributable(conn, "2026-07-01", "2026-07-31")
    assert avail > 100000
    page.approve(); app.update()
    d = dialogs(app)[-1]
    fill(d, amount="1000", on="2026-07-31"); d._changed()
    assert "A:  50.00%  =  Rs 500.00" in d.hints["amount"].cget("text") and "B:  50.00%  =  Rs 500.00" in d.hints["amount"].cget("text")
    d.submit(); app.update()
    assert page._dist is not None and page._dist["amount_paisa"] == 100000              # the new distribution is selected automatically
    assert [(r["name"], r["share_bps"], r["amount_paisa"]) for r in page.l.rows] == [("A", 5000, 50000), ("B", 5000, 50000)]
    assert "Rs 1,000.00" in page.pay_all_btn.cget("text") and page.pay_all_btn.cget("state") == "normal"
    buttons = [w for _f, cells in page.l.items for w in cells[5].winfo_children()]
    assert len(buttons) == 2 and all("Rs 500.00" in b.cget("text") for b in buttons)


def test_pay_one_partner_then_pay_the_rest(app):
    page = _profit_page(app)
    conn = app.ctx.conn
    page.approve(); app.update()
    d = dialogs(app)[-1]; fill(d, amount="1000", on="2026-07-31"); d.submit(); app.update()
    page.pay_one(page.l.rows[0]); app.update()
    d = dialogs(app)[-1]; fill(d, amount="200", on="2026-08-01"); d.submit(); app.update()
    assert page.l.rows[0]["paid_paisa"] == 20000 and "Rs 800.00" in page.pay_all_btn.cget("text")      # 1,000 - 200 still to pay
    page.pay_all(); app.update()
    assert all(r["amount_paisa"] == r["paid_paisa"] for r in page.l.rows) and page.pay_all_btn.cget("state") == "disabled"
    assert ac.balance(conn, "2400") == 0
    app.update()
    for frame, _cells in page.l.items:                                              # a fully paid row (no Pay button) must stay slim
        assert frame.winfo_height() < 90


def test_profit_page_shows_both_tables_on_a_small_window(app):
    page = _profit_page(app)
    app.geometry("1280x700+0+0"); app.update(); app.update_idletasks()
    page.approve(); app.update()
    d = dialogs(app)[-1]; fill(d, amount="500", on="2026-07-31"); d.submit(); app.update()
    top = app.winfo_rooty() + app.winfo_height()
    for w in (page.t, page.l):
        assert w.winfo_viewable() and w.winfo_height() >= 60
        assert w.winfo_rooty() + w.winfo_height() <= top + 2                       # nothing is cut off at the bottom of the window
    assert page.pay_all_btn.winfo_viewable()


def test_backup_now_with_unreachable_extra_folder_keeps_local_copy(app, monkeypatch, tmp_path):
    from services import settings_service as st
    import time
    login(app); app.show("backup"); app.update()
    page = app.screen.page_cache["backup"]
    blocker = tmp_path / "drive"; blocker.write_text("x")                      # its sub-folder can never be created
    st.set_values(app.ctx.conn, app.ctx.user, {"backup_dir": str(blocker / "usb")})
    shown = []
    monkeypatch.setattr("ui.pages.admin_pages.show_info", lambda *a, **k: shown.append(a))
    monkeypatch.setattr("ui.pages.admin_pages.show_error", lambda *a, **k: shown.append(("ERR",) + a))
    page.backup_now()
    for _ in range(100):
        app.update()
        if shown:
            break
        time.sleep(0.05)
    assert shown and shown[0][0] != "ERR" and "extra folder" in shown[0][1]
    rows = [r for r in q.backups(app.ctx.conn) if r["kind"] == "manual"]
    assert sorted(r["status"] for r in rows) == ["failed", "ok"]
