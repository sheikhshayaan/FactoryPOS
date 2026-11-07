"""Business insights: this period against the one before it (month, quarter, year or any dates).

Every number is read from the books / stock records at call time - nothing is typed in or invented. Money is whole paisa (integers),
percentages are basis points (1% = 100). The English sentences in `story` are built here from those numbers so the page only shows them.

Definitions (kept the same as the Dashboard):
  sales               = revenue recognised on invoices
  net profit          = sales - production cost - operating expenses
  expenses (kharchay) = only what is recorded on the Expenses page (same figures and same dates as that page). Salaries and the stock
                        accounts (materials used, wastage, adjustments) are not part of it. Reversed (cancelled) entries are left out.
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

from services import accounting_service as acct, queries as q
from services.dashboard_service import _bps, monthly_series

KINDS = ("month", "quarter", "year", "custom")
STOCK_ACCOUNTS = ("5000", "5300", "5900")                  # materials consumed, wastage, inventory adjustments
ELECTRICITY = "5200"
EXCEED_BPS = 1000                                           # a category is "exceeding" when it grew by 10% or more
DAYS_PER_MONTH_X10000 = 304375                              # 30.4375 days
DISPLAY = {"5100": "Salaries - production", "6100": "Salaries - office", "5400": "Factory Overheads"}


def rs(paisa: int) -> str:
    sign = "-" if paisa < 0 else ""
    p = abs(paisa)
    return f"{sign}Rs {p // 100:,}" + (f".{p % 100:02d}" if p % 100 else "")


def pct(bps: int | None) -> str:
    if bps is None:
        return "-"
    return f"{abs(bps) / 100:.1f}%".replace(".0%", "%")


# ------------------------------------------------------------------ periods
def _first_of(idx: int) -> date:
    return date(idx // 12, idx % 12 + 1, 1)


def _label(kind: str, s: date, e: date) -> str:
    if kind == "month":
        return s.strftime("%b %Y")
    if kind == "quarter":
        return f"{s:%b}-{e:%b %Y}" if s.year == e.year else f"{s:%b %Y}-{e:%b %Y}"
    if kind == "year":
        return str(s.year) if s.year == e.year else f"{s:%b %Y}-{e:%b %Y}"
    if s == e:
        return f"{s.day} {s:%b %Y}"
    if (s.year, s.month) == (e.year, e.month):
        return f"{s.day}-{e.day} {s:%b %Y}"
    return f"{s.day} {s:%b} - {e.day} {e:%b %Y}" if s.year == e.year else f"{s.day} {s:%b %Y} - {e.day} {e:%b %Y}"


def span(kind: str, today: date, offset: int = 0, fy_start_month: int = 7, custom: tuple[str, str] | None = None) -> tuple[date, date]:
    """(start, end) of the period `offset` steps from today's (0 = this one, -1 = the one before ...). Quarters and years follow the financial year."""
    if kind == "custom":
        if not custom:
            raise ValueError("Pick the start and end dates.")
        s, e = date.fromisoformat(custom[0]), date.fromisoformat(custom[1])
        if e < s:
            raise ValueError("The end date is before the start date.")
        n = (e - s).days + 1
        return s + timedelta(days=n * offset), e + timedelta(days=n * offset)
    if kind not in KINDS:
        raise ValueError(f"Unknown period: {kind}")
    size = {"month": 1, "quarter": 3, "year": 12}[kind]
    absm, f = today.year * 12 + today.month - 1, fy_start_month - 1
    start = absm - ((absm - f) % size) + offset * size
    return _first_of(start), _first_of(start + size) - timedelta(days=1)


