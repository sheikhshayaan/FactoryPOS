"""Attendance & Salaries: one page for the gate (IN / OUT / Absent / Leave buttons), the month sheet and the salary buttons."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from tkinter import filedialog

import customtkinter as ctk

from reports import documents
from services import payroll_service as pr, queries as q, settings_service as st
from services.units import from_paisa, to_paisa
from ui import theme as T
from ui.base import Page
from ui.widgets import Col, DataTable, Field, RowList, Stat, confirm, make_button, make_tabs, show_error, show_info, surface

WHITE = ("#ffffff", "#ffffff")


def _hm(minutes: int) -> str:
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _stamp(iso: str | None, today: str) -> str:
    """'08:03' for today, '05 Oct 22:00' for another day."""
    if not iso:
        return "-"
    return iso[11:16] if iso[:10] == today else f"{date.fromisoformat(iso[:10]).strftime('%d %b')} {iso[11:16]}"


def month_label(period: str) -> str:
    return f"{calendar.month_name[int(period[5:])]} {period[:4]}"


def _chip(parent, text: str, kind: str):
    strong, soft = T.KINDS[kind]
    return ctk.CTkLabel(parent, text=f"  {text}  ", text_color=strong, fg_color=soft, corner_radius=8, height=26, font=T.font(12, "bold"))


# state of a board row -> (chip text, chip kind, outline colour)
STATES = {
    "idle": ("Not in yet", "neutral", T.BORDER),
    "in": ("IN", "good", T.GOOD),
    "done": ("Finished", "info", T.BORDER),
    "absent": ("Absent", "bad", T.BAD),
    "leave": ("On leave", "info", T.INFO),
    "half_day": ("Half day", "cost", T.COST),
}


class AttendancePage(Page):
    title = "Attendance & Salaries"
    subtitle = ""

    # ------------------------------------------------------------------ layout
    def build(self):
        self.period = date.today().strftime("%Y-%m")
        self._board: list[dict] = []
        self._index: dict[int, int] = {}
        self._live: dict[int, tuple] = {}
        self.btns: dict[tuple[int, str], ctk.CTkButton] = {}      # Today board buttons
        self.sbtns: dict[tuple[int, str], ctk.CTkButton] = {}     # Month sheet buttons
        self._month_labels: list[ctk.CTkLabel] = []
        self._tickn, self._today, self._job, self._flash_job = 0, date.today(), None, None
        self.can_mark = self.ctx.can("manage_attendance")
        self.can_pay = self.ctx.can("manage_payroll")
        self.sees_money = self.ctx.can("view_all") or self.can_pay
        self.tabs = make_tabs(self.body, command=self._tab_changed)
        self.tabs.pack(fill="both", expand=True)
        t_today = self.tabs.add("Today")
        t_month = self.tabs.add("Month & Salaries") if self.sees_money else None
        t_log = self.tabs.add("Records")
        t_emp = self.tabs.add("Employees") if self.sees_money else None
        self._build_today(t_today)
        if t_month is not None:
            self._build_month(t_month)
        self._build_log(t_log)
        if t_emp is not None:
            self._build_employees(t_emp)
        self.bind("<Destroy>", self._on_destroy)
        self._job = self.after(1000, self._tick)

    def _on_destroy(self, e):
        if e.widget is self and self._job:
            for job in (self._job, self._flash_job):
                try:
                    if job:
                        self.after_cancel(job)
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------ TODAY
    def _build_today(self, tab):
        top = ctk.CTkFrame(tab, fg_color="transparent")
        top.pack(fill="x", pady=(2, 8))
        left = ctk.CTkFrame(top, fg_color="transparent"); left.pack(side="left")
        self.clock = ctk.CTkLabel(left, text="", font=T.font(26, "bold"), text_color=T.TEXT, anchor="w")
        self.clock.pack(anchor="w")
        chips = ctk.CTkFrame(left, fg_color="transparent"); chips.pack(anchor="w", pady=(6, 0))
        self.count_chips = {}
        for key, kind in (("in", "good"), ("done", "info"), ("absent", "bad"), ("leave", "info"), ("idle", "neutral")):
            c = _chip(chips, "", kind); c.pack(side="left", padx=(0, 8)); self.count_chips[key] = c
        self.flash = ctk.CTkLabel(left, text="", font=T.font(13, "bold"), text_color=T.GOOD, anchor="w", height=20)
        self.flash.pack(anchor="w", pady=(6, 0))
        self.search = ctk.CTkEntry(top, placeholder_text="Find an employee", width=260, height=40, font=T.font(14))
        self.search.pack(side="right", anchor="s")
        self.search.bind("<KeyRelease>", lambda _e: self._draw_board())
        self.notice_box = ctk.CTkFrame(tab, fg_color=T.SOFT_COST, corner_radius=10)
        self.notice = ctk.CTkLabel(self.notice_box, text="", text_color=T.COST, font=T.font(13), wraplength=860, justify="left", anchor="w")
        self.notice.pack(padx=14, pady=10, anchor="w")
        if not self.can_mark:
            ctk.CTkLabel(tab, text="You can look at attendance here, but only the owner and the production supervisor can press the buttons.",
                         text_color=T.MUTED, font=T.font(13)).pack(anchor="w", pady=(0, 6))
        self.board = RowList(tab, [("Employee", 0, "w"), ("Status", 132, "w"), ("In", 84, "center"), ("Out", 84, "center"), ("Hours", 72, "center"),
                                   ("", 306, "e")], empty_text="No employees yet. Add them on the Employees tab.")
        self.board.pack(fill="both", expand=True)

    def _load_board(self):
        self._board = pr.live_board(self.conn)
        self._draw_board()
        self._update_notice()

    def _draw_board(self):
        term = self.search.get().strip().lower()
        rows = [r for r in self._board if not term or term in f"{r['name']} {r['emp_code']} {r['job_role'] or ''}".lower()]
        self._index = {r["id"]: i for i, r in enumerate(rows)}
        self.board.set_rows(rows, self._render_member, border=lambda r: STATES[r["state"]][2] if not r["auto_out"] else T.COST,
                            key=lambda r: r["id"], sig=self._row_sig)
        self._prune(self._live), self._prune(self.btns)
        self._update_counts()

    @staticmethod
    def _row_sig(r):
        """A board row is redrawn only when something visible changed. The running hours of someone who is IN tick by themselves (see _update_live)."""
        return {k: v for k, v in r.items() if not (k == "minutes" and r["state"] == "in")}

    @staticmethod
    def _prune(d: dict):
        """Forget widgets that were redrawn away (the dict keeps the buttons/labels of rows that were NOT redrawn)."""
        for k, w in list(d.items()):
            widget = w[0] if isinstance(w, tuple) else w
            try:
                alive = bool(widget.winfo_exists())
            except Exception:  # noqa: BLE001
                alive = False
            if not alive:
                del d[k]

    def _update_counts(self):
        n = {k: 0 for k in self.count_chips}
        for r in self._board:
            n[{"half_day": "leave"}.get(r["state"], r["state"])] += 1
        words = {"in": "working now", "done": "finished", "absent": "absent", "leave": "on leave", "idle": "not marked yet"}
        for k, c in self.count_chips.items():
            c.configure(text=f"  {n[k]} {words[k]}  ")

    def _update_notice(self):
        rows = pr.recent_auto_closed(self.conn)
        if not rows:
            if self.notice_box.winfo_manager():
                self.notice_box.pack_forget()
            return
        lines = [f"{r['name']}: IN {_stamp(r['clock_in'], '')}, closed automatically at {r['clock_out'][11:16]}" for r in rows[:4]]
        text = ("Heads-up - OUT was not pressed for: " + "; ".join(lines) + (f" (+{len(rows) - 4} more)" if len(rows) > 4 else "") +
                ".  A shift left open for 10 hours is closed by the program and counted as 10 hours. If you know the real time, fix it on the Records tab.")
        if self.notice.cget("text") != text:
            self.notice.configure(text=text)
        if not self.notice_box.winfo_manager():                       # re-packing an already shown box makes the list below jump
            self.notice_box.pack(fill="x", pady=(0, 8), before=self.board)

    def _render_member(self, cells, r):
        today = date.today().isoformat()
        name = ctk.CTkFrame(cells[0], fg_color="transparent"); name.pack(anchor="w")
        ctk.CTkLabel(name, text=r["name"], font=T.font(15, "bold"), text_color=T.TEXT, anchor="w").pack(anchor="w")
        sub = f"{r['emp_code']}  ·  {r['job_role'] or 'No role'}" + ("  ·  Night shift" if r["default_shift"] == "night" else "")
        ctk.CTkLabel(name, text=sub, font=T.font(12), text_color=T.MUTED, anchor="w").pack(anchor="w")
        text, kind, _b = STATES[r["state"]]
        st_cell = ctk.CTkFrame(cells[1], fg_color="transparent"); st_cell.pack(anchor="w")
        if r["auto_out"]:
            _chip(st_cell, "AUTO OUT", "cost").pack(anchor="w")
            ctk.CTkLabel(st_cell, text="OUT not pressed - closed at 10 h", font=T.font(11), text_color=T.COST, wraplength=140, justify="left", anchor="w").pack(anchor="w")
        else:
            _chip(st_cell, ("●  " if r["state"] == "in" else "") + text, kind).pack(anchor="w")
        ctk.CTkLabel(cells[2], text=_stamp(r["clock_in"], today), font=T.font(14), text_color=T.TEXT).pack(anchor="center")
        ctk.CTkLabel(cells[3], text=_stamp(r["clock_out"], today), font=T.font(14), text_color=T.TEXT).pack(anchor="center")
        shown = r["minutes"] if r["state"] in ("in", "done") else None
        hrs = ctk.CTkLabel(cells[4], text=_hm(shown) if shown is not None else "-", font=T.font(14, "bold" if r["state"] == "in" else "normal"),
                           text_color=T.GOOD if r["state"] == "in" else T.TEXT)
        hrs.pack(anchor="center")
        if r["state"] == "in":
            self._live[r["id"]] = (hrs, r["clock_in"])
        on = self.can_mark
        s = r["state"]
        rules = {"in": (s in ("idle", "absent", "leave", "half_day")), "out": s == "in", "absent": s in ("idle", "leave"),
                 "leave": s in ("idle", "absent"), "undo": bool(r["can_undo"])}
        row = ctk.CTkFrame(cells[5], fg_color="transparent"); row.pack(anchor="e")
        for key, label, width in (("in", "IN", 52), ("out", "OUT", 56), ("absent", "Absent", 66), ("leave", "Leave", 60), ("undo", "Undo", 52)):
            b = self._action_button(row, key, label, width, on and rules[key], lambda e=r["id"], k=key: self.press(e, k))
            b.pack(side="left", padx=2)
            self.btns[(r["id"], key)] = b

    @staticmethod
    def _action_button(parent, key, label, width, enabled, cmd):
        look = {"in": dict(fg_color=T.GOOD, hover_color=T.GOOD, text_color=WHITE, border_width=0),
                "out": dict(fg_color=T.ACCENT_FILL, hover_color=T.ACCENT_HOVER, text_color=WHITE, border_width=0),
                "absent": dict(fg_color="transparent", hover_color=T.SOFT_BAD, text_color=T.BAD, border_width=1, border_color=T.BAD),
                "leave": dict(fg_color="transparent", hover_color=T.CARD_ALT, text_color=T.TEXT, border_width=1, border_color=T.BORDER),
                "undo": dict(fg_color="transparent", hover_color=T.CARD_ALT, text_color=T.MUTED, border_width=0)}[key]
        if not enabled:
            look = dict(fg_color=T.CARD_ALT if key != "undo" else "transparent", hover_color=T.CARD_ALT, text_color=T.MUTED, border_width=0)
        return ctk.CTkButton(parent, text=label, width=width, height=36, corner_radius=10, font=T.font(13, "bold" if key in ("in", "out") else "normal"),
                             command=cmd, state="normal" if enabled else "disabled", **look)

    # -- the buttons
    def press(self, employee_id: int, action: str):
        """IN / OUT / Absent / Leave / Undo for one employee. Errors are shown; the row redraws on success."""
        fn = {"in": lambda: pr.clock_in(self.conn, self.user, employee_id),
              "out": lambda: pr.clock_out(self.conn, self.user, employee_id),
              "absent": lambda: pr.mark_status(self.conn, self.user, employee_id, "absent"),
              "leave": lambda: pr.mark_status(self.conn, self.user, employee_id, "leave"),
              "undo": lambda: pr.clear_mark(self.conn, self.user, employee_id)}[action]
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            show_error(self, str(e) or type(e).__name__)
            return None
        self._board = pr.live_board(self.conn)
        new = next((r for r in self._board if r["id"] == employee_id), None)
        if new is not None and employee_id in self._index:
            self.board.replace(self._index[employee_id], new)
            self._prune(self._live), self._prune(self.btns)
        self._update_counts()
        if new is not None:
            self._say(f"{new['name']}: " + {"in": "IN marked", "out": "OUT marked", "absent": "marked Absent", "leave": "marked on Leave", "undo": "mark undone"}[action])
        if action == "out" and res.get("auto"):
            show_info(self, "Closed at the 10-hour limit", "This shift had been open for 10 hours or more, so it was closed at 10 hours and marked AUTO OUT. "
                                                           "If the real leaving time is known, fix it on the Records tab.")
        self._update_notice()
        return res

    def _say(self, text: str):
        """One short confirmation line under the clock, so the operator sees the button worked; it fades after a few seconds."""
        self.flash.configure(text="✓  " + text)
        if self._flash_job:
            try:
                self.after_cancel(self._flash_job)
            except Exception:  # noqa: BLE001
                pass
        self._flash_job = self.after(6000, lambda: self.flash.winfo_exists() and self.flash.configure(text=""))

    def _tab_changed(self):
        """Whatever tab is opened shows the books as they are NOW (an IN pressed on Today is in the Month and Records tabs the moment you open them)."""
        name = self.tabs.get()
        try:
            if name == "Today":
                self._load_board()
            elif name == "Month & Salaries":
                self._load_month()
            elif name == "Records":
                self._load_log()
            elif name == "Employees":
                self._load_emp()
        except Exception:  # noqa: BLE001 - never leave the operator on a broken tab
            pass

    # -- live updates
    def _tick(self):
        if not self.winfo_exists():
            return
        self.clock.configure(text=datetime.now().strftime("%a %d %b %Y    %H:%M:%S"))
        self._tickn += 1
        try:
            if self.winfo_ismapped():
                if self._tickn % 20 == 0:
                    self._update_live()
                if self._tickn % 15 == 0 and self.tabs.get() == "Today":
                    self._sync_board()
                if self._tickn % 60 == 0:
                    self._housekeeping()
        except Exception:  # noqa: BLE001 - the clock must keep running whatever happens
            pass
        self._job = self.after(1000, self._tick)

    def _update_live(self, now: datetime | None = None):
        now = (now or datetime.now()).replace(second=0, microsecond=0)
        for label, cin in self._live.values():
            label.configure(text=_hm(max(0, int((now - datetime.fromisoformat(cin)).total_seconds() // 60))))

    def _sync_board(self):
        """Pick up changes made elsewhere (another window, the Records tab) without redrawing rows that did not change."""
        new = pr.live_board(self.conn)
        if [self._row_sig(r) for r in new] != [self._row_sig(r) for r in self._board]:
            self._board = new
            self._draw_board()
            self._update_notice()

    def _housekeeping(self):
        closed = pr.auto_close_open(self.conn)
        if date.today() != self._today:                      # midnight passed with the program open
            self._today = date.today()
            self.app._auto_accrue(announce=True)
            self.refresh()
        elif closed:
            self._load_board()
            if self.sees_money:
                self._load_month()
            self._load_log()

    # ------------------------------------------------------------------ MONTH & SALARIES
    def _month_bar(self, parent, extra=None):
        bar = ctk.CTkFrame(parent, fg_color="transparent"); bar.pack(fill="x", pady=(2, 8))
        make_button(bar, "‹", lambda: self._shift_month(-1), "secondary", width=44).pack(side="left")
        lab = ctk.CTkLabel(bar, text="", font=T.font(18, "bold"), text_color=T.TEXT, width=190); lab.pack(side="left", padx=6)
        self._month_labels.append(lab)
        make_button(bar, "›", lambda: self._shift_month(1), "secondary", width=44).pack(side="left")
        make_button(bar, "This month", self._this_month, "ghost", width=100).pack(side="left", padx=8)
        return bar

    def _shift_month(self, d: int):
        t = int(self.period[:4]) * 12 + int(self.period[5:]) - 1 + d
        self.period = f"{t // 12:04d}-{t % 12 + 1:02d}"
        self._reload_months()

    def _this_month(self):
        self.period = date.today().strftime("%Y-%m")
        self._reload_months()

    def _reload_months(self):
        for lab in self._month_labels:
            lab.configure(text=month_label(self.period))
        if self.sees_money:
            self._load_month()
        self._load_log()

    def _build_month(self, tab):
        self._month_bar(tab)
        box = surface(tab); box.pack(fill="x", pady=(0, 8))
        inner = ctk.CTkFrame(box, fg_color="transparent"); inner.pack(fill="x", padx=18, pady=12)
        self.ms: dict[str, Stat] = {}
        for i, (k, label) in enumerate((("salary", "Salaries this month"), ("ded", "Estimated deductions"), ("accrued", "Accrued so far"),
                                        ("paid", "Paid"), ("due", "Still to pay"))):
            s = Stat(inner, label); s.pack(side="left", expand=True, fill="x"); self.ms[k] = s
        bar = self.toolbar(tab)
        self.button(bar, "Accrue all salaries", self.accrue_all, "manage_payroll", T.GREEN)
        self.auto_btn = self.button(bar, "Auto-accrue: ON", self.toggle_auto, "manage_users", T.CARD_ALT)
        self.button(bar, "Deduction rules", self.rules, "manage_users", T.CARD_ALT)
        self.m_search = ctk.CTkEntry(bar, placeholder_text="Search employee", width=260, height=40, font=T.font(14))
        self.m_search.pack(side="right")
        self.m_search.bind("<KeyRelease>", lambda _e: self._draw_month())
        self.m_notice = ctk.CTkLabel(tab, text="", text_color=T.WARN, font=T.font(13), anchor="w", wraplength=900, justify="left")
        self.sheet = RowList(tab, [("Employee", 0, "w"), ("Attendance", 150, "w"), ("Salary", 108, "e"), ("Net pay", 112, "e"), ("Status", 104, "w"), ("", 226, "e")],
                             empty_text="No employees yet. Add them on the Employees tab.")
        self.sheet.pack(fill="both", expand=True)
        self.m_foot = ctk.CTkLabel(tab, text="", text_color=T.MUTED, font=T.font(12), anchor="w")
        self.m_foot.pack(fill="x", pady=(4, 0))

    def _load_month(self):
        rows = pr.month_overview(self.conn, self.period)
        active = [r for r in rows if r["is_active"]]
        tot = lambda k, rs=rows: sum(r[k] or 0 for r in rs)   # noqa: E731
        self.ms["salary"].set(T.money(tot("salary_paisa", active)))
        self.ms["ded"].set(T.money(tot("est_deduction_paisa", active)))
        self.ms["accrued"].set(T.money(tot("net_paisa")))
        self.ms["paid"].set(T.money(tot("paid_paisa")))
        self.ms["due"].set(T.money(tot("due_paisa")), tone="bad" if tot("due_paisa") else None)
        on, unpaid = st.get(self.conn, "absence_deduction_basis") == "calendar_days", st.get(self.conn, "leave_deduction") == "unpaid"
        auto = st.get(self.conn, "auto_accrue_payroll") == "1"
        self.auto_btn.configure(text=f"Auto-accrue: {'ON' if auto else 'OFF'}")
        self._month_rows = rows
        self._draw_month()
        if any(r["auto_n"] for r in rows):
            self.m_foot.configure(text="* includes shifts that were closed automatically after 10 hours (counted as 10 h).")
        else:
            self.m_foot.configure(text="")
        todo = [r for r in rows if r["is_active"] and r["payslip_id"] is None and r["manual_reason"]]
        if todo:
            text = "Accrue by hand: " + "; ".join(f"{r['name']} ({r['manual_reason']})" for r in todo)
            if self.m_notice.cget("text") != text:
                self.m_notice.configure(text=text)
            if not self.m_notice.winfo_manager():
                self.m_notice.pack(fill="x", pady=(0, 4), before=self.sheet)
        elif self.m_notice.winfo_manager():
            self.m_notice.pack_forget()

    def _draw_month(self):
        """The month sheet, narrowed to what is typed in the search box (name, code or role). The totals above always cover everyone."""
        term = self.m_search.get().strip().lower()
        rows = [r for r in self._month_rows if not term or term in f"{r['name']} {r['emp_code']} {r['job_role'] or ''}".lower()]
        self.sheet.empty_text = "No employee matches your search." if term and self._month_rows else "No employees yet. Add them on the Employees tab."
        self.sheet.set_rows(rows, self._render_salary, border=lambda r: T.BAD if r["status"] in ("unpaid", "partial") and r["is_active"] else T.BORDER,
                            key=lambda r: r["id"])
        self._prune(self.sbtns)

    def _render_salary(self, cells, r):
        name = ctk.CTkFrame(cells[0], fg_color="transparent"); name.pack(anchor="w")
        ctk.CTkLabel(name, text=r["name"] + ("" if r["is_active"] else f"  (left {r['left_on']})" if r.get("left_on") else "  (inactive)"), font=T.font(15, "bold"), text_color=T.TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(name, text=f"{r['emp_code']}  ·  {r['job_role'] or 'No role'}", font=T.font(12), text_color=T.MUTED, anchor="w").pack(anchor="w")
        a = ctk.CTkFrame(cells[1], fg_color="transparent"); a.pack(anchor="w")
        days = f"{r['present']} day{'s' if r['present'] != 1 else ''}" + (f" + {r['half']} half" if r["half"] else "")
        ctk.CTkLabel(a, text=f"{days}  ·  {_hm(r['minutes'])}{'*' if r['auto_n'] else ''}", font=T.font(13), text_color=T.TEXT, anchor="w").pack(anchor="w")
        ctk.CTkLabel(a, text=f"{r['absent']} absent  ·  {r['leave']} leave", font=T.font(12), text_color=T.BAD if r["absent"] else T.MUTED, anchor="w").pack(anchor="w")
        s = ctk.CTkFrame(cells[2], fg_color="transparent"); s.pack(anchor="e")
        ctk.CTkLabel(s, text=T.money(r["salary_paisa"]) if r["salary_paisa"] is not None else "No salary set", font=T.font(14), text_color=T.TEXT, anchor="e").pack(anchor="e")
        if r["payslip_id"]:
            ded, tag = r["ps_deduction_paisa"], "deducted"
        else:
            ded, tag = r["est_deduction_paisa"], "est."
        ctk.CTkLabel(s, text=f"- {T.money(ded)} {tag}" if ded else "no deduction", font=T.font(12), text_color=T.WARN if ded else T.MUTED, anchor="e").pack(anchor="e")
        if r["payslip_id"]:
            ctk.CTkLabel(cells[3], text=T.money(r["net_paisa"]), font=T.font(15, "bold"), text_color=T.TEXT).pack(anchor="e")
        else:
            ctk.CTkLabel(cells[3], text="~ " + (T.money(r["est_net_paisa"]) if r["est_net_paisa"] is not None else "-"), font=T.font(14), text_color=T.MUTED).pack(anchor="e")
        kind = {"not accrued": "neutral", "unpaid": "cost", "partial": "owe", "paid": "good"}[r["status"]]
        c = ctk.CTkFrame(cells[4], fg_color="transparent"); c.pack(anchor="w")
        _chip(c, {"not accrued": "Not accrued", "unpaid": "Unpaid", "partial": "Part paid", "paid": "Paid"}[r["status"]], kind).pack(anchor="w")
        if r["payslip_id"] and r["due_paisa"]:
            ctk.CTkLabel(c, text=f"due {T.money(r['due_paisa'])}", font=T.font(11), text_color=T.MUTED, anchor="w").pack(anchor="w")
        has, due, paid = bool(r["payslip_id"]), r["due_paisa"] or 0, r["paid_paisa"] or 0
        row = ctk.CTkFrame(cells[5], fg_color="transparent"); row.pack(anchor="e")
        for key, label, width, ok, cmd in (
                ("accrue", "Accrue", 62, self.can_pay and not has and (r["is_active"] or bool(r.get("left_on")) and self.period <= r["left_on"][:7]) and r["salary_paisa"] is not None, lambda x=r: self.accrue(x)),
                ("pay", "Pay", 48, self.can_pay and has and due > 0, lambda x=r: self.pay(x)),
                ("slip", "Slip", 48, has, lambda x=r: self.slip(x)),
                ("edit", "Edit", 48, self.can_pay and has and paid == 0, lambda x=r: self.revise(x))):
            look = {"accrue": dict(fg_color=T.ACCENT_FILL, hover_color=T.ACCENT_HOVER, text_color=WHITE, border_width=0),
                    "pay": dict(fg_color=T.GOOD, hover_color=T.GOOD, text_color=WHITE, border_width=0)}.get(
                key, dict(fg_color="transparent", hover_color=T.CARD_ALT, text_color=T.TEXT, border_width=1, border_color=T.BORDER))
            if not ok:
                look = dict(fg_color=T.CARD_ALT if key in ("accrue", "pay") else "transparent", hover_color=T.CARD_ALT, text_color=T.MUTED, border_width=0)
            b = ctk.CTkButton(row, text=label, width=width, height=34, corner_radius=10, font=T.font(13, "bold" if key in ("accrue", "pay") else "normal"),
                              command=cmd, state="normal" if ok else "disabled", **look)
            b.pack(side="left", padx=2)
            self.sbtns[(r["id"], key)] = b

    # -- salary actions
    def _net_hint(self, basic_paisa):
        """Live 'Net pay = ...' line under the last money field of the accrue / edit dialogs."""
        def live(v, dlg):
            try:
                basic = to_paisa(v["bo"]) if v.get("bo") else (to_paisa(v["ba"]) if "ba" in v else basic_paisa)
                net = basic + to_paisa(v["al"] or "0") - to_paisa(v["de"] or "0") - to_paisa(v["ar"] or "0")
                dlg.set_hint("ar", f"Net pay = {T.money(basic)} + allowances - deductions - advance = {T.money(net)}", bad=net < 0)
            except Exception:  # noqa: BLE001 - half-typed numbers
                pass
        return live

    def accrue(self, r):
        adv = next((e["advance_paisa"] for e in q.employees(self.conn) if e["id"] == r["id"]), 0)
        per = self.period
        self.form(f"Accrue salary - {r['name']} - {month_label(per)}", [
            Field("al", "Allowances (Rs)", "money", default="0"),
            Field("de", "Deductions (Rs)", "money", default=str(from_paisa(r["est_deduction_paisa"])),
                  hint=r["deduction_note"] or "No absence deduction for this month."),
            Field("dn", "Deduction note (needed when deducting)", default=r["deduction_note"] if r["est_deduction_paisa"] else ""),
            Field("ar", "Advance to recover (Rs)", "money", default="0", live_hint=True),
            Field("bo", "Different basic salary (leave empty for the full salary)", advanced=True),
            Field("br", "Reason for the different basic salary", advanced=True)],
            lambda v: pr.accrue_salary(self.conn, self.user, r["id"], per, allowances=v["al"] or "0", deductions=v["de"] or "0", deduction_notes=v["dn"],
                                       advance_recovery=v["ar"] or "0", basic_override=v["bo"] or None, override_reason=v["br"]),
            intro=f"Basic salary {T.money(r['salary_paisa'])}.  Outstanding advance {T.money(adv)}.  Accruing books the expense once and creates what you owe; "
                  f"Pay then reduces it.", submit_text="Accrue salary", live=self._net_hint(r["salary_paisa"] or 0))

    def pay(self, r):
        self.form(f"Pay {r['name']} - {month_label(self.period)}", [
            Field("a", "Amount (Rs)", "money", default=str(from_paisa(r["due_paisa"])), hint=f"Still due: {T.money(r['due_paisa'])}. Partial payments are fine."),
            Field("on", "Payment date", "date"), Field("ref", "Reference (optional)")],
            lambda v: pr.pay_salary(self.conn, self.user, r["payslip_id"], v["a"], v["on"], reference=v["ref"]), submit_text="Record payment")

    def slip(self, r):
        f = filedialog.asksaveasfilename(defaultextension=".pdf", initialfile=f"salary_{r['emp_code']}_{self.period}.pdf", filetypes=[("PDF", "*.pdf")])
        if f:
            self.guard(lambda: documents.payslip_pdf(self.conn, r["payslip_id"], f), done=lambda: show_info(self, "Saved", f))

    def revise(self, r):
        ps = self.conn.execute("SELECT * FROM payslips WHERE id=?", (r["payslip_id"],)).fetchone()
        self.form(f"Edit payslip - {r['name']} - {month_label(self.period)}", [
            Field("ba", "Basic salary (Rs)", "money", default=str(from_paisa(ps["basic_paisa"]))),
            Field("al", "Allowances (Rs)", "money", default=str(from_paisa(ps["allowances_paisa"]))),
            Field("de", "Deductions (Rs)", "money", default=str(from_paisa(ps["deductions_paisa"]))),
            Field("dn", "Deduction note (needed when deducting)", default=ps["deduction_notes"] or ""),
            Field("ar", "Advance recovered (Rs)", "money", default=str(from_paisa(ps["advance_recovered_paisa"])), live_hint=True),
            Field("why", "Why are you changing it? (required)")],
            lambda v: pr.revise_payslip(self.conn, self.user, r["payslip_id"], v["why"], basic=v["ba"] or "0", allowances=v["al"] or "0",
                                        deductions=v["de"] or "0", deduction_notes=v["dn"], advance_recovery=v["ar"] or "0"),
            intro="Only possible before any payment. The old payslip is reversed in the books and a new one is posted; both stay on record.",
            submit_text="Save changes", live=self._net_hint(ps["basic_paisa"]))

    def accrue_all(self):
        todo = [r for r in pr.month_overview(self.conn, self.period) if r["is_active"] and not r["payslip_id"]]
        ready = [r for r in todo if not r["manual_reason"] and r["salary_paisa"] is not None]
        if not ready:
            show_info(self, "Nothing to accrue", "Every employee already has a payslip for this month" + (", or is left for you to accrue by hand (joined this month)." if todo else "."))
            return
        if not confirm(self, "Accrue all salaries", f"Accrue {month_label(self.period)} salaries for {len(ready)} employee(s)? Estimated deductions from attendance are applied. "
                       f"Employees who joined this month are skipped - accrue them with their own button when you decide.", yes="Accrue all"):
            return
        try:
            res = pr.auto_accrue_month(self.conn, self.user, self.period, auto=False)
        except Exception as e:  # noqa: BLE001
            show_error(self, str(e)); return
        self.refresh()
        show_info(self, "Salaries accrued", f"{len(res['created'])} payslip(s) created." + ("\nSkipped: " + "; ".join(f"{n} ({why})" for n, why in res["skipped"]) if res["skipped"] else ""))

    def toggle_auto(self):
        on = st.get(self.conn, "auto_accrue_payroll") == "1"
        self.guard(lambda: st.set_values(self.conn, self.user, {"auto_accrue_payroll": "0" if on else "1"}), done=self._load_month)

    def rules(self):
        self.form("Deduction rules", [
            Field("ab", "Absent days", "choice", [("Deduct salary / days in the month per absent day", "calendar_days"), ("Do not deduct", "off")], st.get(self.conn, "absence_deduction_basis")),
            Field("lv", "Leave days", "choice", [("Paid - no deduction", "paid"), ("Unpaid - deduct like an absent day", "unpaid")], st.get(self.conn, "leave_deduction"))],
            lambda v: st.set_values(self.conn, self.user, {"absence_deduction_basis": v["ab"], "leave_deduction": v["lv"]}),
            intro="These only change the ESTIMATE shown here and the deduction filled in when salaries are accrued. You can always edit a payslip.")

    # ------------------------------------------------------------------ RECORDS
    def _build_log(self, tab):
        self._month_bar(tab)
        bar = self.toolbar(tab)
        self.button(bar, "Add a past day", self.add_day, "manage_attendance", T.GREEN)
        self.button(bar, "Correct selected", self.correct, "manage_payroll", T.AMBER)
        self.att = DataTable(tab, [Col("work_date", "Date", 90), Col("emp_code", "ID", 50), Col("name", "Employee", 140), Col("status", "Status", 80, lambda v: v.replace("_", " ")),
                                   Col("clock_in", "In", 60, lambda v: (v or "")[11:16] or "-"), Col("clock_out", "Out", 60, lambda v: (v or "")[11:16] or "-"),
                                   Col("break_minutes", "Break", 55, None, "e"), Col("hours", "Worked", 80, None, "e"), Col("flag", "Note", 230)],
                             page_size=30, row_tags=lambda r: "warn" if r["auto_out"] else ("bad" if r["status"] == "absent" else None))
        self.att.pack(fill="both", expand=True)

    def _load_log(self):
        self.att.set_rows([dict(r, hours=_hm(r["worked_minutes"]) if r["worked_minutes"] else "-",
                                flag="AUTO OUT - not pressed, closed at 10 h" if r["auto_out"] else (r["notes"] or ""))
                           for r in q.attendance(self.conn, self.period)])

    def _emps(self):
        return [(f"{e['emp_code']} - {e['name']}", e["id"]) for e in q.employees(self.conn) if e["is_active"]]

    @staticmethod
    def _times(d, tin, tout, next_day, status):
        """(clock_in, clock_out) ISO strings from a date, two times and 'out is the next day'."""
        if status in ("absent", "leave"):
            return None, None
        if bool(tin) != bool(tout):
            raise pr.PayrollError("Give both the IN and the OUT time, or leave both empty.")
        if not tin:
            return None, None
        out_day = (date.fromisoformat(d) + timedelta(days=1 if next_day == "next" else 0)).isoformat()
        return f"{d}T{tin}", f"{out_day}T{tout}"

    def add_day(self):
        sel = self.att.selected() or self.emp.selected()                          # the employee selected in the log (or on the Employees tab)
        who = (sel.get("employee_id") or sel.get("id")) if sel else ""
        def submit(v):
            ci, co = self._times(v["d"], v["in"], v["out"], v["nx"], v["st"])
            pr.record_attendance(self.conn, self.user, v["e"], v["d"], v["st"], shift=v["sh"], clock_in=ci, clock_out=co,
                                 break_minutes=int(v["br"] or 0), notes=v["nt"])
        self.form("Add a past day", [
            Field("e", "Employee", "choice", self._emps(), who), Field("d", "Day (the day the shift started)", "date"),
            Field("st", "Status", "choice", [("Present", "present"), ("Half day", "half_day"), ("Absent", "absent"), ("Leave", "leave")]),
            Field("in", "IN time", "time"), Field("out", "OUT time", "time"),
            Field("nx", "OUT happened", "choice", [("The same day", "same"), ("The next day (night shift)", "next")]),
            Field("sh", "Shift", "choice", [("Day", "day"), ("Night", "night")]),
            Field("br", "Break (minutes)", default="0"), Field("nt", "Notes")], submit,
            intro="For a day nobody pressed the buttons. Today's attendance is taken with the IN and OUT buttons on the Today tab.")

    def correct(self):
        r = self.att.selected()
        if not self.need(r, "a record"):
            return
        base = (r["clock_in"] or r["work_date"])[:10]
        nxt = "next" if r["clock_out"] and r["clock_out"][:10] > base else "same"

        def submit(v):
            ci, co = self._times(base, v["in"], v["out"], v["nx"], v["st"])
            pr.correct_attendance(self.conn, self.user, r["id"], v["why"], status=v["st"], clock_in=ci, clock_out=co, break_minutes=int(v["br"] or 0))
        self.form(f"Correct {r['work_date']} - {r['name']}", [
            Field("st", "Status", "choice", [("Present", "present"), ("Half day", "half_day"), ("Absent", "absent"), ("Leave", "leave")], r["status"]),
            Field("in", "IN time", "time", default=(r["clock_in"] or "")[11:16]), Field("out", "OUT time", "time", default=(r["clock_out"] or "")[11:16]),
            Field("nx", "OUT happened", "choice", [("The same day", "same"), ("The next day (night shift)", "next")], nxt),
            Field("br", "Break (minutes)", default=str(r["break_minutes"])), Field("why", "Reason for the correction (required)")], submit,
            intro="The old values are kept in the correction history. Fixing the OUT time of an AUTO OUT record removes its warning.")

    # ------------------------------------------------------------------ EMPLOYEES
    def _build_employees(self, tab):
        bar = self.toolbar(tab)
        self.button(bar, "+ Add employee", self.add, "manage_payroll", T.GREEN)
        self.button(bar, "Change salary", self.salary, "manage_payroll", T.PURPLE)
        self.button(bar, "Give advance", self.advance, "manage_payroll", T.AMBER)
        self.exit_btn = self.button(bar, "Resign / Remove", self.resign, "manage_payroll", T.RED)
        self.back_btn = self.button(bar, "Bring back", self.reinstate, "manage_payroll")
        self.emp = DataTable(tab, [Col("emp_code", "ID", 70), Col("name", "Name", 160), Col("job_role", "Role", 110), Col("default_shift", "Shift", 60), Col("cost_category", "Cost type", 80),
                                   Col("joined_on", "Joined", 90), Col("status", "Status", 150), Col("salary_paisa", "Monthly salary", 120, T.money, "e"),
                                   Col("advance_paisa", "Advance outstanding", 140, T.money, "e")],
                             row_tags=lambda r: None if r["is_active"] else "muted")
        self.emp.pack(fill="both", expand=True)

    def _load_emp(self):
        rows = q.employees(self.conn)
        for r in rows:
            r["status"] = "Active" if r["is_active"] else f"{pr.EXIT_KINDS.get(r['exit_kind'], 'Left')} {r['left_on'] or ''}".strip()
        self.emp.set_rows(rows)

    def add(self):
        self.form("Add employee", [Field("c", "Employee ID"), Field("n", "Name"), Field("ph", "Phone"), Field("r", "Job role"), Field("j", "Joining date", "date"),
                                   Field("s", "Monthly salary (Rs)", "money"), Field("eff", "Salary effective from", "date"),
                                   Field("sh", "Default shift", "choice", [("Day", "day"), ("Night", "night")]),
                                   Field("cc", "Salary cost type", "choice", [("Direct labor (production)", "direct"), ("Administrative", "admin")])],
                  lambda v: pr.add_employee(self.conn, self.user, emp_code=v["c"], name=v["n"], monthly_salary=v["s"], effective_from=v["eff"], job_role=v["r"], phone=v["ph"],
                                            joined_on=v["j"], default_shift=v["sh"], cost_category=v["cc"]),
                  intro="Tip: set 'Salary effective from' to the joining date. A salary that starts in the middle of a month is accrued by hand for that first month.")

    def salary(self):
        r = self.emp.selected()
        if self.need(r, "an employee"):
            self.form(f"Change salary - {r['name']}", [Field("s", "New monthly salary (Rs)", "money"), Field("eff", "Effective from", "date"), Field("why", "Reason (required)")],
                      lambda v: pr.change_salary(self.conn, self.user, r["id"], v["s"], v["eff"], v["why"]))

    def resign(self):
        r = self.emp.selected()
        if not self.need(r, "an employee"):
            return
        if not r["is_active"]:
            return show_error(self, f"{r['name']} has already left ({r['status']}).")

        def submit(v):
            res = pr.end_employment(self.conn, self.user, r["id"], v["on"], v["kind"], v["why"])
            todo = []
            if not res["last_month_accrued"]:
                todo.append(f"- Salary for {month_label(res['last_month'])} is not accrued yet. Do it by hand in Month & Salaries (Accrue / Pay).")
            if res["unpaid_paisa"]:
                todo.append(f"- Salary still unpaid: {T.money(res['unpaid_paisa'])}.")
            if res["advance_paisa"] > 0:
                todo.append(f"- Advance still outstanding: {T.money(res['advance_paisa'])}. Recover it from the last salary (Advance recovery) or settle it separately.")
            self.after(250, lambda: show_info(self, f"{r['name']} - exit recorded",
                                              "Attendance, salary slips and advances stay on file." + ("\n\nStill to settle:\n" + "\n".join(todo) if todo else "\n\nNothing is left to settle.")))
        self.form(f"Resign / Remove - {r['name']}",
                  [Field("kind", "What happened", "choice", [(label, k) for k, label in pr.EXIT_KINDS.items()], "resigned"),
                   Field("on", "Last working day", "date", default=date.today()), Field("why", "Note (optional)")],
                  submit, submit_text="Record exit",
                  intro="The employee leaves the Today board and no salary is accrued for later months. Nothing is deleted: attendance, payslips and advances stay readable.")

    def reinstate(self):
        r = self.emp.selected()
        if not self.need(r, "an employee"):
            return
        if r["is_active"]:
            return show_error(self, f"{r['name']} is already active.")
        self.form(f"Bring back - {r['name']}", [Field("why", "Reason (required)")], lambda v: pr.reinstate_employee(self.conn, self.user, r["id"], v["why"]),
                  submit_text="Bring back", intro="Use this if the exit was recorded by mistake or the person came back.")

    def advance(self):
        r = self.emp.selected()
        if self.need(r, "an employee"):
            self.form(f"Salary advance - {r['name']}", [Field("a", "Amount (Rs)", "money"), Field("on", "Date", "date")],
                      lambda v: pr.give_advance(self.conn, self.user, r["id"], v["a"], v["on"]), intro="Cash paid out now; recovered from a later salary.")

    # ------------------------------------------------------------------ refresh
    def refresh(self):
        self.app._auto_accrue(announce=False)
        self._today = date.today()
        for lab in self._month_labels:
            lab.configure(text=month_label(self.period))
        self.clock.configure(text=datetime.now().strftime("%a %d %b %Y    %H:%M:%S"))
        pr.auto_close_open(self.conn)
        self._load_board()
        if self.sees_money:
            self._load_month()
            self._load_emp()
        self._load_log()
