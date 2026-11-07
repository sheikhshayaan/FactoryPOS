"""UI checks for: smooth Attendance page, Expenses + Assets, supplier advance refund, stock given by a customer, fabric from either owner in Use stock."""
from datetime import date, timedelta

import pytest

pytest.importorskip("tkinter")
from tests.test_ui_smoke import (app, populated, no_blocking_confirm, login, dialogs, fill, submit, _find, _labor, _select, _emp_id, use_stock)  # noqa: E402,F401
from services import accounting_service as ac, auth_service as auth, inventory_service as inv, payroll_service as pr, queries as q  # noqa: E402
from services.units import to_paisa as P  # noqa: E402


# ---------------------------------------------------------------- attendance: smooth + never stale
def test_refresh_redraws_only_rows_that_changed(app):
    page = _labor(app)
    pr.add_employee(app.ctx.conn, app.ctx.user, emp_code="E2", name="Chand", monthly_salary="20000", effective_from="2026-01-01")
    page.refresh(); app.update()
    frames = [f for f, _c in page.board.items]
    page.refresh(); app.update()
    assert [f for f, _c in page.board.items] == frames                                          # same widgets: nothing was rebuilt, nothing flickered
    bilal, chand = _emp_id(app), _emp_id(app, "Chand")
    other = page.board.items[page._index[chand]][0]
    keep = page.btns[(chand, "in")]
    page.press(bilal, "in"); app.update()
    assert page.board.items[page._index[chand]][0] is other and page.btns[(chand, "in")] is keep   # the other employee's row was not touched
    assert page.btns[(bilal, "out")].cget("state") == "normal"                                   # while the pressed one changed at once


def test_a_search_does_not_rebuild_the_rows_that_stay(app):
    page = _labor(app)
    pr.add_employee(app.ctx.conn, app.ctx.user, emp_code="E2", name="Chand", monthly_salary="20000", effective_from="2026-01-01"); page.refresh(); app.update()
    chand = _emp_id(app, "Chand")
    f = page.board.items[page._index[chand]][0]
    page.search.insert(0, "Chand"); page._draw_board(); app.update()
    assert len(page.board.items) == 1 and page.board.items[0][0] is f
    page.search.delete(0, "end"); page._draw_board(); app.update()
    assert len(page.board.items) == 2 and f in [i[0] for i in page.board.items]                  # and the hidden row comes back


def test_pressing_in_shows_a_confirmation_and_the_other_tabs_are_current_when_opened(app):
    page = _labor(app); eid = _emp_id(app)
    page.refresh(); app.update()
    before = next(r for r in page.sheet.rows if r["id"] == eid)["present"]
    page.press(eid, "in"); app.update()
    assert "Bilal" in page.flash.cget("text") and "IN" in page.flash.cget("text")                # the operator sees the button worked
    page.tabs.set("Month & Salaries"); page._tab_changed(); app.update()                         # opening the tab loads the books as they are now
    assert next(r for r in page.sheet.rows if r["id"] == eid)["present"] == before + 1
    page.tabs.set("Records"); page._tab_changed(); app.update()
    assert any(r["emp_code"] == "E1" and r["work_date"] == pr._now(None).date().isoformat() for r in page.att.rows)


def test_a_change_made_elsewhere_appears_without_leaving_the_page(app):
    page = _labor(app); eid = _emp_id(app)
    page.refresh(); app.update()
    pr.clock_in(app.ctx.conn, app.ctx.user, eid)                                                 # another window / the Records tab
    page._sync_board(); app.update()
    assert next(r for r in page._board if r["id"] == eid)["state"] == "in" and page.btns[(eid, "out")].cget("state") == "normal"


def test_records_selection_survives_a_refresh(app):
    page = _labor(app); eid = _emp_id(app)
    page.press(eid, "in"); page.press(eid, "out"); page.refresh(); app.update()
    page.att.tree.selection_set("0"); app.update()
    chosen = page.att.selected()
    page.refresh(); app.update()
    assert page.att.selected() == chosen                                                          # the row you picked is still picked


# ---------------------------------------------------------------- expenses + assets
def _expenses(app):
    login(app); app.show("expenses"); app.update()
    return app.screen.page_cache["expenses"]


def test_adding_an_asset_changes_no_figure_and_shows_in_the_list(app):
    page = _expenses(app); conn = app.ctx.conn
    assert "Capital purchase" not in [t for t, _b, _e in page.buttons]                           # the old book-keeping button is gone
    cash, tb = ac.balance(conn, "1000"), ac.trial_balance(conn)
    page.add_asset(); app.update()
    submit(app, n="Rotary printing machine", amt="1200000", on="2026-08-01", nt="")
    app.update()
    assert ac.balance(conn, "1000") == cash and ac.trial_balance(conn) == tb                      # no cash used, nothing booked
    assert [(r["name"], r["status"]) for r in page.assets.rows] == [("Rotary printing machine", "Asset")]
    assert "12,00,000" in page.a_total.cget("text") or "1,200,000" in page.a_total.cget("text")
    _select(page, page.assets, "name", "Rotary printing machine", app)
    page.void_asset(); app.update()
    submit(app, why="entered twice")
    assert page.assets.rows[0]["status"] == "Voided" and page.assets.row_tags(page.assets.rows[0]) == "bad"
    assert "Rs 0.00" in page.a_total.cget("text")


