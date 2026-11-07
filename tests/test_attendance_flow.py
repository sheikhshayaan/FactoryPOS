"""IN/OUT buttons, 10-hour auto OUT, absent/leave, month sheet, estimated deduction, automatic accrual, payslip revision.
All expected numbers are worked by hand in the comments."""
from datetime import datetime, date

import pytest

from services import accounting_service as ac, auth_service as auth, payroll_service as pr
from services.units import to_paisa as P


def dt(s):
    return datetime.fromisoformat(s)


@pytest.fixture
def w(conn, owner):
    auth.create_user(conn, owner, "acc", "accountant-pw", "accountant")
    auth.create_user(conn, owner, "sup", "supervisor-pw", "accountant")
    auth.create_user(conn, owner, "aud", "auditor-pw1", "auditor")
    ac.record_opening_cash(conn, owner, "1000000", "2026-09-01")
    e1 = pr.add_employee(conn, owner, emp_code="E1", name="Bilal", monthly_salary="30000", effective_from="2026-01-01", joined_on="2026-01-01")
    e2 = pr.add_employee(conn, owner, emp_code="E2", name="Sana", monthly_salary="45000", effective_from="2026-01-01", joined_on="2026-01-01",
                         cost_category="admin")
    conn.execute("UPDATE employees SET created_at='2026-01-02T00:00:00Z'")        # they were added long ago
    return dict(owner=owner, acc=auth.authenticate(conn, "acc", "accountant-pw"),
                sup=auth.authenticate(conn, "sup", "supervisor-pw"), aud=auth.authenticate(conn, "aud", "auditor-pw1"), e1=e1, e2=e2)


def row(conn, eid, d):
    return conn.execute("SELECT * FROM attendance WHERE employee_id=? AND work_date=?", (eid, d)).fetchone()


# ---------------------------------------------------------------- IN / OUT
def test_in_then_out_stamps_times_and_hours(conn, w):
    pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-05T08:03:40"))              # seconds are dropped
    r = row(conn, w["e1"], "2026-10-05")
    assert r["status"] == "present" and r["clock_in"] == "2026-10-05T08:03" and r["clock_out"] is None
    res = pr.clock_out(conn, w["sup"], w["e1"], dt("2026-10-05T16:48"))
    assert res["minutes"] == 8 * 60 + 45 and not res["auto"]                       # 08:03 -> 16:48 = 8h45m = 525
    assert row(conn, w["e1"], "2026-10-05")["worked_minutes"] == 525


def test_cannot_in_twice_or_out_without_in_or_second_shift_same_day(conn, w):
    with pytest.raises(pr.PayrollError, match="not IN"):
        pr.clock_out(conn, w["sup"], w["e1"], dt("2026-10-05T09:00"))
    pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-05T08:00"))
    with pytest.raises(pr.PayrollError, match="already IN since 08:00"):
        pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-05T08:30"))
    pr.clock_out(conn, w["sup"], w["e1"], dt("2026-10-05T12:00"))
    with pytest.raises(pr.PayrollError, match="already finished"):
        pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-05T13:00"))


def test_out_in_the_same_minute_is_refused(conn, w):
    pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-05T08:00:10"))
    with pytest.raises(pr.PayrollError, match="less than a minute"):
        pr.clock_out(conn, w["sup"], w["e1"], dt("2026-10-05T08:00:50"))


def test_night_shift_across_midnight_belongs_to_the_day_it_started(conn, w):
    pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-05T22:00"))
    board = {b["id"]: b for b in pr.live_board(conn, dt("2026-10-06T02:00"))}
    assert board[w["e1"]]["state"] == "in" and board[w["e1"]]["minutes"] == 240    # still IN at 02:00 next day
    with pytest.raises(pr.PayrollError, match="already IN"):
        pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-06T02:00"))
    pr.clock_out(conn, w["sup"], w["e1"], dt("2026-10-06T06:00"))
    r = row(conn, w["e1"], "2026-10-05")
    assert r["worked_minutes"] == 8 * 60 and r["clock_out"] == "2026-10-06T06:00"
    assert row(conn, w["e1"], "2026-10-06") is None


