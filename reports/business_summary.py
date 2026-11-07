"""Complete Business Summary: one PDF (about 7-9 A4 pages) with the numbers and charts of the Dashboard and Insights pages, plus salaries,
stock (opening / received / used / closing), production entries, customers, suppliers and partners - for a month, a financial year or any dates.

Every figure is read from the books and stock records when the file is made; nothing is typed in or estimated. Money is whole paisa until
it is printed. Reports only read - nothing is changed in the database.
"""
from __future__ import annotations

import io
from datetime import date
from decimal import Decimal
from pathlib import Path

from services import accounting_service as acct, dashboard_service as ds, insights_service as ins, queries as q, settings_service
from services.units import mul_div_round

INDIGO, GREY, ROSE, GREEN, AMBER, TEAL, INK, MUTED, LINE = "#4F46E5", "#9CA3AF", "#E11D48", "#16A34A", "#D97706", "#0D9488", "#111827", "#6B7280", "#E5E7EB"
CYCLE = [INDIGO, TEAL, AMBER, ROSE, GREEN, "#7C3AED", "#0EA5E9", "#DB2777"]


class SummaryError(ValueError):
    """The chosen period cannot be summarised (for example, it lies in the future)."""


# ------------------------------------------------------------------ small formatters
def rs(p) -> str:
    if p is None:
        return "-"
    return ("-" if p < 0 else "") + "Rs " + f"{abs(Decimal(int(p))) / 100:,.2f}"


def short(rupees: float) -> str:
    a = abs(rupees)
    for div, suf in ((10_000_000, " Cr"), (100_000, " Lac"), (1_000, " K")):
        if a >= div:
            return f"{rupees / div:,.1f}".rstrip("0").rstrip(".") + suf
    return f"{rupees:,.0f}"


def pct(bps) -> str:
    return "-" if bps is None else f"{Decimal(bps) / 100:.1f}%"


def qty(milli, unit="") -> str:
    d = Decimal(int(milli or 0)) / 1000
    s = f"{d:,.3f}".rstrip("0").rstrip(".")
    if unit == "roll" and d != 1:
        unit = "rolls"
    return f"{s} {unit}".strip()


def meters(milli) -> str:
    s = f"{(Decimal(int(milli or 0)) / 1000).quantize(Decimal('0.1'), rounding='ROUND_HALF_UP'):,.1f}".rstrip("0").rstrip(".")
    return f"{s} m"


def qty_full(milli, unit, roll_len) -> str:
    t = qty(milli, unit)
    if unit == "roll" and roll_len:
        t += f" ({meters(mul_div_round(int(milli or 0), roll_len, 1000))})"
    return t


def delta_text(ch: dict, lower_is_good: bool = False) -> str:
    d = ch["delta"]
    if d == 0:
        return "no change"
    p = "" if ch["pct_bps"] is None else f" ({pct(abs(ch['pct_bps']))})"
    return ("up " if d > 0 else "down ") + rs(abs(d)) + p


# ------------------------------------------------------------------ choosing the period
def month_choices(conn, today: date | None = None) -> list[tuple[str, str]]:
    """[(label, 'YYYY-MM')] newest first, from the first month that has any bookkeeping up to this month."""
    today = today or date.today()
    first = conn.execute("SELECT MIN(entry_date) FROM journal_entries").fetchone()[0]
    start = date.fromisoformat(first[:7] + "-01") if first else today.replace(day=1)
    out, y, m = [], today.year, today.month
    while (y, m) >= (start.year, start.month):
        out.append((date(y, m, 1).strftime("%B %Y"), f"{y}-{m:02d}"))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out


def year_choices(conn, today: date | None = None) -> list[tuple[str, int]]:
    """[(label, financial-year start year)] newest first."""
    today = today or date.today()
    fy = int(settings_service.get(conn, "fiscal_year_start_month", "7") or 7)
    cur = today.year if today.month >= fy else today.year - 1
    first = conn.execute("SELECT MIN(entry_date) FROM journal_entries").fetchone()[0]
    f = date.fromisoformat(first) if first else today
    firsty = f.year if f.month >= fy else f.year - 1
    out = []
    for y in range(cur, min(firsty, cur) - 1, -1):
        s = date(y, fy, 1)
        e = date(y + (1 if fy > 1 else 0), fy - 1 if fy > 1 else 12, 1)
        out.append((str(y) if fy == 1 else f"{y}-{str(y + 1)[2:]}  (from {s.strftime('%b %Y')})", y))
    return out


def resolve(conn, kind: str, today: date, *, month: str | None = None, fy_year: int | None = None, custom=None) -> dict:
    """The period to report on (and the one before it) as insights_service.periods() describes them."""
    fy = int(settings_service.get(conn, "fiscal_year_start_month", "7") or 7)
    if kind == "month":
        if not month:
            raise SummaryError("Choose the month.")
        y, m = int(month[:4]), int(month[5:7])
        offset = (y * 12 + m) - (today.year * 12 + today.month)
        per = ins.periods("month", today, offset, fy)
    elif kind == "year":
        if fy_year is None:
            raise SummaryError("Choose the year.")
        cur = today.year if today.month >= fy else today.year - 1
        per = ins.periods("year", today, fy_year - cur, fy)
    elif kind == "custom":
        try:
            per = ins.periods("custom", today, 0, fy, custom)
        except ValueError as e:
            raise SummaryError(str(e)) from e
    else:
        raise SummaryError(f"Unknown period: {kind}")
    if per["cur"]["future"]:
        raise SummaryError("That period has not started yet - there is nothing to summarise.")
    return per


# ------------------------------------------------------------------ gathering the figures
def _sum_codes(conn, codes, start, end) -> int:
    ph = ",".join("?" * len(codes))
    return conn.execute(f"SELECT COALESCE(SUM(l.debit_paisa-l.credit_paisa),0) FROM journal_lines l JOIN journal_entries e ON e.id=l.entry_id "
                        f"JOIN accounts a ON a.id=l.account_id WHERE a.code IN ({ph}) AND e.entry_date BETWEEN ? AND ?", (*codes, start, end)).fetchone()[0]


