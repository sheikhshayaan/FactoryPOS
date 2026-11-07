"""Settings, Reports, Audit Logs, Backup & Restore, User Management."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from tkinter import filedialog

import customtkinter as ctk

from database import connection
from reports import business_summary as bsum, exporters, financial_reports as fr
from services import accounting_service as acct, auth_service as auth, backup_service as bk, queries as q, settings_service as st
from services.dashboard_service import resolve_range
from ui import theme as T
from ui.base import Page
from ui.widgets import Col, DataTable, DateField, Field, FormDialog, make_button, run_bg, show_error, show_info


class SettingsPage(Page):
    title = "Factory Registration & Settings"
    subtitle = "Owner only. Changes are audited."

    def build(self):
        f = ctk.CTkScrollableFrame(self.body, fg_color=T.CARD); f.pack(fill="both", expand=True)
        self.e: dict[str, ctk.CTkBaseClass] = {}

        def row(key, label, hint="", choices=None, folder=False):
            ctk.CTkLabel(f, text=label, text_color=T.MUTED, anchor="w").pack(fill="x", padx=18, pady=(10, 0))
            if folder:                                                 # a folder is chosen with a button, not typed
                line = ctk.CTkFrame(f, fg_color="transparent"); line.pack(anchor="w", padx=18)
                w = ctk.CTkEntry(line, width=420, placeholder_text="No folder chosen"); w.pack(side="left")
                self.choose_btn = make_button(line, "Choose folder...", lambda: self._choose_folder(w), "primary", height=36)
                self.choose_btn.pack(side="left", padx=(8, 0))
                self.clear_folder_btn = make_button(line, "Remove", lambda: (w.delete(0, "end"), self.msg.configure(text="Press Save settings to keep this change.")), "ghost", width=80, height=36)
                self.clear_folder_btn.pack(side="left", padx=(4, 0))
            else:
                w = ctk.CTkComboBox(f, values=choices, state="readonly", width=320) if choices else ctk.CTkEntry(f, width=420)
                w.pack(anchor="w", padx=18)
            if hint:
                ctk.CTkLabel(f, text=hint, text_color=T.MUTED, font=T.font(12), wraplength=700, justify="left").pack(anchor="w", padx=18)
            self.e[key] = w
        row("factory_name", "Factory name"); row("factory_address", "Address"); row("factory_phone", "Phone / contact"); row("currency", "Currency code", "PKR by default")
        row("fiscal_year_start_month", "Financial year starts in month (1-12)", "7 = July (July-June). Dashboard quarters/years follow this.")
        row("absence_deduction_basis", "Absence deduction", "off = nothing is deducted automatically; calendar_days = suggest salary / days-in-month per absent day (you still confirm each payslip).", ["off", "calendar_days"])
        row("backup_retention", "Keep this many automatic daily backups", "Manual and safety backups are never deleted automatically.")
        row("backup_dir", "Extra backup folder (optional)", "e.g. a USB drive. A second copy is written here on every manual backup and every automatic daily backup (re-tried every 30 minutes while the app is open, so a USB plugged in later still gets today's copy). Nothing is ever uploaded online.", folder=True)
        row("allow_negative_cash", "Allow cash to go below zero?", "0 = block payments when the cash balance is insufficient (recommended).", ["0", "1"])
        self.msg = ctk.CTkLabel(f, text="", text_color=T.GREEN); self.msg.pack(anchor="w", padx=18, pady=(10, 0))
        bar = ctk.CTkFrame(f, fg_color="transparent"); bar.pack(anchor="w", padx=18, pady=10)
        self.button(bar, "Save settings", self.save, "manage_users")
        self.button(bar, "Record opening cash balance...", self.opening, "post_journal", T.PURPLE)
        ctk.CTkLabel(f, text="Initial capital: record each partner's contribution on Partners & Investments. Opening stock: Inventory -> Receive stock -> 'Opening balance'.",
                     text_color=T.MUTED, wraplength=760, justify="left").pack(anchor="w", padx=18, pady=(0, 14))

    def _choose_folder(self, entry):
        """Pick the extra backup folder in the normal Windows folder window (it starts where the current one is)."""
        import os
        cur = entry.get().strip()
        d = filedialog.askdirectory(parent=self, title="Choose the folder for the extra backup copy", initialdir=cur if cur and os.path.isdir(cur) else None, mustexist=True)
        if d:
            entry.delete(0, "end"); entry.insert(0, os.path.normpath(d))
            self.msg.configure(text="Folder chosen. Press Save settings to keep it.")

    def refresh(self):
        for k, w in self.e.items():
            v = st.get(self.conn, k)
            if isinstance(w, ctk.CTkComboBox):
                w.set(v or w.cget("values")[0])
            else:
                w.delete(0, "end"); w.insert(0, v)

    def save(self):
        vals = {k: w.get() for k, w in self.e.items()}
        try:
            st.set_values(self.conn, self.user, vals)
        except Exception as e:  # noqa: BLE001
            self.msg.configure(text="")
            show_error(self, str(e))
            return
        self.msg.configure(text="Saved.")

    def opening(self):
        has = acct.balance(self.conn, "3500") != 0
        self.form("Opening cash balance", [Field("a", "Cash in hand when you start using the system (Rs)", "money"), Field("on", "As of date", "date")],
                  lambda v: acct.record_opening_cash(self.conn, self.user, v["a"], v["on"]),
                  intro=("An opening balance already exists - recording another ADDS to it." if has else "Recorded once as opening equity (not income)."))


class ReportsPage(Page):
    title = "Reports"
    subtitle = "Reports only read data. Export to Excel, CSV or PDF (saved only where you choose)."

    def build(self):
        bar = self.toolbar()
        self.sel = ctk.CTkComboBox(bar, values=list(fr.REPORTS), state="readonly", width=210); self.sel.set("Income statement"); self.sel.pack(side="left", padx=(0, 8))
        today = date.today()
        s, e = resolve_range("this_year", today, fy_start_month=7)
        self.a = DateField(bar, s, width=150, height=38, compact=True)                 # calendars, not typed dates
        self.b = DateField(bar, today, width=150, height=38, compact=True)
        self.a.pack(side="left"); ctk.CTkLabel(bar, text="to").pack(side="left", padx=4); self.b.pack(side="left", padx=(0, 8))
        self.button(bar, "Run", self.run, None, T.BLUE)
        self.button(bar, "Excel", lambda: self.export(".xlsx"), "export_reports", T.GREEN)
        self.button(bar, "CSV", lambda: self.export(".csv"), "export_reports", T.GREEN)
        self.button(bar, "PDF", lambda: self.export(".pdf"), "export_reports", T.GREEN)
        bar2 = self.toolbar()                                                                  # its own row: it is wide and would push the others off the screen
        self.button(bar2, "Complete Business Summary", self.summary, "export_reports", T.BLUE)
        ctk.CTkLabel(bar2, text="One PDF (about 8 pages) for a month, a year or your own dates.",
                     text_color=T.MUTED, font=T.font(13)).pack(side="left", padx=(4, 0))
        self.sub = ctk.CTkLabel(self.body, text="", text_color=T.MUTED); self.sub.pack(anchor="w")
        self.holder = ctk.CTkFrame(self.body, fg_color="transparent"); self.holder.pack(fill="both", expand=True)
        self.result = None

    def refresh(self):
        self.run()

    @staticmethod
    def _fmt(v):
        if isinstance(v, Decimal):
            return f"{v:,.2f}"
        return "" if v is None else str(v)

    def run(self):
        try:
            headers, rows, sub = fr.REPORTS[self.sel.get()](self.conn, self.a.get().strip(), self.b.get().strip())
        except Exception as e:  # noqa: BLE001
            show_error(self, f"Could not build the report: {e}"); return
        self.result = (self.sel.get(), headers, rows, sub)
        for w in self.holder.winfo_children():
            w.destroy()
        self.sub.configure(text=sub)
        cols = [Col(str(i), h, 150 if i else 220, self._fmt, "e" if rows and isinstance(rows[0][i], (Decimal, int, float)) and i else "w") for i, h in enumerate(headers)]
        t = DataTable(self.holder, cols, page_size=60)
        t.pack(fill="both", expand=True)
        t.set_rows([{str(i): v for i, v in enumerate(r)} for r in rows])

    def summary(self):
        """One PDF (about 8 pages, numbers and charts) for a month, a financial year or any dates."""
        months, years = bsum.month_choices(self.conn), bsum.year_choices(self.conn)
        today = date.today()
        s0, _e0 = resolve_range("this_month", today, fy_start_month=7)

        def live(v, d):
            d.show("month", v["kind"] == "month")
            d.show("year", v["kind"] == "year")
            d.show("a", v["kind"] == "custom")
            d.show("b", v["kind"] == "custom")

        def submit(v):
            kind = v["kind"]
            custom = (v["a"], v["b"]) if kind == "custom" else None
            label = {"month": v["month"], "year": f"fy{v['year']}", "custom": f"{v['a']}_to_{v['b']}"}[kind]
            f = filedialog.asksaveasfilename(parent=self.winfo_toplevel(), defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
                                             initialfile=f"business_summary_{label}.pdf")
            if not f:
                raise ValueError("Choose where to save the file.")
            try:
                self.winfo_toplevel().configure(cursor="watch"); self.update_idletasks()
                bsum.business_summary_pdf(self.conn, f, kind, month=v["month"], fy_year=v["year"], custom=custom)
            except bsum.SummaryError as e:
                raise ValueError(str(e)) from e
            finally:
                self.winfo_toplevel().configure(cursor="")
            self._summary_saved = f

        d = FormDialog(self, "Complete Business Summary", [
            Field("kind", "Which period?", "choice", [("A month", "month"), ("A financial year", "year"), ("Choose dates", "custom")], "month"),
            Field("month", "Month", "choice", months or [("-", None)]),
            Field("year", "Financial year", "choice", years or [("-", None)]),
            Field("a", "From", "date", default=str(s0)), Field("b", "To", "date", default=today.isoformat())],
            submit, submit_text="Create PDF", live=live,
            intro="One PDF of about 8 pages with numbers and charts, compared with the period before.", on_done=lambda: show_info(self, "Saved", f"Saved to:\n{self._summary_saved}\n\nThis file contains confidential business data."))
        return d

    def export(self, ext):
        if not self.result:
            self.run()
        if not self.result:
            return
        title, headers, rows, sub = self.result
        f = filedialog.asksaveasfilename(defaultextension=ext, initialfile=f"{title.replace(' ', '_').lower()}_{date.today().isoformat()}{ext}", filetypes=[(ext.upper(), f"*{ext}")])
        if f:
            self.guard(lambda: exporters.export_rows(f, title, headers, rows, subtitle=sub), done=lambda: show_info(self, "Exported", f"Saved to:\n{f}\n\nThis file contains confidential business data."))


class AuditPage(Page):
    title = "Audit Logs"
    subtitle = "Append-only record of logins, stock, money, payroll, partner and backup actions. Passwords are never logged."
    PAGE = 100

    def build(self):
        bar = self.toolbar()
        self.s = ctk.CTkEntry(bar, width=260, placeholder_text="Search action, user, record..."); self.s.pack(side="left", padx=(0, 8))
        self.s.bind("<Return>", lambda _e: self._go(0))
        self.button(bar, "Search", lambda: self._go(0), None, T.CARD_ALT)
        self.button(bar, "‹ Newer", lambda: self._go(-1), None, T.CARD_ALT)
        self.button(bar, "Older ›", lambda: self._go(1), None, T.CARD_ALT)
        self.info = ctk.CTkLabel(bar, text="", text_color=T.MUTED); self.info.pack(side="right")
        self.page = 0
        self.t = DataTable(self.body, [Col("id", "#", 55), Col("ts", "When", 130, T.when), Col("username", "User", 90), Col("action", "Action", 150), Col("entity", "Record", 90), Col("entity_id", "Id", 60),
                                       Col("reason", "Reason", 160), Col("details", "Details", 380)], page_size=self.PAGE, searchable=False)
        self.t.pack(fill="both", expand=True)

    def _go(self, d):
        self.page = 0 if d == 0 else max(0, self.page + d)
        self.refresh()

    def refresh(self):
        total, rows = q.audit(self.conn, self.s.get().strip(), self.PAGE, self.page * self.PAGE)
        if not rows and self.page > 0:
            self.page -= 1
            return self.refresh()
        self.t.set_rows(rows)
        self.info.configure(text=f"{total} entries - page {self.page + 1}")


class BackupPage(Page):
    title = "Backup & Restore"
    subtitle = "Daily automatic backups plus manual ones. Backups use SQLite's safe online backup and are verified. Nothing is uploaded anywhere."

    def build(self):
        self.status = ctk.CTkLabel(self.body, text="", justify="left", anchor="w", font=T.font(14)); self.status.pack(fill="x")
        bar = self.toolbar()
        self.button(bar, "Back up now", self.backup_now, "create_backup", T.GREEN)
        self.button(bar, "Check database health", self.check, None, T.BLUE)
        self.button(bar, "Restore from backup...", self.restore, "restore_backup", T.RED)
        self.busy = ctk.CTkLabel(bar, text="", text_color=T.AMBER); self.busy.pack(side="left", padx=10)
        ctk.CTkLabel(self.body, text=f"Database: {self.ctx.paths.db}\nBackups:  {self.ctx.paths.backups}\nRecovery copies of replaced databases: {self.ctx.paths.recovery}",
                     text_color=T.MUTED, justify="left", anchor="w").pack(fill="x", pady=4)
        self.t = DataTable(self.body, [Col("created_at", "When", 140, T.when), Col("kind", "Kind", 100), Col("status", "Result", 70), Col("size", "Size", 90, None, "e"), Col("path", "File", 420), Col("error", "Error", 200)])
        self.t.pack(fill="both", expand=True)

    def refresh(self):
        last = bk.last_successful_backup(self.conn)
        failed = self.conn.execute("SELECT COUNT(*) FROM backup_history WHERE status='failed' AND created_at > COALESCE(?, '')", (last,)).fetchone()[0]
        age = None
        if last:
            age = (datetime.now(timezone.utc) - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds() / 3600
        txt = f"Last successful backup: {T.when(last) if last else 'NEVER - please back up now'}"
        col = T.GREEN
        if last is None or (age is not None and age > 48) or failed:
            col = T.AMBER
            txt += (f"\n⚠ {failed} backup attempt(s) failed since." if failed else "") + ("\n⚠ Last good backup is more than 2 days old." if age and age > 48 else "")
        self.status.configure(text=txt, text_color=col)
        self.t.set_rows([dict(r, size=f"{(r['size_bytes'] or 0) / 1024:,.0f} KB") for r in q.backups(self.conn)])

    def backup_now(self):
        self.busy.configure(text="Backing up...")
        extra = st.get(self.conn, "backup_dir", "")

        def work():
            local = bk.create_backup(self.ctx.paths, "manual")      # a failure here is a real failure
            if not extra:
                return local, None, None
            try:
                return local, bk.create_backup(self.ctx.paths, "manual", dest_dir=extra), None
            except bk.BackupError as e:                              # the app's own copy is safe; only the extra copy failed
                return local, None, str(e)

        def done(res, err):
            self.busy.configure(text="")
            if err:
                bk.record_backup(self.conn, "manual", None, str(err), self.user)
                show_error(self, f"Backup failed: {err}")
            else:
                local, extra_info, extra_err = res
                bk.record_backup(self.conn, "manual", local, None, self.user)
                if extra_info:
                    bk.record_backup(self.conn, "manual", extra_info, None, self.user)
                if extra_err:
                    bk.record_backup(self.conn, "manual", None, extra_err, self.user, where=extra)
                    show_info(self, "Saved, but not in the extra folder",
                              f"Saved and verified:\n{local.path}\n\nThe extra copy could not be written to {extra}:\n{extra_err}\n\nCheck that the drive is plugged in, then press Back up now again.")
                else:
                    show_info(self, "Backup complete", "Saved and verified:\n" + "\n".join(str(i.path) for i in (local, extra_info) if i))
            self.refresh()
        run_bg(self, work, done)

    def check(self):
        self.busy.configure(text="Checking...")

        def done(res, err):
            self.busy.configure(text="")
            if err:
                show_error(self, f"Problem found: {err}")
            else:
                issues = acct.check_integrity(self.conn)
                show_info(self, "Database health", "SQLite integrity and foreign-key checks passed." + ("\n\nBooks consistent." if not issues else "\n\nBut:\n- " + "\n- ".join(issues)))
        run_bg(self, lambda: bk.verify_database_file(self.ctx.paths.db), done)

    def restore(self):
        r = self.t.selected()
        f = r["path"] if r and r["status"] == "ok" else filedialog.askopenfilename(initialdir=str(self.ctx.paths.backups), filetypes=[("Backups", "*.db")])
        if not f:
            return
        if not self.confirm("Restore backup", f"Restore from:\n{f}\n\nThe backup is verified first. Your current database is kept in the recovery folder. You will be signed out.", yes="Restore", danger=True):
            return
        user = self.user
        self.ctx.conn.close(); self.ctx.conn = None
        try:
            kept = bk.restore_backup(self.ctx.paths, f, confirmed=True, user=user)
        except Exception as e:  # noqa: BLE001
            self.ctx.conn = connection.connect(self.ctx.paths.db)
            show_error(self, f"Restore did not happen - your data is unchanged.\n{e}"); return
        show_info(self, "Restored", f"Previous database kept at:\n{kept}")
        self.app.logout()


class UsersPage(Page):
    title = "User Management"
    subtitle = "Three roles: owner (everything), accountant (daily work, no Dashboard / Insights / Partners / Profit / Reports / system pages), auditor (view only)."

    def build(self):
        bar = self.toolbar()
        self.button(bar, "+ Add user", self.add, "manage_users")
        self.button(bar, "Reset password", self.reset, "manage_users", T.PURPLE)
        self.button(bar, "Security question", self.recovery, None, T.PURPLE)
        self.button(bar, "Activate / deactivate", self.toggle, "manage_users", T.AMBER)
        self.t = DataTable(self.body, [Col("username", "Username", 140), Col("full_name", "Name", 180), Col("role", "Role", 160), Col("is_active", "Active", 70, lambda v: "yes" if v else "no"),
                                       Col("has_recovery", "Recovery", 90, lambda v: "set" if v else "NOT SET"), Col("created_at", "Created", 140, T.when)])
        self.t.pack(fill="both", expand=True)

    def refresh(self):
        self.t.set_rows(q.users(self.conn))

    @staticmethod
    def _question_fields():
        return [Field("sq", "Security question", "choice", [(x, x) for x in auth.SECURITY_QUESTIONS], auth.SECURITY_QUESTIONS[0],
                      hint="Used on the sign-in screen if the password is forgotten."),
                Field("sc", "...or write your own question", hint="Leave empty to use the question above.", advanced=False),
                Field("sa", "Answer", hint="Not case sensitive. Remember it.")]

    @staticmethod
    def _question_of(v):
        return (v.get("sc") or "").strip() or v["sq"]

    def add(self):
        self.form("Add user", [Field("u", "Username"), Field("n", "Full name"), Field("r", "Role", "choice", [(r.replace("_", " ").title(), r) for r in auth.ROLES], "accountant"),
                               Field("p", "Password (min 8)", "password")] + self._question_fields(),
                  lambda v: auth.create_user(self.conn, self.user, v["u"], v["p"], v["r"], v["n"],
                                             security_question=self._question_of(v), security_answer=v["sa"]))

    def recovery(self):
        """Set / change the security question. Your own: asks for your current password. Someone else's: owner only."""
        r = self.t.selected()
        if not self.need(r, "a user"):
            return
        mine = r["id"] == self.user["id"]
        if not mine and not self.ctx.can("manage_users"):
            show_error(self, "You can only change your own security question."); return
        fields = ([Field("cp", "Your current password", "password")] if mine else []) + self._question_fields()
        self.form(f"Security question - {r['username']}", fields,
                  lambda v: auth.set_security_question(self.conn, self.user, r["id"], self._question_of(v), v["sa"], v.get("cp")))

    def reset(self):
        r = self.t.selected()
        if self.need(r, "a user"):
            self.form(f"Reset password - {r['username']}", [Field("p", "New password (min 8)", "password")], lambda v: auth.change_password(self.conn, self.user, r["id"], v["p"]))

    def toggle(self):
        r = self.t.selected()
        if self.need(r, "a user"):
            self.guard(lambda: auth.set_active(self.conn, self.user, r["id"], not r["is_active"]))
