"""Employee resignation/removal, the locked cost per meter (Cost Calculator -> Create invoice) and the compact Calculation window."""
from datetime import date, datetime

import pytest

from services import accounting_service as ac, auth_service as auth, payroll_service as pr, settings_service as st


@pytest.fixture
def w(conn, owner):
    auth.create_user(conn, owner, "acc", "accountant-pw", "accountant")
    auth.create_user(conn, owner, "sup", "supervisor-pw", "production_supervisor")
    ac.record_opening_cash(conn, owner, "1000000", "2026-09-01")
    e1 = pr.add_employee(conn, owner, emp_code="E1", name="Bilal", monthly_salary="30000", effective_from="2026-01-01", joined_on="2026-01-01")
    return dict(owner=owner, acc=auth.authenticate(conn, "acc", "accountant-pw"), sup=auth.authenticate(conn, "sup", "supervisor-pw"), e1=e1)


# ---------------------------------------------------------------- resignation / removal
def test_exit_keeps_the_history_and_stops_future_salary(conn, w):
    o, e = w["owner"], w["e1"]
    conn.execute("INSERT INTO attendance(employee_id,work_date,status,worked_minutes) VALUES (?,?,?,?)", (e, "2026-10-05", "present", 480))
    pr.give_advance(conn, o, e, "5000", "2026-10-06")
    res = pr.end_employment(conn, o, e, "2026-10-20", "resigned", "better job")
    emp = conn.execute("SELECT * FROM employees WHERE id=?", (e,)).fetchone()
    assert (emp["is_active"], emp["left_on"], emp["exit_kind"], emp["exit_reason"]) == (0, "2026-10-20", "resigned", "better job")
    assert res["advance_paisa"] == 500000 and res["unpaid_paisa"] == 0 and res["last_month"] == "2026-10" and res["last_month_accrued"] is False
    assert conn.execute("SELECT COUNT(*) FROM attendance WHERE employee_id=?", (e,)).fetchone()[0] == 1          # nothing deleted
    assert e not in [r["id"] for r in pr.live_board(conn)]                                                       # off the Today board
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='employee.exit'").fetchone()[0] == 1
    # the last month can still be accrued (by hand), a later month cannot
    pr.accrue_salary(conn, o, e, "2026-10", advance_recovery="5000")
    with pytest.raises(pr.PayrollError, match="has left"):
        pr.accrue_salary(conn, o, e, "2026-11")
    with pytest.raises(pr.PayrollError):
        pr.give_advance(conn, o, e, "100", "2026-10-25")                                                         # no new advances for someone who left
    rows = {r["id"]: r for r in pr.month_overview(conn, "2026-10")}
    assert rows[e]["left_on"] == "2026-10-20"                                                                    # still on the October sheet
    assert not any(r["id"] == e for r in pr.month_overview(conn, "2026-12"))                                      # gone from later sheets
    assert pr.auto_accrue_month(conn, o, "2026-11")["created"] == []                                            # automatic accrual never picks them up


def test_exit_rules(conn, w):
    o, e = w["owner"], w["e1"]
    with pytest.raises(Exception):
        pr.end_employment(conn, w["sup"], e, "2026-10-20", "resigned")                                           # supervisors cannot
    with pytest.raises(pr.PayrollError, match="future"):
        pr.end_employment(conn, o, e, "2099-01-01", "resigned")
    with pytest.raises(pr.PayrollError, match="before the joining"):
        pr.end_employment(conn, o, e, "2025-12-31", "resigned")
    with pytest.raises(pr.PayrollError, match="resigned or was removed"):
        pr.end_employment(conn, o, e, "2026-10-20", "")
    pr.clock_in(conn, o, e, datetime(2026, 10, 20, 8, 0))
    with pytest.raises(pr.PayrollError, match="clocked in"):
        pr.end_employment(conn, o, e, "2026-10-20", "removed")
    pr.clock_out(conn, o, e, datetime(2026, 10, 20, 16, 0))
    pr.end_employment(conn, w["acc"], e, "2026-10-20", "removed", "")                                            # the accountant can
    with pytest.raises(pr.PayrollError, match="already left"):
        pr.end_employment(conn, o, e, "2026-10-21", "resigned")


