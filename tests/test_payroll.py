import pytest

from services import accounting_service as ac, auth_service as auth, payroll_service as pr
from services.units import to_paisa as P


@pytest.fixture
def w(conn, owner):
    auth.create_user(conn, owner, "acc", "accountant-pw", "accountant")
    auth.create_user(conn, owner, "sup", "supervisor-pw", "accountant")
    auth.create_user(conn, owner, "aud", "auditor-pw1", "auditor")
    ac.record_opening_cash(conn, owner, "1000000", "2026-09-01")
    e1 = pr.add_employee(conn, owner, emp_code="E1", name="Bilal", monthly_salary="30000", effective_from="2026-01-01")
    e2 = pr.add_employee(conn, owner, emp_code="E2", name="Sana", monthly_salary="45000", effective_from="2026-01-01",
                         cost_category="admin")
    return dict(owner=owner, acc=auth.authenticate(conn, "acc", "accountant-pw"),
                sup=auth.authenticate(conn, "sup", "supervisor-pw"), aud=auth.authenticate(conn, "aud", "auditor-pw1"), e1=e1, e2=e2)


# ---------- attendance ----------
def test_overnight_shift_hours():
    assert pr.worked_minutes("2026-10-01T22:00", "2026-10-02T06:00") == 8 * 60
    assert pr.worked_minutes("2026-10-01T22:00", "2026-10-02T06:30", 30) == 8 * 60
    assert pr.worked_minutes("2026-12-31T20:00", "2027-01-01T04:00") == 8 * 60      # across year end
    with pytest.raises(pr.PayrollError):
        pr.worked_minutes("2026-10-01T22:00", "2026-10-01T06:00")                    # out before in
    with pytest.raises(pr.PayrollError):
        pr.worked_minutes("2026-10-01T08:00", "2026-10-01T09:00", 60)


def test_attendance_unique_and_correction_history(conn, w):
    a = pr.record_attendance(conn, w["sup"], w["e1"], "2026-10-01", "present", shift="night",
                             clock_in="2026-10-01T22:00", clock_out="2026-10-02T05:00", break_minutes=30)
    assert conn.execute("SELECT worked_minutes FROM attendance WHERE id=?", (a,)).fetchone()[0] == 390
    with pytest.raises(pr.PayrollError):
        pr.record_attendance(conn, w["sup"], w["e1"], "2026-10-01", "present")
    with pytest.raises(auth.PermissionDenied):                                       # an auditor cannot change records
        pr.correct_attendance(conn, w["aud"], a, "typo", clock_out="2026-10-02T06:00")
    with pytest.raises(pr.PayrollError):
        pr.correct_attendance(conn, w["acc"], a, "", clock_out="2026-10-02T06:00")
    pr.correct_attendance(conn, w["acc"], a, "forgot to clock out", clock_out="2026-10-02T06:00")
    assert conn.execute("SELECT worked_minutes FROM attendance WHERE id=?", (a,)).fetchone()[0] == 450
    assert conn.execute("SELECT COUNT(*) FROM attendance_corrections").fetchone()[0] == 1


def test_absent_cannot_have_clock_times(conn, w):
    with pytest.raises(pr.PayrollError):
        pr.record_attendance(conn, w["sup"], w["e1"], "2026-10-02", "absent", clock_in="2026-10-02T08:00")


def test_no_overtime_pay_even_with_long_hours(conn, w):
    pr.record_attendance(conn, w["sup"], w["e1"], "2026-10-03", "present", clock_in="2026-10-03T06:00", clock_out="2026-10-03T22:00")
    s = pr.accrue_salary(conn, w["acc"], w["e1"], "2026-10")
    assert conn.execute("SELECT net_paisa FROM payslips WHERE id=?", (s,)).fetchone()[0] == P("30000")


def test_absence_deduction_on_by_default_and_can_be_switched_off(conn, w):
    for d in ("2026-10-01", "2026-10-02"):
        pr.record_attendance(conn, w["sup"], w["e1"], d, "absent")
    pr.record_attendance(conn, w["sup"], w["e1"], "2026-10-05", "half_day")
    pr.record_attendance(conn, w["sup"], w["e1"], "2026-10-06", "leave")
    # default: 2 absent + 1 half day = 5 half-days of 31 days; leave is paid -> 30000 x 5 / 62 = 2419.35
    assert pr.suggest_absence_deduction(conn, w["e1"], "2026-10") == P("30000") * 5 // (2 * 31)
    conn.execute("UPDATE app_settings SET value='off' WHERE key='absence_deduction_basis'")
    assert pr.suggest_absence_deduction(conn, w["e1"], "2026-10") == 0


# ---------- payroll accounting ----------
def test_accrual_books_expense_payable_once_payment_not_expense(conn, w):
    s = pr.accrue_salary(conn, w["acc"], w["e1"], "2026-10")
    assert ac.balance(conn, "5100") == P("30000") and ac.balance(conn, "2300") == P("30000")
    pr.pay_salary(conn, w["acc"], s, "10000", "2026-11-01", reference="adv-1")
    pr.pay_salary(conn, w["acc"], s, "20000", "2026-11-05")
    assert ac.balance(conn, "5100") == P("30000")                                    # expense unchanged
    assert ac.balance(conn, "2300") == 0 and pr.payslip_status(conn, s) == "paid"
    assert ac.balance(conn, "1000") == P("1000000") - P("30000")
    assert ac.check_integrity(conn) == []


