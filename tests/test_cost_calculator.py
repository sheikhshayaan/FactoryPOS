"""Cost Calculator: the arithmetic (typed-in figures only, never the database) and the page."""
from decimal import Decimal

import pytest

from services import cost_calculator as cc

FIGS = dict(paper="60", ink_price="2000", ink_use="20", width="2.6", power="6", per_day="2000", days="30", hours="20", rate="38.44", labour="100000")


def test_one_metre_costs_paper_plus_ink_plus_labour_plus_electricity():
    r = cc.calculate(FIGS)
    assert r["ink"] == Decimal("104")                                    # 20 ml/m2 x 2.6 m = 52 ml, at Rs 2 per ml
    assert r["labour"].quantize(Decimal("0.0001")) == Decimal("1.6667")  # 100,000 / (2,000 x 30 m)
    assert r["elec"].quantize(Decimal("0.0001")) == Decimal("2.3064")    # (38.44 x 6 kW) per hour / 100 m per hour
    assert cc.money(r["total"]) == "Rs 167.97"
    assert r["total"] == r["paper"] + r["ink"] + r["labour"] + r["elec"]
    assert cc.money(r["per_day_cost"], 0) == "Rs 335,946" and cc.money(r["per_month_cost"], 0) == "Rs 10,078,384"
    assert r["monthly_m"] == 60000 and r["per_hour"] == 100


def test_shares_add_up_to_one_hundred_percent():
    r = cc.calculate(FIGS)
    assert [n for n, _a, _p in r["shares"]] == ["Paper", "Ink", "Labour", "Electricity"]
    assert sum(p for _n, _a, p in r["shares"]).quantize(Decimal("0.0001")) == Decimal("100.0000")


def test_scenarios_show_the_figure_typed_and_the_difference():
    r = cc.calculate(FIGS)
    cur = [s for s in r["scenarios"] if s["current"]]
    assert len(cur) == 1 and cur[0]["coverage"] == 20 and cur[0]["diff"] == 0
    assert [s["coverage"] for s in r["scenarios"]] == sorted(s["coverage"] for s in r["scenarios"])
    assert next(s for s in r["scenarios"] if s["coverage"] == 40)["diff"] == Decimal("104")        # 20 ml/m2 more = 52 ml more = Rs 104
    r2 = cc.calculate({**FIGS, "ink_use": "17"})                                                    # a figure that is not a preset step is added
    assert any(s["current"] and s["coverage"] == 17 for s in r2["scenarios"])


def test_presets_are_complete_and_calculate():
    assert list(cc.PRESETS) == ["standard", "high_speed", "heavy_ink"]
    for _key, (_label, vals) in cc.PRESETS.items():
        assert cc.calculate(vals)["total"] > 0
    assert cc.money(cc.calculate(cc.PRESETS["standard"][1])["total"]) == "Rs 167.97"
    assert cc.calculate(cc.PRESETS["heavy_ink"][1])["ink"] == Decimal("156")          # 30 ml/m2 x 2.6 m x Rs 2 per ml


def test_commas_are_fine_and_bad_input_names_the_field():
    assert cc.calculate({**FIGS, "labour": "1,00,000"})["labour"] == cc.calculate(FIGS)["labour"]
    with pytest.raises(cc.CostError, match="Still to fill in: Paper cost per linear metre"):
        cc.calculate({**FIGS, "paper": ""})
    with pytest.raises(cc.CostError, match="Ink price per litre: 'abc' is not a number"):
        cc.calculate({**FIGS, "ink_price": "abc"})
    with pytest.raises(cc.CostError, match="not negative"):
        cc.calculate({**FIGS, "rate": "-1"})
    for k in ("per_day", "days", "hours"):                               # these divide, so zero is refused instead of crashing
        with pytest.raises(cc.CostError, match="more than 0"):
            cc.calculate({**FIGS, k: "0"})
    assert cc.calculate({**FIGS, "labour": "0", "power": "0"})["total"] == Decimal("164")           # zero is fine where nothing divides by it


# ---------------------------------------------------------------- the page
tk = pytest.importorskip("tkinter")
from tests.test_ui_smoke import app, populated, no_blocking_confirm, login  # noqa: E402,F401