def test_bring_back(conn, w):
    o, e = w["owner"], w["e1"]
    pr.end_employment(conn, o, e, "2026-10-20", "resigned")
    with pytest.raises(pr.PayrollError, match="reason"):
        pr.reinstate_employee(conn, o, e, " ")
    pr.reinstate_employee(conn, o, e, "recorded by mistake")
    emp = conn.execute("SELECT * FROM employees WHERE id=?", (e,)).fetchone()
    assert (emp["is_active"], emp["left_on"], emp["exit_kind"]) == (1, None, None)
    assert e in [r["id"] for r in pr.live_board(conn)]
    with pytest.raises(pr.PayrollError, match="already active"):
        pr.reinstate_employee(conn, o, e, "again")


# ---------------------------------------------------------------- locked cost per meter
def test_locked_cost_is_stored_in_paisa_and_audited(conn, w):
    assert st.locked_cost(conn) is None
    assert st.set_locked_cost(conn, w["owner"], "170") == 17000 and st.locked_cost(conn) == 17000
    assert st.set_locked_cost(conn, w["acc"], "1,70.5") == 17050
    for bad, msg in (("abc", "not a number"), ("0", "more than 0"), ("-5", "more than 0"), ("99999999", "too large")):
        with pytest.raises(st.SettingsError, match=msg):
            st.set_locked_cost(conn, w["owner"], bad)
    assert st.locked_cost(conn) == 17050                                                                        # a bad entry changes nothing
    with pytest.raises(Exception):
        st.set_locked_cost(conn, w["sup"], "180")
    assert st.set_locked_cost(conn, w["owner"], "") is None and st.locked_cost(conn) is None
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE entity_id='locked_cost_per_meter'").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 1                              # only the opening cash: nothing reaches the books


# ---------------------------------------------------------------- screens
tk = pytest.importorskip("tkinter")
from tests.test_ui_smoke import app, populated, no_blocking_confirm, login  # noqa: E402,F401


def _texts(w, out=None):
    out = [] if out is None else out
    try:
        t = w.cget("text")
        if isinstance(t, str):
            out.append(t)
    except Exception:  # noqa: BLE001
        pass
    for c in w.winfo_children():
        _texts(c, out)
    return out


def test_employee_pane_shows_who_left_and_the_buttons(app):
    login(app); app.show("labor"); app.update()
    page = app.screen.page_cache["labor"]
    assert page.exit_btn.cget("text") == "Resign / Remove" and page.back_btn.cget("text") == "Bring back" and str(page.exit_btn.cget("state")) == "normal"
    e = pr.add_employee(app.ctx.conn, app.ctx.user, emp_code="Z1", name="Zahid", monthly_salary="20000", effective_from="2026-01-01", joined_on="2026-01-01")
    pr.end_employment(app.ctx.conn, app.ctx.user, e, date.today().isoformat(), "resigned")
    page.refresh(); app.update()
    z = next(r for r in page.emp.rows if r["emp_code"] == "Z1")
    assert z["status"].startswith("Resigned ") and z["is_active"] == 0
    assert all(r["status"] == "Active" for r in page.emp.rows if r["is_active"])


