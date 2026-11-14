"""Dashboard: every figure comes from services.dashboard_service (database queries)."""
from __future__ import annotations

from datetime import date, datetime

import customtkinter as ctk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from services import dashboard_service as ds, queries as q, settings_service
from ui import smooth, theme as T
from ui.base import Page
from ui.widgets import Col, DataTable, DateField, Stat, StatBox, make_button, make_tabs, show_error, surface

RANGES = [("Today", "today"), ("This week", "this_week"), ("This month", "this_month"),
          ("This quarter", "this_quarter"), ("This year", "this_year"), ("Custom", "custom")]


class DashboardPage(Page):
    title = "Dashboard"
    subtitle = "Performance figures cover the selected period. Position figures (cash, inventory, receivables...) are balances as of the period end."

    def build(self):
        bar = self.toolbar()
        self.kind = ctk.StringVar(value="this_month")
        self.seg = ctk.CTkSegmentedButton(bar, values=[r[0] for r in RANGES], command=self._pick, font=T.font(14), height=38)
        self.seg.set("This month")
        self.seg.pack(side="left")
        self.c_from = DateField(bar, date.today().replace(day=1), width=150, height=38, compact=True, command=self._custom_changed)     # calendars - no typing of dates
        self.c_to = DateField(bar, date.today(), width=150, height=38, compact=True, command=self._custom_changed)
        self.to_lbl = ctk.CTkLabel(bar, text="to", text_color=T.MUTED, font=T.font(14))
        self.period_lbl = ctk.CTkLabel(bar, text="", text_color=T.MUTED, font=T.font(13))
        self.period_lbl.pack(side="right")
        self.scroll = ctk.CTkScrollableFrame(self.body, fg_color="transparent")
        self.scroll.pack(fill="both", expand=True)
        self.warn = ctk.CTkLabel(self.scroll, text="", text_color=T.WARN, justify="left", anchor="w", wraplength=1000, font=T.font(14))
        self.cards: dict[str, Stat | StatBox] = {}
        # the three numbers an owner looks at first
        hero = self.hero = ctk.CTkFrame(self.scroll, fg_color="transparent"); hero.pack(fill="x", pady=(4, 0))
        for i, (k, t, kind) in enumerate([("net", "Net profit", "good"), ("sales", "Total sales", "info"), ("cash", "Cash in hand", "cash")]):
            hero.grid_columnconfigure(i, weight=1, uniform="hero")
            c = StatBox(hero, t, "-", kind=kind, big=True)
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 7, 0 if i == 2 else 7))
            self.cards[k] = c
        self._section("This period")
        self._grid([("prod", "Production cost"), ("opex", "Total expenses"), ("gross", "Gross profit")], 3)
        self._section("Where things stand")
        self._grid([("inv", "Inventory value"), ("ar", "Customer has to pay you"), ("ap", "You have to pay suppliers"), ("sal", "Unpaid salaries"),
                    ("cap", "Partner capital"), ("adv", "Customer advances"), ("dist", "Owner's profit share payable")], 4)
        self._section("Monthly trend")
        self.chart_frame = surface(self.scroll)
        self.chart_frame.pack(fill="x")
        self.canvas = None
        self._section("Details")
        self.tabs = make_tabs(self.scroll, height=400)
        self.tabs.pack(fill="x", pady=(0, 8))
        self.tables = {}
        spec = {
            "Low stock": [Col("material", "Material", 220), Col("qty", "On hand", 120), Col("reorder_level", "Reorder level", 120)],
            "Inventory": [Col("material", "Material", 200), Col("category", "Category", 90), Col("qty", "Factory qty", 110), Col("unit", "Unit", 50),
                          Col("value_paisa", "Value", 120, T.money, "e"), Col("customer_owned_qty", "Customer property qty (no value)", 190)],
            "Receivables": [Col("customer", "Customer", 260), Col("outstanding_paisa", "Outstanding", 140, T.money, "e")],
            "Suppliers owed": [Col("supplier", "Supplier", 260), Col("owed_paisa", "Balance", 140, T.money, "e")],
            "Unpaid salaries": [Col("employee", "Employee", 200), Col("period", "Month", 90), Col("due_paisa", "Due", 130, T.money, "e"), Col("status", "Status", 90)],
            "Partners": [Col("partner", "Partner", 200), Col("capital_paisa", "Capital", 130, T.money, "e"),
                         Col("distributions_approved_paisa", "Distributions approved", 170, T.money, "e"), Col("distributions_unpaid_paisa", "Unpaid", 120, T.money, "e")],
        }
        for name, cols in spec.items():
            t = self.tabs.add(name)
            self.tables[name] = DataTable(t, cols, height=8, page_size=25)
            self.tables[name].pack(fill="both", expand=True)

    def _section(self, text):
        ctk.CTkLabel(self.scroll, text=text, font=T.font(17, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x", pady=(26, 10))

    def _grid(self, spec, per_row):
        """Each figure in its own plain box, so every number stands apart."""
        grid = ctk.CTkFrame(self.scroll, fg_color="transparent"); grid.pack(fill="x")
        for i, (k, t) in enumerate(spec):
            grid.grid_columnconfigure(i % per_row, weight=1, uniform="g")
            box = surface(grid)
            box.grid(row=i // per_row, column=i % per_row, sticky="nsew", padx=5, pady=5)
            c = Stat(box, t, "-"); c.pack(fill="x", padx=20, pady=18)
            self.cards[k] = c

    def _pick(self, label):
        kind = dict(RANGES)[label]
        self.kind.set(kind)
        if kind == "custom":
            self.c_from.pack(side="left", padx=(10, 0)); self.to_lbl.pack(side="left", padx=8); self.c_to.pack(side="left")
            self.refresh()
        else:
            for w in (self.c_to, self.to_lbl, self.c_from):
                w.pack_forget()
            self.refresh()

    def _custom_changed(self):
        """The figures follow the calendars straight away (once the range makes sense), so there is no Apply button to forget."""
        if self.c_from.get() <= self.c_to.get():
            self.refresh()

    def _range(self):
        fy = int(settings_service.get(self.conn, "fiscal_year_start_month", "7") or 7)
        kind = self.kind.get()
        custom = (self.c_from.get().strip(), self.c_to.get().strip()) if kind == "custom" else None
        return ds.resolve_range(kind, date.today(), fy_start_month=fy, custom=custom)

    def refresh(self):
        try:
            start, end = self._range()
            s = ds.snapshot(self.conn, start, end)
        except (ds.RangeError, ValueError) as e:
            show_error(self, f"Check the dates: {e}"); return
        self.period_lbl.configure(text=f"{start}  –  {end}")
        p, pos, c = s["performance"], s["position"], self.cards
        net, gross = p["net_profit_paisa"], p["gross_profit_paisa"]
        nm, gm = p["net_margin_bps"], p["gross_margin_bps"]
        c["sales"].set(T.money(p["sales_paisa"]), "revenue recognised on invoices")
        c["prod"].set(T.money(p["production_cost_paisa"]), f"direct {T.money(p['direct_production_paisa'])} + overhead {T.money(p['factory_overhead_paisa'])}")
        exp_total, exp_n = q.expense_total(self.conn, start, end)               # exactly what the Expenses page adds up for the same dates
        c["opex"].set(T.money(exp_total), f"{exp_n} entr{'y' if exp_n == 1 else 'ies'} on the Expenses page")
        c["gross"].set(T.money(gross), "sales - production cost" + (f"  ·  {T.pct_bps(gm)} of sales" if gm is not None else ""))
        c["net"].set(T.money(net), "accounting profit, not cash" + (f"  ·  {T.pct_bps(nm)} of sales" if nm is not None else ""),
                     "good" if net > 0 else "bad" if net < 0 else "neutral", "Profit" if net > 0 else "Loss" if net < 0 else "Break-even")
        c["cash"].set(T.money(pos["cash_paisa"]), "actual cash available", "cash" if pos["cash_paisa"] >= 0 else "bad")
        c["inv"].set(T.money(pos["inventory_value_paisa"]), "factory-owned, weighted average")
        c["ar"].set(T.money(pos["receivables_paisa"]), f"{len(s['receivables'])} customer(s)")
        c["ap"].set(T.money(pos["payables_paisa"]), f"{len(s['payables'])} supplier(s)")
        c["sal"].set(T.money(pos["salaries_payable_paisa"]), f"{len(s['unpaid_salaries'])} payslip(s) unpaid/partial")
        c["cap"].set(T.money(pos["partner_capital_paisa"]), "contributions less withdrawals")
        c["adv"].set(T.money(pos["customer_advances_paisa"]), "not yet earned revenue")
        c["dist"].set(T.money(pos["distributions_payable_paisa"]), "approved, not yet paid")
        inv = s["inventory"]
        if s["warnings"]:
            self.warn.configure(text="⚠  " + "\n⚠  ".join(s["warnings"]))
            self.warn.pack(fill="x", pady=(0, 6), before=self.hero)
        else:
            self.warn.pack_forget()
        T_ = self.tables
        T_["Low stock"].set_rows(inv["low_stock"]); T_["Inventory"].set_rows(inv["items"]); T_["Receivables"].set_rows(s["receivables"])
        T_["Suppliers owed"].set_rows(s["payables"]); T_["Unpaid salaries"].set_rows(s["unpaid_salaries"]); T_["Partners"].set_rows(s["partners"])
        self._chart(s["chart_monthly"])

    def _chart(self, series):
        if self.canvas and series == getattr(self, "_chart_series", None):
            return                                                              # same figures as on screen: keep the chart
        self._chart_series = [dict(m) for m in series]
        if self.canvas:
            self.canvas.get_tk_widget().destroy()
        card, muted, border, text = T.rc(T.CARD), T.rc(T.MUTED), T.rc(T.BORDER), T.rc(T.TEXT)
        c_sales, c_cost, c_good, c_bad = T.rc(T.ACCENT_FILL), border, T.rc(T.GOOD), T.rc(T.BAD)
        fig = Figure(figsize=(9, 3.6), dpi=100, facecolor=card)
        ax = fig.add_subplot(111, facecolor=card)
        months = [datetime.strptime(m["month"], "%Y-%m").strftime("%b %Y") for m in series]
        n = len(series)
        x = list(range(n))
        sales = [m["revenue"] / 100 for m in series]
        costs = [(m["cost_of_production"] + m["operating_expenses"]) / 100 for m in series]
        net = [m["net_profit"] / 100 for m in series]
        w = 0.34 if n > 1 else 0.22
        b1 = ax.bar([i - w / 2 - 0.02 for i in x], sales, w, color=c_sales, label="Sales", zorder=3)
        b2 = ax.bar([i + w / 2 + 0.02 for i in x], costs, w, color=c_cost, label="Costs & expenses", zorder=3)
        # net profit: green while positive, red below zero. Months with no activity at all are left blank, not drawn as "0".
        active = [bool(m["revenue"] or m["cost_of_production"] or m["operating_expenses"] or m["net_profit"]) for m in series]
        netp = [v if a else float("nan") for v, a in zip(net, active)]
        ax.plot(x, netp, color=text, lw=1.6, alpha=0.55, zorder=4)
        if n > 1:
            ax.fill_between(x, netp, 0, where=[a and v >= 0 for v, a in zip(net, active)], color=c_good, alpha=0.14, zorder=2)
            ax.fill_between(x, netp, 0, where=[a and v < 0 for v, a in zip(net, active)], color=c_bad, alpha=0.18, zorder=2)
        ax.scatter(x, netp, s=70, c=[c_good if v >= 0 else c_bad for v in net], edgecolors=card, linewidths=2, zorder=5, label="Net profit")
        top = max([*sales, *costs, *net, 1]); low = min([*net, 0]); span = (top - low) or 1
        for i, v in enumerate(net):
            if active[i]:
                ax.annotate(self._short(v), (i, v), textcoords="offset points", xytext=(0, 12 if v >= 0 else -18), ha="center", fontsize=10,
                            fontweight="bold", color=c_good if v >= 0 else c_bad, zorder=6,
                            bbox=dict(boxstyle="round,pad=0.2", fc=card, ec="none", alpha=0.85))
        if n <= 6:
            for bars in (b1, b2):
                for r in bars:
                    h = r.get_height()
                    if h:
                        ax.annotate(self._short(h), (r.get_x() + r.get_width() / 2, h), textcoords="offset points", xytext=(0, 3),
                                    ha="center", fontsize=9, color=muted, zorder=6)
        ax.axhline(0, color=muted, lw=1, zorder=3)
        ax.set_ylim(low - span * 0.14 if low < 0 else 0, top + span * 0.16)
        ax.set_xlim(-0.6, n - 0.4)
        ax.set_xticks(x); ax.set_xticklabels(months, color=muted, fontsize=11)
        ax.tick_params(colors=muted, labelsize=10, length=0)
        ax.yaxis.set_major_formatter(lambda v, _p: self._short(v))
        ax.grid(axis="y", color=border, lw=0.6, alpha=0.8); ax.set_axisbelow(True)
        for sp in ax.spines.values():
            sp.set_visible(False)
        h, l = ax.get_legend_handles_labels(); order = [l.index(k) for k in ("Sales", "Costs & expenses", "Net profit")]
        ax.legend([h[i] for i in order], [l[i] for i in order], facecolor=card, edgecolor="none", labelcolor=text, fontsize=11, frameon=False, loc="lower center",
                  bbox_to_anchor=(0.5, 1.0), ncol=3, handlelength=1.2, columnspacing=2)
        fig.tight_layout()
        self.canvas = smooth.embed_chart(fig, self.chart_frame, fill="x", padx=10, pady=10)
        self.canvas.draw_idle()

    @staticmethod
    def _short(v: float) -> str:
        """Axis/label text: 1,250,000 -> 1.25M, 85,000 -> 85k, 450 -> 450."""
        a = abs(v)
        t = f"{a / 1e6:.2f}M" if a >= 1e6 else f"{a / 1e3:.1f}k" if a >= 1e4 else f"{a:,.0f}"
        t = t.replace(".00M", "M").replace(".0k", "k")
        return ("-" if v < 0 else "") + t
