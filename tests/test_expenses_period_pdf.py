"""Expenses: period filter, total (reversed entries not counted), PDF, and the two payroll categories no longer offered."""
from datetime import date, timedelta

import pytest

from services import accounting_service as ac, auth_service as auth, queries as q
from services.units import to_paisa as P
from reports import documents


@pytest.fixture
def w(conn, owner):
    ac.record_opening_cash(conn, owner, "500000", "2026-01-01")
    return owner


def spend(conn, o, amount, on, desc, code=None):
    return ac.record_expense(conn, o, account_code=code or q.expense_accounts(conn)[0]["code"], amount=amount, on=on, description=desc)


def test_payroll_categories_are_not_offered_and_cannot_be_used(conn, w):
    codes = [a["code"] for a in q.expense_accounts(conn)]
    assert "5100" not in codes and "6100" not in codes and "5200" in codes and "6000" in codes
    for bad in ("5100", "6100"):
        with pytest.raises(ac.AccountingError):
            spend(conn, w, "100", "2026-08-01", "x", code=bad)


def test_the_total_follows_the_period_and_skips_reversed_entries(conn, w):
    e1 = spend(conn, w, "1000", "2026-08-03", "Tea")
    spend(conn, w, "2500", "2026-08-10", "Oil")
    spend(conn, w, "400", "2026-09-01", "Soap")
    ac.reverse_entry(conn, w, e1, "typed twice")
    assert q.expense_total(conn) == (P("2900"), 2)                                            # Tea was cancelled
    assert q.expense_total(conn, "2026-08-01", "2026-08-31") == (P("2500"), 1)
    assert q.expense_total(conn, "2026-09-01", "2026-09-01") == (P("400"), 1)
    assert q.expense_total(conn, "2026-10-01", "2026-10-31") == (0, 0)
    assert [r["description"] for r in q.expenses(conn, start="2026-08-01", end="2026-08-31")] == ["Oil", "Tea"]          # Tea still listed (flagged), not counted
    assert q.expenses(conn, start="2026-08-03", end="2026-08-03")[0]["reversed"] == 1


def test_category_totals_add_up_to_the_total(conn, w):
    a, b = q.expense_accounts(conn)[0]["code"], q.expense_accounts(conn)[1]["code"]
    spend(conn, w, "1000", "2026-08-03", "x", a); spend(conn, w, "300", "2026-08-04", "y", b); spend(conn, w, "700", "2026-08-05", "z", a)
    cats = q.expenses_by_category(conn)
    assert sum(t for _n, t in cats) == q.expense_total(conn)[0] == P("2000") and cats[0][1] == P("1700")


def test_expenses_pdf_is_written(conn, w, tmp_path):
    spend(conn, w, "1000", "2026-08-03", "Tea & sugar <b>")
    e = spend(conn, w, "2500", "2026-08-10", "Oil")
    ac.reverse_entry(conn, w, e, "wrong")
    out = documents.expenses_pdf(conn, "2026-08-01", "2026-08-31", tmp_path / "sub" / "e.pdf")
    data = out.read_bytes()
    assert data.startswith(b"%PDF") and len(data) > 1000
    assert documents.expenses_pdf(conn, None, None, tmp_path / "all.pdf").read_bytes().startswith(b"%PDF")
    assert documents.expenses_pdf(conn, "2030-01-01", "2030-01-31", tmp_path / "none.pdf").read_bytes().startswith(b"%PDF")        # an empty period still prints