def stock_table(conn, start: str, end: str) -> list[dict]:
    """Per material (factory-owned stock): opening, received, used in production, lost/returned/adjusted, closing - quantity and value as of the dates."""
    d = "COALESCE(mv.moved_on, substr(mv.created_at,1,10))"
    sql = (f"SELECT m.id, m.name, m.category, m.base_unit, m.roll_length_milli, "
           f"COALESCE(SUM(CASE WHEN {d}<? THEN mv.qty_milli END),0) oq, COALESCE(SUM(CASE WHEN {d}<? THEN mv.value_paisa END),0) ov, "
           f"COALESCE(SUM(CASE WHEN {d} BETWEEN ? AND ? AND mv.qty_milli>0 THEN mv.qty_milli END),0) iq, "
           f"COALESCE(SUM(CASE WHEN {d} BETWEEN ? AND ? AND mv.qty_milli>0 THEN mv.value_paisa END),0) iv, "
           f"COALESCE(SUM(CASE WHEN {d} BETWEEN ? AND ? AND mv.movement_type='production' THEN -mv.qty_milli END),0) uq, "
           f"COALESCE(SUM(CASE WHEN {d} BETWEEN ? AND ? AND mv.movement_type='production' THEN -mv.value_paisa END),0) uv, "
           f"COALESCE(SUM(CASE WHEN {d} BETWEEN ? AND ? AND mv.qty_milli<0 AND mv.movement_type<>'production' THEN -mv.qty_milli END),0) xq, "
           f"COALESCE(SUM(CASE WHEN {d} BETWEEN ? AND ? AND mv.qty_milli<0 AND mv.movement_type<>'production' THEN -mv.value_paisa END),0) xv, "
           f"COALESCE(SUM(CASE WHEN {d}<=? THEN mv.qty_milli END),0) cq, COALESCE(SUM(CASE WHEN {d}<=? THEN mv.value_paisa END),0) cv "
           f"FROM materials m LEFT JOIN stock_movements mv ON mv.material_id=m.id AND mv.ownership='factory' GROUP BY m.id ORDER BY m.name")
    args = (start, start, start, end, start, end, start, end, start, end, start, end, start, end, end, end)
    out = []
    for r in conn.execute(sql, args):
        r = dict(r)
        if any(r[k] for k in ("oq", "ov", "iq", "iv", "uq", "uv", "xq", "xv", "cq", "cv")):
            out.append(r)
    return out


def gather(conn, per: dict, today: date) -> dict:
    """Everything the PDF prints, as plain numbers and strings."""
    s, e = per["cur"]["start"], per["cur"]["end"]
    # insights_service builds its page from a period kind + offset; use the same building blocks here so any chosen month / year / dates work
    cur, prev = ins._block(conn, per["cur"]), ins._block(conn, per["prev"])
    exp_rows = ins._expense_rows(cur["expenses"], prev["expenses"])
    inv_cur, inv_prev = ins.inventory_use(conn, s, e), ins.inventory_use(conn, per["prev"]["start"], per["prev"]["end"])
    assets = ins.assets_summary(conn)
    pay = ins.payback(conn, assets, per["cur"], cur["net_profit_paisa"], today)
    ec, ep = cur["expenses"].get("Electricity & Utilities", 0), prev["expenses"].get("Electricity & Utilities", 0)
    o = {"periods": per, "cur": cur, "prev": prev,
         "change": {k: ins._change(cur[k], prev[k]) for k in ("sales_paisa", "net_profit_paisa", "expenses_total_paisa", "production_cost_paisa", "operating_expenses_paisa")},
         "expense_rows": exp_rows, "electricity": {"cur_paisa": ec, "prev_paisa": ep, **ins._change(ec, ep)},
         "assets": assets, "payback": pay, "inventory": {"cur": inv_cur, "prev": inv_prev, "cur_stock": cur["stock"], "prev_stock": prev["stock"]},
         "sales_split": ins._sales_split(cur)}
    n = 12 if per["kind"] == "year" else 6
    last = date.fromisoformat(e)
    first_idx = last.year * 12 + last.month - 1 - (n - 1)
    o["trend"] = ds.monthly_series(conn, ins._first_of(first_idx).isoformat(), e)
    o["story"] = ins._story(o, per["cur"]["label"], per["prev"]["label"])
    snap = ds.snapshot(conn, s, e)

    # salaries
    p0, p1 = s[:7], e[:7]
    slips = [dict(r) for r in q.payslips(conn) if p0 <= r["period"] <= p1]
    slips.sort(key=lambda r: (r["period"], r["name"]))
    paid_in = conn.execute("SELECT COALESCE(SUM(amount_paisa),0) FROM payslip_payments WHERE paid_on BETWEEN ? AND ?", (s, e)).fetchone()[0]
    att = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM attendance WHERE work_date BETWEEN ? AND ? GROUP BY status", (s, e))}
    sal = {"slips": slips, "net_total": sum(r["net_paisa"] for r in slips), "due_total": sum(r["due_paisa"] for r in slips), "paid_in_period": paid_in,
           "direct": _sum_codes(conn, ("5100",), s, e), "admin": _sum_codes(conn, ("6100",), s, e), "attendance": att,
           "prev_direct": _sum_codes(conn, ("5100",), per["prev"]["start"], per["prev"]["end"]),
           "prev_admin": _sum_codes(conn, ("6100",), per["prev"]["start"], per["prev"]["end"])}

    # production + invoices
    entries = [r for r in q.production_entries(conn) if s <= (r["order_date"] or "") <= e]
    entries.sort(key=lambda r: (r["order_date"] or "", r["job_no"]))
    invs = [r for r in q.invoices(conn) if s <= r["invoice_date"] <= e]
    by_cust: dict[str, dict] = {}
    for r in invs:
        c = by_cust.setdefault(r["customer"], {"customer": r["customer"], "billed": 0, "paid": 0, "n": 0})
        c["billed"] += r["total_paisa"]; c["paid"] += r["paid_paisa"]; c["n"] += 1
    received = [r for r in q.receipts(conn, 100000) if s <= r["entry_date"] <= e]
    prod = {"entries": entries, "invoices": invs, "customers": sorted(by_cust.values(), key=lambda c: -c["billed"]),
            "billed": sum(r["total_paisa"] for r in invs), "received": sum(r["amount_paisa"] for r in received), "received_n": len(received),
            "meters": sum(int(r["quantity_pieces"] or 0) for r in entries)}
    return {"o": o, "snap": snap, "sal": sal, "prod": prod, "stock": stock_table(conn, s, e), "today": today}


# ------------------------------------------------------------------ charts (white background, made for print)
def _fig(w_mm: float, h_mm: float):
    from matplotlib.figure import Figure
    return Figure(figsize=(w_mm / 25.4, h_mm / 25.4), dpi=200, facecolor="white")


def _png(fig) -> io.BytesIO:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    FigureCanvasAgg(fig)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, facecolor="white")
    buf.seek(0)
    return buf