def test_cost_calculator_locks_the_figure_you_type(app):
    from services.units import to_paisa  # noqa: F401
    login(app); app.show("costing"); app.update()
    page = app.screen.page_cache["costing"]
    assert page.lock_btn.cget("text") == "LOCK" and "Not locked" in page.lock_msg.cget("text")
    for k, v in dict(paper="60", ink_price="2000", ink_use="20", width="2.6", power="6", per_day="2000", days="30", hours="20", rate="38.44", labour="100000").items():
        page.vars[k].set(v)
    page.calculate(); app.update()
    assert "Rs 167.97" in page.lock_hint.cget("text")                          # the calculated figure is offered as a guide, never locked by itself
    assert st.locked_cost(app.ctx.conn) is None
    page.lock_var.set("170"); page.lock_btn.invoke(); app.update()
    assert st.locked_cost(app.ctx.conn) == 17000 and "Locked at Rs 170 per meter" in page.lock_msg.cget("text")
    page.clear(); app.update()                                                  # clearing the calculation keeps the decision
    assert st.locked_cost(app.ctx.conn) == 17000 and "Locked at Rs 170" in page.lock_msg.cget("text")
    page.unlock_btn.invoke(); app.update()
    assert st.locked_cost(app.ctx.conn) is None and page.lock_var.get() == "" and "Not locked" in page.lock_msg.cget("text")


def _invoice_dialog(app, pieces=1000, stock=90_000_00):
    from ui.pages.production_pages import InvoiceDialog
    job = {"id": 1, "job_no": "P-0001", "customer": "Test Customer", "product_type": "Bedsheet", "quantity_pieces": pieces}
    return InvoiceDialog(app, app.ctx.conn, app.ctx.user, job, {"total_paisa": stock}, 0, on_done=lambda: None)


def test_create_invoice_shows_the_locked_cost_per_meter(app):
    login(app)
    d = _invoice_dialog(app); app.update()
    t = _texts(d)
    assert "Cost per meter" in t and "Rs 90,000.00" in t and "Rs 90.00" in t                          # no lock: the stock cost (90,000 / 1,000 m = Rs 90 per meter)
    d.destroy()
    st.set_locked_cost(app.ctx.conn, app.ctx.user, "170")
    d = _invoice_dialog(app); app.update()
    t = _texts(d)
    assert "Cost per meter" in t and "Rs 170.00" in t and "Rs 170,000.00" in t                  # 170 x 1,000 m
    assert not any("locked)" in x or x.startswith("From the stock used") for x in t) and "Rs 90.00" not in t     # no "(locked)" wording and no stock sub-text
    for b in (d.go_btn,):                                                                                         # the buttons are inside the window
        assert b.winfo_ismapped() and b.winfo_rooty() + b.winfo_height() <= d.winfo_rooty() + d.winfo_height() + 2
    d.rate.insert(0, "200"); d._from_rate(); app.update()
    assert "Profit Rs 30,000.00" in d.profit.cget("text") and "at the locked cost" in d.profit.cget("text")      # (200 - 170) x 1,000 m
    d.destroy()


def test_calculation_window_stays_on_screen_even_with_many_stock_lines(app):
    from ui.pages.production_pages import CalculationDialog
    login(app)
    lines = [{"date": "2026-10-01", "kind": "production", "item": f"Material number {i} with a long name", "unit": "kg", "qty_milli": 12345 * 1000, "cost_paisa": 123456789,
              "customer_owned": False, "reverses_id": None, "rate_paisa_per_unit": 1234567, "estimate": False} for i in range(40)]
    s = {"job": {"job_no": "P-0001", "customer": "Test Customer", "product_type": "Bedsheet", "order_date": "2026-10-01"}, "pieces": 1000, "lines": lines,
         "stock_cost_paisa": 123456789, "other_cost_paisa": 5000, "total_paisa": 123461789, "cost_per_piece_paisa": 123461, "warnings": ["Something to look at."]}
    d = CalculationDialog(app, s); app.update()
    sw, sh = app.winfo_screenwidth(), app.winfo_screenheight()
    assert d.winfo_width() <= sw and d.winfo_height() <= sh - 100
    assert d.default_btn.winfo_ismapped() and d.default_btn.winfo_rooty() + d.default_btn.winfo_height() <= d.winfo_rooty() + d.winfo_height() + 2   # Close is never cut off
    d.destroy()
