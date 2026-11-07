"""Employees, attendance and monthly payroll.

Accounting
  accrual  : Dr Direct Labor (5100) / Admin Salaries (6100)  = basic + allowances - deductions
             Cr Salaries Payable (2300)                      = net pay
             Cr Employee Advances (1300)                     = advance recovered
  payment  : Dr Salaries Payable / Cr Cash   (NO expense - the expense was booked at accrual)
Rules (configurable, off by default - nothing is assumed): no overtime pay; absence deductions only when
the setting 'absence_deduction_basis' = 'calendar_days'; no tax/statutory deductions.
"""
from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import date, datetime, timedelta

from database.connection import claim_idempotency_key, transaction, utc_now
from services import accounting_service as acct, audit_service
from services.auth_service import require_permission
from services import settings_service
from services.units import from_paisa, mul_div_round, to_paisa


class PayrollError(Exception):
    pass


# ------------------------------------------------------------------ employees
def add_employee(conn, actor, *, emp_code, name, monthly_salary, effective_from, job_role="", phone="",
                 joined_on=None, default_shift="day", cost_category="direct") -> int:
    require_permission(actor, "manage_payroll")
    sal = to_paisa(monthly_salary)
    if sal < 0:
        raise PayrollError("Salary cannot be negative.")
    with transaction(conn):
        try:
            cur = conn.execute(
                "INSERT INTO employees(emp_code,name,phone,job_role,joined_on,default_shift,cost_category,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (emp_code.strip(), name.strip(), phone, job_role, joined_on, default_shift, cost_category, utc_now()))
        except sqlite3.IntegrityError as e:
            raise PayrollError(f"Cannot add employee: {e}") from e
        conn.execute("INSERT INTO salary_history(employee_id,effective_from,monthly_paisa,reason,set_by,created_at) "
                     "VALUES (?,?,?,?,?,?)", (cur.lastrowid, effective_from, sal, "initial", actor["id"], utc_now()))
        audit_service.log(conn, "employee.add", user=actor, entity="employee", entity_id=cur.lastrowid)
        return cur.lastrowid


EXIT_KINDS = {"resigned": "Resigned", "removed": "Removed by the factory", "other": "Other"}


def end_employment(conn, actor, employee_id, left_on: str, kind: str, reason: str = "") -> dict:
    """Resignation / removal. The employee stays on file (attendance, payslips, advances stay readable) but is no longer active: they leave the
    Today board and no salary is accrued automatically. Their last month can still be accrued and paid by hand. Nothing is deleted or reversed.
    Returns what the owner may still need to settle: unpaid salary, advance outstanding, months not accrued yet."""
    require_permission(actor, "manage_payroll")
    if kind not in EXIT_KINDS:
        raise PayrollError("Choose whether the employee resigned or was removed.")
    try:
        d = date.fromisoformat(left_on)
    except (TypeError, ValueError):
        raise PayrollError("Choose the last working day.") from None
    if d > date.today():
        raise PayrollError("The last working day cannot be in the future. Record it on that day.")
    with transaction(conn):
        emp = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
        if not emp:
            raise PayrollError("Unknown employee.")
        if not emp["is_active"]:
            raise PayrollError("This employee has already left.")
        if emp["joined_on"] and left_on < emp["joined_on"]:
            raise PayrollError(f"The last working day is before the joining date ({emp['joined_on']}).")
        if _open_shift(conn, employee_id):
            raise PayrollError("This employee is clocked in right now. Press OUT for them first (Today board), then record the exit.")
        conn.execute("UPDATE employees SET is_active=0, left_on=?, exit_kind=?, exit_reason=? WHERE id=?", (left_on, kind, (reason or "").strip(), employee_id))
        audit_service.log(conn, "employee.exit", user=actor, entity="employee", entity_id=employee_id, reason=(reason or "").strip() or None,
                          details={"left_on": left_on, "kind": kind})
        slips = conn.execute("SELECT p.id, p.net_paisa - COALESCE((SELECT SUM(amount_paisa) FROM payslip_payments WHERE payslip_id=p.id),0) AS due "
                             "FROM payslips p WHERE p.employee_id=?", (employee_id,)).fetchall()
        has_last = conn.execute("SELECT 1 FROM payslips WHERE employee_id=? AND period=?", (employee_id, left_on[:7])).fetchone() is not None
        return {"unpaid_paisa": sum(r["due"] for r in slips if r["due"] > 0), "advance_paisa": advance_balance(conn, employee_id),
                "last_month": left_on[:7], "last_month_accrued": has_last}


def reinstate_employee(conn, actor, employee_id, reason: str) -> None:
    """Undo an exit (e.g. recorded by mistake, or the person came back). Needs a reason; it is audited."""
    require_permission(actor, "manage_payroll")
    if not (reason or "").strip():
        raise PayrollError("A reason is required to bring an employee back.")
    with transaction(conn):
        emp = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
        if not emp or emp["is_active"]:
            raise PayrollError("This employee is already active.")
        conn.execute("UPDATE employees SET is_active=1, left_on=NULL, exit_kind=NULL, exit_reason=NULL WHERE id=?", (employee_id,))
        audit_service.log(conn, "employee.reinstate", user=actor, entity="employee", entity_id=employee_id, reason=reason.strip(),
                          details={"was_left_on": emp["left_on"], "was": emp["exit_kind"]})


def change_salary(conn, actor, employee_id, new_monthly, effective_from, reason):
    require_permission(actor, "manage_payroll")
    if not reason:
        raise PayrollError("A reason is required for salary changes.")
    with transaction(conn):
        try:
            conn.execute("INSERT INTO salary_history(employee_id,effective_from,monthly_paisa,reason,set_by,created_at) "
                         "VALUES (?,?,?,?,?,?)",
                         (employee_id, effective_from, to_paisa(new_monthly), reason, actor["id"], utc_now()))
        except sqlite3.IntegrityError as e:
            raise PayrollError("A salary is already set for that effective date.") from e
        audit_service.log(conn, "salary.change", user=actor, entity="employee", entity_id=employee_id,
                          reason=reason, details={"monthly_paisa": to_paisa(new_monthly), "from": effective_from})


def salary_on(conn, employee_id, on: str) -> int:
    r = conn.execute("SELECT monthly_paisa FROM salary_history WHERE employee_id=? AND effective_from<=? "
                     "ORDER BY effective_from DESC LIMIT 1", (employee_id, on)).fetchone()
    if not r:
        raise PayrollError("No salary effective on that date.")
    return r[0]


# ------------------------------------------------------------------ attendance
def worked_minutes(clock_in: str, clock_out: str, break_minutes: int = 0) -> int:
    """Minutes worked; clock_out may be on the next day (overnight shift). Max 24h span."""
    a, b = datetime.fromisoformat(clock_in), datetime.fromisoformat(clock_out)
    span = int((b - a).total_seconds() // 60)
    if span <= 0:
        raise PayrollError("Clock-out must be after clock-in (give the real next-day date for night shifts).")
    if span > 24 * 60:
        raise PayrollError("A single shift cannot exceed 24 hours.")
    if break_minutes < 0 or break_minutes >= span:
        raise PayrollError("Break must be shorter than the shift.")
    return span - break_minutes


def record_attendance(conn, actor, employee_id, work_date, status, *, shift=None, clock_in=None, clock_out=None,
                      break_minutes=0, notes="") -> int:
    require_permission(actor, "manage_attendance")
    wm = 0
    if status in ("present", "half_day") and clock_in and clock_out:
        wm = worked_minutes(clock_in, clock_out, break_minutes)
    elif status in ("absent", "leave") and (clock_in or clock_out):
        raise PayrollError("Absent/leave records cannot have clock times.")
    date.fromisoformat(work_date)
    with transaction(conn):
        try:
            cur = conn.execute(
                "INSERT INTO attendance(employee_id,work_date,status,shift,clock_in,clock_out,break_minutes,worked_minutes,"
                "notes,recorded_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (employee_id, work_date, status, shift, clock_in, clock_out, break_minutes, wm, notes, actor["id"]))
        except sqlite3.IntegrityError as e:
            raise PayrollError("Attendance already recorded for that day (use a correction).") from e
        return cur.lastrowid


def correct_attendance(conn, actor, attendance_id, reason, **changes) -> None:
    """Approved correction: keeps the old values in attendance_corrections."""
    require_permission(actor, "manage_payroll")
    if not reason:
        raise PayrollError("A reason is required.")
    allowed = {"status", "shift", "clock_in", "clock_out", "break_minutes", "notes"}
    if not changes or set(changes) - allowed:
        raise PayrollError(f"Changeable fields: {sorted(allowed)}")
    with transaction(conn):
        old = conn.execute("SELECT * FROM attendance WHERE id=?", (attendance_id,)).fetchone()
        if not old:
            raise PayrollError("Attendance record not found.")
        new = dict(old) | changes
        wm = 0
        if new["status"] in ("present", "half_day") and new["clock_in"] and new["clock_out"]:
            wm = worked_minutes(new["clock_in"], new["clock_out"], new["break_minutes"])
        auto = old["auto_out"] if (new["clock_out"] == old["clock_out"] and new["status"] == old["status"]) else 0   # a human fixed it: no warning
        conn.execute("UPDATE attendance SET status=?,shift=?,clock_in=?,clock_out=?,break_minutes=?,worked_minutes=?,notes=?,auto_out=? "
                     "WHERE id=?", (new["status"], new["shift"], new["clock_in"], new["clock_out"], new["break_minutes"],
                                    wm, new["notes"], auto, attendance_id))
        conn.execute("INSERT INTO attendance_corrections(attendance_id,old_values,new_values,reason,approved_by,created_at) "
                     "VALUES (?,?,?,?,?,?)", (attendance_id, json.dumps(dict(old)), json.dumps(new), reason, actor["id"], utc_now()))
        audit_service.log(conn, "attendance.correct", user=actor, entity="attendance", entity_id=attendance_id, reason=reason)


def month_summary(conn, employee_id, period: str) -> dict:
    rows = conn.execute("SELECT status, COUNT(*) n, COALESCE(SUM(worked_minutes),0) m FROM attendance "
                        "WHERE employee_id=? AND substr(work_date,1,7)=? GROUP BY status", (employee_id, period)).fetchall()
    d = {r["status"]: r["n"] for r in rows}
    return {"present": d.get("present", 0), "half_day": d.get("half_day", 0), "absent": d.get("absent", 0),
            "leave": d.get("leave", 0), "worked_minutes": sum(r["m"] for r in rows)}


def _rules(conn) -> tuple[bool, bool]:
    """(absence deduction on?, leave is unpaid?) from the settings."""
    return (settings_service.get(conn, "absence_deduction_basis") == "calendar_days",
            settings_service.get(conn, "leave_deduction") == "unpaid")


def deduction_units(absent: int, half_day: int, leave: int, leave_unpaid: bool) -> int:
    """Deduction measured in half-days: an absent day = 2, a half day = 1, an unpaid leave day = 2. Paid leave = 0."""
    return absent * 2 + half_day + (leave * 2 if leave_unpaid else 0)


def estimate_deduction(salary_paisa: int, dim: int, absent: int, half_day: int, leave: int, leave_unpaid: bool) -> int:
    """salary / days-in-month per deducted day, integer half-up. Never more than the salary."""
    units = deduction_units(absent, half_day, leave, leave_unpaid)
    return min(salary_paisa, mul_div_round(salary_paisa, units, 2 * dim))


def deduction_text(dim: int, absent: int, half_day: int, leave: int, leave_unpaid: bool) -> str:
    parts = []
    if absent:
        parts.append(f"{absent} absent day(s)")
    if half_day:
        parts.append(f"{half_day} half day(s)")
    if leave and leave_unpaid:
        parts.append(f"{leave} unpaid leave day(s)")
    return ("Estimated from attendance: " + ", ".join(parts) + f" (salary / {dim} days)") if parts else ""


def suggest_absence_deduction(conn, employee_id, period: str) -> int:
    """0 when the absence rule is switched off. Half-day counts 0.5; leave counts only when the owner made it unpaid."""
    on, leave_unpaid = _rules(conn)
    if not on:
        return 0
    y, m = int(period[:4]), int(period[5:])
    dim = calendar.monthrange(y, m)[1]
    salary = salary_on(conn, employee_id, f"{period}-{dim:02d}")
    s = month_summary(conn, employee_id, period)
    return estimate_deduction(salary, dim, s["absent"], s["half_day"], s["leave"], leave_unpaid)


# ------------------------------------------------------------------ advances / payroll
def advance_balance(conn, employee_id) -> int:
    return conn.execute(
        "SELECT COALESCE(SUM(l.debit_paisa)-SUM(l.credit_paisa),0) FROM journal_lines l JOIN accounts a ON a.id=l.account_id "
        "WHERE a.code='1300' AND l.employee_id=?", (employee_id,)).fetchone()[0]


def give_advance(conn, actor, employee_id, amount, on, idem_key=None) -> int:
    require_permission(actor, "manage_payroll")
    p = to_paisa(amount)
    if p <= 0:
        raise PayrollError("Amount must be positive.")
    with transaction(conn):
        if idem_key and not claim_idempotency_key(conn, idem_key, "give_advance"):
            raise PayrollError("This advance was already recorded.")
        _employee(conn, employee_id)                       # no advance for someone who has left (or does not exist)
        e = acct.post(conn, entry_date=on, description="Salary advance", user=actor, source_type="employee_advance",
                      lines=[{"account": "1300", "debit": p, "employee_id": employee_id}, {"account": "1000", "credit": p}])
        audit_service.log(conn, "salary.advance", user=actor, entity="employee", entity_id=employee_id, details={"paisa": p})
        return e


def _figures(conn, emp, on, basic, allow, ded, rec, deduction_notes):
    """Validate one payslip and return (net, journal lines). Shared by accrual and revision."""
    if min(allow, ded, rec) < 0:
        raise PayrollError("Amounts cannot be negative.")
    if ded and not deduction_notes:
        raise PayrollError("Deductions need a note (reason).")
    if rec > advance_balance(conn, emp["id"]):
        raise PayrollError("Recovery exceeds the employee's outstanding advance.")
    gross = basic + allow
    if ded > gross:
        raise PayrollError("Deductions exceed gross pay.")
    net = gross - ded - rec
    if net < 0:
        raise PayrollError("Net pay cannot be negative.")
    expense = gross - ded
    lines = []
    if expense > 0:
        lines.append({"account": "5100" if emp["cost_category"] == "direct" else "6100", "debit": expense})
    if net > 0:
        lines.append({"account": "2300", "credit": net, "employee_id": emp["id"]})
    if rec > 0:
        lines.append({"account": "1300", "credit": rec, "employee_id": emp["id"]})
    if len(lines) < 2:
        raise PayrollError("Nothing to post for this payslip.")
    return net, lines


def accrue_salary(conn, actor, employee_id, period: str, *, allowances="0", deductions="0", deduction_notes="",
                  advance_recovery="0", basic_override=None, override_reason="", on: str | None = None,
                  auto: bool = False) -> int:
    """Create the month's payslip and book the expense + payable. One per employee per period."""
    require_permission(actor, "manage_payroll")
    y, m = int(period[:4]), int(period[5:])
    dim = calendar.monthrange(y, m)[1]
    month_end = f"{period}-{dim:02d}"
    # The salary is booked on the last day of its month - but never in the future: accruing the month that is still running books it
    # TODAY, so a payment made today can never come before the expense it pays (that showed up as a negative "salaries payable").
    rate_date = on or month_end
    on = on or min(month_end, date.today().isoformat())
    allow, ded, rec = to_paisa(allowances), to_paisa(deductions), to_paisa(advance_recovery)
    with transaction(conn):
        emp = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
        if not emp:
            raise PayrollError("Unknown employee.")
        if not emp["is_active"] and period > (emp["left_on"] or "")[:7]:       # someone who has left is still paid up to the month they left
            raise PayrollError("This employee has left: no salary is accrued after the month they left.")
        if basic_override is not None:
            if not override_reason:
                raise PayrollError("A reason is required to override the basic salary (e.g. an agreed amount).")
            basic = to_paisa(basic_override)
        else:
            basic = salary_on(conn, employee_id, rate_date)
        net, lines = _figures(conn, emp, on, basic, allow, ded, rec, deduction_notes)
        try:
            e = acct.post(conn, entry_date=on, description=f"Salary {period} {emp['emp_code']}", user=actor,
                          source_type="payslip", source_id=f"{emp['emp_code']}:{period}", lines=lines)
            cur = conn.execute(
                "INSERT INTO payslips(employee_id,period,basic_paisa,allowances_paisa,deductions_paisa,advance_recovered_paisa,"
                "net_paisa,deduction_notes,entry_id,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (employee_id, period, basic, allow, ded, rec, net, deduction_notes, e, actor["id"], utc_now()))
        except sqlite3.IntegrityError as ex:
            raise PayrollError("A payslip already exists for this employee and period.") from ex
        audit_service.log(conn, "salary.accrue", user=actor, entity="payslip", entity_id=cur.lastrowid,
                          details={"basic": basic, "net": net, "override": basic_override is not None, "auto": auto},
                          reason=override_reason or ("automatic accrual at month start" if auto else None))
        return cur.lastrowid


def revise_payslip(conn, actor, payslip_id: int, reason: str, *, basic, allowances="0", deductions="0",
                   deduction_notes="", advance_recovery="0") -> int:
    """Correct an UNPAID payslip: its journal entry is reversed and a new one posted (nothing is edited or deleted);
    both old and new figures are kept in payslip_revisions."""
    require_permission(actor, "manage_payroll")
    if not (reason or "").strip():
        raise PayrollError("A reason is required to revise a payslip.")
    new_basic, allow, ded, rec = to_paisa(basic), to_paisa(allowances), to_paisa(deductions), to_paisa(advance_recovery)
    if new_basic < 0:
        raise PayrollError("Basic salary cannot be negative.")
    with transaction(conn):
        ps = conn.execute("SELECT * FROM payslips WHERE id=?", (payslip_id,)).fetchone()
        if not ps:
            raise PayrollError("Payslip not found.")
        if conn.execute("SELECT 1 FROM payslip_payments WHERE payslip_id=?", (payslip_id,)).fetchone():
            raise PayrollError("This payslip already has a payment, so it can no longer be revised.")
        emp = conn.execute("SELECT * FROM employees WHERE id=?", (ps["employee_id"],)).fetchone()
        old = conn.execute("SELECT * FROM journal_entries WHERE id=?", (ps["entry_id"],)).fetchone()
        old_lines = conn.execute("SELECT j.*, a.code FROM journal_lines j JOIN accounts a ON a.id=j.account_id "
                                 "WHERE entry_id=?", (old["id"],)).fetchall()
        rev = acct.post(conn, entry_date=old["entry_date"], description=f"REVERSAL of #{old['id']}: payslip revised - {reason.strip()}",
                        user=actor, source_type="reversal", source_id=old["id"], reverses_entry_id=old["id"],
                        lines=[{"account": l["code"], "debit": l["credit_paisa"], "credit": l["debit_paisa"],
                                "employee_id": l["employee_id"]} for l in old_lines])
        net, lines = _figures(conn, emp, old["entry_date"], new_basic, allow, ded, rec, deduction_notes)   # after the reversal, so the old recovery is back in the advance balance
        new = acct.post(conn, entry_date=old["entry_date"], description=f"Salary {ps['period']} {emp['emp_code']} (revised)", user=actor,
                        source_type="payslip", source_id=f"{emp['emp_code']}:{ps['period']}", lines=lines)
        conn.execute("UPDATE payslips SET basic_paisa=?, allowances_paisa=?, deductions_paisa=?, advance_recovered_paisa=?, "
                     "net_paisa=?, deduction_notes=?, entry_id=? WHERE id=?",
                     (new_basic, allow, ded, rec, net, deduction_notes, new, payslip_id))
        fields = ("basic_paisa", "allowances_paisa", "deductions_paisa", "advance_recovered_paisa", "net_paisa", "deduction_notes")
        conn.execute("INSERT INTO payslip_revisions(payslip_id,old_values,new_values,reason,old_entry_id,reversal_entry_id,"
                     "new_entry_id,revised_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (payslip_id, json.dumps({k: ps[k] for k in fields}),
                      json.dumps({"basic_paisa": new_basic, "allowances_paisa": allow, "deductions_paisa": ded,
                                  "advance_recovered_paisa": rec, "net_paisa": net, "deduction_notes": deduction_notes}),
                      reason.strip(), old["id"], rev, new, actor["id"], utc_now()))
        audit_service.log(conn, "salary.revise", user=actor, entity="payslip", entity_id=payslip_id, reason=reason.strip(),
                          details={"old_net": ps["net_paisa"], "new_net": net})
        return payslip_id


def payslip_balance(conn, payslip_id) -> int:
    r = conn.execute("SELECT net_paisa - COALESCE((SELECT SUM(amount_paisa) FROM payslip_payments WHERE payslip_id=p.id),0) "
                     "FROM payslips p WHERE id=?", (payslip_id,)).fetchone()
    if r is None:
        raise PayrollError("Payslip not found.")
    return r[0]


def pay_salary(conn, actor, payslip_id, amount, on, *, method="cash", reference="", idem_key=None) -> int:
    require_permission(actor, "manage_payroll")
    p = to_paisa(amount)
    with transaction(conn):
        if idem_key and not claim_idempotency_key(conn, idem_key, "pay_salary"):
            raise PayrollError("This payment was already recorded.")
        ps = conn.execute("SELECT * FROM payslips WHERE id=?", (payslip_id,)).fetchone()
        if not ps:
            raise PayrollError("Payslip not found.")
        if p <= 0 or p > payslip_balance(conn, payslip_id):
            raise PayrollError("Payment must be positive and not exceed the unpaid balance.")
        booked = conn.execute("SELECT entry_date FROM journal_entries WHERE id=?", (ps["entry_id"],)).fetchone()
        if booked and on < booked[0]:
            raise PayrollError(f"This salary is booked on {booked[0]}. A payment cannot be dated before that - pick {booked[0]} or later.")
        e = acct.post(conn, entry_date=on, description=f"Salary payment {reference}".strip(), user=actor,
                      source_type="salary_payment", source_id=payslip_id,
                      lines=[{"account": "2300", "debit": p, "employee_id": ps["employee_id"]}, {"account": "1000", "credit": p}])
        cur = conn.execute("INSERT INTO payslip_payments(payslip_id,amount_paisa,paid_on,method,reference,entry_id,created_by) "
                           "VALUES (?,?,?,?,?,?,?)", (payslip_id, p, on, method, reference, e, actor["id"]))
        audit_service.log(conn, "salary.pay", user=actor, entity="payslip", entity_id=payslip_id, details={"paisa": p})
        return cur.lastrowid


def pay_salary_for_month(conn, actor, employee_id, period: str, *, amount=None, on: str, reference="", allowances="0", deductions="0",
                         deduction_notes="", advance_recovery="0", basic_override=None, override_reason="") -> int:
    """The one 'Pay salary' action. In a single all-or-nothing step it
      - books the month's payslip if there is none yet (allowances, deductions, advance recovery, optional basic override),
      - or, if the payslip exists and nothing was paid on it yet, adjusts it (old entry reversed, new one posted) when the figures changed,
      - then records the payment (`amount` empty = the whole net pay).
    Once part of a payslip is paid its figures are fixed: only the amount can be paid."""
    require_permission(actor, "manage_payroll")
    allow, ded, rec = to_paisa(allowances), to_paisa(deductions), to_paisa(advance_recovery)
    with transaction(conn):
        ps = conn.execute("SELECT * FROM payslips WHERE employee_id=? AND period=?", (employee_id, period)).fetchone()
        if ps is None:
            pid = accrue_salary(conn, actor, employee_id, period, allowances=allowances, deductions=deductions, deduction_notes=deduction_notes,
                                advance_recovery=advance_recovery, basic_override=basic_override or None, override_reason=override_reason)
        else:
            pid = ps["id"]
            paid_any = conn.execute("SELECT 1 FROM payslip_payments WHERE payslip_id=?", (pid,)).fetchone() is not None
            basic = to_paisa(basic_override) if basic_override else ps["basic_paisa"]
            same = (basic, allow, ded, rec, (deduction_notes or "")) == (ps["basic_paisa"], ps["allowances_paisa"], ps["deductions_paisa"],
                                                                         ps["advance_recovered_paisa"], (ps["deduction_notes"] or ""))
            if not same:
                if paid_any:
                    raise PayrollError("Part of this salary is already paid, so allowances and deductions can no longer change. Pay the remaining amount only.")
                revise_payslip(conn, actor, pid, "Adjusted while paying salary", basic=str(from_paisa(basic)), allowances=allowances,
                               deductions=deductions, deduction_notes=deduction_notes, advance_recovery=advance_recovery)
        pay = to_paisa(amount) if amount not in (None, "") else payslip_balance(conn, pid)
        return pay_salary(conn, actor, pid, str(from_paisa(pay)), on, reference=reference)


def payslip_status(conn, payslip_id) -> str:
    bal = payslip_balance(conn, payslip_id)
    net = conn.execute("SELECT net_paisa FROM payslips WHERE id=?", (payslip_id,)).fetchone()[0]
    return "paid" if bal == 0 else ("unpaid" if bal == net else "partial")


# ================================================================== live attendance (IN / OUT buttons)
AUTO_OUT_HOURS = 10
UNDO_WINDOW_MINUTES = 15
AUTO_NOTE = ("AUTO OUT: OUT was not pressed, so the shift was closed after 10 hours. "
             "The real leaving time is unknown - correct it if you know it.")


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now()).replace(second=0, microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M")


_OPEN = "status='present' AND clock_in IS NOT NULL AND clock_out IS NULL"


def _employee(conn, employee_id):
    e = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
    if not e or not e["is_active"]:
        raise PayrollError("Unknown or inactive employee.")
    return e


def _open_shift(conn, employee_id):
    return conn.execute(f"SELECT * FROM attendance WHERE employee_id=? AND {_OPEN} ORDER BY clock_in DESC LIMIT 1",
                        (employee_id,)).fetchone()


def _close(conn, row, out_dt: datetime, *, auto: bool, actor=None) -> int:
    out_iso = _iso(out_dt)
    wm = worked_minutes(row["clock_in"], out_iso, row["break_minutes"])
    note = row["notes"] or ""
    if auto:
        note = (note + " | " if note else "") + AUTO_NOTE
    conn.execute("UPDATE attendance SET clock_out=?, worked_minutes=?, auto_out=?, notes=? WHERE id=?",
                 (out_iso, wm, 1 if auto else 0, note, row["id"]))
    audit_service.log(conn, "attendance.auto_out" if auto else "attendance.out", user=actor,
                      username=None if actor else "system", entity="attendance", entity_id=row["id"],
                      details={"minutes": wm})
    return wm


def auto_close_open(conn, now: datetime | None = None) -> list[int]:
    """Housekeeping (no permission needed): every shift still IN after AUTO_OUT_HOURS is closed at exactly IN + 10 h and flagged
    `auto_out`. Safe to call often; it also catches shifts left open while the program was closed."""
    now = _now(now)
    due = []
    for r in conn.execute(f"SELECT * FROM attendance WHERE {_OPEN}").fetchall():
        if datetime.fromisoformat(r["clock_in"]) + timedelta(hours=AUTO_OUT_HOURS) <= now:
            due.append(r)
    if not due:
        return []
    with transaction(conn):
        for r in due:
            _close(conn, r, datetime.fromisoformat(r["clock_in"]) + timedelta(hours=AUTO_OUT_HOURS), auto=True)
    return [r["id"] for r in due]


def clock_in(conn, actor, employee_id: int, now: datetime | None = None) -> int:
    """IN button: stamps the current time. One shift per employee per day; a person marked Absent/Leave earlier can still come in."""
    require_permission(actor, "manage_attendance")
    now = _now(now)
    with transaction(conn):
        auto_close_open(conn, now)
        emp = _employee(conn, employee_id)
        op = _open_shift(conn, employee_id)
        if op:
            raise PayrollError(f"{emp['name']} is already IN since {op['clock_in'][11:16]}.")
        d = now.date().isoformat()
        row = conn.execute("SELECT * FROM attendance WHERE employee_id=? AND work_date=?", (employee_id, d)).fetchone()
        if row is None:
            rid = conn.execute(
                "INSERT INTO attendance(employee_id,work_date,status,shift,clock_in,worked_minutes,notes,recorded_by) "
                "VALUES (?,?,?,?,?,0,'',?)", (employee_id, d, "present", emp["default_shift"], _iso(now), actor["id"])).lastrowid
            was = None
        elif row["status"] in ("absent", "leave", "half_day") and not row["clock_in"]:
            conn.execute("UPDATE attendance SET status='present', shift=?, clock_in=?, clock_out=NULL, worked_minutes=0, "
                         "auto_out=0, recorded_by=? WHERE id=?", (emp["default_shift"], _iso(now), actor["id"], row["id"]))
            rid, was = row["id"], row["status"]
        elif row["clock_out"]:
            raise PayrollError(f"{emp['name']} already finished today's shift ({row['clock_in'][11:16]} to "
                               f"{row['clock_out'][11:16]}). If a time is wrong, use Correct.")
        else:
            raise PayrollError(f"{emp['name']} is already marked present today. Use Correct to change it.")
        audit_service.log(conn, "attendance.in", user=actor, entity="attendance", entity_id=rid, details={"was": was})
        return rid


def clock_out(conn, actor, employee_id: int, now: datetime | None = None) -> dict:
    """OUT button: stamps the current time. If the shift has run past AUTO_OUT_HOURS it is closed at the 10-hour mark and flagged."""
    require_permission(actor, "manage_attendance")
    now = _now(now)
    with transaction(conn):
        emp = _employee(conn, employee_id)
        op = _open_shift(conn, employee_id)
        if not op:
            raise PayrollError(f"{emp['name']} is not IN right now.")
        cin = datetime.fromisoformat(op["clock_in"])
        cap = cin + timedelta(hours=AUTO_OUT_HOURS)
        auto = now >= cap
        out = cap if auto else now
        if out <= cin:
            raise PayrollError(f"{emp['name']} pressed IN less than a minute ago. Wait a minute, or use Undo if IN was a mistake.")
        wm = _close(conn, op, out, auto=auto, actor=None if auto else actor)
        return {"id": op["id"], "minutes": wm, "auto": auto, "clock_in": op["clock_in"], "clock_out": _iso(out)}


def mark_status(conn, actor, employee_id: int, status: str, now: datetime | None = None) -> int:
    """Absent / Leave buttons for today. Can be switched between the two; cannot overwrite a shift that has started."""
    require_permission(actor, "manage_attendance")
    if status not in ("absent", "leave"):
        raise PayrollError("Use Absent or Leave.")
    now = _now(now)
    with transaction(conn):
        auto_close_open(conn, now)
        emp = _employee(conn, employee_id)
        if _open_shift(conn, employee_id):
            raise PayrollError(f"{emp['name']} is IN right now. Press OUT first.")
        d = now.date().isoformat()
        row = conn.execute("SELECT * FROM attendance WHERE employee_id=? AND work_date=?", (employee_id, d)).fetchone()
        if row is None:
            rid = conn.execute("INSERT INTO attendance(employee_id,work_date,status,shift,worked_minutes,notes,recorded_by) "
                               "VALUES (?,?,?,?,0,'',?)", (employee_id, d, status, emp["default_shift"], actor["id"])).lastrowid
            was = None
        elif row["clock_in"] or row["status"] not in ("absent", "leave"):
            raise PayrollError(f"{emp['name']} already has a {row['status'].replace('_', ' ')} record today, so cannot be marked "
                               f"{status}. Use Correct if that is wrong.")
        else:
            rid, was = row["id"], row["status"]
            if was != status:
                conn.execute("UPDATE attendance SET status=?, recorded_by=? WHERE id=?", (status, actor["id"], rid))
        audit_service.log(conn, "attendance.mark", user=actor, entity="attendance", entity_id=rid, details={"status": status, "was": was})
        return rid


def clear_mark(conn, actor, employee_id: int, now: datetime | None = None) -> str:
    """Undo button: removes today's Absent/Leave mark, or an IN pressed by mistake in the last UNDO_WINDOW_MINUTES.
    Finished shifts and records that were corrected can only be changed with Correct (which keeps history)."""
    require_permission(actor, "manage_attendance")
    now = _now(now)
    with transaction(conn):
        emp = _employee(conn, employee_id)
        row = _open_shift(conn, employee_id) or conn.execute(
            "SELECT * FROM attendance WHERE employee_id=? AND work_date=?", (employee_id, now.date().isoformat())).fetchone()
        if row is None:
            raise PayrollError(f"Nothing to undo for {emp['name']}.")
        if conn.execute("SELECT 1 FROM attendance_corrections WHERE attendance_id=?", (row["id"],)).fetchone():
            raise PayrollError("This record was corrected before, so it cannot be undone here.")
        if row["clock_out"]:
            raise PayrollError(f"{emp['name']}'s shift is finished. Use Correct to change it.")
        if row["clock_in"] and now - datetime.fromisoformat(row["clock_in"]) > timedelta(minutes=UNDO_WINDOW_MINUTES):
            raise PayrollError(f"IN was pressed more than {UNDO_WINDOW_MINUTES} minutes ago. Use Correct to change it.")
        if row["status"] == "present" and not row["clock_in"]:
            raise PayrollError("This record was typed in by hand. Use Correct to change it.")
        conn.execute("DELETE FROM attendance WHERE id=?", (row["id"],))
        audit_service.log(conn, "attendance.undo", user=actor, entity="attendance", entity_id=row["id"],
                          details={"status": row["status"], "clock_in": row["clock_in"]})
        return row["status"]


def live_board(conn, now: datetime | None = None) -> list[dict]:
    """One row per active employee for the Today board: state = idle | in | done | absent | leave | half_day."""
    now = _now(now)
    today = now.date().isoformat()
    out = []
    for e in conn.execute(
            f"SELECT e.id, e.emp_code, e.name, e.job_role, e.default_shift, a.id aid, a.work_date, a.status, a.clock_in, a.clock_out, "
            f"a.worked_minutes, a.auto_out, a.notes, "
            f"EXISTS(SELECT 1 FROM attendance_corrections c WHERE c.attendance_id=a.id) AS corrected "
            f"FROM employees e LEFT JOIN attendance a ON a.id = ("
            f"SELECT id FROM attendance WHERE employee_id=e.id AND (work_date=? OR ({_OPEN})) "
            f"ORDER BY ({_OPEN}) DESC, work_date DESC LIMIT 1) WHERE e.is_active=1 ORDER BY e.emp_code", (today,)).fetchall():
        r = dict(e)
        if r["aid"] is None:
            r["state"] = "idle"
        elif r["status"] == "present" and r["clock_in"] and not r["clock_out"]:
            r["state"] = "in"
        elif r["status"] == "present":
            r["state"] = "done"
        else:
            r["state"] = r["status"]
        mins = r["worked_minutes"] or 0
        if r["state"] == "in":
            mins = max(0, int((now - datetime.fromisoformat(r["clock_in"])).total_seconds() // 60))
        r["minutes"] = mins
        r["can_undo"] = bool(r["aid"]) and not r["corrected"] and (
            (r["state"] in ("absent", "leave", "half_day") and not r["clock_in"]) or
            (r["state"] == "in" and now - datetime.fromisoformat(r["clock_in"]) <= timedelta(minutes=UNDO_WINDOW_MINUTES)))
        out.append(r)
    return out


def recent_auto_closed(conn, now: datetime | None = None, days: int = 3) -> list[dict]:
    """Shifts the system closed itself in the last few days, for the warning on the Today board."""
    since = _iso(_now(now) - timedelta(days=days))
    return [dict(r) for r in conn.execute(
        "SELECT a.id, e.name, a.work_date, a.clock_in, a.clock_out FROM attendance a JOIN employees e ON e.id=a.employee_id "
        "WHERE a.auto_out=1 AND a.clock_out>=? ORDER BY a.clock_out DESC", (since,)).fetchall()]


# ================================================================== month sheet + automatic accrual
def previous_period(today: date | None = None) -> str:
    t = today or date.today()
    y, m = (t.year - 1, 12) if t.month == 1 else (t.year, t.month - 1)
    return f"{y:04d}-{m:02d}"


def _manual_reason(conn, emp, period: str) -> str | None:
    """Why this employee is left for you to accrue by hand (the owner decides when and how much; nothing is prorated or guessed)."""
    first = f"{period}-01"
    if (emp["created_at"] or "")[:7] > period:
        return "added to the system after this month"
    if emp["joined_on"] and emp["joined_on"] > first:
        return "joined during this month (accrue by hand when you decide)" if emp["joined_on"][:7] == period else "joined after this month"
    r = conn.execute("SELECT MIN(effective_from) FROM salary_history WHERE employee_id=?", (emp["id"],)).fetchone()[0]
    if r is None or r > first:
        return "salary starts during this month (accrue by hand when you decide)"
    return None


def month_overview(conn, period: str) -> list[dict]:
    """Everything the Month sheet shows, per employee, computed from the database."""
    y, m = int(period[:4]), int(period[5:])
    dim = calendar.monthrange(y, m)[1]
    last = f"{period}-{dim:02d}"
    ded_on, leave_unpaid = _rules(conn)
    att = {r["employee_id"]: r for r in conn.execute(
        "SELECT employee_id, SUM(status='present') present, SUM(status='half_day') half, SUM(status='absent') absent, "
        "SUM(status='leave') leave, COALESCE(SUM(worked_minutes),0) minutes, COALESCE(SUM(auto_out),0) auto_n "
        "FROM attendance WHERE substr(work_date,1,7)=? GROUP BY employee_id", (period,)).fetchall()}
    slips = {r["employee_id"]: r for r in conn.execute(
        "SELECT p.*, COALESCE((SELECT SUM(amount_paisa) FROM payslip_payments WHERE payslip_id=p.id),0) paid "
        "FROM payslips p WHERE period=?", (period,)).fetchall()}
    out = []
    for e in conn.execute("SELECT * FROM employees ORDER BY emp_code").fetchall():
        a, ps = att.get(e["id"]), slips.get(e["id"])
        if not e["is_active"] and not a and not ps:
            continue
        try:
            salary = salary_on(conn, e["id"], last)
        except PayrollError:
            salary = None
        n = lambda k: int(a[k] or 0) if a else 0          # noqa: E731
        est = estimate_deduction(salary, dim, n("absent"), n("half"), n("leave"), leave_unpaid) if (ded_on and salary is not None) else 0
        row = {"id": e["id"], "emp_code": e["emp_code"], "name": e["name"], "job_role": e["job_role"], "is_active": e["is_active"], "left_on": e["left_on"],
               "present": n("present"), "half": n("half"), "absent": n("absent"), "leave": n("leave"), "minutes": n("minutes"),
               "auto_n": n("auto_n"), "salary_paisa": salary, "est_deduction_paisa": est,
               "est_net_paisa": None if salary is None else salary - est,
               "deduction_note": deduction_text(dim, n("absent"), n("half"), n("leave"), leave_unpaid) if ded_on else "",
               "payslip_id": ps["id"] if ps else None, "net_paisa": ps["net_paisa"] if ps else None,
               "ps_deduction_paisa": ps["deductions_paisa"] if ps else 0,
               "paid_paisa": ps["paid"] if ps else 0, "due_paisa": (ps["net_paisa"] - ps["paid"]) if ps else None,
               "manual_reason": None if ps or not e["is_active"] else _manual_reason(conn, e, period)}
        row["status"] = ("not accrued" if not ps else "paid" if row["due_paisa"] == 0 else
                         "unpaid" if row["paid_paisa"] == 0 else "partial")
        out.append(row)
    return out


def auto_accrue_month(conn, actor, period: str, *, auto: bool = True) -> dict:
    """Accrue every active employee who has no payslip for `period` yet, using the attendance-based estimate as the deduction
    (allowances 0, no advance recovery). People who joined during the month are skipped and reported: the owner decides when to accrue them."""
    require_permission(actor, "manage_payroll")
    created, skipped = [], []
    for row in month_overview(conn, period):
        if not row["is_active"] or row["payslip_id"]:
            continue
        if row["manual_reason"]:
            skipped.append((row["name"], row["manual_reason"])); continue
        if row["salary_paisa"] is None:
            skipped.append((row["name"], "no salary set")); continue
        try:
            created.append(accrue_salary(conn, actor, row["id"], period, deductions=str(from_paisa(row["est_deduction_paisa"])),
                                         deduction_notes=row["deduction_note"] if row["est_deduction_paisa"] else "", auto=auto))
        except PayrollError as e:
            skipped.append((row["name"], str(e)))
    return {"period": period, "created": created, "skipped": skipped}


def auto_accrue_if_due(conn, actor, today: date | None = None) -> dict | None:
    """Called at sign-in, when the Attendance page opens and when the date changes: from the 1st of a month the PREVIOUS month is
    accrued automatically (if the owner left 'auto accrue' on and this user may run payroll). Does nothing when there is nothing to do."""
    from services.auth_service import has_permission
    if actor is None or not has_permission(actor["role"], "manage_payroll"):
        return None
    if settings_service.get(conn, "auto_accrue_payroll") != "1":
        return None
    res = auto_accrue_month(conn, actor, previous_period(today))
    return res if (res["created"] or res["skipped"]) else None
