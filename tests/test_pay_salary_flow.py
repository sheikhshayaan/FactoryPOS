"""One 'Pay salary' action: books the payslip if needed, applies allowances / deductions, and pays - atomically."""
import pytest

from services import accounting_service as ac, payroll_service as pr
from tests.test_attendance_flow import w  # noqa: F401
from services.units import from_paisa


def _emp(conn):
    return conn.execute("SELECT id FROM employees WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()[0]


def test_pay_without_a_payslip_books_it_and_pays_the_full_net(conn, w):
    e = _emp(conn)
    pid = pr.pay_salary_for_month(conn, w["owner"], e, "2026-10", on="2026-10-31", allowances="1000", deductions="500", deduction_notes="1 day")
    ps = conn.execute("SELECT * FROM payslips WHERE employee_id=? AND period='2026-10'", (e,)).fetchone()
    assert ps["allowances_paisa"] == 100000 and ps["deductions_paisa"] == 50000
    assert pr.payslip_balance(conn, ps["id"]) == 0 and pid


def test_pay_adjusts_an_auto_booked_unpaid_payslip(conn, w):
    e = _emp(conn)
    pr.auto_accrue_month(conn, w["owner"], "2026-10")
    before = conn.execute("SELECT * FROM payslips WHERE employee_id=?", (e,)).fetchone()
    pr.pay_salary_for_month(conn, w["owner"], e, "2026-10", amount="1000", on="2026-11-02", allowances="2000",
                            deductions=str(from_paisa(before["deductions_paisa"])), deduction_notes=before["deduction_notes"] or "")
    after = conn.execute("SELECT * FROM payslips WHERE employee_id=?", (e,)).fetchone()
    assert after["allowances_paisa"] == 200000 and after["net_paisa"] == before["net_paisa"] + 200000
    assert pr.payslip_balance(conn, after["id"]) == after["net_paisa"] - 100000
    assert conn.execute("SELECT COUNT(*) FROM payslip_revisions").fetchone()[0] == 1


def test_after_a_part_payment_the_figures_are_fixed(conn, w):
    e = _emp(conn)
    pr.pay_salary_for_month(conn, w["owner"], e, "2026-10", amount="1000", on="2026-10-31")
    with pytest.raises(pr.PayrollError):
        pr.pay_salary_for_month(conn, w["owner"], e, "2026-10", on="2026-11-01", allowances="999")


def test_a_failing_payment_leaves_no_payslip_behind(conn, w):
    e = _emp(conn)
    with pytest.raises(pr.PayrollError):
        pr.pay_salary_for_month(conn, w["owner"], e, "2026-10", amount="999999999", on="2026-10-31")
    assert conn.execute("SELECT COUNT(*) FROM payslips").fetchone()[0] == 0
    assert ac.balance(conn, "2300") == 0
