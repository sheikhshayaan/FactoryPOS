"""Dashboard figures. Every value is computed from database records at call time.

Kept distinct (never mixed):
  performance (period)  : sales (revenue recognised), production cost, operating expenses, gross/net profit
  position (as of end)  : cash balance, inventory value, receivables, payables, salary liabilities, capital
Weeks start on Monday. Quarters/years follow the configured financial year start month.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from services import accounting_service as acct
from services import inventory_service as inv
from services.units import from_milli


class RangeError(ValueError):
    pass


def _fy_start_month(conn) -> int:
    r = conn.execute("SELECT value FROM app_settings WHERE key='fiscal_year_start_month'").fetchone()
    return int(r[0]) if r else 7


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def resolve_range(kind: str, today: date, *, fy_start_month: int = 7, custom: tuple[str, str] | None = None) -> tuple[str, str]:
    """Return (start, end) ISO dates, inclusive."""
    if kind == "today":
        s = e = today
    elif kind == "this_week":
        s = today - timedelta(days=today.weekday()); e = s + timedelta(days=6)
    elif kind == "this_month":
        s = today.replace(day=1); e = today.replace(day=calendar.monthrange(today.year, today.month)[1])
    elif kind in ("this_quarter", "this_year"):
        fy_year = today.year if today.month >= fy_start_month else today.year - 1
        fy_start = date(fy_year, fy_start_month, 1)
        if kind == "this_year":
            s, e = fy_start, _add_months(fy_start, 12) - timedelta(days=1)
        else:
            q = ((today.year - fy_start.year) * 12 + today.month - fy_start.month) // 3
            s = _add_months(fy_start, q * 3); e = _add_months(s, 3) - timedelta(days=1)
    elif kind == "custom":
        if not custom:
            raise RangeError("Custom range needs start and end dates.")
        s, e = date.fromisoformat(custom[0]), date.fromisoformat(custom[1])
        if e < s:
            raise RangeError("End date is before start date.")
    else:
        raise RangeError(f"Unknown range: {kind}")
    return s.isoformat(), e.isoformat()


def monthly_series(conn, start: str, end: str) -> list[dict]:
    """Per-month revenue, cost of production, operating expenses and net profit between start and end."""
    s, e = date.fromisoformat(start).replace(day=1), date.fromisoformat(end)
    out, cur = [], s
    while cur <= e:
        nxt = _add_months(cur, 1)
        a, b = max(cur, date.fromisoformat(start)).isoformat(), min(nxt - timedelta(days=1), e).isoformat()
        st = acct.income_statement(conn, a, b)
        out.append({"month": cur.strftime("%Y-%m"), "revenue": st["revenue"], "cost_of_production": st["cost_of_production"],
                    "operating_expenses": st["selling_admin"], "net_profit": st["net_profit"]})
        cur = nxt
    return out


def _bps(num: int, den: int) -> int | None:
    """num/den in basis points, integer, rounded half away from zero. None when there is nothing to divide by."""
    if den <= 0:
        return None
    q = (abs(num) * 10000 * 2 + den) // (2 * den)
    return -q if num < 0 else q


def snapshot(conn, start: str, end: str) -> dict:
    st = acct.income_statement(conn, start, end)
    bal = lambda code, **k: acct.balance(conn, code, as_of=end, **k)  # noqa: E731

    # ---- inventory (factory-owned valuation; customer property reported separately, no value)
    mats = conn.execute("SELECT m.id, m.name, m.base_unit, m.category, s.qty_milli, s.value_paisa, m.reorder_level_milli "
                        "FROM materials m JOIN material_stock s ON s.material_id=m.id WHERE m.is_active=1 ORDER BY m.name").fetchall()
    cust_q = {r[0]: r[1] for r in conn.execute(
        "SELECT material_id, SUM(qty_remaining_milli) FROM stock_lots WHERE ownership='customer' GROUP BY material_id")}
    inventory = {
        "value_paisa": sum(m["value_paisa"] for m in mats),
        "items": [{"material": m["name"], "unit": m["base_unit"], "category": m["category"],
                   "qty": from_milli(m["qty_milli"]), "value_paisa": m["value_paisa"],
                   "customer_owned_qty": from_milli(cust_q.get(m["id"], 0))} for m in mats],
        "low_stock": [{"material": r["name"], "qty": from_milli(r["qty_milli"]), "reorder_level": from_milli(r["reorder_level_milli"])}
                      for r in inv.low_stock_materials(conn)],
    }
    # Paper is counted in rolls (a roll-unit material's quantity). Old meter-tracked paper, if any exists, counts its physical rolls.
    paper_milli = conn.execute("SELECT COALESCE(SUM(s.qty_milli),0) FROM material_stock s JOIN materials m ON m.id=s.material_id "
                               "WHERE m.category='paper' AND m.is_active=1 AND m.base_unit='roll'").fetchone()[0]
    legacy = conn.execute("SELECT COUNT(*) FROM stock_rolls r JOIN stock_lots l ON l.id=r.lot_id JOIN materials m ON m.id=l.material_id "
                          "WHERE m.category='paper' AND m.tracks_rolls=1 AND l.ownership='factory' AND r.remaining_milli>0").fetchone()[0]
    inventory["paper_rolls_on_hand"] = from_milli(paper_milli + legacy * 1000)
    used = conn.execute("SELECT COALESCE(-SUM(mv.qty_milli),0) FROM stock_movements mv JOIN materials m ON m.id=mv.material_id "
                        "WHERE m.category='paper' AND m.base_unit='roll' AND mv.ownership='factory' AND mv.movement_type='production' "
                        "AND COALESCE(mv.moved_on, substr(mv.created_at,1,10)) BETWEEN ? AND ?", (start, end)).fetchone()[0]
    inventory["paper_rolls_used_in_period"] = from_milli(used)
    # The same paper in meters: every paper material has its own roll length (meters in one roll).
    inventory["paper_meters_on_hand"] = from_milli(sum(inv.roll_meters(r[0], r[1]) for r in conn.execute(
        "SELECT s.qty_milli, m.roll_length_milli FROM material_stock s JOIN materials m ON m.id=s.material_id "
        "WHERE m.category='paper' AND m.is_active=1 AND m.base_unit='roll' AND m.roll_length_milli IS NOT NULL")))
    inventory["paper_meters_used_in_period"] = from_milli(sum(inv.roll_meters(r[0], r[1]) for r in conn.execute(
        "SELECT -SUM(mv.qty_milli), m.roll_length_milli FROM stock_movements mv JOIN materials m ON m.id=mv.material_id "
        "WHERE m.category='paper' AND m.base_unit='roll' AND m.roll_length_milli IS NOT NULL AND mv.ownership='factory' "
        "AND mv.movement_type='production' AND COALESCE(mv.moved_on, substr(mv.created_at,1,10)) BETWEEN ? AND ? GROUP BY m.id", (start, end))
        if r[0]))

    # ---- jobs
    js = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
    jobs = {"active": js.get("approved", 0) + js.get("in_progress", 0), "quotations": js.get("quotation", 0),
            "completed_awaiting_finalisation": js.get("completed", 0), "finalized_total": js.get("finalized", 0),
            "entries_in_period": conn.execute("SELECT COUNT(*) FROM jobs WHERE status<>'cancelled' AND order_date BETWEEN ? AND ?", (start, end)).fetchone()[0],
            "finalized_in_period": conn.execute("SELECT COUNT(*) FROM jobs WHERE status='finalized' AND substr(finalized_at,1,10) BETWEEN ? AND ?",
                                                (start, end)).fetchone()[0]}

    # ---- machines (usage recorded in the period; no live status is assumed)
    machines = []
    for m in conn.execute("SELECT * FROM machines WHERE is_active=1 ORDER BY id"):
        r = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(minutes),0) mins, COALESCE(SUM(downtime_minutes),0) dt, MAX(ended_at) last_end, "
                         "COALESCE(SUM(meters_milli),0) mt FROM machine_runs WHERE machine_id=? AND substr(started_at,1,10) BETWEEN ? AND ?",
                         (m["id"], start, end)).fetchone()
        machines.append({"machine": m["name"], "runs": r["n"], "run_minutes": r["mins"], "downtime_minutes": r["dt"],
                         "meters": from_milli(r["mt"]), "last_run_end": r["last_end"],
                         "electricity_rate_configured": m["hourly_rate_paisa"] is not None})

    # ---- receivables / payables / salaries / partners
    receivables = [{"customer": r["name"], "outstanding_paisa": r["o"]} for r in conn.execute(
        "SELECT c.name, SUM(l.debit_paisa)-SUM(l.credit_paisa) o FROM journal_lines l JOIN accounts a ON a.id=l.account_id "
        "JOIN customers c ON c.id=l.customer_id JOIN journal_entries e ON e.id=l.entry_id WHERE a.code='1100' AND e.entry_date<=? "
        "GROUP BY c.id HAVING o<>0 ORDER BY o DESC", (end,))]
    payables = [{"supplier": r["name"], "owed_paisa": r["o"]} for r in conn.execute(
        "SELECT s.name, SUM(l.credit_paisa)-SUM(l.debit_paisa) o FROM journal_lines l JOIN accounts a ON a.id=l.account_id "
        "JOIN suppliers s ON s.id=l.supplier_id JOIN journal_entries e ON e.id=l.entry_id WHERE a.code='2000' AND e.entry_date<=? "
        "GROUP BY s.id HAVING o<>0 ORDER BY o DESC", (end,))]
    # salaries still owed AS OF the period end (same basis as the GL balance): payslips for months up to the end month,
    # less payments dated on or before the end date
    unpaid = conn.execute(
        "SELECT e.name, p.period, p.net_paisa - COALESCE((SELECT SUM(amount_paisa) FROM payslip_payments "
        "WHERE payslip_id=p.id AND paid_on<=?),0) AS due, p.net_paisa FROM payslips p JOIN employees e ON e.id=p.employee_id "
        "WHERE p.period<=? ORDER BY p.period, e.name", (end, end[:7])).fetchall()
    salaries = [{"employee": r["name"], "period": r["period"], "due_paisa": r["due"],
                 "status": "unpaid" if r["due"] == r["net_paisa"] else "partial"} for r in unpaid if r["due"] > 0]
    partners = []
    for p in conn.execute("SELECT id,name FROM partners WHERE is_active=1 ORDER BY name"):
        partners.append({"partner": p["name"],
                         "capital_paisa": bal("3000", partner_id=p["id"]) + bal("3100", partner_id=p["id"]),
                         "distributions_approved_paisa": conn.execute(
                             "SELECT COALESCE(SUM(dl.amount_paisa),0) FROM distribution_lines dl JOIN distributions d ON d.id=dl.distribution_id "
                             "WHERE dl.partner_id=? AND d.period_end<=?", (p["id"], end)).fetchone()[0],
                         "distributions_unpaid_paisa": bal("2400", partner_id=p["id"])})

    # ---- health / warnings
    last_bk = conn.execute("SELECT MAX(created_at) FROM backup_history WHERE status='ok'").fetchone()[0]
    failed_bk = conn.execute("SELECT COUNT(*) FROM backup_history WHERE status='failed' AND created_at > COALESCE(?, '')", (last_bk,)).fetchone()[0]
    warnings = acct.check_integrity(conn)
    if last_bk is None:
        warnings.append("No successful backup has been made yet.")
    if failed_bk:
        warnings.append(f"{failed_bk} backup attempt(s) failed since the last successful backup.")
    no_rate = [m["machine"] for m in machines if m["runs"] and not m["electricity_rate_configured"]]
    if no_rate:
        warnings.append("Electricity cost unknown for machine(s) without an hourly rate: " + ", ".join(no_rate))

    return {
        "period": {"start": start, "end": end},
        "performance": {
            "sales_paisa": st["revenue"], "production_cost_paisa": st["cost_of_production"],
            "direct_production_paisa": st["direct_production"], "factory_overhead_paisa": st["factory_overhead"],
            "operating_expenses_paisa": st["selling_admin"], "gross_profit_paisa": st["gross_profit"],
            "net_profit_paisa": st["net_profit"],
            "gross_margin_bps": _bps(st["gross_profit"], st["revenue"]),
            "net_margin_bps": _bps(st["net_profit"], st["revenue"]),
        },
        "position": {
            "cash_paisa": bal("1000"), "inventory_value_paisa": inventory["value_paisa"],
            "receivables_paisa": bal("1100"), "customer_advances_paisa": bal("2200"),
            "payables_paisa": bal("2000"), "salaries_payable_paisa": bal("2300"),
            "distributions_payable_paisa": bal("2400"), "partner_capital_paisa": bal("3000") + bal("3100"),
        },
        "inventory": inventory, "jobs": jobs, "machines": machines,
        "receivables": receivables, "payables": payables, "unpaid_salaries": salaries, "partners": partners,
        "chart_monthly": monthly_series(conn, start, end),
        "backup": {"last_successful": last_bk, "failed_since": failed_bk},
        "warnings": warnings,
    }