def test_page_is_in_the_menu_and_calculates_from_typed_figures(app):
    login(app)
    n_books = app.ctx.conn.execute("SELECT (SELECT COUNT(*) FROM journal_entries) + (SELECT COUNT(*) FROM audit_log)").fetchone()[0]
    app.show("costing"); app.update()
    page = app.screen.page_cache["costing"]
    assert page.calc_btn.cget("text") == "CALCULATE COST" and page.boxes["metre"].value.cget("text") == "-" and page.result is None
    page.calc_btn.invoke(); app.update()                                 # nothing typed yet: says what is missing, does not crash
    assert "Still to fill in" in page.status.cget("text") and page.result is None
    for k, v in FIGS.items():
        page.vars[k].set(v)
    app.update()
    assert page.boxes["metre"].value.cget("text") == "-"                 # typing alone does not change the answer
    page.calc_btn.invoke(); app.update()
    assert page.boxes["metre"].value.cget("text") == "Rs 167.97" and page.boxes["day"].value.cget("text") == "Rs 335,946.13"
    assert page.boxes["month"].value.cget("text") == "Rs 10,078,384.00" and "2,000" in page.boxes["day"].note.cget("text")
    assert page.total_left.cget("text") == "Rs 167.97" and page.total_main.cget("text") == "Rs 167.97"
    assert page.status.cget("text") == "" and len(page.table.rows) == 10
    assert page.share["Ink"][1].cget("text") == "(61.9%)" and page.width_chip.cget("text") == "Width: 2.6 m"
    assert [r["change"] for r in page.table.rows if r["cov"].startswith("20 ")] == ["Active input"]
    page.vars["ink_use"].set("30"); app.update()                         # an edit after calculating: the page warns the answer is old
    assert "Press Calculate cost" in page.status.cget("text") and page.boxes["metre"].value.cget("text") == "Rs 167.97"
    page.entries["ink_use"]._entry.focus_force(); app.update(); page.entries["ink_use"]._entry.event_generate("<Return>"); app.update()     # Enter calculates too
    assert page.boxes["metre"].value.cget("text") == "Rs 219.97" and page.status.cget("text") == ""
    page.vars["per_day"].set("0"); page.calculate(); app.update()
    assert page.result is None and "more than 0" in page.status.cget("text") and page.boxes["metre"].value.cget("text") == "-"
    page.vars["per_day"].set("2000"); page.calculate()
    assert page._file.exists()                                           # the figures are remembered for next time
    assert [b.cget("text") for b in page.preset_btns.values()] == ["Standard preset", "High speed", "Heavy ink pass"]
    page.preset_btns["high_speed"].invoke(); app.update()                # presets fill the figures and calculate
    assert page.vars["per_day"].get() == "2800" and page.vars["ink_use"].get() == "15" and page.boxes["metre"].value.cget("text") == cc.money(cc.calculate(cc.PRESETS["high_speed"][1])["total"])
    page.preset_btns["standard"].invoke(); app.update()
    assert page.boxes["metre"].value.cget("text") == "Rs 167.97"
    page.preset_btns["heavy_ink"].invoke(); app.update()
    assert page.vars["ink_use"].get() == "30" and page.vars["per_day"].get() == "1600" and page.status.cget("text") == ""
    assert page.clear_btn.cget("text") == "CLEAR CALCULATIONS"
    page.clear_btn.invoke(); app.update()
    assert not page._file.exists() and all(v.get() == "" for v in page.vars.values()) and page.boxes["metre"].value.cget("text") == "-"
    assert app.ctx.conn.execute("SELECT (SELECT COUNT(*) FROM journal_entries) + (SELECT COUNT(*) FROM audit_log)").fetchone()[0] == n_books   # nothing reaches the books


def test_page_rearranges_for_wide_and_narrow_windows(app):
    login(app); app.show("costing")
    page = app.screen.page_cache["costing"]
    for k, v in FIGS.items():
        page.vars[k].set(v)
    page.calculate()
    keys = {}
    for size in ("1800x1000+0+0", "1280x800+0+0", "760x900+0+0"):
        app.geometry(size); app.update(); page._apply_layout(); app.update()
        keys[size] = page._layout_key
        for w in (page.calc_btn, page.table, page.boxes["metre"], page.inputs, page.breakdown, page.distribution):
            assert w.winfo_ismapped() and w.winfo_width() > 50, (size, w)
        assert page.boxes["metre"].winfo_x() + page.boxes["metre"].winfo_width() <= page.hero.winfo_width() + 2         # nothing pushed out sideways
        assert page.calc_btn.winfo_width() <= page.inputs.winfo_width()
    assert keys["1800x1000+0+0"][:2] == (3, True)                        # three result cards and the inputs beside the workings
    assert keys["760x900+0+0"][1] is False and keys["760x900+0+0"] != keys["1800x1000+0+0"]
    for size in ("1800x1000+0+0", "1280x800+0+0"):                        # the three result cards always share one row
        app.geometry(size); app.update(); page._resized(); app.update()
        ys = {page.boxes[k].winfo_y() for k in ("metre", "day", "month")}
        assert len(ys) == 1 and page.boxes["month"].winfo_x() > page.boxes["day"].winfo_x() > page.boxes["metre"].winfo_x()
        big = page.boxes["month"]
        assert big.value.winfo_x() + big.value.winfo_width() <= big.winfo_width()          # the long number sits inside its card