def test_partial_status_overpay_and_duplicate(conn, w):
    s = pr.accrue_salary(conn, w["acc"], w["e1"], "2026-10")
    pr.pay_salary(conn, w["acc"], s, "5000", "2026-11-01", idem_key="p1")
    assert pr.payslip_status(conn, s) == "partial"
    with pytest.raises(pr.PayrollError):
        pr.pay_salary(conn, w["acc"], s, "5000", "2026-11-01", idem_key="p1")
    with pytest.raises(pr.PayrollError):
        pr.pay_salary(conn, w["acc"], s, "25000.01", "2026-11-02")
    assert pr.payslip_balance(conn, s) == P("25000")


def test_one_payslip_per_period_atomic(conn, w):
    pr.accrue_salary(conn, w["acc"], w["e1"], "2026-10")
    n = conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0]
    with pytest.raises(pr.PayrollError):
        pr.accrue_salary(conn, w["acc"], w["e1"], "2026-10")
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == n   # no orphan entry


def test_admin_vs_direct_accounts(conn, w):
    pr.accrue_salary(conn, w["acc"], w["e2"], "2026-10")
    assert ac.balance(conn, "6100") == P("45000") and ac.balance(conn, "5100") == 0


def test_advance_and_recovery(conn, w):
    pr.give_advance(conn, w["acc"], w["e1"], "8000", "2026-10-10")
    assert pr.advance_balance(conn, w["e1"]) == P("8000")
    assert ac.income_statement(conn, "2026-10-01", "2026-10-31")["net_profit"] == 0     # advance is not an expense
    with pytest.raises(pr.PayrollError):
        pr.accrue_salary(conn, w["acc"], w["e1"], "2026-10", advance_recovery="9000")
    s = pr.accrue_salary(conn, w["acc"], w["e1"], "2026-10", advance_recovery="5000")
    assert conn.execute("SELECT net_paisa FROM payslips WHERE id=?", (s,)).fetchone()[0] == P("25000")
    assert pr.advance_balance(conn, w["e1"]) == P("3000")
    assert ac.balance(conn, "5100") == P("30000")                                       # full salary cost, once
    assert ac.check_integrity(conn) == []


def test_deductions_need_notes_and_reduce_expense(conn, w):
    with pytest.raises(pr.PayrollError):
        pr.accrue_salary(conn, w["acc"], w["e1"], "2026-10", deductions="1000")
    s = pr.accrue_salary(conn, w["acc"], w["e1"], "2026-10", deductions="1000", deduction_notes="2 days absent", allowances="500")
    assert conn.execute("SELECT net_paisa FROM payslips WHERE id=?", (s,)).fetchone()[0] == P("29500")
    assert ac.balance(conn, "5100") == P("29500")


def test_part_month_override_needs_reason(conn, w):
    with pytest.raises(pr.PayrollError):
        pr.accrue_salary(conn, w["acc"], w["e1"], "2026-10", basic_override="15000")
    s = pr.accrue_salary(conn, w["acc"], w["e1"], "2026-10", basic_override="15000", override_reason="joined mid-month")
    assert conn.execute("SELECT net_paisa FROM payslips WHERE id=?", (s,)).fetchone()[0] == P("15000")


def test_salary_history_dated_and_audited(conn, w):
    pr.change_salary(conn, w["acc"], w["e1"], "35000", "2026-11-01", "annual raise")
    assert pr.salary_on(conn, w["e1"], "2026-10-31") == P("30000") and pr.salary_on(conn, w["e1"], "2026-11-30") == P("35000")
    with pytest.raises(pr.PayrollError):
        pr.change_salary(conn, w["acc"], w["e1"], "1", "2026-12-01", "")
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='salary.change'").fetchone()[0] == 1


def test_auditor_cannot_run_payroll(conn, w):
    with pytest.raises(auth.PermissionDenied):
        pr.accrue_salary(conn, w["aud"], w["e1"], "2026-10")


def test_payroll_blocked_by_no_cash_but_accrual_ok(conn, w):
    s = pr.accrue_salary(conn, w["acc"], w["e1"], "2026-10")
    ac.record_expense(conn, w["acc"], account_code="6000", amount="990000", on="2026-10-20", description="big")
    with pytest.raises(ac.InsufficientCash):
        pr.pay_salary(conn, w["acc"], s, "30000", "2026-11-01")
    assert pr.payslip_balance(conn, s) == P("30000")


def test_accruing_the_running_month_books_it_today_so_paying_today_never_makes_payable_negative(conn, owner):
    from datetime import date
    today = date.today().isoformat()
    period = today[:7]
    ac.record_opening_cash(conn, owner, "500000", "2026-01-01")
    e = pr.add_employee(conn, owner, emp_code="E9", name="Zubair", monthly_salary="40000", effective_from="2026-01-01")
    ps = pr.accrue_salary(conn, owner, e, period)                                    # no date given: the month may not be over yet
    assert conn.execute("SELECT je.entry_date FROM payslips p JOIN journal_entries je ON je.id=p.entry_id WHERE p.id=?", (ps,)).fetchone()[0] <= today
    pr.pay_salary(conn, owner, ps, "40000", today)
    assert ac.balance(conn, "2300") == 0                                           # was -40,000 when the expense was dated month-end
    assert ac.balance(conn, "5100", as_of=today) + ac.balance(conn, "6100", as_of=today) == 4000000   # the expense is already in today's figures


def test_a_salary_payment_cannot_be_dated_before_the_salary_was_booked(conn, owner):
    ac.record_opening_cash(conn, owner, "500000", "2026-01-01")
    e = pr.add_employee(conn, owner, emp_code="E8", name="Hamid", monthly_salary="40000", effective_from="2026-01-01")
    ps = pr.accrue_salary(conn, owner, e, "2026-07")                                 # booked on 31 July
    with pytest.raises(pr.PayrollError, match="booked on 2026-07-31"):
        pr.pay_salary(conn, owner, ps, "1000", "2026-07-15")
    pr.pay_salary(conn, owner, ps, "1000", "2026-07-31")
