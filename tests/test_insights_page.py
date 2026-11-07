"""The Business Insights page: opens, follows the buttons, shows the numbers from the books."""
from datetime import date, timedelta

import pytest

pytest.importorskip("tkinter")
from tests.test_ui_smoke import app, populated, no_blocking_confirm, login  # noqa: E402,F401
from services import accounting_service as ac, asset_service, inventory_service as inv, job_service as js, insights_service as ins  # noqa: E402


def _data(app):
    conn, o = app.ctx.conn, app.ctx.user
    t = date.today()
    this = t.replace(day=1)
    last = (this - timedelta(days=1)).replace(day=10)
    cust = next(r[0] for r in conn.execute("SELECT id FROM customers LIMIT 1"))
    ink = inv.add_material(conn, o, "Ink B", "ink", "L")
    inv.receive_stock(conn, o, material_id=ink, qty="50", unit_price="1000", received_on=last.isoformat(), supplier_id=2, payment_status="paid")
    j = js.create_job(conn, o, job_no="J9", customer_id=cust, product_type="Bedsheet", fabric_source="factory", order_date=last.isoformat())
    js.set_status(conn, o, j, "approved")
    js.consume_material(conn, o, j, ink, "5", on_date=last.isoformat())
    js.consume_material(conn, o, j, ink, "9", on_date=this.isoformat())
    ac.issue_invoice(conn, o, invoice_no="X1", customer_id=cust, total="90000", invoice_date=last.isoformat())
    ac.issue_invoice(conn, o, invoice_no="X2", customer_id=cust, total="140000", invoice_date=this.isoformat())
    ac.record_expense(conn, o, account_code="5200", amount="5000", on=last.isoformat(), description="Elec last")
    ac.record_expense(conn, o, account_code="5200", amount="9000", on=this.isoformat(), description="Elec now")
    asset_service.add_asset(conn, o, name="Machine", price="400000", on=last.isoformat())


def test_the_page_opens_and_shows_the_comparison(app):
    login(app); _data(app)
    app.show("insights"); app.update()
    page = app.screen.page_cache["insights"]
    page.refresh(); app.update()
    d = page.data
    assert d["cur"]["sales_paisa"] >= 14_000_000 and "vs" in page.vs_lbl.cget("text")
    assert "PROFIT" in page.banner_lbl.cget("text") or "LOSS" in page.banner_lbl.cget("text")
    assert page.kpi["sales"]["value"].cget("text").startswith("Rs") and "vs" in page.kpi["sales"]["delta"].cget("text")
    assert [r["name"] for r in page.t_exp.rows][:1] == ["Electricity & Utilities"] and page.t_exp.rows[0]["status"] == "exceeding"
    assert "Exceeding" in page.s_exp.cget("text") and "Electricity" in page.s_elec.cget("text")
    assert not hasattr(page, "t_inv") and "Machine hours" not in str([b.cget("text") for a, b in page.elec_card["tiles"]])   # inventory section and machine hours are gone for now
    assert len(page.elec_card["tiles"]) == 1
    assert set(page.canvases) == {"bars", "trend", "split", "pay", "exp", "exp_bars"}
    from matplotlib.text import Text
    assert all(t.get_fontweight() in ("bold", 700) for t in page.canvases["bars"].figure.findobj(Text) if t.get_text())      # chart texts and figures are bold
    assert page.pay_card["head"].cget("text") in ("Coming back", "Not recovering now", "Fully recovered")


def test_period_buttons_arrows_and_custom_dates(app):
    login(app); _data(app)
    app.show("insights"); app.update()
    page = app.screen.page_cache["insights"]
    assert page.b_next.cget("state") == "disabled"                                    # already on the current period
    for label in ("Quarter vs quarter", "Year vs year", "Month vs month"):
        page.seg.set(label); page._pick(label); app.update()
        assert page.data["periods"]["kind"] == dict((m[0], m[1]) for m in __import__("ui.pages.insights_page", fromlist=["MODES"]).MODES)[label]
    page._step(-1); app.update()
    assert page.offset == -1 and page.b_next.cget("state") == "normal" and page.data["periods"]["cur"]["label"] != ins.periods("month", date.today())["cur"]["label"]
    page._step(1); app.update()
    assert page.offset == 0
    page.seg.set("Pick dates"); page._pick("Pick dates"); app.update()
    page.c_from.set_date((date.today() - timedelta(days=9)).isoformat()); page.c_to.set_date(date.today().isoformat()); page._custom_changed(); app.update()
    assert page.data["periods"]["kind"] == "custom" and page.data["periods"]["cur"]["days"] == 10
    assert page.crow.winfo_ismapped() and page.c_from.winfo_ismapped() and page.c_to.winfo_ismapped()         # the calendars are really on screen
    assert "10 days" in page.crow_msg.cget("text") and len(page.vs_lbl.cget("text")) < 50
    page.c_from.set_date(date.today().isoformat()); page.c_to.set_date((date.today() - timedelta(days=3)).isoformat()); page._custom_changed(); app.update()
    assert "after" in page.crow_msg.cget("text")                                       # first day after the last day: told, figures unchanged
    page.seg.set("Month vs month"); page._pick("Month vs month"); app.update()
    assert not page.crow.winfo_ismapped()


def test_an_empty_company_shows_friendly_text_not_errors(app):
    login(app)
    app.ctx.conn.execute("DELETE FROM asset_records") if False else None
    app.show("insights"); app.update()
    page = app.screen.page_cache["insights"]
    page.seg.set("Year vs year"); page._pick("Year vs year"); app.update()
    page.offset = -5; page.refresh(); app.update()                                    # years with nothing in them
    assert page.data["cur"]["sales_paisa"] == 0 and page.banner_lbl.cget("text")
    assert page.t_exp.rows == []


def test_dashboard_total_expenses_is_what_the_expenses_page_adds_up(app):
    from services import queries as q
    login(app); _data(app)
    app.show("dashboard"); app.update()
    page = app.screen.page_cache["dashboard"]; page.refresh(); app.update()
    start, end = page._range()
    total, _n = q.expense_total(app.ctx.conn, start, end)
    assert page.cards["opex"].value.cget("text") == __import__("ui.theme", fromlist=["money"]).money(total)
