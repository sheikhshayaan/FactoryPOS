"""Application shell: first run, login, sidebar navigation, session handling."""
from __future__ import annotations

import logging
import json
import logging.handlers
import threading
import time
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk

from config.paths import AppPaths, get_paths
from database import connection
from database.connection import DatabaseError
from services import auth_service as auth, backup_service as bk, payroll_service as pr, settings_service
from services import registration_service as reg
from services.startup import initialize_new_database, open_database
from ui import smooth, theme as T
from ui.base import Context
from ui.widgets import Message, confirm, make_button, restyle, run_bg, show_error, show_info

log = logging.getLogger("fms.ui")


def setup_logging(paths: AppPaths):
    paths.ensure_dirs()
    h = logging.handlers.RotatingFileHandler(paths.logs / "app.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger("fms")
    if not root.handlers:
        root.addHandler(h)
    root.setLevel(logging.INFO)          # messages carry no passwords or business data by design


def _nav():
    from ui.pages.admin_pages import AuditPage, BackupPage, ReportsPage, SettingsPage, UsersPage
    from ui.pages.cost_calculator_page import CostCalculatorPage
    from ui.pages.dashboard_page import DashboardPage
    from ui.pages.finance_pages import ExpensesPage
    from ui.pages.insights_page import InsightsPage
    from ui.pages.inventory_page import InventoryPage
    from ui.pages.labor_pages import AttendancePage
    from ui.pages.people_pages import CustomersPage, PartnersPage, ProfitPage, SuppliersPage
    from ui.pages.production_pages import JobsPage
    # (key, label, class, any-of permissions that make it visible)
    return [
        ("dashboard", "Dashboard", DashboardPage, ["view_dashboard"]),
        ("insights", "Business Insights", InsightsPage, ["view_insights"]),
        ("settings", "Factory Registration & Settings", SettingsPage, ["manage_users"]),
        ("partners", "Partners & Investments", PartnersPage, ["view_partners"]),
        ("customers", "Customers", CustomersPage, ["view_all", "manage_jobs"]),
        ("suppliers", "Suppliers", SuppliersPage, ["view_all", "manage_suppliers"]),
        ("inventory", "Inventory & Purchases", InventoryPage, ["view_all", "view_inventory"]),
        ("jobs", "Production & Invoices", JobsPage, ["view_all", "manage_jobs", "manage_invoices"]),
        ("labor", "Attendance & Salaries", AttendancePage, ["view_all", "manage_attendance", "manage_payroll"]),
        ("costing", "Cost Calculator", CostCalculatorPage, ["view_all"]),
        ("expenses", "Expenses", ExpensesPage, ["view_all"]),
        ("profit", "Profit Distribution", ProfitPage, ["view_profit"]),
        ("reports", "Reports", ReportsPage, ["view_reports"]),
        ("audit", "Audit Logs", AuditPage, ["view_audit"]),
        ("backup", "Backup & Restore", BackupPage, ["view_backup"]),
        ("users", "User Management", UsersPage, ["manage_users"]),
    ]


def _prefs_file(paths: AppPaths):
    return paths.root / "ui.json"


def load_mode(paths: AppPaths) -> str:
    try:
        m = json.loads(_prefs_file(paths).read_text(encoding="utf-8")).get("mode")
        return m if m in ("light", "dark") else "light"
    except (OSError, ValueError):
        return "light"


def save_mode(paths: AppPaths, mode: str):
    try:
        _prefs_file(paths).write_text(json.dumps({"mode": mode}), encoding="utf-8")
    except OSError:
        log.info("could not save theme preference")


smooth.install()                                   # before any widget exists


def _warm_charts():
    """Load the chart library (and its font list) in the background right after sign-in, so the first Dashboard / Insights / Cost Calculator
    visit does not wait for it (about a second). Never raises."""
    try:
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg  # noqa: F401
        from matplotlib.figure import Figure
        f = Figure(figsize=(1, 1)); f.text(0.5, 0.5, "x", fontweight="bold"); f.draw_without_rendering()
    except Exception:  # noqa: BLE001
        pass


class App(ctk.CTk):

    def report_callback_exception(self, exc, val, tb):
        """Tk calls this when a scheduled callback fails. CustomTkinter, when a window is shown or hidden, schedules 'give the focus back to the
        widget that had it'; if that widget was closed in between (a chart or a table that was redrawn), Tk says 'invalid command name'. That is harmless
        and must not show a scary traceback; every other error is reported as before."""
        if isinstance(val, tk.TclError) and str(val).startswith("invalid command name"):
            log.debug("ignored a callback for a widget that no longer exists: %s", val)
            return
        super().report_callback_exception(exc, val, tb)

    def __init__(self, paths: AppPaths | None = None):
        T.apply_ctk_theme()
        super().__init__()
        self.paths = paths or get_paths()
        ctk.set_appearance_mode(load_mode(self.paths))
        setup_logging(self.paths)
        self.ctx = Context(self.paths)
        self.title("Factory Management System")
        self._fit_to_screen()
        smooth.watch(self)
        self.configure(fg_color=T.BG)
        restyle(self)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.pages: dict[str, ctk.CTkFrame] = {}
        self.current_key: str | None = None
        self.screen: ctk.CTkFrame | None = None
        self._backup_job = None
        self.registration_ok = False                 # set by the code screen; only a NEW factory on this computer needs it
        self.start()

    def _fit_to_screen(self):
        """Open maximised where the system allows it, and never taller or wider than the screen (a 1366x768 laptop, or Windows display scaling
        of 125-150%, used to push the bottom of the window off the screen). The smallest allowed window is small enough for those screens."""
        try:
            sc = float(ctk.ScalingTracker.get_window_scaling(self)) or 1.0
        except Exception:  # noqa: BLE001
            sc = 1.0
        sw, sh = int(self.winfo_screenwidth() / sc), int(self.winfo_screenheight() / sc)
        self.minsize(min(1100, sw), min(600, max(sh - 90, 480)))
        self.geometry(f"{min(1360, sw - 20)}x{min(840, sh - 80)}+10+10")
        self.after(50, self._maximise)

    def _maximise(self):
        for how in (lambda: self.state("zoomed"), lambda: self.attributes("-zoomed", True)):      # Windows / most Linux window managers
            try:
                how()
                return
            except tk.TclError:
                continue

    # ---------------------------------------------------------------- screens
    def _swap(self, frame: ctk.CTkFrame):
        if self.screen is not None:
            self.screen.destroy()
        self.screen = frame
        frame.pack(fill="both", expand=True)

    def start(self):
        self.pages.clear(); self.current_key = None
        if not self.paths.db.exists():
            approved = not reg.required() or self.registration_ok
            self._swap(FirstRunScreen(self, self) if approved else RegisterScreen(self, self))
            return
        try:
            self.ctx.conn = open_database(self.paths)
        except Exception as e:  # noqa: BLE001
            log.error("open failed: %s", type(e).__name__)
            self._swap(RecoveryScreen(self, self, str(e)))
            return
        self._swap(LoginScreen(self, self))

    def logged_in(self, user):
        self.ctx.user = user
        self.pages.clear()
        done = self._auto_accrue(announce=False)        # new month: last month's salaries are booked BEFORE the dashboard reads the books
        self._swap(MainScreen(self, self))
        self.show(self.screen.first_key())              # the Dashboard for the owner; the first page this person may open for everyone else
        self._schedule_backup_check()
        self.screen.prewarm()
        threading.Thread(target=_warm_charts, daemon=True).start()
        if done and done["created"]:
            self.after(500, lambda: self._announce_accrual(done))

    def _auto_accrue(self, announce: bool = True):
        """From the 1st of a month, accrue the previous month's salaries (if the owner left automatic accrual on). Never raises."""
        try:
            res = pr.auto_accrue_if_due(self.ctx.conn, self.ctx.user)
        except Exception as e:  # noqa: BLE001 - payroll problems must never stop the program from opening
            log.warning("auto accrual failed: %s: %s", type(e).__name__, e)
            return None
        if res and res["created"] and announce:
            self._announce_accrual(res)
        return res

    def _announce_accrual(self, res):
        import calendar
        label = f"{calendar.month_name[int(res['period'][5:])]} {res['period'][:4]}"
        text = f"{label} salaries were accrued automatically for {len(res['created'])} employee(s), using the estimated deductions from attendance."
        if res["skipped"]:
            text += "\n\nStill to accrue by hand: " + "; ".join(f"{n} ({why})" for n, why in res["skipped"])
        text += "\n\nOpen Attendance & Salaries to review, edit or pay them."
        show_info(self, "Salaries accrued", text)

    def logout(self):
        self.ctx.user = None
        self.start()

    def show(self, key: str):
        self.screen.show(key)

    def toggle_mode(self):
        mode = "dark" if ctk.get_appearance_mode() == "Light" else "light"
        key = self.screen.key if isinstance(self.screen, MainScreen) else None
        if isinstance(self.screen, MainScreen):
            self.screen.destroy(); self.screen = None; self.pages.clear()    # BEFORE the colour switch: CustomTkinter would otherwise recolour
        ctk.set_appearance_mode(mode)                                            # every widget of every opened page (seconds) just to throw them away
        save_mode(self.paths, mode)
        restyle(self)
        if self.ctx.user is not None and self.screen is None:
            self._swap(MainScreen(self, self))
            self.show(key or self.screen.first_key())

    def refresh_current(self):
        if isinstance(self.screen, MainScreen):
            self.screen.refresh_current()

    def _schedule_backup_check(self):
        """Daily automatic backup: checked at login and every 30 minutes; the copy runs on a worker thread."""
        if self._backup_job:
            self.after_cancel(self._backup_job)                  # never two checking loops at once (sign out / sign in used to stack them up)
            self._backup_job = None

        def check():
            if self.ctx.user is None or self.ctx.conn is None:
                return
            self._auto_accrue(announce=True)
            conn = self.ctx.conn
            due = bk.auto_backup_due(conn, settings_service.get(conn, "backup_dir", ""))
            if due["local"] or due["extra"]:
                def done(res, err):
                    if self.ctx.conn is None or res is None:
                        return
                    keep = int(settings_service.get(self.ctx.conn, "backup_retention", "30") or 30)
                    bk.record_auto_result(self.ctx.conn, res, keep, self.paths)
                    self.refresh_current()
                run_bg(self, lambda: bk.run_auto_backup(self.paths, due), done)
            self._backup_job = self.after(30 * 60 * 1000, check)
        self._backup_job = self.after(60_000, check)

    def on_close(self):
        try:
            if self._backup_job:
                self.after_cancel(self._backup_job)
            if self.ctx.conn:
                self.ctx.conn.close()
        finally:
            self.destroy()


# ====================================================================== screens
class _Center(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color=T.BG)
        self.app = app
        self.box = ctk.CTkFrame(self, fg_color=T.CARD, corner_radius=20, border_width=1, border_color=T.BORDER, width=460)
        self.box.place(relx=0.5, rely=0.5, anchor="center")
        self.pad = ctk.CTkFrame(self.box, fg_color="transparent")
        self.pad.pack(padx=40, pady=34)

    def entry(self, label, show=None, default="", parent=None, gap=14):
        parent = parent or self.pad
        ctk.CTkLabel(parent, text=label, text_color=T.MUTED, font=T.font(13), anchor="w").pack(fill="x", pady=(gap, 4))
        e = ctk.CTkEntry(parent, width=380, height=42, show=show)
        e.pack()
        if default:
            e.insert(0, default)
        return e

    def security_block(self, parent, gap=14):
        """Question picker + answer. Returns a function giving (question, answer) as typed."""
        ctk.CTkLabel(parent, text="Security question (for a forgotten password)", text_color=T.MUTED, font=T.font(13),
                     anchor="w").pack(fill="x", pady=(gap, 4))
        self.sq_combo = ctk.CTkComboBox(parent, width=380, height=42, state="readonly",
                                        values=list(auth.SECURITY_QUESTIONS) + [auth.CUSTOM_QUESTION], command=self._sq_changed)
        self.sq_combo.set(auth.SECURITY_QUESTIONS[0])
        self.sq_combo.pack()
        self.sq_custom = ctk.CTkEntry(parent, width=380, height=42, placeholder_text="Type your own question")
        self.sq_answer = self.entry("Answer (remember it - it is not case sensitive)", parent=parent, gap=10)

        def read():
            q = self.sq_combo.get()
            if q == auth.CUSTOM_QUESTION:
                q = self.sq_custom.get()
            return q, self.sq_answer.get()
        return read

    def _sq_changed(self, value):
        if value == auth.CUSTOM_QUESTION:
            self.sq_custom.pack(after=self.sq_combo, pady=(8, 0)); self.sq_custom.focus_set()
        else:
            self.sq_custom.pack_forget()


class LoginScreen(_Center):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        name = settings_service.get(app.ctx.conn, "factory_name", "") or "Factory Management System"
        ctk.CTkLabel(self.pad, text=name, font=T.font(26, "bold"), text_color=T.TEXT).pack()
        ctk.CTkLabel(self.pad, text="Sign in to continue", text_color=T.MUTED, font=T.font(14)).pack(pady=(2, 4))
        self.u = self.entry("Username")
        self.p = self.entry("Password", show="•")
        self.err = ctk.CTkLabel(self.pad, text="", text_color=T.BAD, wraplength=380, font=T.font(13))
        self.err.pack(pady=(10, 0))
        self.btn = make_button(self.pad, "Sign in", self.go, "primary", width=380, height=44)
        self.btn.pack(pady=(10, 0))
        make_button(self.pad, "Forgot password?", lambda: app._swap(ForgotPasswordScreen(app, app, self.u.get())), "ghost",
                    width=380, height=34).pack(pady=(6, 0))
        self.u.focus_set()
        self.p.bind("<Return>", lambda _e: self.go())
        self.u.bind("<Return>", lambda _e: self.p.focus_set())

    def go(self):
        try:
            user = auth.authenticate(self.app.ctx.conn, self.u.get(), self.p.get())
        except auth.AuthError as e:
            self.err.configure(text=str(e)); self.p.delete(0, "end"); return
        except DatabaseError as e:
            self.err.configure(text=str(e)); return
        self.app.logged_in(user)


class RegisterScreen(_Center):
    """Step 1 of registering a NEW factory on a computer: a 6-digit code is e-mailed to the software owner, who gives it to you."""
    def __init__(self, parent, app):
        super().__init__(parent, app)
        self.challenge: reg.Challenge | None = None
        self.sending = False
        ctk.CTkLabel(self.pad, text="Register this software", font=T.font(24, "bold"), text_color=T.TEXT).pack()
        ctk.CTkLabel(self.pad, text="Press Send code. A 6-digit code goes to the person who gave you the software.\nCall him, get the code and type it below.",
                     text_color=T.MUTED, font=T.font(13), justify="center").pack(pady=(2, 8))
        self.who = self.entry("Your name and business", gap=10)
        self.send_btn = make_button(self.pad, "Send code", self.send, "secondary", width=380, height=42)
        self.send_btn.pack(pady=(12, 0))
        self.code = self.entry("6-digit code", gap=14)
        self.msg = ctk.CTkLabel(self.pad, text="", text_color=T.BAD, wraplength=380, font=T.font(13))
        self.msg.pack(pady=(10, 0))
        self.btn = make_button(self.pad, "Continue", self.go, "primary", width=380, height=44)
        self.btn.pack(pady=(10, 0))
        ctk.CTkLabel(self.pad, text="Needs the internet for this step only. What is sent: the name you type above, the code and an anonymous "
                     "computer code. The program itself works fully offline.", text_color=T.MUTED, font=T.font(12), wraplength=380,
                     justify="left").pack(pady=(10, 0), anchor="w")
        self.who.focus_set()
        self.code.bind("<Return>", lambda _e: self.go())

    def _say(self, text, bad=True):
        self.msg.configure(text=text, text_color=T.BAD if bad else T.GREEN)

    def send(self):
        if self.sending:
            return
        if not self.who.get().strip():
            self._say("Type your name and business first."); return
        if self.challenge and time.time() - self.challenge.created < reg.RESEND_AFTER_S:
            self._say(f"A code was just sent. Wait {reg.RESEND_AFTER_S} seconds before asking for another one."); return
        code = reg.new_code()
        self.sending = True
        self.send_btn.configure(state="disabled", text="Sending...")
        self._say("")

        def done(_res, err):
            self.sending = False
            if not self.winfo_exists():
                return
            self.send_btn.configure(state="normal", text="Send code again")
            if err:
                self._say(str(err) if isinstance(err, reg.RegistrationError) else "Could not send the code. Try again.")
                return
            self.challenge = reg.Challenge(code)
            self._say("Code sent. Ask the software owner for it and type it below. It is valid for 10 minutes.", bad=False)
            self.code.focus_set()
        who = self.who.get()                                 # read the box here: widgets must not be touched from the worker thread
        run_bg(self, lambda: reg.request_code(who, code), done)

    def go(self):
        if self.challenge is None:
            self._say("Press Send code first."); return
        try:
            self.challenge.check(self.code.get())
        except reg.RegistrationError as e:
            self.code.delete(0, "end"); self._say(str(e)); return
        self.app.registration_ok = True
        self.app.start()


class FirstRunScreen(_Center):
    def __init__(self, parent, app):
        super().__init__(parent, app)
        ctk.CTkLabel(self.pad, text="Set up your factory", font=T.font(24, "bold"), text_color=T.TEXT).pack()
        ctk.CTkLabel(self.pad, text="Creates an empty database on this computer.\nNo sample data is added.", text_color=T.MUTED, font=T.font(13)).pack(pady=(2, 0))
        cols = ctk.CTkFrame(self.pad, fg_color="transparent")
        cols.pack(pady=(0, 0))
        left = ctk.CTkFrame(cols, fg_color="transparent"); left.pack(side="left", padx=(0, 22), anchor="n")
        right = ctk.CTkFrame(cols, fg_color="transparent"); right.pack(side="left", padx=(22, 0), anchor="n")
        self.fn = self.entry("Factory name", parent=left, gap=10)
        self.un = self.entry("Owner username", parent=left, gap=10)
        self.full = self.entry("Owner full name", parent=left, gap=10)
        self.pw = self.entry("Password (min 8 characters)", show="•", parent=left, gap=10)
        self.pw2 = self.entry("Repeat password", show="•", parent=left, gap=10)
        self.read_security = self.security_block(right, gap=10)
        ctk.CTkLabel(right, text="If the password is ever forgotten, this question lets you set a new one from the sign-in screen.",
                     text_color=T.MUTED, font=T.font(12), wraplength=380, justify="left").pack(pady=(10, 0), anchor="w")
        ctk.CTkLabel(right, text=f"Data folder: {app.paths.root}", text_color=T.MUTED, font=T.font(12), wraplength=380, justify="left").pack(pady=(8, 0), anchor="w")
        ctk.CTkLabel(right, text="Tip: also enable BitLocker on this PC - the database file is not encrypted.", text_color=T.WARN,
                     font=T.font(12), wraplength=380, justify="left").pack(anchor="w")
        self.err = ctk.CTkLabel(self.pad, text="", text_color=T.BAD, wraplength=860, font=T.font(13))
        self.err.pack(pady=(8, 0))
        make_button(self.pad, "Create factory", self.go, "primary", width=380, height=44).pack(pady=(8, 0))

    def go(self):
        if self.pw.get() != self.pw2.get():
            self.err.configure(text="Passwords do not match."); return
        if not self.fn.get().strip() or not self.un.get().strip():
            self.err.configure(text="Factory name and username are required."); return
        if reg.required() and not self.app.registration_ok:
            self.err.configure(text="This computer is not registered yet."); return
        sq, sa = self.read_security()
        try:
            auth._validate_security(sq, sa)                  # check BEFORE the database file is created
        except auth.AuthError as e:
            self.err.configure(text=str(e)); return
        try:
            conn = initialize_new_database(self.app.paths)
            try:
                auth.create_user(conn, None, self.un.get(), self.pw.get(), "owner", self.full.get(),
                                 security_question=sq, security_answer=sa)
            except Exception:
                conn.close()                                   # roll back the half-made setup so first run can be retried
                for ext in ("", "-wal", "-shm"):
                    (self.app.paths.db.parent / (self.app.paths.db.name + ext)).unlink(missing_ok=True)
                raise
            owner = auth.authenticate(conn, self.un.get(), self.pw.get())
            settings_service.set_values(conn, owner, {"factory_name": self.fn.get().strip()})
            conn.close()
        except Exception as e:  # noqa: BLE001
            self.err.configure(text=str(e)); return
        self.app.start()


class ForgotPasswordScreen(_Center):
    """Step 1: username -> its security question.  Step 2: answer + new password."""
    def __init__(self, parent, app, username=""):
        super().__init__(parent, app)
        ctk.CTkLabel(self.pad, text="Reset your password", font=T.font(24, "bold"), text_color=T.TEXT).pack()
        ctk.CTkLabel(self.pad, text="Answer your security question to choose a new password.", text_color=T.MUTED, font=T.font(13)).pack(pady=(2, 4))
        self.u = self.entry("Username", default=username)
        self.step2 = ctk.CTkFrame(self.pad, fg_color="transparent")
        self.q_label = ctk.CTkLabel(self.step2, text="", font=T.font(15, "bold"), text_color=T.TEXT, wraplength=380, justify="left", anchor="w")
        self.q_label.pack(fill="x", pady=(16, 0))
        self.a = self.entry("Your answer", parent=self.step2, gap=8)
        self.p1 = self.entry("New password (min 8 characters)", show="•", parent=self.step2, gap=10)
        self.p2 = self.entry("Repeat new password", show="•", parent=self.step2, gap=10)
        self.err = ctk.CTkLabel(self.pad, text="", text_color=T.BAD, wraplength=380, font=T.font(13))
        self.err.pack(pady=(10, 0))
        self.btn = make_button(self.pad, "Show my question", self.show_question, "primary", width=380, height=44)
        self.btn.pack(pady=(8, 0))
        make_button(self.pad, "Back to sign in", app.start, "ghost", width=380, height=34).pack(pady=(6, 0))
        self.u.focus_set()
        self.u.bind("<Return>", lambda _e: self.show_question() if not self.step2.winfo_ismapped() else self.a.focus_set())

    def show_question(self):
        try:
            q = auth.security_question_for(self.app.ctx.conn, self.u.get())
        except auth.AuthError as e:
            self.err.configure(text=str(e)); return
        self.err.configure(text="")
        self.q_label.configure(text=q)
        self.u.configure(state="disabled")
        self.step2.pack(before=self.err)
        self.btn.configure(text="Reset password", command=self.reset)
        self.a.focus_set()

    def reset(self):
        if self.p1.get() != self.p2.get():
            self.err.configure(text="The two new passwords do not match."); return
        try:
            auth.reset_password_with_answer(self.app.ctx.conn, self.u.get(), self.a.get(), self.p1.get())
        except (auth.AuthError, DatabaseError) as e:
            self.err.configure(text=str(e)); self.a.delete(0, "end"); return
        show_info(self, "Password changed", "Your password has been changed. Sign in with the new password.")
        self.app.start()


class RecoveryScreen(_Center):
    def __init__(self, parent, app, error: str):
        super().__init__(parent, app)
        ctk.CTkLabel(self.pad, text="The database could not be opened", font=T.font(20, "bold"), text_color=T.BAD).pack()
        ctk.CTkLabel(self.pad, text=error, wraplength=400, text_color=T.TEXT, justify="left").pack(pady=8)
        ctk.CTkLabel(self.pad, text="Nothing has been changed or deleted. Your damaged file is kept as-is.\n"
                                    f"Backups are in:\n{app.paths.backups}", text_color=T.MUTED, wraplength=400, justify="left").pack()
        make_button(self.pad, "Restore from a backup file...", self.restore, "primary", width=380).pack(pady=(14, 0))
        make_button(self.pad, "Try again", app.start, "secondary", width=380).pack(pady=8)

    def restore(self):
        f = filedialog.askopenfilename(initialdir=str(self.app.paths.backups), filetypes=[("Backups", "*.db")])
        if not f:
            return
        if not confirm(self, "Restore backup", "The current database will be preserved in the recovery folder, then replaced by this verified backup.", danger=True):
            return
        try:
            bk.restore_backup(self.app.paths, f, confirmed=True)
        except Exception as e:  # noqa: BLE001
            show_error(self, str(e)); return
        self.app.start()


GROUPS = [("", ["dashboard", "insights"]), ("Production", ["jobs", "inventory", "labor", "costing"]), ("People", ["customers", "suppliers", "partners"]),
          ("Money", ["expenses", "profit", "reports"]), ("System", ["settings", "users", "backup", "audit"])]
SHORT = {"insights": "Insights", "settings": "Factory & Settings", "partners": "Partners", "inventory": "Inventory", "jobs": "Production & Invoices", "labor": "Attendance & Salaries",
         "profit": "Profit Distribution", "backup": "Backup & Restore", "audit": "Audit Log", "users": "Users"}


class MainScreen(ctk.CTkFrame):
    def __init__(self, parent, app):
        super().__init__(parent, fg_color=T.BG)
        self.app = app
        self.nav = [n for n in _nav() if n[3] is None or any(app.ctx.can(p) for p in n[3])]
        side = ctk.CTkFrame(self, width=250, fg_color=T.PANEL, corner_radius=0, border_width=0)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        name = settings_service.get(app.ctx.conn, "factory_name", "") or "Factory"
        ctk.CTkLabel(side, text=name, font=T.font(18, "bold"), text_color=T.TEXT, anchor="w", wraplength=210, justify="left").pack(fill="x", padx=22, pady=(26, 0))
        ctk.CTkLabel(side, text="Management System", font=T.font(12), text_color=T.MUTED, anchor="w").pack(fill="x", padx=22, pady=(0, 10))
        scroll = ctk.CTkScrollableFrame(side, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=(8, 0))
        self.buttons: dict[str, ctk.CTkButton] = {}
        have = {n[0]: n for n in self.nav}
        for group, keys in GROUPS:
            keys = [k for k in keys if k in have]
            if not keys:
                continue
            if group:
                ctk.CTkLabel(scroll, text=group, font=T.font(12, "bold"), text_color=T.MUTED, anchor="w").pack(fill="x", padx=14, pady=(16, 4))
            for key in keys:
                label = SHORT.get(key, have[key][1])
                b = ctk.CTkButton(scroll, text=label, anchor="w", fg_color="transparent", hover_color=T.CARD_ALT, text_color=T.TEXT,
                                  font=T.font(14), command=lambda k=key: self.show(k), height=40, corner_radius=10)
                b.pack(fill="x", padx=6, pady=1)
                self.buttons[key] = b
        foot = ctk.CTkFrame(side, fg_color="transparent")
        foot.pack(fill="x", padx=18, pady=(8, 18))
        u = app.ctx.user
        ctk.CTkLabel(foot, text=u["full_name"] or u["username"], font=T.font(14, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(foot, text=u["role"].replace("_", " ").capitalize(), font=T.font(12), text_color=T.MUTED, anchor="w").pack(fill="x")
        row = ctk.CTkFrame(foot, fg_color="transparent")
        row.pack(fill="x", pady=(10, 0))
        make_button(row, "Sign out", app.logout, "secondary", width=110, height=34).pack(side="left")
        make_button(row, "◐  Theme", app.toggle_mode, "ghost", width=90, height=34).pack(side="left", padx=6)
        self.content = ctk.CTkFrame(self, fg_color="transparent")
        self.content.pack(side="left", fill="both", expand=True)
        self.side, self._side_w = side, 250
        self.bind("<Configure>", lambda e: self._narrow_side() if e.widget is self else None, add="+")
        self.page_cache: dict[str, ctk.CTkFrame] = {}
        self.key = None

    def _narrow_side(self):
        """On a small window the menu gets a little narrower, so the page beside it keeps its room."""
        try:
            logical = self.winfo_width() / float(ctk.ScalingTracker.get_widget_scaling(self))
        except Exception:  # noqa: BLE001
            return
        want = 250 if logical >= 1250 else 210
        if want != self._side_w:
            self._side_w = want
            self.side.configure(width=want)

    PREWARM = True                                   # the tests switch this off
    PREWARM_ORDER = ["jobs", "inventory", "labor", "expenses", "customers", "suppliers", "costing", "partners", "profit", "reports", "audit", "backup",
                     "users", "settings"]            # likely first: day-to-day pages. Dashboard is already open; Insights draws charts that depend on the width, so it is not pre-built

    def prewarm(self):
        """Build the pages the person has not opened yet, one every moment the program is idle (a few tenths of a second each), so the first click
        on each page is instant instead of waiting for it to be built. Stops by itself on sign-out."""
        if not self.PREWARM:
            return
        have = {k for k, *_ in self.nav}

        def step():
            try:
                if not self.winfo_exists() or self.app.screen is not self:
                    return
            except tk.TclError:
                return
            todo = [k for k in self.PREWARM_ORDER if k in have and k not in self.page_cache]
            if not todo:
                return
            cls = {k: c for k, _l, c, _p in self.nav}[todo[0]]
            try:
                self.page_cache[todo[0]] = cls(self.content, self.app)            # built, not shown: show() packs it and refreshes the data
            except Exception as e:  # noqa: BLE001 - a page that fails here fails (visibly) when it is opened
                log.warning("pre-building %s failed: %s", todo[0], type(e).__name__)
                self.page_cache.pop(todo[0], None)
                have.discard(todo[0])
            self.after(350, step)
        self.after(1800, step)

    def first_key(self) -> str | None:
        have = {n[0] for n in self.nav}
        return next((k for _g, keys in GROUPS for k in keys if k in have), None)

    def show(self, key: str):
        cls = {k: c for k, _l, c, _p in self.nav}.get(key)
        if cls is None:
            return
        if self.key and self.key in self.page_cache:
            self.page_cache[self.key].pack_forget()
        if key not in self.page_cache:
            self.page_cache[key] = cls(self.content, self.app)
        self.key = key
        self.page_cache[key].pack(fill="both", expand=True)
        hl = key
        for k, b in self.buttons.items():
            b.configure(fg_color=T.ACCENT_SOFT if k == hl else "transparent", text_color=T.ACCENT if k == hl else T.TEXT)
        self.page_cache[key].refresh()

    def refresh_current(self):
        if self.key and self.key in self.page_cache:
            self.page_cache[self.key].refresh()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