def _style(ax, grid="y"):
    ax.set_facecolor("white")
    ax.tick_params(colors=MUTED, labelsize=7, length=0)
    for sp in ax.spines.values():
        sp.set_visible(False)
    if grid:
        ax.grid(axis=grid, color=LINE, lw=0.6)
    ax.set_axisbelow(True)


def _bold(fig):
    from matplotlib.text import Text
    for t in fig.findobj(Text):
        t.set_fontweight("bold")


def _empty(fig, text):
    fig.text(0.5, 0.5, text, ha="center", va="center", color=MUTED, fontsize=8, fontweight="bold")
    return _png(fig)


def chart_bars(o, w, h):
    from matplotlib.ticker import FuncFormatter, MaxNLocator
    cur, prev, per = o["cur"], o["prev"], o["periods"]
    fig = _fig(w, h); ax = fig.add_subplot(111); _style(ax)
    names = ["Sales", "Production\ncost", "Operating\nexpenses", "Net profit"]
    keys = ["sales_paisa", "production_cost_paisa", "operating_expenses_paisa", "net_profit_paisa"]
    cv, pv = [cur[k] / 100 for k in keys], [prev[k] / 100 for k in keys]
    x = range(4); bw = 0.36
    b1 = ax.bar([i - bw / 2 - 0.02 for i in x], cv, bw, color=INDIGO, label=per["cur"]["label"], zorder=3)
    b2 = ax.bar([i + bw / 2 + 0.02 for i in x], pv, bw, color=GREY, label=per["prev"]["label"], zorder=3)
    for bars in (b1, b2):
        for r in bars:
            v = r.get_height()
            if v:
                ax.annotate(short(v), (r.get_x() + r.get_width() / 2, v), textcoords="offset points", xytext=(0, 2 if v >= 0 else -8), ha="center", fontsize=6.5, color=INK)
    ax.axhline(0, color=MUTED, lw=0.8, zorder=3)
    ax.set_xticks(list(x)); ax.set_xticklabels(names, color=INK, fontsize=7)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: short(v)))
    ax.legend(frameon=False, fontsize=7, loc="upper right", ncol=2)
    fig.tight_layout(); _bold(fig)
    return _png(fig)


def chart_trend(o, w, h):
    from matplotlib.ticker import FuncFormatter, MaxNLocator
    tr = o["trend"]
    fig = _fig(w, h); ax = fig.add_subplot(111); _style(ax)
    if not tr or not any(m["revenue"] or m["cost_of_production"] or m["operating_expenses"] or m["net_profit"] for m in tr):
        return _empty(fig, "No sales or costs in these months")
    xs = list(range(len(tr)))
    ax.plot(xs, [m["revenue"] / 100 for m in tr], color=INDIGO, lw=1.8, marker="o", ms=3, label="Sales", zorder=3)
    ax.plot(xs, [(m["cost_of_production"] + m["operating_expenses"]) / 100 for m in tr], color=ROSE, lw=1.8, marker="o", ms=3, label="Costs and expenses", zorder=3)
    ax.plot(xs, [m["net_profit"] / 100 for m in tr], color=GREEN, lw=1.8, marker="o", ms=3, label="Net profit", zorder=3)
    ax.axhline(0, color=MUTED, lw=0.8)
    ax.set_xticks(xs); ax.set_xticklabels([date.fromisoformat(m["month"] + "-01").strftime("%b %y") for m in tr], color=INK, fontsize=7)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: short(v)))
    ax.legend(frameon=False, fontsize=7, loc="upper left", ncol=3)
    fig.tight_layout(); _bold(fig)
    return _png(fig)


def _donut(fig, values, colors, labels, big, small):
    ax = fig.add_axes([0.0, 0.05, 0.42, 0.9])
    ax.pie(values, colors=colors, startangle=90, counterclock=False, wedgeprops=dict(width=0.38, edgecolor="white", linewidth=1.5))
    ax.text(0, 0.07, big, ha="center", va="center", fontsize=10, fontweight="bold", color=INK)
    ax.text(0, -0.2, small, ha="center", va="center", fontsize=6.5, color=MUTED, fontweight="bold")
    ax.set_aspect("equal")
    from matplotlib.patches import Patch
    fig.legend([Patch(color=c) for c in colors], labels, loc="center left", bbox_to_anchor=(0.45, 0.5), frameon=False, fontsize=6.5, handlelength=1, labelspacing=0.7)
    _bold(fig)


def chart_split(o, w, h):
    fig = _fig(w, h)
    parts = o["sales_split"]
    if not parts:
        return _empty(fig, "No sales in this period")
    cols = [{"Production cost": ROSE, "Operating expenses": AMBER, "Net profit": GREEN}.get(p["name"], GREY) for p in parts]
    _donut(fig, [p["paisa"] for p in parts], cols, [f"{p['name']}\n{pct(p['bps'])}  -  {rs(p['paisa'])}" for p in parts], short(o["cur"]["sales_paisa"] / 100), "of sales")
    return _png(fig)


def chart_expense_donut(o, w, h):
    fig = _fig(w, h)
    rows = [r for r in o["expense_rows"] if r["cur_paisa"] > 0]
    tot = o["cur"]["expenses_total_paisa"]
    if not rows or tot <= 0:
        return _empty(fig, "No expenses recorded in this period")
    cols = [CYCLE[i % len(CYCLE)] for i in range(len(rows))]
    _donut(fig, [r["cur_paisa"] for r in rows], cols, [f"{r['name'][:24]}\n{pct(ds._bps(r['cur_paisa'], tot))}  -  {rs(r['cur_paisa'])}" for r in rows], short(tot / 100), "Rs spent")
    return _png(fig)


def chart_expense_bars(o, w, h):
    from matplotlib.patches import Patch
    from matplotlib.ticker import FuncFormatter, MaxNLocator
    fig = _fig(w, h); ax = fig.add_subplot(111); _style(ax, "x"); ax.grid(axis="y", visible=False)
    rows = o["expense_rows"][:7]
    if not rows:
        ax.axis("off"); return _empty(fig, "No expenses to compare")
    ys = list(range(len(rows))); bh = 0.36
    ax.barh([i - bh / 2 - 0.02 for i in ys], [r["cur_paisa"] / 100 for r in rows], bh, color=[ROSE if r["status"] in ("exceeding", "new") else INDIGO for r in rows], zorder=3)
    ax.barh([i + bh / 2 + 0.02 for i in ys], [r["prev_paisa"] / 100 for r in rows], bh, color=GREY, zorder=3)
    ax.set_yticks(ys); ax.set_yticklabels([r["name"][:24] for r in rows], color=INK, fontsize=7); ax.set_ylim(max(len(rows), 4) - 0.5, -0.5)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: short(v)))
    ax.xaxis.set_major_locator(MaxNLocator(4))
    ax.legend([Patch(color=INDIGO), Patch(color=ROSE), Patch(color=GREY)], [o["periods"]["cur"]["label"], "Exceeding", o["periods"]["prev"]["label"]], frameon=False, fontsize=6.5, loc="lower right")
    fig.tight_layout(); _bold(fig)
    return _png(fig)