def periods(kind: str, today: date, offset: int = 0, fy_start_month: int = 7, custom=None) -> dict:
    cs, ce = span(kind, today, offset, fy_start_month, custom)
    ps, pe = span(kind, today, offset - 1, fy_start_month, custom)
    if kind == "custom":
        n = (ce - cs).days + 1
        ps, pe = cs - timedelta(days=n), cs - timedelta(days=1)

    def one(s, e):
        total = (e - s).days + 1
        elapsed = 0 if today < s else min(total, (today - s).days + 1)
        return {"start": s.isoformat(), "end": e.isoformat(), "label": _label(kind, s, e), "days": total, "days_elapsed": elapsed,
                "running": s <= today < e, "future": today < s}
    return {"cur": one(cs, ce), "prev": one(ps, pe), "kind": kind}


# ------------------------------------------------------------------ building blocks
def _change(cur: int, prev: int) -> dict:
    d = cur - prev
    return {"delta": d, "pct_bps": _bps(d, abs(prev)) if prev else None}


def expense_by_category(conn, start: str, end: str) -> dict[str, int]:
    """{category name: paisa} of the expenses recorded on the Expenses page dated start..end - exactly what that page adds up
    (salaries and production stock costs are NOT in it; reversed entries are left out)."""
    return {DISPLAY.get(name, name): t for name, t in q.expenses_by_category(conn, start, end) if t}


def stock_costs(conn, start: str, end: str) -> dict:
    """What stock cost the profit in the period (from the books): materials used, wastage, adjustments."""
    got = {r["code"]: r["t"] for r in conn.execute(
        "SELECT a.code, SUM(l.debit_paisa - l.credit_paisa) AS t FROM journal_lines l JOIN journal_entries e ON e.id=l.entry_id "
        "JOIN accounts a ON a.id=l.account_id WHERE a.code IN (?,?,?) AND e.entry_date BETWEEN ? AND ? GROUP BY a.code", (*STOCK_ACCOUNTS, start, end))}
    d = {"materials_paisa": got.get("5000", 0), "wastage_paisa": got.get("5300", 0), "adjustments_paisa": got.get("5900", 0)}
    d["total_paisa"] = sum(d.values())
    return d


def _movements(conn, kinds: tuple, start: str, end: str) -> list[dict]:
    ph = ",".join("?" * len(kinds))
    return [dict(r) for r in conn.execute(
        f"SELECT m.id, m.name, m.category, m.base_unit, m.roll_length_milli, -SUM(mv.qty_milli) AS qty_milli, -SUM(mv.value_paisa) AS cost_paisa "
        f"FROM stock_movements mv JOIN materials m ON m.id=mv.material_id WHERE mv.ownership='factory' AND mv.movement_type IN ({ph}) "
        f"AND COALESCE(mv.moved_on, substr(mv.created_at,1,10)) BETWEEN ? AND ? GROUP BY m.id HAVING cost_paisa<>0 ORDER BY cost_paisa DESC, m.name",
        (*kinds, start, end))]


def inventory_use(conn, start: str, end: str) -> dict:
    """Which materials the factory used up (at their stock cost), and which were lost as wastage / adjustments, in the period."""
    used = _movements(conn, ("production",), start, end)
    lost = _movements(conn, ("wastage", "adjustment_out", "adjustment_in"), start, end)
    total = sum(r["cost_paisa"] for r in used)
    for r in used:
        r["share_bps"] = _bps(r["cost_paisa"], total)
    return {"used": used, "used_total_paisa": total, "lost": lost, "lost_total_paisa": sum(r["cost_paisa"] for r in lost)}


def _block(conn, p: dict) -> dict:
    s, e = p["start"], p["end"]
    st = acct.income_statement(conn, s, e)
    cats = expense_by_category(conn, s, e)
    runs = conn.execute("SELECT COALESCE(SUM(minutes),0) FROM machine_runs WHERE substr(started_at,1,10) BETWEEN ? AND ?", (s, e)).fetchone()[0]
    return {"sales_paisa": st["revenue"], "production_cost_paisa": st["cost_of_production"], "operating_expenses_paisa": st["selling_admin"],
            "gross_profit_paisa": st["gross_profit"], "net_profit_paisa": st["net_profit"], "net_margin_bps": _bps(st["net_profit"], st["revenue"]),
            "expenses": cats, "expenses_total_paisa": sum(cats.values()), "stock": stock_costs(conn, s, e), "machine_minutes": runs}