# ---------------------------------------------------------------- 10 hour auto OUT
def test_forgotten_out_is_closed_at_exactly_ten_hours_and_flagged(conn, w):
    pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-05T08:00"))
    assert pr.auto_close_open(conn, dt("2026-10-05T17:59")) == []                  # 9h59m: still IN
    done = pr.auto_close_open(conn, dt("2026-10-05T18:00"))                        # 10h00m: closed
    assert len(done) == 1
    r = row(conn, w["e1"], "2026-10-05")
    assert r["clock_out"] == "2026-10-05T18:00" and r["worked_minutes"] == 600 and r["auto_out"] == 1
    assert "AUTO OUT" in r["notes"]
    assert conn.execute("SELECT username FROM audit_log WHERE action='attendance.auto_out'").fetchone()[0] == "system"
    assert pr.auto_close_open(conn, dt("2026-10-09T00:00")) == []                  # nothing left to close, idempotent


def test_auto_out_uses_in_plus_ten_hours_even_if_the_program_was_closed_for_days(conn, w):
    pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-05T08:00"))
    pr.auto_close_open(conn, dt("2026-10-09T09:15"))                               # opened again four days later
    r = row(conn, w["e1"], "2026-10-05")
    assert r["clock_out"] == "2026-10-05T18:00" and r["worked_minutes"] == 600     # NOT 4 days


def test_pressing_out_after_ten_hours_is_capped_and_flagged(conn, w):
    pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-05T08:00"))
    res = pr.clock_out(conn, w["sup"], w["e1"], dt("2026-10-05T21:30"))
    assert res["auto"] and res["minutes"] == 600 and res["clock_out"] == "2026-10-05T18:00"
    assert row(conn, w["e1"], "2026-10-05")["auto_out"] == 1


def test_clocking_in_next_day_first_closes_yesterdays_forgotten_shift(conn, w):
    pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-05T08:00"))
    pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-06T08:05"))
    assert row(conn, w["e1"], "2026-10-05")["auto_out"] == 1
    assert row(conn, w["e1"], "2026-10-06")["clock_in"] == "2026-10-06T08:05"
    assert [a["name"] for a in pr.recent_auto_closed(conn, dt("2026-10-06T09:00"))] == ["Bilal"]


# ---------------------------------------------------------------- absent / leave / undo
def test_absent_leave_switch_and_late_arrival(conn, w):
    pr.mark_status(conn, w["sup"], w["e1"], "absent", dt("2026-10-05T07:00"))
    pr.mark_status(conn, w["sup"], w["e1"], "leave", dt("2026-10-05T07:30"))       # switched
    assert row(conn, w["e1"], "2026-10-05")["status"] == "leave"
    pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-05T11:00"))                    # came after all
    r = row(conn, w["e1"], "2026-10-05")
    assert r["status"] == "present" and r["clock_in"] == "2026-10-05T11:00"
    with pytest.raises(pr.PayrollError, match="IN right now"):
        pr.mark_status(conn, w["sup"], w["e1"], "absent", dt("2026-10-05T12:00"))


def test_cannot_mark_absent_after_working(conn, w):
    pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-05T08:00"))
    pr.clock_out(conn, w["sup"], w["e1"], dt("2026-10-05T12:00"))
    with pytest.raises(pr.PayrollError, match="already has a present record"):
        pr.mark_status(conn, w["sup"], w["e1"], "absent", dt("2026-10-05T13:00"))


def test_undo_rules(conn, w):
    pr.mark_status(conn, w["sup"], w["e1"], "absent", dt("2026-10-05T07:00"))
    assert pr.clear_mark(conn, w["sup"], w["e1"], dt("2026-10-05T07:01")) == "absent"
    assert row(conn, w["e1"], "2026-10-05") is None
    pr.clock_in(conn, w["sup"], w["e2"], dt("2026-10-05T08:00"))                     # wrong person pressed IN
    pr.clear_mark(conn, w["sup"], w["e2"], dt("2026-10-05T08:10"))
    assert row(conn, w["e2"], "2026-10-05") is None
    pr.clock_in(conn, w["sup"], w["e2"], dt("2026-10-05T09:00"))
    with pytest.raises(pr.PayrollError, match="more than 15 minutes"):
        pr.clear_mark(conn, w["sup"], w["e2"], dt("2026-10-05T09:16"))
    pr.clock_out(conn, w["sup"], w["e2"], dt("2026-10-05T12:00"))
    with pytest.raises(pr.PayrollError, match="finished"):
        pr.clear_mark(conn, w["sup"], w["e2"], dt("2026-10-05T12:01"))