def chart_hbars(items, w, h, empty_text, color=INDIGO, notes=None):
    """items = [(label, rupees)] drawn as horizontal bars; notes = optional text drawn after each bar."""
    from matplotlib.ticker import FuncFormatter, MaxNLocator
    fig = _fig(w, h); ax = fig.add_subplot(111); _style(ax, "x"); ax.grid(axis="y", visible=False)
    items = [i for i in items if i[1]]
    if not items:
        ax.axis("off"); return _empty(fig, empty_text)
    ys = list(range(len(items)))
    bars = ax.barh(ys, [v for _l, v in items], 0.55, color=[CYCLE[i % len(CYCLE)] if color is None else color for i in ys], zorder=3)
    for i, b in enumerate(bars):
        ax.annotate(" " + (notes[i] if notes else short(items[i][1])), (b.get_width(), b.get_y() + b.get_height() / 2), va="center", fontsize=6.5, color=INK)
    ax.set_yticks(ys); ax.set_yticklabels([l[:22] for l, _v in items], color=INK, fontsize=7); ax.set_ylim(max(len(items), 4) - 0.5, -0.5)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: short(v))); ax.set_xlim(0, max(v for _l, v in items) * 1.4)
    ax.xaxis.set_major_locator(MaxNLocator(4))
    fig.tight_layout(); _bold(fig)
    return _png(fig)


def chart_two_hbars(items, w, h, empty_text, la, lb, cb=GREEN):
    """items = [(label, a, b)] - two bars per label (for example billed and received)."""
    from matplotlib.ticker import FuncFormatter, MaxNLocator
    fig = _fig(w, h); ax = fig.add_subplot(111); _style(ax, "x"); ax.grid(axis="y", visible=False)
    items = [i for i in items if i[1] or i[2]]
    if not items:
        ax.axis("off"); return _empty(fig, empty_text)
    ys = list(range(len(items))); bh = 0.36
    ax.barh([i - bh / 2 - 0.02 for i in ys], [a for _l, a, _b in items], bh, color=INDIGO, zorder=3, label=la)
    ax.barh([i + bh / 2 + 0.02 for i in ys], [b for _l, _a, b in items], bh, color=cb, zorder=3, label=lb)
    ax.set_yticks(ys); ax.set_yticklabels([l[:22] for l, _a, _b in items], color=INK, fontsize=7); ax.set_ylim(max(len(items), 4) - 0.5, -0.5)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: short(v)))
    ax.xaxis.set_major_locator(MaxNLocator(4))
    ax.legend(frameon=False, fontsize=6.5, loc="lower right")
    fig.tight_layout(); _bold(fig)
    return _png(fig)


def chart_payback(o, w, h):
    fig = _fig(w, h)
    p = o["payback"]
    if not p["has_assets"]:
        return _empty(fig, "No assets recorded yet")
    rec, rem = p["recovered_paisa"], p["remaining_paisa"]
    _donut(fig, [max(rec, 0.0001), max(rem, 0.0001)], [GREEN, GREY], [f"Won back\n{rs(rec)}", f"Still to win back\n{rs(rem)}"], pct(p["recovered_bps"]), "won back")
    return _png(fig)


# ------------------------------------------------------------------ the PDF
def _esc(s) -> str:
    return str(s if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _styles():
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    base = dict(fontName="Helvetica", textColor=colors.HexColor(INK), leading=12.5)
    return {
        "body": ParagraphStyle("body", fontSize=9, **base),
        "small": ParagraphStyle("small", fontSize=7.5, **{**base, "textColor": colors.HexColor(MUTED), "leading": 10}),
        "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=17, leading=21, textColor=colors.HexColor(INK), spaceAfter=2),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=10.5, leading=14, textColor=colors.HexColor(INK), spaceBefore=9, spaceAfter=3),
        "sub": ParagraphStyle("sub", fontSize=8.5, **{**base, "textColor": colors.HexColor(MUTED)}),
        "cell": ParagraphStyle("cell", fontSize=7.8, **{**base, "leading": 9.6}),
        "cellb": ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=7.8, leading=9.6, textColor=colors.HexColor(INK)),
        "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=26, leading=30, textColor=colors.HexColor(INK)),
    }


def _table(rows, widths_mm, *, right_from=1, total_last=False, zebra=True, size=7.8):
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import Table, TableStyle
    t = Table(rows, colWidths=[w * mm for w in widths_mm], repeatRows=1)
    st = [("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), size), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor(MUTED)),
          ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(INK)), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
          ("ALIGN", (right_from, 0), (-1, -1), "RIGHT"), ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor(LINE))]
    if zebra:
        for i in range(2, len(rows), 2):
            st.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor("#F9FAFB")))
    if total_last and len(rows) > 1:
        st += [("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"), ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor(INK))]
    t.setStyle(TableStyle(st))
    return t