def _expense_rows(cur: dict, prev: dict) -> list[dict]:
    rows = []
    for name in sorted(set(cur) | set(prev), key=lambda n: (-cur.get(n, 0), n)):
        c, p = cur.get(name, 0), prev.get(name, 0)
        ch = _change(c, p)
        if c > 0 and p == 0:
            status = "new"
        elif ch["delta"] > 0 and ch["pct_bps"] is not None and ch["pct_bps"] >= EXCEED_BPS:
            status = "exceeding"
        elif ch["delta"] > 0:
            status = "higher"
        elif ch["delta"] < 0:
            status = "lower"
        else:
            status = "same"
        rows.append({"name": name, "cur_paisa": c, "prev_paisa": p, **ch, "status": status})
    return rows


def assets_summary(conn, total_net_to_date_from: str | None = None) -> dict:
    rows = [r for r in q.assets(conn) if not r["voided"]]
    return {"count": len(rows), "total_paisa": sum(r["price_paisa"] for r in rows), "first_on": min((r["on_date"] for r in rows), default=None),
            "items": [{"name": r["name"], "on": r["on_date"], "price_paisa": r["price_paisa"]} for r in rows]}


def payback(conn, assets: dict, cur: dict, cur_net: int, today: date) -> dict:
    """How fast the profit is winning back the money put into assets. Speed = net profit per day over the days of this period that have passed."""
    total = assets["total_paisa"]
    out = {"assets_paisa": total, "has_assets": total > 0, "recovered_paisa": 0, "recovered_bps": 0, "remaining_paisa": total,
           "per_month_paisa": None, "months_to_recover_x10": None, "days_to_recover": None, "recover_on": None, "yearly_return_bps": None, "state": "no_assets"}
    if total <= 0:
        return out
    first = assets["first_on"]
    end = min(today, date.fromisoformat(cur["end"])).isoformat()
    earned = acct.income_statement(conn, first, end)["net_profit"] if first and first <= end else 0
    recovered = max(0, min(total, earned))
    out.update(recovered_paisa=recovered, recovered_bps=_bps(recovered, total) or 0, remaining_paisa=total - recovered)
    days = cur["days_elapsed"]
    if days <= 0:
        out["state"] = "no_data"
        return out
    # per month = net * 30.4375 / days, rounded half away from zero
    num = abs(cur_net) * DAYS_PER_MONTH_X10000
    per_month = (2 * num + 10000 * days) // (2 * 10000 * days)
    per_month = -per_month if cur_net < 0 else per_month
    out["per_month_paisa"] = per_month
    out["yearly_return_bps"] = _bps(per_month * 12, total)
    if out["remaining_paisa"] == 0:
        out["state"] = "recovered"
        return out
    if cur_net <= 0:
        out["state"] = "not_recovering"
        return out
    need_days = -((-out["remaining_paisa"] * days) // cur_net)                 # ceil
    out.update(state="recovering", days_to_recover=need_days, recover_on=(today + timedelta(days=need_days)).isoformat(),
               months_to_recover_x10=(need_days * 100000 + DAYS_PER_MONTH_X10000 // 2) // DAYS_PER_MONTH_X10000)
    return out


# ------------------------------------------------------------------ the whole page
def compare(conn, today: date, kind: str = "month", offset: int = 0, fy_start_month: int = 7, custom=None) -> dict:
    per = periods(kind, today, offset, fy_start_month, custom)
    cur, prev = _block(conn, per["cur"]), _block(conn, per["prev"])
    cl, pl = per["cur"]["label"], per["prev"]["label"]
    exp_rows = _expense_rows(cur["expenses"], prev["expenses"])
    inv_cur, inv_prev = inventory_use(conn, per["cur"]["start"], per["cur"]["end"]), inventory_use(conn, per["prev"]["start"], per["prev"]["end"])
    assets = assets_summary(conn)
    pay = payback(conn, assets, per["cur"], cur["net_profit_paisa"], today)
    elec_c, elec_p = cur["expenses"].get("Electricity & Utilities", 0), prev["expenses"].get("Electricity & Utilities", 0)
    ts = date.fromisoformat(per["cur"]["start"])
    n = 12 if kind == "year" else 6
    first_idx = date.fromisoformat(per["cur"]["end"]).year * 12 + date.fromisoformat(per["cur"]["end"]).month - 1 - (n - 1)
    trend = monthly_series(conn, _first_of(first_idx).isoformat(), per["cur"]["end"])
    out = {
        "periods": per, "cur": cur, "prev": prev,
        "change": {k: _change(cur[k], prev[k]) for k in ("sales_paisa", "net_profit_paisa", "expenses_total_paisa", "production_cost_paisa", "operating_expenses_paisa")},
        "expense_rows": exp_rows,
        "electricity": {"cur_paisa": elec_c, "prev_paisa": elec_p, **_change(elec_c, elec_p), "cur_minutes": cur["machine_minutes"], "prev_minutes": prev["machine_minutes"]},
        "assets": assets, "payback": pay,
        "inventory": {"cur": inv_cur, "prev": inv_prev, "cur_stock": cur["stock"], "prev_stock": prev["stock"]},
        "trend": trend, "sales_split": _sales_split(cur),
    }
    out["story"] = _story(out, cl, pl)
    del ts
    return out


def _sales_split(cur: dict) -> list[dict]:
    """Where each rupee of sales goes: production cost, operating expenses, and what is left as profit (when there is one)."""
    sales = cur["sales_paisa"]
    parts = [("Production cost", cur["production_cost_paisa"]), ("Operating expenses", cur["operating_expenses_paisa"])]
    if cur["net_profit_paisa"] > 0:
        parts.append(("Net profit", cur["net_profit_paisa"]))
    return [{"name": n, "paisa": v, "bps": _bps(v, sales) if sales > 0 else None} for n, v in parts if v > 0]


def _word(delta: int, up: str, down: str) -> str:
    return up if delta > 0 else down


def _story(o: dict, cl: str, pl: str) -> dict:
    cur, prev, ch = o["cur"], o["prev"], o["change"]
    net = cur["net_profit_paisa"]
    s: dict = {}
    run = ""
    if net > 0:
        s["verdict"] = ("good", f"{cl} is in PROFIT: net profit {rs(net)}{run}.")
    elif net < 0:
        s["verdict"] = ("bad", f"{cl} is in LOSS: {rs(-net)} lost{run}.")
    else:
        s["verdict"] = ("neutral", f"{cl} is at break-even{run}.")
    d = ch["net_profit_paisa"]
    s["net"] = ("good" if d["delta"] > 0 else "bad" if d["delta"] < 0 else "neutral",
                f"Net profit is {rs(abs(d['delta']))}" + (f" ({pct(d['pct_bps'])})" if d["pct_bps"] is not None else "") + f" {_word(d['delta'], 'higher', 'lower')} than {pl} ({rs(prev['net_profit_paisa'])})."
                if d["delta"] else f"Net profit is the same as {pl}.")
    d = ch["sales_paisa"]
    s["sales"] = ("good" if d["delta"] > 0 else "bad" if d["delta"] < 0 else "neutral",
                  f"Sales are {rs(cur['sales_paisa'])} against {rs(prev['sales_paisa'])} in {pl}"
                  + (f": {_word(d['delta'], 'up', 'down')} {pct(d['pct_bps'])}." if d["pct_bps"] is not None else "."))
    rows = o["expense_rows"]
    ex = [r for r in rows if r["status"] in ("exceeding", "new")]
    if ex:
        names = ", ".join(f"{r['name']} (+{rs(r['delta'])})" for r in sorted(ex, key=lambda r: -r["delta"]))
        s["expenses"] = ("bad", f"Exceeding: {names}.")
    elif rows:
        s["expenses"] = ("good", "No expense category has grown by 10% or more.")
    else:
        s["expenses"] = ("neutral", "No expenses were recorded in these periods.")
    tc, tp = cur["expenses_total_paisa"], prev["expenses_total_paisa"]
    if tc or tp:
        big, small, bl = (("cur", "prev", cl) if tc >= tp else ("prev", "cur", pl))
        top = max(rows, key=lambda r: r[f"{big}_paisa"] - r[f"{small}_paisa"], default=None)
        why = f", mostly because of {top['name']}" if top and top[f"{big}_paisa"] > top[f"{small}_paisa"] else ""
        s["heavier"] = ("neutral", f"More was spent in {bl} ({rs(max(tc, tp))} against {rs(min(tc, tp))}){why}." if tc != tp else f"Expenses are the same in both periods ({rs(tc)}).")
    else:
        s["heavier"] = ("neutral", "")
    e = o["electricity"]
    if e["cur_paisa"] or e["prev_paisa"]:
        if e["delta"]:
            s["electricity"] = ("bad" if e["delta"] > 0 else "good",
                                f"Electricity & utilities cost {rs(e['cur_paisa'])} in {cl} against {rs(e['prev_paisa'])} in {pl}: you used "
                                f"{'more' if e['delta'] > 0 else 'less'} ({rs(abs(e['delta']))}" + (f", {pct(e['pct_bps'])}" if e["pct_bps"] is not None else "") + ").")
        else:
            s["electricity"] = ("neutral", f"Electricity cost is the same in both periods ({rs(e['cur_paisa'])}).")
    else:
        s["electricity"] = ("neutral", "No electricity bill is recorded in either period. Record it as an expense ('Electricity & Utilities').")
    p = o["payback"]
    if p["state"] == "no_assets":
        s["payback"] = ("neutral", "No assets are recorded yet. Add machines or construction on the Expenses > Assets tab to see the payback speed.")
    elif p["state"] == "recovered":
        s["payback"] = ("good", f"The profit earned since the first asset ({rs(p['recovered_paisa'])}) already covers the {rs(p['assets_paisa'])} invested.")
    elif p["state"] == "recovering":
        months = p["months_to_recover_x10"] / 10
        s["payback"] = ("good", f"At the current speed ({rs(p['per_month_paisa'])} profit a month) the remaining {rs(p['remaining_paisa'])} comes back in about "
                                f"{months:g} months, around {date.fromisoformat(p['recover_on']):%d %b %Y}.")
    elif p["state"] == "not_recovering":
        s["payback"] = ("bad", f"The factory is not earning a profit in {cl}, so the {rs(p['remaining_paisa'])} still to recover is not coming back at the moment.")
    else:
        s["payback"] = ("neutral", "This period has not started yet, so there is no speed to measure.")
    ic, i0 = o["inventory"]["cur"], o["inventory"]["prev"]
    if ic["used"]:
        top = ic["used"][0]
        s["inventory"] = ("neutral", f"{top['name']} used the most stock: {rs(top['cost_paisa'])}" + (f" ({pct(top['share_bps'])} of all stock used)." if top["share_bps"] is not None else "."))
    else:
        s["inventory"] = ("neutral", "No stock was used in production in this period.")
    cs, ps = o["inventory"]["cur_stock"], o["inventory"]["prev_stock"]
    loss = cs["wastage_paisa"] + cs["adjustments_paisa"]
    if loss > 0:
        s["losses"] = ("bad", f"Wastage and stock adjustments cost {rs(loss)} in {cl} (it was {rs(ps['wastage_paisa'] + ps['adjustments_paisa'])} in {pl}); this comes straight off the profit.")
    else:
        s["losses"] = ("good", "No wastage or stock losses were booked in this period.")
    del i0
    return s