def test_buttons_need_attendance_permission(conn, w):
    with pytest.raises(auth.PermissionDenied):
        pr.clock_in(conn, w["aud"], w["e1"], dt("2026-10-05T08:00"))                 # an auditor only looks
    with pytest.raises(auth.PermissionDenied):
        pr.mark_status(conn, w["aud"], w["e1"], "absent", dt("2026-10-05T08:00"))


def test_live_board_states(conn, w):
    n = dt("2026-10-05T10:00")
    pr.clock_in(conn, w["sup"], w["e1"], dt("2026-10-05T08:30"))
    b = {x["id"]: x for x in pr.live_board(conn, n)}
    assert b[w["e1"]]["state"] == "in" and b[w["e1"]]["minutes"] == 90 and b[w["e1"]]["can_undo"] is False
    assert b[w["e2"]]["state"] == "idle"
    pr.mark_status(conn, w["sup"], w["e2"], "leave", n)
    pr.clock_out(conn, w["sup"], w["e1"], dt("2026-10-05T10:00"))
    b = {x["id"]: x for x in pr.live_board(conn, n)}
    assert b[w["e1"]]["state"] == "done" and b[w["e1"]]["minutes"] == 90
    assert b[w["e2"]]["state"] == "leave" and b[w["e2"]]["can_undo"] is True


# ---------------------------------------------------------------- month sheet and deduction estimate
def _work(conn, w, eid, day, i="08:00", o="16:00"):
    pr.clock_in(conn, w["sup"], eid, dt(f"2026-10-{day:02d}T{i}"))
    pr.clock_out(conn, w["sup"], eid, dt(f"2026-10-{day:02d}T{o}"))


def test_month_overview_counts_days_hours_and_estimates_deduction(conn, w):
    for d in (1, 2, 3):
        _work(conn, w, w["e1"], d)                                      # 3 x 8h = 1440 min
    _work(conn, w, w["e1"], 4, "09:00", "17:30")                        # 8h30 = 510 min
    for d in (5, 6):
        pr.mark_status(conn, w["sup"], w["e1"], "absent", dt(f"2026-10-{d:02d}T07:00"))
    pr.mark_status(conn, w["sup"], w["e1"], "leave", dt("2026-10-07T07:00"))
    pr.record_attendance(conn, w["sup"], w["e1"], "2026-10-08", "half_day")
    r = {x["id"]: x for x in pr.month_overview(conn, "2026-10")}[w["e1"]]
    assert (r["present"], r["absent"], r["leave"], r["half"]) == (4, 2, 1, 1)
    assert r["minutes"] == 1440 + 510
    # October has 31 days. units = 2 absent x2 + 1 half = 5 half-days; leave is paid -> 3,000,000 x 5 / 62 = 241,935.48 -> 241,935 paisa
    assert r["salary_paisa"] == 3_000_000 and r["est_deduction_paisa"] == 241_935
    assert r["est_net_paisa"] == 3_000_000 - 241_935
    assert "2 absent day(s)" in r["deduction_note"] and "1 half day(s)" in r["deduction_note"] and "leave" not in r["deduction_note"]
    assert r["status"] == "not accrued"


def test_unpaid_leave_rule_and_rules_off(conn, w):
    pr.mark_status(conn, w["sup"], w["e1"], "absent", dt("2026-10-05T07:00"))
    pr.mark_status(conn, w["sup"], w["e1"], "leave", dt("2026-10-06T07:00"))
    get = lambda: {x["id"]: x for x in pr.month_overview(conn, "2026-10")}[w["e1"]]["est_deduction_paisa"]   # noqa: E731
    assert get() == 96_774                                                         # 1 absent day = 3,000,000 / 31 = 96,774.19 -> 96,774
    conn.execute("UPDATE app_settings SET value='unpaid' WHERE key='leave_deduction'")
    assert get() == 193_548                                                        # 2 days = 6,000,000/31 = 193,548.39 -> 193,548
    conn.execute("UPDATE app_settings SET value='off' WHERE key='absence_deduction_basis'")
    assert get() == 0


def test_deduction_never_exceeds_salary(conn, w):
    for d in range(1, 32):
        pr.record_attendance(conn, w["sup"], w["e1"], f"2026-10-{d:02d}", "absent")
    r = {x["id"]: x for x in pr.month_overview(conn, "2026-10")}[w["e1"]]
    assert r["est_deduction_paisa"] == 3_000_000                                   # 31 of 31 days = the whole salary, not more


