"""Customers, Suppliers, Partners & Investments, Profit Distribution."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

import customtkinter as ctk

from services import accounting_service as acct, inventory_service as inv, profit_service as ps, queries as q
from ui import theme as T
from ui.base import Page
from services.units import from_paisa, to_paisa
from ui.widgets import Col, DataTable, DateField, Field, RowList, Stat, _Modal, make_button, show_info, surface


class CustomersPage(Page):
    title = "Customers"
    subtitle = "Owes = invoiced and not yet paid. Advance = money received before an invoice; it is used automatically on the customer's next invoice."

    def build(self):
        bar = self.toolbar()
        self.button(bar, "+ Add customer", self.add, "manage_jobs", T.GREEN)
        self.button(bar, "Edit", self.edit, "manage_jobs")
        self.button(bar, "Receive advance", self.advance, "record_payment")
        self.button(bar, "Stock given", self.stock, None)
        self.button(bar, "Delete", self.delete, "manage_jobs", T.RED)
        self.t = DataTable(self.body, [Col("name", "Customer", 150), Col("phone", "Phone", 95), Col("entries", "Entries", 80, None, "w"),
                                       Col("stock", "Stock left (his own)", 250), Col("billed", "Billed", 100, T.money, "e"), Col("outstanding", "Owes", 100, T.money, "e"),
                                       Col("advance", "Advance", 100, T.money, "e")],
                           row_tags=lambda r: "warn" if r["outstanding"] > 0 else None, empty_text="No customers yet. Add the first one with + Add customer.", bold=True)
        self.t.pack(fill="both", expand=True)

    @staticmethod
    def _stock_text(items, show=2) -> str:
        """Short list of what is LEFT of the stock this customer gave ('Kapra 200 m; Paper 8 rolls'); more than `show` materials end in '+N more'.
        The full figures (given, used, left) are in the 'Stock given' window."""
        items = [i for i in (items or []) if i["left_milli"] > 0]
        if not items:
            return "-"
        parts = [f"{i['material']} {T.qty(i['left_milli'], i['base_unit'])}" for i in items[:show]]
        if len(items) > show:
            parts.append(f"+{len(items) - show} more")
        return ";  ".join(parts)

    def refresh(self):
        by: dict[int, list] = {}
        for it in q.customer_stock(self.conn):
            by.setdefault(it["customer_id"], []).append(it)
        self.t.set_rows([dict(r, stock=self._stock_text(by.get(r["id"]))) for r in q.customers(self.conn)])

    def stock(self):
        c = self._cust()
        if c:
            CustomerStockDialog(self, self.conn, c)

    def _cust(self):
        r = self.t.selected()
        return r if self.need(r, "a customer") else None

    def add(self):
        self.form("Add customer", [Field("name", "Name"), Field("phone", "Phone"), Field("address", "Address")],
                  lambda v: inv.add_customer(self.conn, self.user, v["name"], v["phone"], v["address"]), submit_text="Add customer")

    def edit(self):
        c = self._cust()
        if not c:
            return
        self.form(f"Edit {c['name']}", [Field("name", "Name", default=c["name"]), Field("phone", "Phone", default=c["phone"] or ""), Field("address", "Address", default=c["address"] or "")],
                  lambda v: inv.update_customer(self.conn, self.user, c["id"], name=v["name"], phone=v["phone"], address=v["address"]), submit_text="Save changes")

    def advance(self):
        c = self._cust()
        if not c:
            return
        self.form(f"Advance from {c['name']}", [Field("a", "Amount (Rs)", "money"), Field("on", "Date received", "date"),
                                                Field("m", "How was it paid?", "choice", [("Cash", "cash"), ("Bank transfer", "bank"), ("Cheque", "cheque")]), Field("ref", "Reference")],
                  lambda v: acct.receive_payment(self.conn, self.user, customer_id=c["id"], amount=v["a"], paid_on=v["on"], method=v["m"], reference=v["ref"]),
                  submit_text="Record advance", intro="Held for this customer and used automatically when his next invoice is created. It is not counted as sales until then.")

    def delete(self):
        c = self._cust()
        if not c:
            return
        if not self.confirm("Delete customer", f"Delete {c['name']}?\n\nA customer with past entries or invoices is hidden, not erased, so the records stay correct. "
                            "A customer who still owes money, or whose advance has not been used, cannot be deleted.", yes="Delete", danger=True):
            return
        if self.guard(lambda: inv.delete_customer(self.conn, self.user, c["id"])) == "archived":
            show_info(self, "Customer hidden", f"{c['name']} has history, so he was hidden from the list instead of erased. Adding the same name again brings him back.")


class CustomerStockDialog(_Modal):
    """Everything this customer gave the factory (his own fabric etc.): how much came in, how much is still here, receipt by receipt."""

    def __init__(self, parent, conn, customer):
        super().__init__(parent, f"Stock given by {customer['name']}", 760)
        pad = ctk.CTkFrame(self, fg_color="transparent"); pad.pack(fill="both", expand=True, padx=26, pady=22)
        ctk.CTkLabel(pad, text=f"Stock given by {customer['name']}", font=T.font(21, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(pad, text="This is the customer's own material. It is not the factory's stock, has no cost, and is only used on his entries.",
                     font=T.font(13), text_color=T.MUTED, anchor="w", wraplength=680, justify="left").pack(fill="x", pady=(2, 12))
        tot = q.customer_stock(conn, customer["id"])
        if tot:
            row = ctk.CTkFrame(pad, fg_color="transparent"); row.pack(fill="x", pady=(0, 12))
            PER_ROW = 3                                                                 # more materials wrap onto the next row instead of running out of the window
            for c in range(PER_ROW):
                row.grid_columnconfigure(c, weight=1, uniform="t")
            for i, it in enumerate(tot):
                box = surface(row); box.grid(row=i // PER_ROW, column=i % PER_ROW, sticky="nsew", padx=4, pady=4)
                u, rl = it["base_unit"], it["roll_length_milli"]
                st = Stat(box, it["material"], T.qty_full(it["left_milli"], u, rl)); st.pack(fill="x", padx=14, pady=(10, 2))
                st.value.configure(wraplength=200, justify="left")                      # a long figure wraps inside its box
                ctk.CTkLabel(box, text=f"left  -  {T.qty_full(it['received_milli'], u, rl)} given in total", font=T.font(12), text_color=T.MUTED,
                             anchor="w", justify="left", wraplength=190).pack(fill="x", padx=18, pady=(0, 10))
        lots = [dict(l, rec=T.qty_full(l["qty_received_milli"], l["base_unit"], l["roll_length_milli"]), left=T.qty_full(l["qty_remaining_milli"], l["base_unit"], l["roll_length_milli"]),
                     note="Receipt undone" if l["undone"] else (l["reference"] or "")) for l in q.customer_lots(conn, customer["id"])]
        t = DataTable(pad, [Col("received_on", "Date", 100), Col("material", "Material", 200), Col("rec", "Given", 130, None, "e"), Col("left", "Left", 130, None, "e"),
                            Col("note", "Note", 160)], searchable=False, height=min(10, max(3, len(lots) + 1)),
                      empty_text="This customer has not given any stock yet. Receive it on the Inventory page (\"Customer's own\").",
                      row_tags=lambda r: "muted" if r["undone"] else None)
        t.pack(fill="x"); t.set_rows(lots)
        self.default_btn = make_button(pad, "Close", self.destroy, "primary", width=110); self.default_btn.pack(anchor="e", pady=(16, 0))
        self.present(True)


class SuppliersPage(Page):
    title = "Suppliers"
    subtitle = "We owe = what the factory still owes the supplier. A minus figure (for example -5,000) means the supplier is holding that much of ours - an advance."

    def build(self):
        bar = self.toolbar()
        self.button(bar, "+ Add supplier", self.add, "manage_suppliers")
        self.button(bar, "Pay supplier", self.pay, "record_payment", T.GREEN)
        self.button(bar, "Receive advance back", self.refund, "record_payment", T.AMBER)
        self.t = DataTable(self.body, [Col("name", "Supplier", 170), Col("phone", "Phone", 110), Col("address", "Address", 120),
                                       Col("owed", "We owe (minus = advance)", 190, T.money, "e")],
                           row_tags=lambda r: "good" if r["owed"] < 0 else None, bold=True)
        self.note = ctk.CTkLabel(self.body, text="", text_color=T.AMBER, font=T.font(12), anchor="w", justify="left", wraplength=980)
        self.note.pack(side="bottom", fill="x", pady=(4, 0))
        self.t.pack(fill="both", expand=True)

    def refresh(self):
        rows = q.suppliers(self.conn)
        self.t.set_rows(rows)
        # the Dashboard shows the whole Payables account; if some of it is not tied to a supplier it would be missing above, so say so
        gap = acct.balance(self.conn, "2000") - sum(r["owed"] for r in rows)
        self.note.configure(text=(f"Note: {T.money(gap)} of the factory's supplier balance (shown on the Dashboard) is not tied to any supplier in this list - "
                                  "for example stock bought or returned without choosing a supplier, or a supplier that was switched off.") if gap else "")

    def add(self):
        self.form("Add supplier", [Field("name", "Name"), Field("phone", "Phone"), Field("address", "Address")],
                  lambda v: inv.add_supplier(self.conn, self.user, v["name"], v["phone"], v["address"]))

    def pay(self):
        r = self.t.selected()
        if not self.need(r, "a supplier"):
            return
        if r["owed"] <= 0:
            show_info(self, "Nothing to pay", f"{r['name']} is not owed anything" + (f" - he is holding {T.money(-r['owed'])} of ours. Use Receive advance back to get it, "
                                                                                      "or buy stock from him to use it up." if r["owed"] < 0 else "."))
            return
        self.form(f"Pay {r['name']}", [Field("amount", "Amount (Rs)", "money", hint=f"Owed: {T.money(r['owed'])}"), Field("on", "Payment date", "date"), Field("ref", "Reference / cheque no.")],
                  lambda v: acct.pay_supplier(self.conn, self.user, supplier_id=r["id"], amount=v["amount"], paid_on=v["on"], reference=v["ref"]),
                  submit_text="Record payment")

    def refund(self):
        r = self.t.selected()
        if not self.need(r, "a supplier"):
            return
        if r["owed"] >= 0:
            show_info(self, "No advance to receive", f"{r['name']} is not holding any advance of ours" + (f" - the factory owes him {T.money(r['owed'])}." if r["owed"] else "."))
            return
        held = -r["owed"]
        self.form(f"Receive advance back from {r['name']}", [Field("amount", "Amount received (Rs)", "money", default=str(from_paisa(held)),
                                                                  hint=f"He is holding {T.money(held)} of ours. Partial amounts are fine."),
                                                           Field("on", "Date received", "date"), Field("ref", "Reference / cheque no.")],
                  lambda v: acct.receive_supplier_refund(self.conn, self.user, supplier_id=r["id"], amount=v["amount"], received_on=v["on"], reference=v["ref"]),
                  submit_text="Record money received",
                  intro="Cash comes in and his balance moves toward zero. This is not income, so profit does not change. "
                        "Another way to use the advance up: buy stock from him on credit on the Inventory page.")


def _chip(parent, text: str, kind: str):
    strong, soft = T.KINDS[kind]
    return ctk.CTkLabel(parent, text=f"  {text}  ", text_color=strong, fg_color=soft, corner_radius=8, height=26, font=T.font(12, "bold"))


def _clickable(widget, fn):
    """Make a whole row (the frame and everything in it) react to a click."""
    widget.bind("<Button-1>", lambda _e: fn())
    for c in widget.winfo_children():
        _clickable(c, fn)


def _cell(parent, text, *, bold=False, muted=False, anchor="w", size=15):
    return ctk.CTkLabel(parent, text=text, anchor=anchor, font=T.font(size, "bold" if bold else "normal"), text_color=T.MUTED if muted else T.TEXT)


class PartnersPage(Page):
    title = "Partners & Investments"
    subtitle = "Ownership % comes from capital contributed less withdrawals."

    def build(self):
        bar = self.toolbar()
        self.button(bar, "+ Add partner", self.add, "change_ownership")
        self.button(bar, "Record capital contribution", self.capital, "record_capital", T.GREEN)
        self.button(bar, "Record withdrawal", self.draw, "change_ownership", T.AMBER)
        # one calm panel: how profit is shared, with the two ways one click apart
        box = surface(self.body); box.pack(fill="x", pady=(0, 14))
        inner = ctk.CTkFrame(box, fg_color="transparent"); inner.pack(fill="x", padx=20, pady=14)
        inner.grid_columnconfigure(0, weight=1)
        left = ctk.CTkFrame(inner, fg_color="transparent"); left.grid(row=0, column=0, sticky="w")
        self.mode_lbl = ctk.CTkLabel(left, text="", font=T.font(17, "bold"), text_color=T.TEXT, anchor="w"); self.mode_lbl.pack(fill="x")
        self.mode_note = ctk.CTkLabel(left, text="", font=T.font(13), text_color=T.MUTED, anchor="w", justify="left", wraplength=520); self.mode_note.pack(fill="x", pady=(3, 0))
        btns = ctk.CTkFrame(inner, fg_color="transparent"); btns.grid(row=0, column=1, sticky="e", padx=(16, 0))
        self.auto_btn = make_button(btns, "Automatic", self.use_auto, "secondary", width=120); self.auto_btn.pack(side="left", padx=(0, 8))
        self.manual_btn = make_button(btns, "Manual", self.shares, "secondary", width=120); self.manual_btn.pack(side="left")
        self.mode_allowed = self.ctx.can("change_ownership")
        self.t = RowList(self.body, [("Partner", 0, "w"), ("Capital", 150, "e"), ("Ownership", 110, "e"), ("Profit share", 120, "e")],
                         empty_text="No partners yet. Use + Add partner.", header_in_scroll=True)
        self.t.pack(fill="both", expand=True)
        self.sel_id = None                                                          # the partner clicked in the list
        self.total = ctk.CTkLabel(self.body, text="", text_color=T.MUTED, font=T.font(13), anchor="w")
        self.total.pack(fill="x", pady=(8, 0))

    def refresh(self):
        rows = q.partners(self.conn)
        if self.sel_id not in {r["id"] for r in rows}:
            self.sel_id = None
        self._draw_partners(rows)
        cap = sum(r["capital_paisa"] for r in rows if r["capital_paisa"] > 0)
        self.total.configure(text=f"{len(rows)} partner{'s' if len(rows) != 1 else ''}  -  total capital {T.money(cap)}  -  capital movements are equity, never sales or expenses")
        auto = ps.share_mode(self.conn) == "ownership"
        self.mode_lbl.configure(text="Profit sharing: Automatic (equal to ownership %)" if auto else "Profit sharing: Manual")
        self.mode_note.configure(text=("Each partner's share follows his capital and changes by itself when capital is added or withdrawn." if auto
                                       else "The shares stay as typed until you switch back to Automatic."))
        for b, active in ((self.auto_btn, auto), (self.manual_btn, not auto)):
            b.configure(fg_color=T.ACCENT_FILL if active else "transparent", text_color=("#ffffff", "#ffffff") if active else T.TEXT,
                        hover_color=T.ACCENT_HOVER if active else T.CARD_ALT, border_width=0 if active else 1,
                        state="normal" if self.mode_allowed else "disabled")

    def _draw_partners(self, rows):
        sel = self.sel_id
        self.t.set_rows(rows, self._render_row, border=lambda r: T.ACCENT if r["id"] == sel else T.BORDER)

    def _render_row(self, cells, r):
        _cell(cells[0], r["name"], bold=True).pack(fill="x")
        _cell(cells[1], T.money(r["capital_paisa"]), anchor="e").pack(fill="x")
        _cell(cells[2], T.pct_bps(r["ownership_bps"]), anchor="e").pack(fill="x")
        _cell(cells[3], T.pct_bps(r["share_bps"]), anchor="e", bold=True).pack(fill="x")
        _clickable(cells[0].master, lambda r=r: self._choose(r))

    def _choose(self, row):
        self.sel_id = row["id"]
        self._draw_partners(self.t.rows)

    def _pchoices(self):
        return [(r["name"], r["id"]) for r in q.partners(self.conn)]

    def add(self):
        self.form("Add partner", [Field("name", "Name"), Field("phone", "Phone"), Field("joined", "Joining date", "date")],
                  lambda v: ps.add_partner(self.conn, self.user, v["name"], v["phone"], v["joined"]))

    def capital(self):
        self.form("Record capital contribution", [Field("p", "Partner", "choice", self._pchoices(), self.sel_id), Field("amount", "Amount (Rs)", "money"),
                                                  Field("on", "Date", "date"), Field("note", "Note")],
                  lambda v: ps.record_capital(self.conn, self.user, v["p"], v["amount"], v["on"], v["note"]))

    def draw(self):
        self.form("Record partner withdrawal", [Field("p", "Partner", "choice", self._pchoices(), self.sel_id), Field("amount", "Amount (Rs)", "money"),
                                                Field("on", "Date", "date"), Field("note", "Note")],
                  lambda v: ps.record_drawing(self.conn, self.user, v["p"], v["amount"], v["on"], v["note"]),
                  intro="A withdrawal reduces the partner's capital - and, in Automatic mode, his profit share. It is not a business expense.")

    def use_auto(self):
        if ps.share_mode(self.conn) == "ownership":
            return show_info(self, "Already automatic", "Profit shares already follow ownership %.")
        if not self.confirm("Switch to automatic", "From now on every partner's profit share equals his ownership %, and it changes by itself when capital is "
                            "added or withdrawn. Your typed shares are kept and can be used again with 'Manual %'. Distributions already approved are not touched.",
                            yes="Switch to automatic"):
            return
        self.guard(lambda: ps.set_share_mode(self.conn, self.user, "ownership"))

    def shares(self):
        partners = q.partners(self.conn)
        if not partners:
            return self.need(None, "a partner (add partners first)")
        fields = [Field("eff", "Effective from", "date")] + [Field(f"p{p['id']}", f"{p['name']} - profit share %", "text", default=f"{Decimal(p['share_bps'] or 0) / 100}") for p in partners]
        fields.append(Field("total", "Total", "readonly", default="", live_hint=True))

        def parse(v):
            bps = {}
            for p in partners:
                try:
                    d = Decimal(v[f"p{p['id']}"] or "0") * 100
                except InvalidOperation as e:
                    raise ValueError(f"Invalid percentage for {p['name']}") from e
                if d != d.to_integral_value():
                    raise ValueError("Use at most 2 decimal places.")
                bps[p["id"]] = int(d)
            return bps

        def live(v, d):
            try:
                tot = sum(parse(v).values())
            except ValueError as e:
                d.set_value("total", "-"); return d.set_hint("total", str(e), bad=True)
            d.set_value("total", f"{Decimal(tot) / 100}%")
            d.set_hint("total", "Total is 100%." if tot == 10000 else "The shares must add up to exactly 100%.", bad=tot != 10000)

        def submit(v):
            bps = parse(v)
            if sum(bps.values()) != 10000:
                raise ValueError(f"Shares total {Decimal(sum(bps.values())) / 100}% - they must total exactly 100%.")
            if not self.confirm("Confirm profit-sharing change", "Profit sharing becomes MANUAL: these percentages are used from the effective date and no longer follow "
                                "ownership %. It is recorded permanently and audited.", yes="Yes, change"):
                raise ValueError("Cancelled.")
            ps.set_profit_shares(self.conn, self.user, v["eff"], bps, confirmed=True)
        self.form("Set profit-sharing % manually", fields, submit, live=live,
                  intro="Must total exactly 100%. Needs owner confirmation. You can go back to Automatic (= ownership %) at any time.")


class ProfitPage(Page):
    title = "Profit Distribution"
    subtitle = "Only recognised net profit of a period can be distributed - never a loss. Approval is owner-only; payment is a separate cash step."

    def build(self):
        self._focus_id = None
        self._dist = None
        bar = self.toolbar()
        today = date.today()
        self.s = DateField(bar, today.replace(day=1), width=150, height=38, compact=True, command=self.refresh)       # calendars, not typed dates
        self.e = DateField(bar, today, width=150, height=38, compact=True, command=self.refresh)
        ctk.CTkLabel(bar, text="Period", text_color=T.MUTED, font=T.font(14)).pack(side="left", padx=(0, 8)); self.s.pack(side="left")
        ctk.CTkLabel(bar, text="to", text_color=T.MUTED, font=T.font(14)).pack(side="left", padx=8)
        self.e.pack(side="left", padx=(0, 12))
        self.button(bar, "Approve distribution...", self.approve, "approve_distribution", T.GREEN)
        stats = ctk.CTkFrame(self.body, fg_color="transparent"); stats.pack(fill="x")
        self.fig = {}
        for i, (k, t) in enumerate([("net", "Net profit"), ("dist", "Can still be distributed"), ("cash", "Cash in hand")]):
            stats.grid_columnconfigure(i, weight=1, uniform="s")
            box = surface(stats); box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
            self.fig[k] = Stat(box, t, "-"); self.fig[k].pack(fill="x", padx=18, pady=8)
        self.info = ctk.CTkLabel(self.body, text="", justify="left", text_color=T.MUTED, anchor="w", font=T.font(12))
        self.info.pack(fill="x", pady=(6, 8))
        # both lists are always visible: they share whatever height the window has
        lower = ctk.CTkFrame(self.body, fg_color="transparent"); lower.pack(fill="both", expand=True)
        lower.grid_columnconfigure(0, weight=1)
        lower.grid_rowconfigure(0, weight=1, minsize=118); lower.grid_rowconfigure(1, weight=3, minsize=236)
        top = ctk.CTkFrame(lower, fg_color="transparent"); top.grid(row=0, column=0, sticky="nsew")
        ctk.CTkLabel(top, text="Approved distributions", font=T.font(15, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x", pady=(0, 4))
        self.t = RowList(top, [("Period", 0, "w"), ("Approved", 130, "e"), ("Paid", 130, "e"), ("Status", 100, "w")],
                         empty_text="Nothing approved yet.", header_in_scroll=True)
        self.t.pack(fill="both", expand=True)
        self.t.scroll.configure(height=70)
        self.t.empty.pack_configure(pady=12)
        panel = surface(lower); panel.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        head = ctk.CTkFrame(panel, fg_color="transparent"); head.pack(fill="x", padx=18, pady=(14, 0))
        titles = ctk.CTkFrame(head, fg_color="transparent"); titles.pack(side="left")
        self.panel_title = ctk.CTkLabel(titles, text="Partners' profit", font=T.font(15, "bold"), text_color=T.TEXT, anchor="w"); self.panel_title.pack(fill="x")
        self.panel_sub = ctk.CTkLabel(titles, text="", font=T.font(12), text_color=T.MUTED, anchor="w"); self.panel_sub.pack(fill="x", pady=(2, 0))
        self.pay_all_btn = make_button(head, "Pay all unpaid", self.pay_all, "primary", width=170)
        self.pay_all_btn.pack(side="right")
        if not self.ctx.can("approve_distribution"):
            self.pay_all_btn.configure(state="disabled")
        self.l = RowList(panel, [("Partner", 0, "w"), ("Share", 90, "e"), ("Amount", 130, "e"), ("Paid", 130, "e"), ("Status", 100, "w"), ("", 190, "e")],
                         empty_text="Approve a distribution, or pick one above, to see what each partner gets.", header_in_scroll=True)
        self.l.pack(fill="both", expand=True, padx=10, pady=(8, 10))
        self.l.scroll.configure(height=120)
        self.l.empty.pack_configure(pady=16)

    def refresh(self):
        try:
            st = acct.income_statement(self.conn, self.s.get(), self.e.get())
            d = ps.distributable(self.conn, self.s.get(), self.e.get())
        except Exception as e:  # noqa: BLE001
            self.info.configure(text=f"Check the dates: {e}"); return
        self.fig["net"].set(T.money(st["net_profit"]), tone="good" if st["net_profit"] > 0 else "bad" if st["net_profit"] < 0 else None)
        self.fig["dist"].set(T.money(d))
        self.fig["cash"].set(T.money(acct.balance(self.conn, "1000")))
        mode = "ownership %" if ps.share_mode(self.conn) == "ownership" else "the manual shares"
        self.info.configure(text=f"Split by {mode}. Profit is not the same as cash - check cash before paying partners.")
        rows = q.distributions(self.conn)
        keep = self._focus_id or (self._dist["id"] if self._dist else None)
        self._focus_id = None
        self._dist = next((r for r in rows if r["id"] == keep), None) or (rows[0] if rows else None)
        self._draw_dists(rows)
        self._pick(self._dist, rows)

    def _draw_dists(self, rows):
        sel = self._dist["id"] if self._dist else None
        self.t.set_rows(rows, self._render_dist, border=lambda r: T.ACCENT if r["id"] == sel else T.BORDER)

    def _render_dist(self, cells, r):
        _cell(cells[0], f"#{r['id']}   {r['period_start']}  to  {r['period_end']}", bold=True).pack(fill="x")
        _cell(cells[1], T.money(r["amount_paisa"]), anchor="e").pack(fill="x")
        _cell(cells[2], T.money(r["paid_paisa"]), anchor="e", muted=True).pack(fill="x")
        left = r["amount_paisa"] - r["paid_paisa"]
        kind, word = ("good", "Paid") if left == 0 else ("owe", "Part paid") if r["paid_paisa"] else ("bad", "Unpaid")
        _chip(cells[3], word, kind).pack(anchor="w")
        _clickable(cells[0].master, lambda r=r: self._choose(r))

    def _choose(self, row):
        self._pick(row)
        self._draw_dists(self.t.rows)

    def _pick(self, row, rows=None):
        self._dist = row
        if not row:
            self.panel_title.configure(text="Partners' profit"); self.panel_sub.configure(text="")
            self.pay_all_btn.configure(text="Pay all unpaid", state="disabled", width=170)
            self.l.set_rows([], lambda c, r: None)
            return
        lines = q.distribution_lines(self.conn, row["id"])
        due = sum(x["amount_paisa"] - x["paid_paisa"] for x in lines)
        self.panel_title.configure(text=f"Distribution #{row['id']}  -  {T.money(row['amount_paisa'])}")
        self.panel_sub.configure(text=f"{row['period_start']} to {row['period_end']}   -   paid {T.money(row['amount_paisa'] - due)}   -   still to pay {T.money(due)}")
        self.pay_all_btn.configure(text=f"Pay all  ({T.money(due)})" if due else "All paid",
                                   state="normal" if due and self.ctx.can("approve_distribution") else "disabled", width=230 if due else 130)
        self.l.set_rows(lines, self._render_line)

    def _render_line(self, cells, r):
        unpaid = r["amount_paisa"] - r["paid_paisa"]
        _cell(cells[0], r["name"], bold=True).pack(fill="x")
        _cell(cells[1], T.pct_bps(r["share_bps"]), anchor="e").pack(fill="x")
        _cell(cells[2], T.money(r["amount_paisa"]), anchor="e", bold=True).pack(fill="x")
        _cell(cells[3], T.money(r["paid_paisa"]), anchor="e", muted=True).pack(fill="x")
        kind, word = ("good", "Paid") if r["amount_paisa"] and unpaid == 0 else ("neutral", "No share") if not r["amount_paisa"] else ("owe", "Part paid") if r["paid_paisa"] else ("bad", "Unpaid")
        _chip(cells[4], word, kind).pack(anchor="w")
        if unpaid > 0:
            b = make_button(cells[5], f"Pay {T.money(unpaid)}", lambda r=r: self.pay_one(r), "secondary", width=180, height=34)
            if not self.ctx.can("approve_distribution"):
                b.configure(state="disabled")
            b.pack(anchor="e")

    def approve(self):
        s, e = self.s.get(), self.e.get()
        avail = ps.distributable(self.conn, s, e)

        def live(v, d):
            try:
                p = to_paisa(v["amount"] or "0")
            except Exception:  # noqa: BLE001 - half-typed number
                return d.set_hint("amount", "Type the amount in rupees.", bad=True)
            if p <= 0:
                return d.set_hint("amount", f"Maximum for {s} to {e}: {T.money(avail)}")
            if p > avail:
                return d.set_hint("amount", f"That is more than the distributable profit ({T.money(avail)}).", bad=True)
            pre = ps.preview_split(self.conn, p, e)
            if not pre:
                return d.set_hint("amount", "No profit-sharing arrangement is in force: add partner capital, or set the shares manually.", bad=True)
            d.set_hint("amount", "\n".join(f"{x['name']}:  {T.pct_bps(x['share_bps'])}  =  {T.money(x['amount_paisa'])}" for x in pre))

        def submit(v):
            if not self.confirm("Approve distribution", f"Approve {v['amount']} Rs for {s} to {e}? Each partner's share is fixed now, from the profit-sharing arrangement in force at period end.", yes="Approve"):
                raise ValueError("Cancelled.")
            self._focus_id = ps.approve_distribution(self.conn, self.user, period_start=s, period_end=e, amount=v["amount"], on=v["on"], note=v["note"])
        self.form("Approve profit distribution", [Field("amount", "Amount (Rs)", "money", default=f"{Decimal(avail) / 100:.2f}", hint=f"Maximum for {s} to {e}: {T.money(avail)}", live_hint=True),
                                                  Field("on", "Approval date", "date"), Field("note", "Note")], submit, submit_text="Approve", live=live)

    def pay_one(self, r):
        unpaid = r["amount_paisa"] - r["paid_paisa"]
        d = self._dist
        self.form(f"Pay {r['name']}", [Field("amount", "Amount (Rs)", "money", default=f"{Decimal(unpaid) / 100:.2f}", hint=f"Unpaid: {T.money(unpaid)}"), Field("on", "Payment date", "date")],
                  lambda v: ps.pay_distribution(self.conn, self.user, distribution_id=d["id"], partner_id=r["partner_id"], amount=v["amount"], on=v["on"]),
                  submit_text="Record payment", intro=f"Distribution #{d['id']}  ({d['period_start']} to {d['period_end']}). Cash is paid out of the business.")

    def pay_all(self):
        d = self._dist
        if not d:
            return self.need(None, "a distribution")
        due = sum(x["amount_paisa"] - x["paid_paisa"] for x in q.distribution_lines(self.conn, d["id"]))
        if not self.confirm("Pay all partners", f"Pay {T.money(due)} to the partners of distribution #{d['id']} today?\n\nCash in hand: {T.money(acct.balance(self.conn, '1000'))}. "
                            "Either everyone is paid or nobody is.", yes="Pay all"):
            return
        self.guard(lambda: ps.pay_all_distribution(self.conn, self.user, distribution_id=d["id"], on=date.today().isoformat()))
