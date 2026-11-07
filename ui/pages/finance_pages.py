"""Expenses (the books) and the Assets list (records only). (Invoices and customer payments live on the Production & Invoices page.)"""
from __future__ import annotations

from datetime import date, timedelta
from tkinter import filedialog

import customtkinter as ctk

from reports import documents
from services import accounting_service as acct, asset_service as assets, queries as q
from ui import theme as T
from ui.base import Page
from ui.widgets import Col, DataTable, DateField, Field, make_button, make_tabs, show_error, show_info, surface


class ExpensesPage(Page):
    title = "Expenses"
    subtitle = "Operating and overhead costs. Machines and equipment go on the Assets tab - that list is records only and never changes cash or profit."

    def build(self):
        self.tabs = make_tabs(self.body)
        self.tabs.pack(fill="both", expand=True)
        self._build_expenses(self.tabs.add("Expenses"))
        self._build_assets(self.tabs.add("Assets (records only)"))

    # ------------------------------------------------------------------ EXPENSES (in the books)
    PERIODS = (("today", "Today"), ("week", "This week"), ("month", "This month"), ("all", "All time"))

    def _build_expenses(self, tab):
        bar = self.toolbar(tab)
        self.button(bar, "+ Record expense", self.expense, "record_expense")
        self.button(bar, "Reverse selected", self.reverse, "post_journal", T.AMBER)
        self.button(bar, "Print PDF", self.print_pdf, None)
        self.hide_rev = ctk.CTkCheckBox(bar, text="Hide reversed entries", font=T.font(13), text_color=T.MUTED, command=self._load_expenses)
        self.hide_rev.pack(side="right")
        # which days to show: one click for today / this week / this month, or pick any two dates on the calendar
        pb = ctk.CTkFrame(tab, fg_color="transparent"); pb.pack(fill="x", pady=(0, 10))
        self.period, self.pbtn = "month", {}
        for key, text in self.PERIODS:
            b = make_button(pb, text, lambda k=key: self._set_period(k), "secondary", width=104, height=36)
            b.pack(side="left", padx=(0, 6)); self.pbtn[key] = b
        ctk.CTkLabel(pb, text="or from", text_color=T.MUTED, font=T.font(13)).pack(side="left", padx=(10, 6))
        m0, m1 = self._range_for("month")
        self.d_from = DateField(pb, m0, width=150, height=36, compact=True, command=self._custom); self.d_from.pack(side="left")
        ctk.CTkLabel(pb, text="to", text_color=T.MUTED, font=T.font(13)).pack(side="left", padx=6)
        self.d_to = DateField(pb, m1, width=150, height=36, compact=True, command=self._custom); self.d_to.pack(side="left")
        # the footer is packed first, at the bottom, so the total is always on screen whatever the window height
        self.legend = ctk.CTkLabel(tab, text="", text_color=T.MUTED, font=T.font(12), anchor="w")
        self.legend.pack(side="bottom", fill="x", pady=(4, 0))
        foot = surface(tab); foot.pack(side="bottom", fill="x", pady=(8, 0))
        fi = ctk.CTkFrame(foot, fg_color="transparent"); fi.pack(fill="x", padx=18, pady=10)
        self.total_lbl = ctk.CTkLabel(fi, text="", font=T.font(18, "bold"), text_color=T.TEXT, anchor="w"); self.total_lbl.pack(side="left")
        self.total_note = ctk.CTkLabel(fi, text="", font=T.font(12), text_color=T.MUTED, anchor="e"); self.total_note.pack(side="right")
        self.t = DataTable(tab, [Col("entry_date", "Date", 95), Col("account", "Category", 230), Col("description", "Description", 200), Col("status", "Status", 90),
                                 Col("amount_paisa", "Amount", 130, T.money, "e")], page_size=40, searchable=False, height=6,
                           row_tags=lambda r: "bad" if r["reversed"] else None, empty_text="No expenses in this period.", bold=True)
        self.t.pack(fill="both", expand=True)
        self._paint_period()

    @staticmethod
    def _range_for(key: str):
        today = date.today()
        if key == "today":
            return today.isoformat(), today.isoformat()
        if key == "week":                                                     # Monday to Sunday of this week
            mon = today - timedelta(days=today.weekday())
            return mon.isoformat(), (mon + timedelta(days=6)).isoformat()
        if key == "month":
            nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
            return today.replace(day=1).isoformat(), (nxt - timedelta(days=1)).isoformat()
        return None, None

    def _set_period(self, key: str):
        self.period = key
        start, end = self._range_for(key)
        for d, v in ((self.d_from, start), (self.d_to, end)):
            d.set_date(v) if v else d.delete()
        self._paint_period()
        self._load_expenses()

    def _custom(self):
        self.period = "custom"
        self._paint_period()
        self._load_expenses()

    def _paint_period(self):
        for key, b in self.pbtn.items():
            on = key == self.period
            b.configure(fg_color=T.ACCENT_FILL if on else "transparent", text_color=("#ffffff", "#ffffff") if on else T.TEXT,
                        hover_color=T.ACCENT_HOVER if on else T.CARD_ALT, border_width=0 if on else 1)

    def _range(self):
        """(start, end) ISO dates of what is shown; (None, None) for All time."""
        if self.period == "all":
            return None, None
        return (self.d_from.get() or None), (self.d_to.get() or None)

    def _load_expenses(self):
        start, end = self._range()
        if start and end and start > end:
            self.total_lbl.configure(text="The 'from' date is after the 'to' date.", text_color=T.BAD); self.total_note.configure(text="")
            self.t.set_rows([]); self.legend.configure(text=""); return
        allrows = q.expenses(self.conn, start=start, end=end)
        rows = [dict(r, status="REVERSED" if r["reversed"] else "") for r in allrows]
        gone = sum(1 for r in rows if r["reversed"])
        if self.hide_rev.get():
            rows = [r for r in rows if not r["reversed"]]
        self.t.set_rows(rows)
        total, n = q.expense_total(self.conn, start, end)
        label = {"today": "today", "week": "this week", "month": "this month", "all": "all time"}.get(self.period) or f"{start or '...'} to {end or '...'}"
        self.total_lbl.configure(text=f"Total expenses ({label}):  {T.money(total)}", text_color=T.TEXT)
        self.total_note.configure(text=f"{n} entr{'y' if n == 1 else 'ies'}" + (f"  -  {gone} reversed not counted" if gone else ""))
        self.legend.configure(text=f"{gone} reversed entr{'y' if gone == 1 else 'ies'} shown in red - the original stays on record and its opposite entry cancels it in the books." if gone else "")

    def print_pdf(self):
        start, end = self._range()
        name = f"expenses_{start or 'all'}_{end or 'dates'}.pdf"
        f = filedialog.asksaveasfilename(defaultextension=".pdf", initialfile=name, filetypes=[("PDF", "*.pdf")])
        if f:
            self.guard(lambda: documents.expenses_pdf(self.conn, start, end, f), done=lambda: show_info(self, "Saved", f))

    def expense(self):
        sup = [("- paid in cash -", None)] + [(s["name"], s["id"]) for s in q.suppliers(self.conn)]

        def submit(v):
            acct.record_expense(self.conn, self.user, account_code=v["a"], amount=v["amt"], on=v["on"], description=v["d"], supplier_id=v["s"], on_credit=v["s"] is not None)
        self.form("Record expense", [Field("a", "Category", "choice", [(a["name"], a["code"]) for a in q.expense_accounts(self.conn)]), Field("amt", "Amount (Rs)", "money"),
                                     Field("on", "Date", "date"), Field("d", "Description / reference"), Field("s", "Supplier (if on credit)", "choice", sup)], submit,
                  intro="Electricity bills: choose 'Electricity & Utilities'. Anything that fits nowhere else: 'Other Expenses'. Salaries are booked in Payroll, not here. Machines go on the Assets tab.")

    def reverse(self):
        r = self.t.selected()
        if not self.need(r, "an entry"):
            return
        if r["reversed"]:
            show_error(self, "This entry is already reversed. A reversed entry cannot be reversed again - record a new expense instead.")
            return

        def submit(v):
            if not self.confirm("Reverse entry", f"Post an opposite entry cancelling '{r['description']}'? The original stays in the books.", danger=True):
                raise ValueError("Cancelled.")
            acct.reverse_entry(self.conn, self.user, r["id"], v["why"])
        self.form("Reverse entry", [Field("why", "Reason (required)")], submit)

    # ------------------------------------------------------------------ ASSETS (records only, not in the books)
    def _build_assets(self, tab):
        bar = self.toolbar(tab)
        self.button(bar, "+ Add asset", self.add_asset, "record_expense", T.GREEN)
        self.button(bar, "Void selected", self.void_asset, "record_expense", T.AMBER)
        ctk.CTkLabel(tab, text="Just a list of what the factory owns, with the price paid. It is NOT part of the books: adding an asset does not use cash and does not change "
                               "expenses, profit or any report.", text_color=T.MUTED, font=T.font(13), anchor="w", justify="left", wraplength=980).pack(fill="x", pady=(0, 8))
        # footer first, at the bottom, so the total is always on screen (same as the Expenses tab)
        self.a_note = ctk.CTkLabel(tab, text="", text_color=T.MUTED, font=T.font(12), anchor="w", justify="left", wraplength=980)
        self.a_note.pack(side="bottom", fill="x", pady=(4, 0))
        foot = surface(tab); foot.pack(side="bottom", fill="x", pady=(8, 0))
        fi = ctk.CTkFrame(foot, fg_color="transparent"); fi.pack(fill="x", padx=18, pady=10)
        self.a_total = ctk.CTkLabel(fi, text="", font=T.font(18, "bold"), text_color=T.TEXT, anchor="w"); self.a_total.pack(side="left")
        self.a_count = ctk.CTkLabel(fi, text="", font=T.font(12), text_color=T.MUTED, anchor="e"); self.a_count.pack(side="right")
        self.assets = DataTable(tab, [Col("on_date", "Date", 95), Col("name", "Asset", 210), Col("price_paisa", "Price", 130, T.money, "e"), Col("notes", "Notes", 170),
                                      Col("status", "Status", 140)], page_size=40, searchable=False, height=6,
                                row_tags=lambda r: "bad" if r["voided"] else ("warn" if r["kind"] == "books" else None),
                                empty_text="No assets listed yet. Use + Add asset.", bold=True)
        self.assets.pack(fill="both", expand=True)

    def _load_assets(self):
        rows = q.assets(self.conn)
        for r in rows:
            r["status"] = "Voided" if r["voided"] else ("In books (older)" if r["kind"] == "books" else "Asset")
        self.assets.set_rows(rows)
        live = [r for r in rows if not r["voided"]]
        self.a_total.configure(text=f"Total assets:  {T.money(sum(r['price_paisa'] for r in live))}")
        voided = len(rows) - len(live)
        self.a_count.configure(text=f"{len(live)} asset{'' if len(live) == 1 else 's'}" + (f"  -  {voided} voided not counted" if voided else ""))
        books = sum(1 for r in rows if r["kind"] == "books" and not r["voided"])
        self.a_note.configure(text=(f"{books} older capital purchase{'s are' if books != 1 else ' is'} in orange: they were booked before this list existed, so they did use cash. "
                                    "Void one to cancel it in the books." if books else ""))

    def add_asset(self):
        self.form("Add asset", [Field("n", "What is it?"), Field("amt", "Price (Rs)", "money"), Field("on", "Date bought", "date"), Field("nt", "Notes (optional)")],
                  lambda v: assets.add_asset(self.conn, self.user, name=v["n"], price=v["amt"], on=v["on"], notes=v["nt"]), submit_text="Add asset",
                  intro="Only a record of what the factory owns. It does not use cash and does not change expenses, profit or any report.")

    def void_asset(self):
        r = self.assets.selected()
        if not self.need(r, "an asset"):
            return
        if r["voided"]:
            show_error(self, "This one is already voided.")
            return

        def submit(v):
            if r["kind"] == "record":
                assets.void_asset(self.conn, self.user, r["id"], v["why"])
            else:
                acct.reverse_entry(self.conn, self.user, r["id"], v["why"])
        self.form(f"Void {r['name']}", [Field("why", "Reason (required)")], submit,
                  intro="The record stays on the list, marked Voided." if r["kind"] == "record" else
                        "This purchase was booked earlier. Voiding posts an opposite entry, so the cash goes back and the books are corrected.")

    def refresh(self):
        self._load_expenses()
        self._load_assets()