# ---------------------------------------------------------------- automatic accrual
def test_auto_accrual_books_previous_month_with_attendance_deduction(conn, w):
    for d in (5, 6):
        pr.mark_status(conn, w["sup"], w["e1"], "absent", dt(f"2026-10-{d:02d}T07:00"))
    res = pr.auto_accrue_if_due(conn, w["owner"], date(2026, 11, 1))
    assert res["period"] == "2026-10" and len(res["created"]) == 2 and res["skipped"] == []
    ps = {r["employee_id"]: r for r in conn.execute("SELECT * FROM payslips").fetchall()}
    # Bilal: 2 absent days = 4 half-days of 31 -> 3,000,000 x 4 / 62 = 193,548.39 -> 193,548 ; net 2,806,452
    assert ps[w["e1"]]["deductions_paisa"] == 193_548 and ps[w["e1"]]["net_paisa"] == 3_000_000 - 193_548
    assert "2 absent day(s)" in ps[w["e1"]]["deduction_notes"]
    assert ps[w["e2"]]["deductions_paisa"] == 0 and ps[w["e2"]]["net_paisa"] == 4_500_000
    # books: expense 2,806,452 direct + 4,500,000 admin ; payable the same
    assert ac.balance(conn, "5100") == 2_806_452 and ac.balance(conn, "6100") == 4_500_000
    assert ac.balance(conn, "2300") == 2_806_452 + 4_500_000
    assert conn.execute("SELECT details FROM audit_log WHERE action='salary.accrue' ORDER BY id LIMIT 1").fetchone()[0].count('"auto": true') == 1
    assert ac.check_integrity(conn) == []


def test_auto_accrual_runs_once_and_never_double_books(conn, w):
    pr.auto_accrue_if_due(conn, w["owner"], date(2026, 11, 1))
    assert pr.auto_accrue_if_due(conn, w["owner"], date(2026, 11, 2)) is None       # nothing left to do
    assert conn.execute("SELECT COUNT(*) FROM payslips").fetchone()[0] == 2
    assert ac.balance(conn, "2300") == 3_000_000 + 4_500_000


def test_auto_accrual_january_uses_previous_december():
    assert pr.previous_period(date(2027, 1, 1)) == "2026-12" and pr.previous_period(date(2026, 11, 30)) == "2026-10"


def test_auto_accrual_switch_and_permission(conn, w):
    assert pr.auto_accrue_if_due(conn, w["aud"], date(2026, 11, 1)) is None         # an auditor may not run payroll
    conn.execute("UPDATE app_settings SET value='0' WHERE key='auto_accrue_payroll'")
    assert pr.auto_accrue_if_due(conn, w["owner"], date(2026, 11, 1)) is None
    assert conn.execute("SELECT COUNT(*) FROM payslips").fetchone()[0] == 0
    with pytest.raises(auth.PermissionDenied):
        pr.auto_accrue_month(conn, w["aud"], "2026-10")                             # the manual 'accrue all' needs permission too


def test_auto_accrual_leaves_mid_month_joiners_to_the_owner(conn, w):
    late = pr.add_employee(conn, w["owner"], emp_code="E3", name="Rafi", monthly_salary="24000", effective_from="2026-10-15", joined_on="2026-10-15")
    conn.execute("UPDATE employees SET created_at='2026-10-15T00:00:00Z' WHERE id=?", (late,))
    new = pr.add_employee(conn, w["owner"], emp_code="E4", name="Newbie", monthly_salary="20000", effective_from="2026-11-01", joined_on="2026-11-01")
    res = pr.auto_accrue_if_due(conn, w["owner"], date(2026, 11, 1))
    assert len(res["created"]) == 2                                                 # only Bilal and Sana
    names = dict(res["skipped"])
    assert "accrue by hand" in names["Rafi"] and "Newbie" in names
    assert conn.execute("SELECT COUNT(*) FROM payslips WHERE employee_id IN (?,?)", (late, new)).fetchone()[0] == 0


def test_nothing_is_prorated_for_a_mid_month_joiner(conn, w):
    e = pr.add_employee(conn, w["owner"], emp_code="E5", name="Late", monthly_salary="30000", effective_from="2026-10-30", joined_on="2026-10-30")
    conn.execute("UPDATE employees SET created_at='2026-10-30T00:00:00Z' WHERE id=?", (e,))
    r = next(x for x in pr.month_overview(conn, "2026-10") if x["id"] == e)
    assert r["manual_reason"] and "part_basic_paisa" not in r and "part_days" not in r      # left for the owner; no days x salary guess
    slip = pr.accrue_salary(conn, w["owner"], e, "2026-10")                                 # accrued when the owner chooses: the full salary unless changed
    assert conn.execute("SELECT basic_paisa FROM payslips WHERE id=?", (slip,)).fetchone()[0] == 3_000_000