def test_a_reversed_expense_shows_red_and_can_be_hidden(app):
    page = _expenses(app); conn = app.ctx.conn
    page._set_period('all')                                                                       # the entries below are dated in August
    acct = q.expense_accounts(conn)[0]["code"]
    e1 = ac.record_expense(conn, app.ctx.user, account_code=acct, amount="1000", on="2026-08-03", description="Tea")
    ac.record_expense(conn, app.ctx.user, account_code=acct, amount="2000", on="2026-08-03", description="Sugar")
    ac.reverse_entry(conn, app.ctx.user, e1, "typed twice")
    page.refresh(); app.update()
    by = {r["description"]: r for r in page.t.rows}
    assert by["Tea"]["status"] == "REVERSED" and page.t.row_tags(by["Tea"]) == "bad" and by["Sugar"]["status"] == "" and page.t.row_tags(by["Sugar"]) is None
    page.hide_rev.select(); page.refresh(); app.update()
    assert [r["description"] for r in page.t.rows] == ["Sugar"]
    page.hide_rev.deselect(); page.refresh(); app.update()
    _select(page, page.t, "description", "Tea", app)
    errors = []
    import ui.pages.finance_pages as fp
    orig, fp.show_error = fp.show_error, lambda _p, msg: errors.append(msg)
    try:
        page.reverse(); app.update()
    finally:
        fp.show_error = orig
    assert errors and "already reversed" in errors[0] and not dialogs(app)                       # no second reversal form for it


# ---------------------------------------------------------------- suppliers: get the advance back
def test_supplier_with_a_minus_balance_can_pay_the_advance_back(app):
    login(app); conn = app.ctx.conn
    sid = inv.add_supplier(conn, app.ctx.user, "Dye House")                                         # a supplier with a clean slate
    ac.post(conn, entry_date="2026-08-01", description="Advance", user=app.ctx.user, source_type="test",
            lines=[{"account": "2000", "debit": P("5000"), "supplier_id": sid}, {"account": "1000", "credit": P("5000")}])
    app.show("suppliers"); app.update()
    page = app.screen.page_cache["suppliers"]
    assert "Receive advance back" in [t for t, _b, _e in page.buttons]
    row = next(r for r in page.t.rows if r["name"] == "Dye House")
    assert row["owed"] == -P("5000") and page.t.row_tags(row) == "good"
    _select(page, page.t, "name", "Dye House", app)
    cash = ac.balance(conn, "1000")
    page.refund(); app.update()
    assert dialogs(app)[-1].widgets["amount"].get() == "5000.00"                                     # offers the whole advance
    submit(app, amount="2000", on="2026-08-05", ref="cheque 7")
    assert ac.balance(conn, "1000") == cash + P("2000")
    assert next(r for r in page.t.rows if r["name"] == "Dye House")["owed"] == -P("3000")


# ---------------------------------------------------------------- customers: stock given
def test_customers_page_shows_the_stock_each_customer_gave(app):
    login(app); app.show("customers"); app.update()
    page = app.screen.page_cache["customers"]
    assert "Stock given" in [t for t, _b, _e in page.buttons]
    row = next(r for r in page.t.rows if r["name"] == "Ali Traders")
    assert row["stock"] == "Kapra 200 m"
    other = inv.add_customer(app.ctx.conn, app.ctx.user, "Nobody Yet")
    page.refresh(); app.update()
    assert next(r for r in page.t.rows if r["id"] == other)["stock"] == "-"
    _select(page, page.t, "name", "Ali Traders", app)
    page.stock(); app.update()
    from ui.pages.people_pages import CustomerStockDialog
    dlg = _find(app, CustomerStockDialog)[-1]
    assert dlg.winfo_exists()
    dlg.destroy()


