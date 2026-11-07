"""Complete Business Summary PDF: any month / year / custom dates, 7-9 pages, figures from the books."""
import os
from datetime import date

import pytest

from services import accounting_service as ac, inventory_service as inv, payroll_service as pr, profit_service as ps
from reports import business_summary as bs
from tests.test_insights import world  # noqa: F401  (a factory with September and October activity)


@pytest.fixture
def rich(world):  # noqa: F811
    conn, o, codes = world
    pid = ps.add_partner(conn, o, "Faizan")
    ps.record_capital(conn, o, pid, "500000", "2026-01-01")
    pr.add_employee(conn, o, emp_code="E1", name="Bilal", monthly_salary="30000", effective_from="2026-01-01", joined_on="2026-01-01")
    pr.add_employee(conn, o, emp_code="E2", name="Sana", monthly_salary="45000", effective_from="2026-01-01", joined_on="2026-01-01", cost_category="admin")
    conn.execute("UPDATE employees SET created_at='2026-01-02T00:00:00Z'")
    from services import job_service as js
    j2 = js.create_job(conn, o, job_no="J2", customer_id=conn.execute("SELECT id FROM customers LIMIT 1").fetchone()[0], product_type="Bedsheet",
                       fabric_source="factory", order_date="2026-10-12", quantity_pieces=500)
    js.set_status(conn, o, j2, "approved")
    pr.auto_accrue_month(conn, o, "2026-09")
    pr.auto_accrue_month(conn, o, "2026-10")
    slip = conn.execute("SELECT id FROM payslips WHERE period='2026-10' ORDER BY id LIMIT 1").fetchone()[0]
    pr.pay_salary(conn, o, slip, "10000", "2026-10-31")
    return conn, o


def _pages(path):
    from pypdf import PdfReader
    return PdfReader(str(path))


def test_month_summary_has_seven_to_nine_pages_and_the_key_numbers(rich, tmp_path):
    conn, o = rich
    out = bs.business_summary_pdf(conn, tmp_path / "s.pdf", "month", month="2026-10", today=date(2026, 10, 29))
    r = _pages(out)
    assert 7 <= len(r.pages) <= 9
    text = "\n".join(p.extract_text() for p in r.pages)
    for needle in ("Complete Business Summary", "Oct 2026", "Rs 150,000.00", "Sales and profit", "Expenses", "Salaries", "Inventory", "Production and invoices", "Money position"):
        assert needle in text, needle
    assert "Salaries booked" in text and "Bilal" in text and "Ink" in text and "Kapra" in text and "J2" in text
    if os.environ.get("KEEP_SUMMARY"):
        import shutil
        shutil.copy(out, os.environ["KEEP_SUMMARY"])


def test_year_and_custom_periods_and_future_is_refused(rich, tmp_path):
    conn, o = rich
    y = bs.business_summary_pdf(conn, tmp_path / "y.pdf", "year", fy_year=2026, today=date(2026, 10, 29))
    assert 6 <= len(_pages(y).pages) <= 10
    c = bs.business_summary_pdf(conn, tmp_path / "c.pdf", "custom", custom=("2026-09-01", "2026-10-15"), today=date(2026, 10, 29))
    assert "2026-09-01 to 2026-10-15" in "\n".join(p.extract_text() for p in _pages(c).pages)
    with pytest.raises(bs.SummaryError):
        bs.business_summary_pdf(conn, tmp_path / "f.pdf", "month", month="2026-12", today=date(2026, 10, 29))
    with pytest.raises(bs.SummaryError):
        bs.business_summary_pdf(conn, tmp_path / "f.pdf", "custom", custom=("2026-10-10", "2026-10-01"), today=date(2026, 10, 29))


def test_stock_table_closing_equals_the_stock_on_hand(rich):
    conn, o = rich
    t = bs.stock_table(conn, "2026-08-01", "2099-12-31")
    live = {r["id"]: (r["qty_milli"], r["value_paisa"]) for r in conn.execute("SELECT m.id, s.qty_milli, s.value_paisa FROM materials m JOIN material_stock s ON s.material_id=m.id")}
    assert t and all((r["cq"], r["cv"]) == live[r["id"]] for r in t)
    sep = {r["name"]: r for r in bs.stock_table(conn, "2026-09-01", "2026-09-30")}
    assert sep["Ink"]["uq"] == 10_000 and sep["Ink"]["uv"] == 1_000_000                  # 10 L at Rs 1,000 used in September


# ---- the button on the Reports page
tk = pytest.importorskip("tkinter")
from tests.test_ui_smoke import (app, populated, no_blocking_confirm, login, dialogs, fill, submit)  # noqa: E402,F401


def test_reports_page_button_makes_the_pdf_for_a_month_and_for_dates(app, tmp_path, monkeypatch):
    login(app); app.show("reports"); app.update()
    page = app.screen.page_cache["reports"]
    assert "Complete Business Summary" in [t for t, _b, _e in page.buttons]
    shown = []
    monkeypatch.setattr("ui.pages.admin_pages.show_info", lambda *a, **k: shown.append(a[-1]))
    out = tmp_path / "m.pdf"
    monkeypatch.setattr("ui.pages.admin_pages.filedialog.asksaveasfilename", lambda **k: str(out))
    page.summary(); app.update()
    d = dialogs(app)[-1]
    assert "month" not in d._hidden and {"year", "a", "b"} <= d._hidden                           # a month by default; the other choices are tucked away
    d.widgets["kind"].set("Choose dates"); d._changed(); app.update()
    assert {"month", "year"} <= d._hidden and not {"a", "b"} & d._hidden
    assert d.widgets["a"].get() == date.today().replace(day=1).isoformat() and d.widgets["b"].get() == date.today().isoformat()      # this month so far
    d.widgets["kind"].set("A month"); d._changed(); app.update()
    submit(app)
    assert out.exists() and out.read_bytes()[:4] == b"%PDF" and shown and "Saved to" in shown[-1]
    assert len(_pages(out).pages) >= 7
    # dates in the wrong order are refused with a message and nothing is written
    out2 = tmp_path / "bad.pdf"
    monkeypatch.setattr("ui.pages.admin_pages.filedialog.asksaveasfilename", lambda **k: str(out2))
    page.summary(); app.update()
    d = dialogs(app)[-1]
    d.widgets["kind"].set("Choose dates"); d._changed(); app.update()
    fill(d, a="2026-10-10", b="2026-10-01"); d.submit(); app.update()
    assert not out2.exists() and "end date is before" in d.err.cget("text")
    d.destroy()