def test_month_overview_shows_status_and_payments(conn, w):
    pr.auto_accrue_if_due(conn, w["owner"], date(2026, 11, 1))
    slip = conn.execute("SELECT id FROM payslips WHERE employee_id=?", (w["e1"],)).fetchone()[0]
    pr.pay_salary(conn, w["owner"], slip, "10000", "2026-11-03")
    r = {x["id"]: x for x in pr.month_overview(conn, "2026-10")}
    assert r[w["e1"]]["status"] == "partial" and r[w["e1"]]["due_paisa"] == 3_000_000 - 1_000_000
    assert r[w["e2"]]["status"] == "unpaid"


# ---------------------------------------------------------------- revising a payslip
def test_revise_unpaid_payslip_reverses_and_reposts(conn, w):
    pr.accrue_salary(conn, w["owner"], w["e1"], "2026-10")                           # 30,000 net
    slip = conn.execute("SELECT id, entry_id FROM payslips").fetchone()
    assert ac.balance(conn, "5100") == 3_000_000
    pr.revise_payslip(conn, w["owner"], slip["id"], "Bilal was absent 2 days", basic="30000", deductions="1935.48",
                      deduction_notes="2 absent days")
    p = conn.execute("SELECT * FROM payslips WHERE id=?", (slip["id"],)).fetchone()
    assert p["deductions_paisa"] == 193_548 and p["net_paisa"] == 2_806_452 and p["entry_id"] != slip["entry_id"]
    assert ac.balance(conn, "5100") == 2_806_452 and ac.balance(conn, "2300") == 2_806_452      # old entry reversed, not edited
    assert conn.execute("SELECT COUNT(*) FROM journal_entries WHERE reverses_entry_id=?", (slip["entry_id"],)).fetchone()[0] == 1
    rev = conn.execute("SELECT * FROM payslip_revisions").fetchone()
    assert rev["reason"] == "Bilal was absent 2 days" and '"net_paisa": 3000000' in rev["old_values"]
    pr.revise_payslip(conn, w["owner"], slip["id"], "second thoughts", basic="30000")            # can be revised again
    assert ac.balance(conn, "2300") == 3_000_000 and ac.check_integrity(conn) == []


def test_revise_is_refused_after_a_payment_or_without_reason(conn, w):
    pr.accrue_salary(conn, w["owner"], w["e1"], "2026-10")
    slip = conn.execute("SELECT id FROM payslips").fetchone()[0]
    with pytest.raises(pr.PayrollError, match="reason"):
        pr.revise_payslip(conn, w["owner"], slip, " ", basic="30000")
    pr.pay_salary(conn, w["owner"], slip, "500", "2026-11-01")
    with pytest.raises(pr.PayrollError, match="already has a payment"):
        pr.revise_payslip(conn, w["owner"], slip, "late fix", basic="29000")
    assert ac.balance(conn, "2300") == 3_000_000 - 50_000


def test_failed_revision_changes_nothing(conn, w):
    pr.accrue_salary(conn, w["owner"], w["e1"], "2026-10")
    slip = conn.execute("SELECT id FROM payslips").fetchone()[0]
    with pytest.raises(pr.PayrollError, match="exceed"):
        pr.revise_payslip(conn, w["owner"], slip, "typo", basic="1000", deductions="5000", deduction_notes="x")
    assert ac.balance(conn, "2300") == 3_000_000 and conn.execute("SELECT COUNT(*) FROM payslip_revisions").fetchone()[0] == 0


def test_revise_with_advance_recovery_restores_advance_first(conn, w):
    pr.give_advance(conn, w["owner"], w["e1"], "5000", "2026-10-02")
    pr.accrue_salary(conn, w["owner"], w["e1"], "2026-10", advance_recovery="5000")             # net 25,000, advance cleared
    slip = conn.execute("SELECT id FROM payslips").fetchone()[0]
    assert pr.advance_balance(conn, w["e1"]) == 0
    pr.revise_payslip(conn, w["owner"], slip, "recover only part", basic="30000", advance_recovery="2000")
    assert pr.advance_balance(conn, w["e1"]) == 300_000 and conn.execute("SELECT net_paisa FROM payslips").fetchone()[0] == 2_800_000