# ---------------------------------------------------------------- use stock: fabric from either owner
def test_use_stock_lists_both_the_customers_cloth_and_the_factorys(app):
    from ui.pages.production_pages import StockUseDialog
    login(app); conn, o = app.ctx.conn, app.ctx.user
    fab = next(m["id"] for m in q.materials(conn) if m["name"] == "Kapra")
    app.show("jobs"); app.update()
    page = app.screen.page_cache["jobs"]
    _select(page, page.t, "job_no", "J1", app)
    page.consume(); app.update()
    d = _find(app, StockUseDialog)[-1]
    kapra = [l for l, m in d.mats.items() if m["name"] == "Kapra"]
    assert len(kapra) == 1 and d.mats[kapra[0]]["own"] == "customer" and "(customer's own)" in kapra[0]      # the factory has none yet: only his cloth
    d.destroy()
    inv.receive_stock(conn, o, material_id=fab, qty="80", supplier_id=2, total_cost="16000", received_on="2026-08-03")
    page.refresh(); app.update(); _select(page, page.t, "job_no", "J1", app)
    page.consume(); app.update()
    d = _find(app, StockUseDialog)[-1]
    kapra = [l for l, m in d.mats.items() if m["name"] == "Kapra"]
    assert [d.mats[l]["own"] for l in kapra] == ["customer", "factory"]                          # customer entry: his cloth first, the factory's cloth next
    assert "200 m (customer's own)" in kapra[0] and "80 m in factory stock" in kapra[1]
    r = d.rows[0]
    r["mat"].set(kapra[1]); d._material_changed(r); r["qty"].insert(0, "30"); d._calc()
    assert r["cost"].cget("text") == "Rs 6,000.00"                                              # factory cloth: 30 m x Rs 200
    d.submit(); app.update()
    assert next(m for m in q.materials(conn) if m["id"] == fab)["qty_milli"] == 50_000           # the factory's stock went down, the customer's did not
    assert q.customer_stock(conn)[0]["left_milli"] == 200_000


# ---------------------------------------------------------------- expenses: period buttons, total, PDF
def test_period_buttons_filter_the_list_and_the_total(app):
    page = _expenses(app); conn = app.ctx.conn
    acct = q.expense_accounts(conn)[0]["code"]
    today = date.today()
    mon = today - timedelta(days=today.weekday())
    first = today.replace(day=1)
    old = (first - timedelta(days=40)).isoformat()
    e1 = ac.record_expense(conn, app.ctx.user, account_code=acct, amount="1000", on=today.isoformat(), description="Today A")
    ac.record_expense(conn, app.ctx.user, account_code=acct, amount="200", on=today.isoformat(), description="Today B")
    ac.record_expense(conn, app.ctx.user, account_code=acct, amount="50", on=old, description="Old")
    ac.reverse_entry(conn, app.ctx.user, e1, "typed twice")
    page.refresh(); app.update()
    assert page.period == "month"
    page._set_period("today"); app.update()
    assert sorted(r["description"] for r in page.t.rows) == ["Today A", "Today B"]
    assert "Rs 200.00" in page.total_lbl.cget("text") and "1 reversed not counted" in page.total_note.cget("text")        # Today A was cancelled
    page._set_period("all"); app.update()
    assert len(page.t.rows) == 3 and "Rs 250.00" in page.total_lbl.cget("text") and "all time" in page.total_lbl.cget("text")
    page._set_period("week"); app.update()
    assert all(mon.isoformat() <= r["entry_date"] <= (mon + timedelta(days=6)).isoformat() for r in page.t.rows) and page.t.rows
    page.d_from.set_date(old); page.d_to.set_date(today); page._custom(); app.update()                                 # any dates from the calendar
    assert page.period == "custom" and len(page.t.rows) == 3 and "Rs 250.00" in page.total_lbl.cget("text")
    page.d_from.set_date(today); page.d_to.set_date(old); page._custom(); app.update()
    assert "after" in page.total_lbl.cget("text") and page.t.rows == []
    page.d_from.set_date(old); page.d_to.set_date(old); page._custom(); app.update()
    assert [r["description"] for r in page.t.rows] == ["Old"] and "Rs 50.00" in page.total_lbl.cget("text")


def test_the_category_list_has_no_salary_categories_and_pdf_button_saves(app, tmp_path, monkeypatch):
    page = _expenses(app); conn = app.ctx.conn
    assert "Print PDF" in [t for t, _b, _e in page.buttons]
    page.expense(); app.update()
    d = dialogs(app)[-1]
    labels = [l for l, _v in next(f for f in d.fields if f.key == "a").choices]
    assert "Direct Labor" not in labels and "Administrative Salaries" not in labels and "Electricity & Utilities" in labels and "Other Expenses" in labels
    d.destroy()
    ac.record_expense(conn, app.ctx.user, account_code=q.expense_accounts(conn)[0]["code"], amount="1000", on=date.today().isoformat(), description="Tea")
    page.refresh(); app.update()
    out = tmp_path / "e.pdf"
    import ui.pages.finance_pages as fp
    monkeypatch.setattr(fp.filedialog, "asksaveasfilename", lambda **k: str(out))
    shown = []
    monkeypatch.setattr(fp, "show_info", lambda _p, title, msg: shown.append((title, msg)))
    page.print_pdf(); app.update()
    assert out.read_bytes().startswith(b"%PDF") and shown and shown[0][0] == "Saved"