def _side(a, b, wa=91, wb=91):
    from reportlab.lib.units import mm
    from reportlab.platypus import Table, TableStyle
    t = Table([[a, b]], colWidths=[wa * mm, wb * mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                           ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
    return t


def _img(buf, w, h):
    from reportlab.lib.units import mm
    from reportlab.platypus import Image
    return Image(buf, width=w * mm, height=h * mm)


def _tiles(items, cols=4):
    """items = [(label, value, note, tone)] as a grid of KPI tiles."""
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, Table, TableStyle
    tone = {"good": GREEN, "bad": ROSE, "neutral": INDIGO, None: INDIGO}
    cells = []
    for lab, val, note, t in items:
        c = tone.get(t, INDIGO)
        cells.append([Paragraph(f'<font color="{MUTED}" size="7.5">{_esc(lab)}</font>', ParagraphStyle("a", fontName="Helvetica", leading=10)),
                      Paragraph(f'<font color="{c}" size="12.5"><b>{_esc(val)}</b></font>', ParagraphStyle("b", fontName="Helvetica-Bold", leading=16)),
                      Paragraph(f'<font color="{MUTED}" size="6.8">{_esc(note)}</font>', ParagraphStyle("c", fontName="Helvetica", leading=8.5))])
    rows = [cells[i:i + cols] for i in range(0, len(cells), cols)]
    for r in rows:
        r.extend([""] * (cols - len(r)))
    w = 182 / cols
    from reportlab.platypus import Table
    outer = Table([[Table([[x] for x in cell], colWidths=[(w - 3) * mm]) if cell != "" else "" for cell in r] for r in rows], colWidths=[w * mm] * cols)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 3), ("TOPPADDING", (0, 0), (-1, -1), 3),
          ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    outer.setStyle(TableStyle(st))
    # a coloured rule above each tile
    for ri, r in enumerate(rows):
        for ci, cell in enumerate(r):
            if cell != "":
                inner = outer._cellvalues[ri][ci]
                inner.setStyle(TableStyle([("LINEABOVE", (0, 0), (-1, 0), 1.6, colors.HexColor(tone.get(items[ri * cols + ci][3], INDIGO))),
                                           ("LEFTPADDING", (0, 0), (-1, -1), 1), ("TOPPADDING", (0, 0), (-1, -1), 1), ("BOTTOMPADDING", (0, 0), (-1, -1), 1)]))
    return outer


class _Numbered:
    """Canvas factory that prints 'Page n of N' and the running header on every page after the cover."""

    @staticmethod
    def make(header_left: str, header_right: str):
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.pdfgen import canvas

        class C(canvas.Canvas):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self._saved = []

            def showPage(self):
                self._saved.append(dict(self.__dict__))
                self._startPage()

            def save(self):
                n = len(self._saved)
                for st in self._saved:
                    self.__dict__.update(st)
                    self._decorate(n)
                    super().showPage()
                super().save()

            def _decorate(self, n):
                self.setFont("Helvetica", 7.5)
                self.setFillColor(colors.HexColor(MUTED))
                self.drawString(14 * mm, 9 * mm, header_left)
                self.drawRightString(196 * mm, 9 * mm, f"Page {self._pageNumber} of {n}")
                self.setStrokeColor(colors.HexColor(LINE)); self.setLineWidth(0.5)
                self.line(14 * mm, 12.5 * mm, 196 * mm, 12.5 * mm)
                if self._pageNumber > 1:
                    self.drawRightString(196 * mm, 285 * mm, header_right)
        return C


def _story_lines(o, keys, S):
    from reportlab.platypus import Paragraph
    out = []
    col = {"good": GREEN, "bad": ROSE, "neutral": MUTED}
    for k in keys:
        v = o["story"].get(k)
        if v:
            out.append(Paragraph(f'<font color="{col.get(v[0], MUTED)}">&bull;</font>&nbsp; {_esc(v[1])}', S["body"]))
    return out


def build_flowables(g: dict, conn) -> list:
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, Spacer
    S = _styles()
    o, snap, sal, prod, stock = g["o"], g["snap"], g["sal"], g["prod"], g["stock"]
    per, cur, prev = o["periods"], o["cur"], o["prev"]
    cl, pl = per["cur"]["label"], per["prev"]["label"]
    P = lambda t, st="body": Paragraph(t, S[st])  # noqa: E731
    f: list = []
    factory = settings_service.get(conn, "factory_name", "Factory")
    perf, pos = snap["performance"], snap["position"]

    def head(title, sub=""):
        f.append(P(_esc(title), "h1"))
        if sub:
            f.append(P(_esc(sub), "sub"))
        f.append(Spacer(1, 4 * mm))

    # ---------------- page 1: cover + summary
    f.append(P(_esc(factory), "sub"))
    f.append(P("Complete Business Summary", "title"))
    f.append(Spacer(1, 1.5 * mm))
    running = " (still running - figures so far)" if per["cur"]["running"] else ""
    f.append(P(f"<b>{_esc(cl)}</b> &nbsp; {per['cur']['start']} to {per['cur']['end']}{running}", "body"))
    f.append(P(f"Compared with {_esc(pl)} ({per['prev']['start']} to {per['prev']['end']}). Made on {g['today'].isoformat()}. All figures come from the factory books and stock records.", "sub"))
    f.append(Spacer(1, 5 * mm))
    ch = o["change"]
    tone = lambda v: "good" if v > 0 else "bad" if v < 0 else "neutral"  # noqa: E731
    f.append(_tiles([
        ("Sales", rs(cur["sales_paisa"]), f"{delta_text(ch['sales_paisa'])} vs {pl}", tone(ch["sales_paisa"]["delta"])),
        ("Total expenses (Expenses page)", rs(cur["expenses_total_paisa"]), f"{delta_text(ch['expenses_total_paisa'])} vs {pl}", "neutral"),
        ("Gross profit", rs(cur["gross_profit_paisa"]), f"{pct(ds._bps(cur['gross_profit_paisa'], cur['sales_paisa']))} of sales", tone(cur["gross_profit_paisa"])),
        ("Net profit" if cur["net_profit_paisa"] >= 0 else "Net loss", rs(cur["net_profit_paisa"]), f"{pct(cur['net_margin_bps'])} of sales  |  {delta_text(ch['net_profit_paisa'])} vs {pl}", tone(cur["net_profit_paisa"])),
        ("Cash in hand", rs(pos["cash_paisa"]), f"as of {per['cur']['end']}", "good" if pos["cash_paisa"] >= 0 else "bad"),
        ("Customers have to pay you", rs(pos["receivables_paisa"]), f"{len(snap['receivables'])} customer(s)", "neutral"),
        ("You have to pay suppliers", rs(pos["payables_paisa"]), f"{len(snap['payables'])} supplier(s)", "neutral"),
        ("Inventory value", rs(g["stock_close_value"]), f"stock on hand at {per['cur']['end']}", "neutral"),
    ]))
    f.append(Spacer(1, 3 * mm))
    f.append(_tiles([
        ("Production cost", rs(cur["production_cost_paisa"]), f"direct {rs(perf['direct_production_paisa'])} + overhead {rs(perf['factory_overhead_paisa'])}", "neutral"),
        ("Operating expenses", rs(cur["operating_expenses_paisa"]), "Chai Paani, office, admin salaries, other", "neutral"),
        ("Unpaid salaries", rs(pos["salaries_payable_paisa"]), f"{len(snap['unpaid_salaries'])} payslip(s)", "neutral"),
        ("Production entries", str(len(prod["entries"])), f"{meters(prod['meters'] * 1000)} produced", "neutral"),
    ]))
    f.append(P("The picture in words", "h2"))
    f += _story_lines(o, ("verdict", "sales", "profit", "expenses", "heavier", "electricity", "payback", "inventory", "losses"), S)
    f.append(P("What is inside", "h2"))
    f.append(P("Page 2 Sales and profit &nbsp;|&nbsp; Page 3 Expenses &nbsp;|&nbsp; Page 4 Salaries &nbsp;|&nbsp; Page 5 Inventory &nbsp;|&nbsp; Page 6 Production and invoices &nbsp;|&nbsp; "
               "Page 7 Money position, customers, suppliers, partners and assets &nbsp;|&nbsp; Page 8 Notes and data checks.", "small"))
    f.append(PageBreak())

    # ---------------- page 2: sales and profit
    head("Sales and profit", f"{cl} compared with {pl}")
    rows = [["", cl, pl, "Change"]]
    for lab, k in (("Sales", "sales_paisa"), ("Production cost", "production_cost_paisa"), ("Gross profit", "gross_profit_paisa"),
                   ("Operating expenses", "operating_expenses_paisa"), ("Net profit", "net_profit_paisa")):
        d = cur[k] - prev[k]
        rows.append([lab, rs(cur[k]), rs(prev[k]), ("+" if d > 0 else "") + rs(d)])
    rows.append(["Net margin", pct(cur["net_margin_bps"]), pct(prev["net_margin_bps"]), ""])
    f.append(_table(rows, [64, 40, 40, 38]))
    f.append(P("Net profit = sales - production cost - operating expenses. Profit is an accounting figure, not the same as cash.", "small"))
    f.append(P("This period against the one before", "h2"))
    f.append(_side(_img(chart_bars(o, 91, 62), 91, 62), _img(chart_split(o, 91, 62), 91, 62)))
    f.append(P("Last months", "h2"))
    f.append(_img(chart_trend(o, 182, 60), 182, 60))
    f.append(P("Monthly figures", "h2"))
    rows = [["Month", "Sales", "Production cost", "Operating expenses", "Net profit"]]
    for m in o["trend"]:
        rows.append([date.fromisoformat(m["month"] + "-01").strftime("%b %Y"), rs(m["revenue"]), rs(m["cost_of_production"]), rs(m["operating_expenses"]), rs(m["net_profit"])])
    f.append(_table(rows, [30, 38, 38, 40, 36]))
    f.append(PageBreak())

    # ---------------- page 3: expenses
    head("Expenses", "Only what is recorded on the Expenses page. Salaries and the stock used in production are shown on their own pages.")
    f.append(_tiles([
        ("Total expenses", rs(cur["expenses_total_paisa"]), f"{delta_text(ch['expenses_total_paisa'])} vs {pl}", "neutral"),
        ("Previous period", rs(prev["expenses_total_paisa"]), pl, "neutral"),
        ("Electricity & utilities", rs(o["electricity"]["cur_paisa"]), f"{delta_text(o['electricity'])} vs {pl}", "neutral"),
        ("Categories", str(len([r for r in o["expense_rows"] if r["cur_paisa"] > 0])), "with spending in this period", "neutral"),
    ]))
    f.append(Spacer(1, 3 * mm))
    f.append(_side(_img(chart_expense_donut(o, 91, 62), 91, 62), _img(chart_expense_bars(o, 91, 62), 91, 62)))
    f.append(P("Category by category", "h2"))
    rows = [["Category", cl, pl, "Change", "Status"]]
    word = {"exceeding": "Exceeding", "new": "New", "higher": "Higher", "lower": "Lower", "same": "Same"}
    for r in o["expense_rows"]:
        d = r["delta"]
        rows.append([r["name"], rs(r["cur_paisa"]), rs(r["prev_paisa"]), ("+" if d > 0 else "") + rs(d) if d else "-", word.get(r["status"], r["status"])])
    rows.append(["Total", rs(cur["expenses_total_paisa"]), rs(prev["expenses_total_paisa"]), rs(cur["expenses_total_paisa"] - prev["expenses_total_paisa"]), ""])
    if len(rows) == 2:
        rows.insert(1, ["No expenses were recorded", "-", "-", "-", ""])
    f.append(_table(rows, [58, 36, 36, 32, 20], total_last=True))
    f.append(P("Exceeding = the category grew by 10% or more. Cancelled (reversed) entries are not counted.", "small"))
    f.append(Spacer(1, 2 * mm))
    f += _story_lines(o, ("expenses", "heavier", "electricity"), S)
    f.append(PageBreak())

    # ---------------- page 4: salaries
    head("Salaries", f"Payslips for {cl}, what was paid and what is still to pay")
    att = sal["attendance"]
    f.append(_tiles([
        ("Salaries booked (net)", rs(sal["net_total"]), f"{len(sal['slips'])} payslip(s)", "neutral"),
        ("Paid in this period", rs(sal["paid_in_period"]), "payments dated in the period", "good"),
        ("Still to pay (these payslips)", rs(sal["due_total"]), "not yet paid", "bad" if sal["due_total"] else "good"),
        ("Unpaid salaries at the end", rs(pos["salaries_payable_paisa"]), f"as of {per['cur']['end']}", "neutral"),
    ]))
    f.append(Spacer(1, 3 * mm))
    f.append(P("What salaries cost the business", "h2"))
    rows = [["", cl, pl, "Change"],
            ["Factory workers (direct labor)", rs(sal["direct"]), rs(sal["prev_direct"]), rs(sal["direct"] - sal["prev_direct"])],
            ["Office and admin staff", rs(sal["admin"]), rs(sal["prev_admin"]), rs(sal["admin"] - sal["prev_admin"])],
            ["Total", rs(sal["direct"] + sal["admin"]), rs(sal["prev_direct"] + sal["prev_admin"]), rs(sal["direct"] + sal["admin"] - sal["prev_direct"] - sal["prev_admin"])]]
    f.append(_table(rows, [70, 38, 38, 36], total_last=True))
    f.append(P("Direct labor is part of production cost; office and admin salaries are part of operating expenses.", "small"))
    if att:
        f.append(P("Attendance recorded in the period: " + ",  ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(att.items())) + ".", "small"))
    f.append(P("Payslips", "h2"))
    rows = [["Month", "Employee", "Basic", "Allowances", "Deductions", "Advance", "Net pay", "Still due"]]
    for r in sal["slips"][:24]:
        rows.append([r["period"], r["name"][:22], rs(r["basic_paisa"]), rs(r["allowances_paisa"]), rs(r["deductions_paisa"]), rs(r["advance_recovered_paisa"]), rs(r["net_paisa"]), rs(r["due_paisa"])])
    if len(sal["slips"]) > 24:
        rows.append([f"... and {len(sal['slips']) - 24} more", "", "", "", "", "", "", ""])
    if len(rows) == 1:
        rows.append(["No payslips for these months", "", "", "", "", "", "", ""])
    f.append(_table(rows, [18, 33, 22, 22, 22, 20, 23, 22], right_from=2, size=7))
    by_emp: dict[str, list[int]] = {}
    for r in sal["slips"]:
        a = by_emp.setdefault(r["name"], [0, 0]); a[0] += r["net_paisa"]; a[1] += r["due_paisa"]
    f.append(Spacer(1, 3 * mm))
    f.append(_img(chart_two_hbars([(n, a / 100, b / 100) for n, (a, b) in sorted(by_emp.items(), key=lambda kv: -kv[1][0])[:8]], 182, 52, "No payslips in this period", "Net pay", "Still due", ROSE), 182, 52))
    f.append(PageBreak())

    # ---------------- page 5: inventory
    head("Inventory", f"Stock the factory owns, from {per['cur']['start']} to {per['cur']['end']}. Customers' own stock has no value and is not counted.")
    inv_cur = o["inventory"]["cur"]
    open_v = sum(r["ov"] for r in stock)
    f.append(_tiles([
        ("Value at the start", rs(open_v), f"as of {per['cur']['start']}", "neutral"),
        ("Bought / received", rs(sum(r["iv"] for r in stock)), "stock coming in", "neutral"),
        ("Used in production", rs(inv_cur["used_total_paisa"]), f"{delta_text(ins._change(inv_cur['used_total_paisa'], o['inventory']['prev']['used_total_paisa']))} vs {pl}", "neutral"),
        ("Value left at the end", rs(g["stock_close_value"]), f"as of {per['cur']['end']}", "good"),
    ]))
    f.append(Spacer(1, 3 * mm))
    f.append(_side(_img(chart_hbars([(r["name"], r["cost_paisa"] / 100) for r in inv_cur["used"][:7]], 91, 58, "No stock used in this period", None,
                                    [f"{short(r['cost_paisa'] / 100)} ({pct(r['share_bps'])})" for r in inv_cur["used"][:7]]), 91, 58),
                   _img(chart_hbars(sorted([(r["name"], r["cv"] / 100) for r in stock], key=lambda x: -x[1])[:7], 91, 58, "No stock on hand", None), 91, 58)))
    f.append(P("Used in production (left)  |  Value on hand at the end (right)", "small"))
    f.append(P("Stock movement, material by material", "h2"))
    rows = [["Material", "At start", "Received", "Used", "Lost / returned", "At end", "Value at end"]]
    for r in stock[:30]:
        u, rl = r["base_unit"], r["roll_length_milli"]
        rows.append([r["name"][:24], qty(r["oq"], u), qty(r["iq"], u), qty(r["uq"], u), qty(r["xq"], u), qty_full(r["cq"], u, rl), rs(r["cv"])])
    if len(stock) > 30:
        rows.append([f"... and {len(stock) - 30} more", "", "", "", "", "", ""])
    if len(rows) == 1:
        rows.append(["No stock movements", "", "", "", "", "", ""])
    else:
        rows.append(["Total value", rs(open_v), rs(sum(r["iv"] for r in stock)), rs(sum(r["uv"] for r in stock)), rs(sum(r["xv"] for r in stock)), "", rs(g["stock_close_value"])])
    f.append(_table(rows, [38, 22, 22, 22, 24, 32, 22], right_from=1, total_last=len(rows) > 2, size=7))
    f.append(P("Quantities are in each material's own unit (paper in rolls and meters, ink in litres). 'Lost / returned' = wastage, returns to suppliers and stock adjustments. "
               "The row of values under the table is in rupees.", "small"))
    st = o["inventory"]["cur_stock"]
    f.append(P("What stock cost the profit", "h2"))
    f.append(_table([["", cl, pl], ["Materials used in jobs", rs(st["materials_paisa"]), rs(o["inventory"]["prev_stock"]["materials_paisa"])],
                     ["Wastage", rs(st["wastage_paisa"]), rs(o["inventory"]["prev_stock"]["wastage_paisa"])],
                     ["Stock adjustments", rs(st["adjustments_paisa"]), rs(o["inventory"]["prev_stock"]["adjustments_paisa"])],
                     ["Total", rs(st["total_paisa"]), rs(o["inventory"]["prev_stock"]["total_paisa"])]], [70, 56, 56], total_last=True))
    low = snap["inventory"]["low_stock"]
    if low:
        f.append(P("Running low now (today): " + ";  ".join(f"{_esc(r['material'])} {r['qty']} (reorder at {r['reorder_level']})" for r in low[:8]), "small"))
    f.append(PageBreak())

    # ---------------- page 6: production and invoices
    head("Production and invoices", f"Entries dated in {cl}, and what was billed and received")
    jb = snap["jobs"]
    f.append(_tiles([
        ("Production entries", str(len(prod["entries"])), f"{meters(prod['meters'] * 1000)} produced", "neutral"),
        ("Billed (invoices)", rs(prod["billed"]), f"{len(prod['invoices'])} invoice(s)", "neutral"),
        ("Money received", rs(prod["received"]), f"{prod['received_n']} receipt(s) in the period", "good"),
        ("Entries in progress now", str(jb["active"]), f"{jb['completed_awaiting_finalisation']} done, waiting to be finalised", "neutral"),
    ]))
    f.append(Spacer(1, 3 * mm))
    rows = [["Entry", "Date", "Customer", "Product", "Meters", "Cost", "Billed", "Still due"]]
    for r in prod["entries"][:26]:
        rows.append([r["job_no"], r["order_date"] or "-", (r["customer"] or "")[:18], (r["product_type"] or "")[:14], f"{int(r['quantity_pieces'] or 0):,}", rs(r["cost_paisa"]),
                     rs(r["invoice_paisa"]) if r["invoice_paisa"] is not None else "not billed", rs(r["due_paisa"]) if r["due_paisa"] is not None else "-"])
    if len(prod["entries"]) > 26:
        rows.append([f"... and {len(prod['entries']) - 26} more", "", "", "", "", "", "", ""])
    if len(rows) == 1:
        rows.append(["No production entries in this period", "", "", "", "", "", "", ""])
    f.append(_table(rows, [16, 19, 30, 23, 15, 27, 27, 25], right_from=4, size=7))
    f.append(P("Customers in this period", "h2"))
    f.append(_side(_img(chart_two_hbars([(c["customer"], c["billed"] / 100, c["paid"] / 100) for c in prod["customers"][:7]], 91, 52, "No invoices in this period", "Billed", "Received"), 91, 52),
                   _table([["Customer", "Billed", "Received"]] + [[c["customer"][:20], rs(c["billed"]), rs(c["paid"])] for c in prod["customers"][:8]]
                          if prod["customers"] else [["Customer", "Billed", "Received"], ["No invoices", "-", "-"]], [31, 30, 30], size=7)))
    f.append(P("Received = what has been paid against the invoices issued in the period (it can include payments made later).", "small"))
    ms = [m for m in snap["machines"] if m["runs"]]
    if ms:
        f.append(P("Machines", "h2"))
        rows = [["Machine", "Runs", "Hours run", "Downtime (h)", "Meters"]]
        for m in ms:
            rows.append([m["machine"], str(m["runs"]), f"{m['run_minutes'] / 60:.1f}", f"{m['downtime_minutes'] / 60:.1f}", str(m["meters"])])
        f.append(_table(rows, [70, 25, 30, 30, 27]))
    f.append(PageBreak())

    # ---------------- page 7: position
    head("Money position and assets", f"As of {per['cur']['end']}")
    rows = [["", "Amount"], ["Cash in hand", rs(pos["cash_paisa"])], ["Customers have to pay you", rs(pos["receivables_paisa"])],
            ["Stock on hand (value)", rs(g["stock_close_value"])], ["Customer advances (money you hold for them)", rs(pos["customer_advances_paisa"])],
            ["You have to pay suppliers", rs(pos["payables_paisa"])], ["Unpaid salaries", rs(pos["salaries_payable_paisa"])],
            ["Owner's profit share payable", rs(pos["distributions_payable_paisa"])], ["Partner capital", rs(pos["partner_capital_paisa"])]]
    f.append(_table(rows, [120, 62]))
    f.append(P("Customers and suppliers", "h2"))
    rec = [["Customer", "Owes you"]] + [[r["customer"][:30], rs(r["outstanding_paisa"])] for r in snap["receivables"][:10]]
    pay = [["Supplier", "Balance"]] + [[r["supplier"][:30], rs(r["owed_paisa"])] for r in snap["payables"][:10]]
    if len(rec) == 1:
        rec.append(["Nobody owes you", "-"])
    if len(pay) == 1:
        pay.append(["You owe no supplier", "-"])
    f.append(_side(_table(rec, [56, 31], size=7), _table(pay, [56, 31], size=7)))
    f.append(P("A minus supplier balance means the supplier holds that much of your money (an advance or a return).", "small"))
    f.append(P("Partners", "h2"))
    rows = [["Partner", "Capital", "Distributions approved", "Distribution unpaid"]]
    for p_ in snap["partners"]:
        rows.append([p_["partner"], rs(p_["capital_paisa"]), rs(p_["distributions_approved_paisa"]), rs(p_["distributions_unpaid_paisa"])])
    if len(rows) == 1:
        rows.append(["No partners recorded", "-", "-", "-"])
    f.append(_table(rows, [58, 40, 44, 40]))
    f.append(P("Assets and how fast they are won back", "h2"))
    pb = o["payback"]; a = o["assets"]
    txt = {"no_assets": "No assets are recorded yet. Add machines, construction or equipment on Expenses > Assets.",
           "no_data": "Not enough days have passed in this period to judge.",
           "recovered": "The money put into assets has been fully won back.",
           "not_recovering": "At this period's profit the assets are not being won back.",
           "recovering": ""}[pb["state"]]
    if pb["state"] == "recovering":
        txt = (f"At this period's profit (about {rs(pb['per_month_paisa'])} a month) the remaining {rs(pb['remaining_paisa'])} is won back in about "
               f"{Decimal(pb['months_to_recover_x10']) / 10:.1f} months, around {pb['recover_on']}.")
    note = P(f"<b>{rs(a['total_paisa'])}</b> put into {a['count']} asset(s).<br/>{_esc(txt)}", "body")
    if pb["has_assets"]:
        f.append(_side(_img(chart_payback(o, 80, 50), 80, 50), note, 80, 102))
    else:
        f.append(note)
    if a["items"]:
        rows = [["Asset", "Bought on", "Price"]] + [[i["name"][:40], i["on"], rs(i["price_paisa"])] for i in a["items"][:8]]
        f.append(Spacer(1, 2 * mm)); f.append(_table(rows, [100, 36, 46], right_from=2, size=7))
    f.append(PageBreak())

    # ---------------- page 8: notes
    head("Notes and data checks")
    f.append(P("How the numbers are worked out", "h2"))
    for t in ("<b>Sales</b> = invoices issued in the period (revenue recognised on invoices).",
              "<b>Production cost</b> = materials used + direct labor + factory overheads (electricity and factory expenses).",
              "<b>Gross profit</b> = sales - production cost. <b>Net profit</b> = gross profit - operating expenses (Chai Paani, office, admin salaries, other).",
              "<b>Total expenses</b> = the entries on the Expenses page for the same dates; it does not include salaries or stock used in production.",
              "<b>Stock</b>: value uses the weighted-average cost. Start and end figures are rebuilt from the stock movements up to those dates, so a past month shows the stock as it was then.",
              "<b>Money position</b> figures (cash, customers, suppliers, salaries) are the balances at the end date.",
              "A period that has not finished yet shows what has happened so far. Cancelled (reversed) entries are left out of the expense figures.",
              "Every figure is read from the database when this file is made. Nothing is typed in or guessed."):
        f.append(P("&bull;&nbsp; " + t, "body"))
    f.append(P("Data checks", "h2"))
    warns = snap["warnings"]
    if warns:
        for w_ in warns[:12]:
            f.append(P(f'<font color="{ROSE}">&bull;</font>&nbsp; {_esc(w_)}', "body"))
    else:
        f.append(P(f'<font color="{GREEN}">&bull;</font>&nbsp; The books balance and no problems were found.', "body"))
    bk = snap["backup"]
    f.append(P(f"Last successful backup: {_esc(bk['last_successful'] or 'none yet')}.", "small"))
    return f


def business_summary_pdf(conn, path, kind: str, *, month: str | None = None, fy_year: int | None = None, custom=None, today: date | None = None) -> Path:
    """Write the Complete Business Summary for a month ('YYYY-MM'), a financial year (its start year) or custom (start, end) dates."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate
    today = today or date.today()
    per = resolve(conn, kind, today, month=month, fy_year=fy_year, custom=custom)
    g = gather(conn, per, today)
    g["stock_close_value"] = sum(r["cv"] for r in g["stock"])
    flow = build_flowables(g, conn)
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    factory = settings_service.get(conn, "factory_name", "Factory")
    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
                            title=f"Complete Business Summary - {per['cur']['label']}", author=factory)
    doc.build(flow, canvasmaker=_Numbered.make(f"{factory} - Complete Business Summary", per["cur"]["label"]))
    return out
