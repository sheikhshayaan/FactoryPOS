"""Business Insights: this period against the one before it. Every figure and sentence comes from services.insights_service."""
from __future__ import annotations

import logging
from datetime import date

import customtkinter as ctk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from services import insights_service as ins, settings_service
from ui import smooth, theme as T
from ui.base import Page
from ui.widgets import Col, DataTable, DateField, make_button, surface

log = logging.getLogger("fms.ui")

MODES = [("Month vs month", "month"), ("Quarter vs quarter", "quarter"), ("Year vs year", "year"), ("Pick dates", "custom")]
# one calm palette for every chart (readable on the light and the dark theme)
INDIGO, TEAL, AMBER, ROSE, VIOLET, SKY, GREEN = "#5565f0", "#14b8a6", "#f59e0b", "#f43f5e", "#8b5cf6", "#38bdf8", "#22c55e"
CYCLE = [INDIGO, TEAL, AMBER, ROSE, VIOLET, SKY, GREEN, "#fb7185"]
PREV = ("#c3c9de", "#4b5478")
TONE = {"good": T.GOOD, "bad": T.BAD, "neutral": T.TEXT, "warn": T.WARN}
SOFT = {"good": T.SOFT_GOOD, "bad": T.SOFT_BAD, "neutral": T.NEUTRAL_SOFT}
STATUS = {"exceeding": "Exceeding", "new": "New this period", "higher": "Higher", "lower": "Lower", "same": "Same"}
STATUS_TAG = {"exceeding": "bad", "new": "bad", "higher": "warn", "lower": "good"}


def _short(v: float) -> str:
    """1,250,000 -> 1.25M, 85,000 -> 85k, 450 -> 450."""
    a = abs(v)
    t = f"{a / 1e6:.2f}M" if a >= 1e6 else f"{a / 1e3:.1f}k" if a >= 1e4 else f"{a:,.0f}"
    t = t.replace(".00M", "M").replace(".0k", "k")
    return ("-" if v < 0 else "") + t


class InsightsPage(Page):
    title = "Business Insights"
    subtitle = "Compare this period with the one before: sales, expenses, electricity, profit, stock - and how fast the factory wins back its assets."

    # ------------------------------------------------------------------ layout
    def build(self):
        self.kind, self.offset, self.data, self._drawn_w, self._job = "month", 0, None, 0, None
        self._painted = None                                                   # the figures the charts on screen were drawn from
        self.canvases: dict[str, FigureCanvasTkAgg] = {}
        bar = self.toolbar()
        self.seg = ctk.CTkSegmentedButton(bar, values=[m[0] for m in MODES], command=self._pick, font=T.font(14), height=38)
        self.seg.set(MODES[0][0]); self.seg.pack(side="left")
        self.b_prev = make_button(bar, "‹ Earlier", lambda: self._step(-1), "secondary", width=96); self.b_prev.pack(side="left", padx=(14, 4))
        self.b_next = make_button(bar, "Later ›", lambda: self._step(1), "secondary", width=90); self.b_next.pack(side="left")
        first = date.today().replace(day=1)
        # "Pick dates": the calendars sit on their own line under the buttons (shown only for that mode) so nothing is pushed out of view
        self.crow = ctk.CTkFrame(self.body, fg_color="transparent")
        ctk.CTkLabel(self.crow, text="Choose the days:", text_color=T.MUTED, font=T.font(14)).pack(side="left", padx=(2, 10))
        self.c_from = DateField(self.crow, first, width=160, height=38, compact=True, command=self._custom_changed)
        self.c_from.pack(side="left")
        self.to_lbl = ctk.CTkLabel(self.crow, text="to", text_color=T.MUTED, font=T.font(14)); self.to_lbl.pack(side="left", padx=8)
        self.c_to = DateField(self.crow, date.today(), width=160, height=38, compact=True, command=self._custom_changed)
        self.c_to.pack(side="left")
        self.crow_msg = ctk.CTkLabel(self.crow, text="", text_color=T.MUTED, font=T.font(13), anchor="w"); self.crow_msg.pack(side="left", padx=14)
        self.vs_lbl = ctk.CTkLabel(bar, text="", text_color=T.MUTED, font=T.font(13)); self.vs_lbl.pack(side="right")
        self.scroll = ctk.CTkScrollableFrame(self.body, fg_color="transparent"); self.scroll.pack(fill="both", expand=True)
        S = self.scroll
        self.banner = ctk.CTkFrame(S, corner_radius=14, border_width=0, fg_color=T.NEUTRAL_SOFT); self.banner.pack(fill="x", pady=(2, 0))
        self.banner_lbl = ctk.CTkLabel(self.banner, text="", font=T.font(21, "bold"), anchor="w", justify="left", wraplength=980)
        self.banner_lbl.pack(fill="x", padx=24, pady=(18, 4))
        self.note_lbl = ctk.CTkLabel(self.banner, text="", font=T.font(13), text_color=T.MUTED, anchor="w", justify="left", wraplength=980)
        self.note_lbl.pack(fill="x", padx=24, pady=(0, 16))
        # --- the four numbers that matter
        row = self._row(S, 4, pady=(14, 0)); self.kpi = {}
        for i, (k, t) in enumerate([("sales", "Sales"), ("exp", "Expenses (kharchay)"), ("net", "Net profit"), ("margin", "Profit margin")]):
            self.kpi[k] = self._kpi(row, i, t)
        # --- sales & profit
        self._section("Sales and profit")
        row = self._row(S, 2)
        self.f_bars = self._chart_card(row, 0, "This period against the one before")
        self.f_trend = self._chart_card(row, 1, "Month by month")
        self.s_sales = self._story(S); self.s_net = self._story(S)
        row = self._row(S, 2, pady=(12, 0))
        self.f_split = self._chart_card(row, 0, "Where your sales go")
        self.net_card = self._text_card(row, 1, "Net profit compared")
        # --- assets
        self._section("Your assets and how fast they come back")
        row = self._row(S, 2)
        self.f_pay = self._chart_card(row, 0, "Investment won back")
        self.pay_card = self._text_card(row, 1, "Payback", tiles=6)
        self.s_pay = self._story(S)
        # --- expenses
        self._section("Expenses (kharchay)")
        row = self._row(S, 2)
        self.f_exp = self._chart_card(row, 0, "Where the money went this period")
        self.f_exp_bars = self._chart_card(row, 1, "Each kind of expense, both periods")
        self.t_exp = DataTable(S, [Col("name", "Expense", 300), Col("cur_paisa", "This period", 120, T.money, "e"), Col("prev_paisa", "Previous", 120, T.money, "e"),
                                   Col("change_txt", "Change", 90, None, "e"), Col("status_txt", "Status", 130)], height=7, page_size=30, searchable=False,
                               row_tags=lambda r: STATUS_TAG.get(r["status"]), empty_text="No expenses in these two periods.")
        self.t_exp.pack(fill="x", pady=(12, 0))
        self.s_exp = self._story(S); self.s_heavy = self._story(S)
        row = self._row(S, 2, pady=(12, 0))
        self.elec_card = self._text_card(row, 0, "Electricity & utilities", tiles=1)
        self.top_card = self._text_card(row, 1, "Biggest expense this period", tiles=3)
        self.s_elec = self._story(S)
        ctk.CTkLabel(S, text="Figures come from the books and stock records. A period that has not finished yet shows what has happened so far.",
                     font=T.font(12), text_color=T.MUTED, anchor="w").pack(fill="x", pady=(18, 8))
        self.bind("<Configure>", self._on_resize)

    def _section(self, text):
        ctk.CTkLabel(self.scroll, text=text, font=T.font(18, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x", pady=(28, 10))

    def _row(self, parent, cols, pady=(0, 0)):
        f = ctk.CTkFrame(parent, fg_color="transparent"); f.pack(fill="x", pady=pady)
        for i in range(cols):
            f.grid_columnconfigure(i, weight=1, uniform="c")
        return f

    def _kpi(self, row, i, title):
        box = surface(row); box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 3 else 6))
        ctk.CTkLabel(box, text=title, font=T.font(14), text_color=T.MUTED, anchor="w").pack(fill="x", padx=20, pady=(16, 0))
        v = ctk.CTkLabel(box, text="-", font=T.font(23, "bold"), text_color=T.TEXT, anchor="w"); v.pack(fill="x", padx=20, pady=(2, 0))
        d = ctk.CTkLabel(box, text="", font=T.font(13, "bold"), text_color=T.MUTED, anchor="w"); d.pack(fill="x", padx=20)
        p = ctk.CTkLabel(box, text="", font=T.font(12), text_color=T.MUTED, anchor="w"); p.pack(fill="x", padx=20, pady=(0, 16))
        return {"value": v, "delta": d, "prev": p}

    def _chart_card(self, row, i, title):
        box = surface(row); box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 1 else 6))
        ctk.CTkLabel(box, text=title, font=T.font(15, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x", padx=18, pady=(14, 0))
        holder = ctk.CTkFrame(box, fg_color="transparent"); holder.pack(fill="x", padx=4, pady=(0, 6))
        return holder

    def _text_card(self, row, i, title, tiles=0):
        box = surface(row); box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 1 else 6))
        ctk.CTkLabel(box, text=title, font=T.font(15, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x", padx=18, pady=(14, 6))
        card = {"head": ctk.CTkLabel(box, text="", font=T.font(28, "bold"), text_color=T.TEXT, anchor="w"),
                "sub": ctk.CTkLabel(box, text="", font=T.font(13), text_color=T.MUTED, anchor="w", justify="left", wraplength=470),
                "tiles": []}
        card["head"].pack(fill="x", padx=18); card["sub"].pack(fill="x", padx=18, pady=(0, 6))
        if tiles:
            grid = ctk.CTkFrame(box, fg_color="transparent"); grid.pack(fill="x", padx=12, pady=(0, 14))
            for k in range(tiles):
                grid.grid_columnconfigure(k % 3, weight=1, uniform="t")
                tile = ctk.CTkFrame(grid, fg_color=T.NEUTRAL_SOFT, corner_radius=10); tile.grid(row=k // 3, column=k % 3, sticky="nsew", padx=5, pady=5)
                a = ctk.CTkLabel(tile, text="", font=T.font(12), text_color=T.MUTED, anchor="w", wraplength=118, justify="left"); a.pack(fill="x", padx=12, pady=(8, 0))
                b = ctk.CTkLabel(tile, text="-", font=T.font(15, "bold"), text_color=T.TEXT, anchor="w", wraplength=118, justify="left"); b.pack(fill="x", padx=12, pady=(0, 9))
                card["tiles"].append((a, b))
        else:
            card["sub"].pack_configure(pady=(0, 16))
        return card

    def _story(self, parent):
        lbl = ctk.CTkLabel(parent, text="", font=T.font(14), anchor="w", justify="left", wraplength=1000)
        lbl.pack(fill="x", pady=(8, 0), padx=4)
        return lbl

    # ------------------------------------------------------------------ choosing the period
    def _pick(self, label):
        self.kind, self.offset = dict(MODES)[label], 0
        custom = self.kind == "custom"
        self.crow.pack_forget()
        if custom:
            self.crow.pack(fill="x", pady=(0, 10), before=self.scroll._parent_frame)
        self.refresh()

    def _step(self, n):
        self.offset = min(0, self.offset + n)
        self.refresh()

    def _custom_changed(self):
        if self.kind != "custom":
            return
        if self.c_from.get() > self.c_to.get():
            self.crow_msg.configure(text="The first day is after the last day - change one of them.", text_color=T.BAD)
            return
        self.offset = 0
        self.refresh()

    def refresh(self):
        fy = int(settings_service.get(self.conn, "fiscal_year_start_month", "7") or 7)
        custom = (self.c_from.get(), self.c_to.get()) if self.kind == "custom" else None
        try:
            d = ins.compare(self.conn, date.today(), self.kind, self.offset, fy, custom)
        except ValueError as e:
            self.banner_lbl.configure(text=str(e), text_color=T.BAD); return
        if self.kind == "custom":
            n = d["periods"]["cur"]["days"]
            self.crow_msg.configure(text=f"{n} day{'' if n == 1 else 's'}, compared with the {n} day{'' if n == 1 else 's'} just before.", text_color=T.MUTED)
        same = self._painted is not None and d == self._painted and abs(self.body.winfo_width() - self._drawn_w) <= 40
        self.data = d
        self.b_next.configure(state="normal" if self.offset < 0 else "disabled")
        self._fill(d)
        if not same:                                                           # visiting the page again with unchanged figures keeps the charts as they are
            self._paint()
            self._painted = d

    # ------------------------------------------------------------------ texts and tables
    @staticmethod
    def _delta(ch: dict, prev_label: str, good_up: bool = True) -> tuple[str, str]:
        d = ch["delta"]
        if d == 0:
            return f"same as {prev_label}", "neutral"
        arrow = "▲" if d > 0 else "▼"
        amount = ins.pct(ch["pct_bps"]) if ch["pct_bps"] is not None else ins.rs(abs(d))
        return f"{arrow} {amount} vs {prev_label}", ("good" if (d > 0) == good_up else "bad")

    def _set_story(self, lbl, pair):
        tone, text = pair
        lbl.configure(text="●  " + text, text_color=TONE.get(tone, T.TEXT))

    def _fill(self, d):
        cur, prev, per, ch = d["cur"], d["prev"], d["periods"], d["change"]
        cl, pl = per["cur"]["label"], per["prev"]["label"]
        self.vs_lbl.configure(text=f"{cl}  vs  {pl}", text_color=T.MUTED)
        if per["kind"] == "custom":                                       # long date ranges would not fit in the small boxes
            cl, pl = "this range", "before"
        tone, text = d["story"]["verdict"]
        self.banner.configure(fg_color=SOFT.get(tone, T.NEUTRAL_SOFT))
        self.banner_lbl.configure(text=("✔  " if tone == "good" else "✖  " if tone == "bad" else "") + text, text_color=TONE[tone])
        c = per["cur"]
        self.note_lbl.configure(text=(f"This period is still running: {c['days_elapsed']} of {c['days']} days have passed, so the comparison is not complete yet."
                                      if c["running"] else "This period has not started yet." if c["future"] else
                                      ("Compared with the same number of days just before." if per["kind"] == "custom" else f"Compared with {pl}.")))
        k = self.kpi
        for key, val, chg, prv, up in (("sales", cur["sales_paisa"], ch["sales_paisa"], prev["sales_paisa"], True),
                                       ("exp", cur["expenses_total_paisa"], ch["expenses_total_paisa"], prev["expenses_total_paisa"], False),
                                       ("net", cur["net_profit_paisa"], ch["net_profit_paisa"], prev["net_profit_paisa"], True)):
            text, tn = self._delta(chg, pl, up)
            k[key]["value"].configure(text=T.money(val), text_color=(TONE["good"] if val > 0 else TONE["bad"] if val < 0 else T.TEXT) if key == "net" else T.TEXT)
            k[key]["delta"].configure(text=text, text_color=TONE[tn]); k[key]["prev"].configure(text=f"{pl}: {T.money(prv)}")
        m, pm = cur["net_margin_bps"], prev["net_margin_bps"]
        k["margin"]["value"].configure(text=T.pct_bps(m) if m is not None else "-", text_color=T.TEXT)
        dm = (m - pm) if (m is not None and pm is not None) else None
        k["margin"]["delta"].configure(text="" if dm is None else ("same as " + pl if dm == 0 else f"{'▲' if dm > 0 else '▼'} {abs(dm) / 100:.1f} points vs {pl}"),
                                       text_color=TONE["neutral"] if not dm else TONE["good" if dm > 0 else "bad"])
        k["margin"]["prev"].configure(text=f"{pl}: {T.pct_bps(pm) if pm is not None else '-'}  (of sales)")
        st = d["story"]
        self._set_story(self.s_sales, st["sales"]); self._set_story(self.s_net, st["net"])
        # net profit card
        nc = self.net_card
        nc["head"].configure(text=T.money(cur["net_profit_paisa"]), text_color=TONE["good" if cur["net_profit_paisa"] > 0 else "bad" if cur["net_profit_paisa"] < 0 else "neutral"])
        nc["sub"].configure(text=f"{pl}: {T.money(prev['net_profit_paisa'])}\n{st['net'][1]}")
        # assets and payback
        p, a = d["payback"], d["assets"]
        self._fill_payback(p, a, per)
        self._set_story(self.s_pay, st["payback"])
        # expenses
        rows = [dict(r, change_txt=("-" if r["delta"] == 0 else ("+" if r["delta"] > 0 else "-") + (ins.pct(r["pct_bps"]) if r["pct_bps"] is not None else "new")),
                     status_txt=STATUS[r["status"]]) for r in d["expense_rows"]]
        self.t_exp.set_rows(rows); self._fit_table(self.t_exp, len(rows))
        self._set_story(self.s_exp, st["expenses"]); self._set_story(self.s_heavy, st["heavier"])
        e = d["electricity"]
        ec = self.elec_card
        ec["head"].configure(text=T.money(e["cur_paisa"]), text_color=T.TEXT)
        ec["sub"].configure(text=f"{pl}: {T.money(e['prev_paisa'])}")
        et, en = self._delta(e, pl, False)
        for (lab, val), (a_, b_) in zip([("Change", et)], ec["tiles"]):
            a_.configure(text=lab); b_.configure(text=val, text_color=TONE[en] if lab == "Change" else T.TEXT)
        self._set_story(self.s_elec, st["electricity"])
        top = d["expense_rows"][0] if d["expense_rows"] and d["expense_rows"][0]["cur_paisa"] > 0 else None
        tc = self.top_card
        if top:
            share = ins._bps(top["cur_paisa"], cur["expenses_total_paisa"])
            tc["head"].configure(text=top["name"], text_color=T.TEXT)
            tc["sub"].configure(text=f"{T.money(top['cur_paisa'])}  -  {T.pct_bps(share)} of all expenses")
            vals = [("Previous period", T.money(top["prev_paisa"])), ("Change", "-" if top["delta"] == 0 else ("+" if top["delta"] > 0 else "-") + T.money(abs(top["delta"]))),
                    ("Status", STATUS[top["status"]])]
        else:
            tc["head"].configure(text="-", text_color=T.MUTED); tc["sub"].configure(text="No expenses in this period.")
            vals = [("Previous period", "-"), ("Change", "-"), ("Status", "-")]
        for (lab, val), (a_, b_) in zip(vals, tc["tiles"]):
            a_.configure(text=lab); b_.configure(text=val)

    @staticmethod
    def _fit_table(table, n):
        """A table only as tall as its rows (at least two lines, at most eight), so short lists do not leave a big empty box."""
        try:
            table.tree.configure(height=max(2, min(8, n)))
        except Exception:  # noqa: BLE001 - cosmetic only
            pass

    def _fill_payback(self, p, a, per):
        pc = self.pay_card
        if not p["has_assets"]:
            pc["head"].configure(text="No assets yet", text_color=T.MUTED)
            pc["sub"].configure(text="Add machines, construction or equipment on  Expenses > Assets  and this page works out how fast they are won back.")
            for (x, y) in pc["tiles"]:
                x.configure(text=""); y.configure(text="")
            return
        txt = {"recovering": "Coming back", "recovered": "Fully recovered", "not_recovering": "Not recovering now", "no_data": "Not measured yet"}[p["state"]]
        tone = {"recovering": "good", "recovered": "good", "not_recovering": "bad", "no_data": "neutral"}[p["state"]]
        pc["head"].configure(text=txt, text_color=TONE[tone])
        pc["sub"].configure(text=f"{a['count']} asset{'' if a['count'] == 1 else 's'} worth {T.money(p['assets_paisa'])}. Speed is measured on {per['cur']['label']}.")
        when = "-"
        if p["state"] == "recovering":
            months = p["months_to_recover_x10"] / 10
            when = f"about {months:g} months  ({date.fromisoformat(p['recover_on']):%d %b %Y})"
        elif p["state"] == "recovered":
            when = "done"
        vals = [("Total assets", T.money(p["assets_paisa"])), ("Won back so far", f"{T.money(p['recovered_paisa'])}  ({T.pct_bps(p['recovered_bps'])})"),
                ("Still to win back", T.money(p["remaining_paisa"])),
                ("Profit a month", "-" if p["per_month_paisa"] is None else T.money(p["per_month_paisa"])),
                ("Time to win back", when), ("Yearly return", T.pct_bps(p["yearly_return_bps"]) if p["yearly_return_bps"] is not None else "-")]
        for (lab, val), (x, y) in zip(vals, pc["tiles"]):
            x.configure(text=lab); y.configure(text=val)

    # ------------------------------------------------------------------ charts
    def _on_resize(self, _e=None):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(300, self._resized)

    def _resized(self):
        self._job = None
        if self.data and abs(self.body.winfo_width() - self._drawn_w) > 40:
            self._paint()

    def _put(self, key, holder, fig):
        old = self.canvases.pop(key, None)
        if old is not None:
            if self.focus_get() is old.get_tk_widget():
                self.focus_set()                                              # never leave the keyboard focus on a chart that is about to be closed
            old.get_tk_widget().destroy()
        c = smooth.embed_chart(fig, holder, fill="x", padx=6, pady=6)
        smooth.bold_all_text(fig)
        c.draw_idle()                                                         # one real drawing (it used to be four per chart)
        self.canvases[key] = c

    def _fig(self, w, h):
        return Figure(figsize=(max(w, 280) / 100, h), dpi=100, facecolor=T.rc(T.CARD))

    def _style(self, ax):
        muted, border = T.rc(T.MUTED), T.rc(T.BORDER)
        ax.set_facecolor(T.rc(T.CARD))
        ax.tick_params(colors=muted, labelsize=10, length=0)
        for sp in ax.spines.values():
            sp.set_visible(False)
        ax.grid(axis="y", color=border, lw=0.6, alpha=0.8); ax.set_axisbelow(True)

    def _empty(self, fig, text):
        fig.text(0.5, 0.5, text, ha="center", va="center", color=T.rc(T.MUTED), fontsize=12)

    def _donut(self, ax, values, colors, big, small):
        ax.pie(values, colors=colors, startangle=90, counterclock=False, wedgeprops=dict(width=0.36, edgecolor=T.rc(T.CARD), linewidth=2))
        ax.text(0, 0.07, big, ha="center", va="center", fontsize=19, fontweight="bold", color=T.rc(T.TEXT))
        ax.text(0, -0.2, small, ha="center", va="center", fontsize=10, color=T.rc(T.MUTED))
        ax.set_aspect("equal")

    def _legend(self, fig, ax, labels, colors):
        from matplotlib.patches import Patch
        fig.legend([Patch(color=c) for c in colors], labels, loc="center left", bbox_to_anchor=(0.48, 0.5), frameon=False, labelcolor=T.rc(T.TEXT), fontsize=10,
                   handlelength=1.1, labelspacing=0.8)

    STAGGER = True                                                             # the tests draw everything at once

    def _paint(self):
        """Draw the six charts one after the other (a moment apart), so the page and its figures show at once and the program never freezes
        for the whole second that six charts take."""
        self._paint_token = getattr(self, "_paint_token", 0) + 1
        token, steps = self._paint_token, self._paint_steps()

        def run():
            if token != self._paint_token:
                return                                                         # a newer drawing replaced this one
            try:
                next(steps)
            except StopIteration:
                return
            except Exception as e:  # noqa: BLE001 - a chart problem must not break the page
                log.warning("insights chart failed: %s: %s", type(e).__name__, e)
                return
            self.after(1, run)
        if not self.STAGGER:
            for _ in steps:
                pass
            return
        run()

    def _paint_steps(self):
        d = self.data
        if not d:
            return
        W = self.body.winfo_width()
        self._drawn_w = W
        half = (W - 40) // 2 - 12 if W > 100 else 520
        full = half
        cur, prev, per = d["cur"], d["prev"], d["periods"]
        cl, pl = per["cur"]["label"], per["prev"]["label"]
        if per["kind"] == "custom":
            cl, pl = "This range", "Before"
        text, muted, border = T.rc(T.TEXT), T.rc(T.MUTED), T.rc(T.BORDER)
        pcol = T.rc(PREV)

        # 1. this period vs previous: four bars each
        fig = self._fig(half, 3.9); ax = fig.add_subplot(111); self._style(ax)
        names = ["Sales", "Production\ncost", "Operating\nexpenses", "Net profit"]
        keys = ["sales_paisa", "production_cost_paisa", "operating_expenses_paisa", "net_profit_paisa"]
        cv, pv = [cur[k] / 100 for k in keys], [prev[k] / 100 for k in keys]
        x = range(4); w = 0.36
        b1 = ax.bar([i - w / 2 - 0.02 for i in x], cv, w, color=INDIGO, label=cl, zorder=3)
        b2 = ax.bar([i + w / 2 + 0.02 for i in x], pv, w, color=pcol, label=pl, zorder=3)
        for bars in (b1, b2):
            for r in bars:
                h = r.get_height()
                if h:
                    ax.annotate(_short(h), (r.get_x() + r.get_width() / 2, h), textcoords="offset points", xytext=(0, 3 if h >= 0 else -11), ha="center", fontsize=9, color=muted)
        ax.axhline(0, color=muted, lw=1, zorder=3)
        ax.set_xticks(list(x)); ax.set_xticklabels(names, color=muted, fontsize=10)
        ax.yaxis.set_major_formatter(lambda v, _p: _short(v))
        top = max([*cv, *pv, 1]); ax.set_ylim(min([*cv, *pv, 0]) * 1.25, top * 1.4)
        ax.legend(frameon=False, labelcolor=text, fontsize=10, loc="upper left", ncol=2)
        fig.tight_layout(); self._put("bars", self.f_bars, fig)
        yield

        # 2. month by month: sales bars, costs bars, net profit dots
        fig = self._fig(half, 3.9); ax = fig.add_subplot(111); self._style(ax)
        tr = d["trend"]
        if any(m["revenue"] or m["cost_of_production"] or m["operating_expenses"] for m in tr):
            xs = list(range(len(tr))); w = 0.36
            ax.bar([i - w / 2 - 0.02 for i in xs], [m["revenue"] / 100 for m in tr], w, color=TEAL, label="Sales", zorder=3)
            ax.bar([i + w / 2 + 0.02 for i in xs], [(m["cost_of_production"] + m["operating_expenses"]) / 100 for m in tr], w, color=pcol, label="Costs", zorder=3)
            net = [m["net_profit"] / 100 for m in tr]
            ax.plot(xs, net, color=text, lw=1.4, alpha=0.5, zorder=4)
            ax.scatter(xs, net, s=48, c=[GREEN if v >= 0 else ROSE for v in net], edgecolors=T.rc(T.CARD), linewidths=1.5, zorder=5, label="Net profit")
            ax.axhline(0, color=muted, lw=1, zorder=3)
            ax.set_xticks(xs); ax.set_xticklabels([date.fromisoformat(m["month"] + "-01").strftime("%b %y") for m in tr], color=muted, fontsize=9)
            ax.yaxis.set_major_formatter(lambda v, _p: _short(v))
            hi = max([m["revenue"] / 100 for m in tr] + [(m["cost_of_production"] + m["operating_expenses"]) / 100 for m in tr] + net + [1])
            ax.set_ylim(min(net + [0]) * 1.25, hi * 1.3)
            ax.legend(frameon=False, labelcolor=text, fontsize=10, loc="upper left", ncol=3)
        else:
            ax.axis("off"); self._empty(fig, "No sales or costs in these months yet")
        fig.tight_layout(); self._put("trend", self.f_trend, fig)
        yield

        # 3. where each rupee of sales goes
        fig = self._fig(half, 3.0)
        split = d["sales_split"]
        if cur["sales_paisa"] > 0 and split:
            ax = fig.add_axes([0.0, 0.04, 0.46, 0.92])
            cols = [AMBER if s["name"] == "Production cost" else VIOLET if s["name"] == "Operating expenses" else GREEN for s in split]
            self._donut(ax, [s["paisa"] for s in split], cols, T.pct_bps(split[-1]["bps"]) if split[-1]["name"] == "Net profit" else "100%", "profit" if split[-1]["name"] == "Net profit" else "of sales")
            self._legend(fig, ax, [f"{s['name']}\n{T.pct_bps(s['bps'])}  -  {T.money(s['paisa'])}" for s in split], cols)
        else:
            self._empty(fig, "No sales in this period yet" if cur["sales_paisa"] <= 0 else "Costs are higher than sales: no profit to show")
        self._put("split", self.f_split, fig)
        yield

        # 4. assets won back
        fig = self._fig(half, 3.0); p = d["payback"]
        if p["has_assets"]:
            ax = fig.add_axes([0.0, 0.04, 0.46, 0.92])
            rec, rem = p["recovered_paisa"], p["remaining_paisa"]
            self._donut(ax, [max(rec, 0.0001), max(rem, 0.0001)] if rec or rem else [1], [GREEN, pcol] if rec or rem else [pcol], T.pct_bps(p["recovered_bps"]), "won back")
            self._legend(fig, ax, [f"Won back\n{T.money(rec)}", f"Still to win back\n{T.money(rem)}"], [GREEN, pcol])
        else:
            self._empty(fig, "No assets recorded yet")
        self._put("pay", self.f_pay, fig)
        yield

        # 5. expenses this period: donut by kind
        fig = self._fig(half, 3.3)
        rows = [r for r in d["expense_rows"] if r["cur_paisa"] > 0]
        tot = cur["expenses_total_paisa"]
        if rows and tot > 0:
            ax = fig.add_axes([0.0, 0.04, 0.46, 0.92]); cols = [CYCLE[i % len(CYCLE)] for i in range(len(rows))]
            self._donut(ax, [r["cur_paisa"] for r in rows], cols, _short(tot / 100), "Rs spent")
            self._legend(fig, ax, [f"{r['name'][:24]}\n{T.pct_bps(ins._bps(r['cur_paisa'], tot))}  -  {T.money(r['cur_paisa'])}" for r in rows], cols)
        else:
            self._empty(fig, "No expenses in this period")
        self._put("exp", self.f_exp, fig)
        yield

        # 6. expenses: each kind, both periods (horizontal)
        fig = self._fig(half, 3.3); ax = fig.add_subplot(111); self._style(ax); ax.grid(axis="x", color=border, lw=0.6, alpha=0.8); ax.grid(axis="y", visible=False)
        rows = d["expense_rows"][:7]
        if rows:
            ys = list(range(len(rows))); h = 0.36
            ax.barh([i - h / 2 - 0.02 for i in ys], [r["cur_paisa"] / 100 for r in rows], h, color=[ROSE if r["status"] in ("exceeding", "new") else INDIGO for r in rows], zorder=3, label=cl)
            ax.barh([i + h / 2 + 0.02 for i in ys], [r["prev_paisa"] / 100 for r in rows], h, color=pcol, zorder=3, label=pl)
            ax.set_yticks(ys); ax.set_yticklabels([r["name"][:26] for r in rows], color=muted, fontsize=9); ax.set_ylim(max(len(rows), 4) - 0.5, -0.5)
            ax.xaxis.set_major_formatter(lambda v, _p: _short(v))
            from matplotlib.patches import Patch
            ax.legend([Patch(color=INDIGO), Patch(color=ROSE), Patch(color=pcol)], [cl, "Exceeding", pl], frameon=False, labelcolor=text, fontsize=9, loc="lower right")
        else:
            ax.axis("off"); self._empty(fig, "No expenses to compare")
        fig.tight_layout(); self._put("exp_bars", self.f_exp_bars, fig)
        yield
