"""Production (entries, stock used, cost calculation) and Machines/electricity."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from tkinter import filedialog

import customtkinter as ctk

from reports import documents
from services import accounting_service as acct, inventory_service as inv, job_service as js, machine_service as ms, queries as q, settings_service
from services.units import from_paisa, mul_div_round, to_milli, to_paisa
from ui import theme as T
from ui.base import Page
from ui.widgets import Col, DataTable, DateField, Field, Stat, _Modal, make_button, make_tabs, show_info, surface


class JobsPage(Page):
    title = "Production & Invoices"
    subtitle = "One number for everything: the entry number is also the invoice number. Add the entry, use stock, invoice it, collect the money."

    STATUS = {"none": "Not invoiced", "unpaid": "Unpaid", "part": "Part paid", "paid": "Paid"}
    TAG = {"none": "muted", "unpaid": "bad", "part": "warn", "paid": "good"}

    def build(self):
        self.tabs = make_tabs(self.body)
        self.tabs.pack(fill="both", expand=True)
        ent, pay = self.tabs.add("Entries"), self.tabs.add("Payments received")
        bar = ctk.CTkFrame(ent, fg_color="transparent"); bar.pack(fill="x", pady=(0, 8))
        self.button(bar, "+ New entry", self.new_job, "manage_jobs", T.GREEN)
        self.button(bar, "Use stock", self.consume, "consume_stock")
        self.button(bar, "Calculation", self.details, None)
        self.button(bar, "Edit", self.edit, "manage_jobs")
        self.button(bar, "Return stock", self.give_back, "consume_stock")
        self.button(bar, "Delete", self.delete, "manage_jobs", T.RED)
        bar2 = ctk.CTkFrame(ent, fg_color="transparent"); bar2.pack(fill="x", pady=(0, 10))
        self.button(bar2, "Create invoice", self.invoice, "manage_invoices", T.GREEN)
        self.button(bar2, "Receive payment", self.payment, "record_payment")
        self.button(bar2, "Invoice PDF", self.pdf, None)
        C = "center"                                                   # everything centred under its heading, so neighbouring columns never run into each other
        self.t = DataTable(ent, [Col("job_no", "Entry no.", 78, None, C), Col("order_date", "Date", 88, None, C), Col("customer", "Customer", 125, None, C),
                                 Col("quantity_pieces", "Meters", 62, lambda v: "-" if v is None else f"{v:,}", C),
                                 Col("cost_paisa", "Stock cost", 100, T.money, C), Col("invoice_paisa", "Invoice", 100, lambda v: "-" if v is None else T.money(v), C),
                                 Col("due_paisa", "Balance", 100, lambda v: "-" if v is None else T.money(v), C), Col("status_text", "Status", 78, None, C)],
                           row_tags=lambda r: self.TAG[r["bill_status"]], bold=True)
        self.t.pack(fill="both", expand=True)
        self.r = DataTable(pay, [Col("entry_date", "Date", 95), Col("customer", "Customer", 190), Col("kind", "Applied to", 120), Col("amount_paisa", "Amount", 130, T.money, "e"),
                                 Col("description", "Reference", 240)], bold=True)
        self.r.pack(fill="both", expand=True)

    def refresh(self):
        self.t.set_rows([dict(r, status_text=self.STATUS[r["bill_status"]]) for r in q.production_entries(self.conn)])
        self.r.set_rows(q.receipts(self.conn))

    def _job(self):
        r = self.t.selected()
        return r if self.need(r, "a production entry") else None

    def _next_no(self) -> str:
        """Next free entry number: one above the highest P-#### ever used (cancelled and deleted entries must not cause a clash)."""
        import re
        used = [int(m.group(1)) for (n,) in self.conn.execute("SELECT job_no FROM jobs") if (m := re.fullmatch(r"P-(\d+)", n or ""))]
        return f"P-{max(used, default=0) + 1:04d}"

    def new_job(self):
        cus = [(c["name"], c["id"]) for c in q.customers(self.conn)]
        if not cus:
            return self.need(None, "a customer (add one on the Customers page first)")
        nxt = self._next_no()

        def submit(v):
            try:
                pcs = int(v["pcs"]) if v["pcs"] else None
            except ValueError as e:
                raise ValueError("Meters produced must be a whole number.") from e
            js.create_job(self.conn, self.user, job_no=v["no"], customer_id=v["cus"], product_type=v["prod"] or "Bedsheet", fabric_source=v["src"],
                          order_date=v["od"], quantity_pieces=pcs, specs=v["notes"], status="in_progress")
        self.form("New production entry", [Field("no", "Entry number", default=nxt), Field("od", "Date", "date"), Field("cus", "Customer", "choice", cus),
                                           Field("prod", "Product", default="Bedsheet"), Field("pcs", "Meters produced", hint="Used to work out the cost per meter. You can leave it empty and set it later."),
                                           Field("src", "Fabric from", "choice", [("Customer supplies fabric (no cost)", "customer"), ("Factory supplies fabric", "factory")]),
                                           Field("notes", "Notes", "multiline")], submit, submit_text="Add entry")

    def consume(self):
        j = self._job()
        if not j:
            return
        mats = q.materials(self.conn)
        if not mats:
            return self.need(None, "a material (add one on the Inventory page first)")
        StockUseDialog(self, self.conn, self.user, j, mats, on_done=self.refresh)

    def details(self):
        j = self._job()
        if j:
            CalculationDialog(self, js.job_summary(self.conn, j["id"]))

    def edit(self):
        j = self._job()
        if not j:
            return
        full = self.conn.execute("SELECT * FROM jobs WHERE id=?", (j["id"],)).fetchone()
        cus = [(c["name"], c["id"]) for c in q.customers(self.conn)]

        def submit(v):
            try:
                pcs = int(v["pcs"]) if v["pcs"] else None
            except ValueError as e:
                raise ValueError("Meters produced must be a whole number.") from e
            js.update_job(self.conn, self.user, j["id"], customer_id=v["cus"], product_type=v["prod"], order_date=v["od"],
                          quantity_pieces=pcs, specs=v["notes"], fabric_source=v["src"])
        self.form(f"Edit {j['job_no']}", [Field("no", "Entry number", "readonly", default=j["job_no"], hint="The number cannot change."),
                                          Field("od", "Date", "date", default=full["order_date"]), Field("cus", "Customer", "choice", cus, default=full["customer_id"]),
                                          Field("prod", "Product", default=full["product_type"]),
                                          Field("pcs", "Meters produced", default="" if full["quantity_pieces"] is None else str(full["quantity_pieces"])),
                                          Field("src", "Fabric from", "choice", [("Customer supplies fabric (no cost)", "customer"), ("Factory supplies fabric", "factory")], default=full["fabric_source"],
                                                hint="Customer and fabric source are locked once stock has been used."),
                                          Field("notes", "Notes", "multiline", default=full["specs"] or "")], submit, submit_text="Save changes")

    def give_back(self):
        j = self._job()
        if not j:
            return
        lines = [l for l in js.job_summary(self.conn, j["id"])["lines"]
                 if l["kind"] in ("material", "wastage") and not l["reverses_id"] and not l["returned"]]
        if not lines:
            return show_info(self, "Nothing to give back", "No stock is currently used on this entry.")
        choices = [(f"{l['date']}  -  {l['item']}  -  {T.qty(l['qty_milli'], l['unit'])}  -  {T.money(l['cost_paisa'])}", l["id"]) for l in lines]

        def submit(v):
            js.return_material(self.conn, self.user, j["id"], v["line"], v["reason"], on_date=v["on"])
        self.form(f"Return stock from {j['job_no']}", [Field("line", "Which stock?", "choice", choices), Field("on", "Date", "date"),
                                                       Field("reason", "Reason", hint="For example: entered on the wrong entry, or left over.")], submit, submit_text="Give back",
                  intro="The whole quantity goes back to inventory at the cost it left at. The entry's cost goes down by the same amount.")

    def delete(self):
        j = self._job()
        if not j:
            return
        if not self.confirm("Delete entry", f"Delete {j['job_no']} ({j['customer']})?\n\nIf stock is still used on it, give that stock back first. "
                            "An entry with history is hidden, not erased, so the records stay correct.", yes="Delete", danger=True):
            return
        self.guard(lambda: js.delete_job(self.conn, self.user, j["id"]))


    # ------------------------------------------------------------------ billing
    def invoice(self):
        j = self._job()
        if not j:
            return
        if j["invoice_id"]:
            return show_info(self, "Already invoiced", f"Invoice {j['job_no']} was already created for this entry ({T.money(j['invoice_paisa'])}).")
        advance = next((c["advance"] for c in q.customers(self.conn) if c["id"] == j["customer_id"]), 0)
        InvoiceDialog(self, self.conn, self.user, j, js.job_summary(self.conn, j["id"]), max(advance, 0), on_done=self.refresh)

    def payment(self):
        r = self.t.selected()
        due = [i for i in q.production_entries(self.conn) if i["invoice_id"] and i["due_paisa"] > 0]
        cus = [(c["name"], c["id"]) for c in q.customers(self.conn)]
        if not cus:
            return self.need(None, "a customer")
        against = [(f"{i['job_no']}  -  {i['customer']}  -  balance {T.money(i['due_paisa'])}", i["invoice_id"]) for i in due] + [("Advance (money before the invoice)", None)]
        dues = {i["invoice_id"]: i for i in due}
        if r and r["invoice_id"] and r["due_paisa"] > 0:
            start = r["invoice_id"]
        else:
            start = None
        state = {"last": object()}

        def live(v, dlg):
            inv_row = dues.get(v["i"])
            if state["last"] != v["i"]:                                  # the choice changed: fill in the full balance
                state["last"] = v["i"]
                if inv_row:
                    dlg.set_value("a", str(from_paisa(inv_row["due_paisa"])))
                    dlg.set_value("c", inv_row["customer_id"])
                dlg.lock("c", bool(inv_row))
            try:
                paid = to_paisa(v["a"] or "0")
            except Exception:  # noqa: BLE001
                return dlg.set_hint("a", "")
            if inv_row:
                left = inv_row["due_paisa"] - paid
                dlg.set_hint("a", f"Balance after this payment: {T.money(left)}" if left >= 0 else f"More than the balance ({T.money(inv_row['due_paisa'])}).", bad=left < 0)
            else:
                dlg.set_hint("a", "Kept as the customer's advance. It is used automatically when his next invoice is created.")

        def submit(v):
            ch = dues.get(v["i"])
            acct.receive_payment(self.conn, self.user, customer_id=ch["customer_id"] if ch else v["c"], amount=v["a"], paid_on=v["on"], method=v["m"],
                                 reference=v["ref"], invoice_id=ch["invoice_id"] if ch else None)
        default_c = r["customer_id"] if r else cus[0][1]
        self.form("Receive payment", [Field("i", "Payment for", "choice", against, start), Field("c", "Customer (advances only)", "choice", cus, default_c),
                                      Field("a", "Amount (Rs)", "money", live_hint=True), Field("on", "Date received", "date"),
                                      Field("m", "How was it paid?", "choice", [("Cash", "cash"), ("Bank transfer", "bank"), ("Cheque", "cheque")]),
                                      Field("ref", "Reference (cheque / transfer no.)")], submit, submit_text="Record payment", live=live,
                  intro="Choose an invoice to pay it, or 'Advance' if the customer pays before his invoice exists.")

    def pdf(self):
        r = self._job()
        if not r:
            return
        if not r["invoice_id"]:
            return show_info(self, "No invoice yet", "Create the invoice for this entry first.")
        f = filedialog.asksaveasfilename(defaultextension=".pdf", initialfile=f"invoice_{r['job_no']}.pdf", filetypes=[("PDF", "*.pdf")])
        if f:
            self.guard(lambda: documents.invoice_pdf(self.conn, r["invoice_id"], f), done=lambda: show_info(self, "Saved", f))


class CalculationDialog(_Modal):
    """What went into one production entry, and what it cost. Compact on purpose: the list scrolls inside the window, the buttons never leave the screen."""

    def __init__(self, parent, s: dict):
        super().__init__(parent, f"Calculation - {s['job']['job_no']}", 640)
        j = s["job"]
        outer = ctk.CTkFrame(self, fg_color="transparent"); outer.pack(fill="both", expand=True, padx=20, pady=16)
        self.default_btn = make_button(outer, "Close", self.destroy, "primary", width=100)          # pinned first so it is always visible
        self.default_btn.pack(side="bottom", anchor="e", pady=(10, 0))
        room = max(260, min(560, self.winfo_screenheight() - 330))
        pad = ctk.CTkScrollableFrame(outer, fg_color="transparent", height=room, width=600); pad.pack(fill="both", expand=True)
        ctk.CTkLabel(pad, text=f"{j['job_no']}  -  {j['customer']}", font=T.font(17, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(pad, text=f"{j['product_type']}  ·  {j['order_date']}" + (f"  ·  {s['pieces']:,} m" if s["pieces"] else ""),
                     font=T.font(12), text_color=T.MUTED, anchor="w").pack(fill="x", pady=(1, 8))
        rows = []
        for l in s["lines"]:
            qty = T.qty(l["qty_milli"], l["unit"]) if l["qty_milli"] else "-"
            rate = "customer's (no cost)" if l["customer_owned"] else ("-" if l["rate_paisa_per_unit"] is None else f"{T.money(l['rate_paisa_per_unit'])} / {l['unit'] or 'unit'}")
            rows.append({"date": l["date"], "item": l["item"] + (" (wastage)" if l["kind"] == "wastage" else "") + (" (given back)" if l["reverses_id"] else ""), "qty": qty, "rate": rate,
                         "cost": T.money(l["cost_paisa"]) + ("  (estimate)" if l["estimate"] else "")})
        t = DataTable(pad, [Col("date", "Date", 78), Col("item", "Item", 140), Col("qty", "Quantity", 86), Col("rate", "Rate", 112), Col("cost", "Cost", 104)],
                      searchable=False, height=min(6, max(2, len(rows) + 1)), page_size=100, empty_text="No stock used yet. Use the 'Use stock' button.")
        t.pack(fill="x"); t.set_rows(rows)
        tot = ctk.CTkFrame(pad, fg_color="transparent"); tot.pack(fill="x", pady=(10, 0))
        figs = [("Stock used", T.money(s["stock_cost_paisa"]))]
        if s["other_cost_paisa"]:
            figs.append(("Other costs", T.money(s["other_cost_paisa"])))
        figs += [("Total cost", T.money(s["total_paisa"])), ("Cost per meter", "-" if s["cost_per_piece_paisa"] is None else T.money(s["cost_per_piece_paisa"]))]
        for i, (k, v) in enumerate(figs):
            tot.grid_columnconfigure(i, weight=1, uniform="f")
            box = surface(tot); box.grid(row=0, column=i, sticky="nsew", padx=3)
            ctk.CTkLabel(box, text=k, font=T.font(12), text_color=T.MUTED, anchor="w").pack(fill="x", padx=10, pady=(8, 0))
            ctk.CTkLabel(box, text=v, font=T.font(15, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x", padx=10, pady=(0, 8))
        if not s["pieces"]:
            ctk.CTkLabel(pad, text="Meters were not entered for this entry, so cost per meter is not shown.", font=T.font(11), text_color=T.MUTED, anchor="w").pack(fill="x", pady=(8, 0))
        for w in s["warnings"]:
            ctk.CTkLabel(pad, text="! " + w, font=T.font(11), text_color=T.WARN, anchor="w", wraplength=560, justify="left").pack(fill="x", pady=(5, 0))
        self.present(True)


class StockUseDialog(_Modal):
    """Take several materials from stock for one entry in one go. Nothing is taken unless every line works."""
    V_CENTER = True

    def __init__(self, parent, conn, user, job, mats, *, on_done):
        super().__init__(parent, f"Use stock for {job['job_no']}", 1020)
        self.conn, self.user, self.job, self.on_done = conn, user, job, on_done
        self.cust_fabric = job["fabric_source"] == "customer"
        self.mats = {}
        # fabric can come from two places, so it is listed twice: the CUSTOMER'S OWN cloth (this customer only, no cost) and the FACTORY'S cloth (bought, at average cost).
        mine = {r[0]: r[1] for r in conn.execute("SELECT material_id, SUM(qty_remaining_milli) FROM stock_lots WHERE ownership='customer' AND customer_id=? "
                                                 "AND qty_remaining_milli>0 GROUP BY material_id", (job["customer_id"],))}
        for m in mats:
            have_mine = mine.get(m["id"], 0)
            if m["category"] == "fabric":                                   # fabric: the usual source (set on the entry) is listed first and always
                options = [("customer", have_mine), ("factory", m["qty_milli"])]
                if not self.cust_fabric:
                    options.reverse()
                for i, (own, have) in enumerate(options):
                    if i == 0 or have > 0:                                  # the other source only when there is some
                        self._add_option(m, own, have)
            else:                                                           # ink, paper, other: the factory's stock is always listed; this customer's own too when he gave some
                if have_mine > 0:
                    self._add_option(m, "customer", have_mine)
                self._add_option(m, "factory", m["qty_milli"])
        self.rows: list[dict] = []
        pad = ctk.CTkFrame(self, fg_color="transparent"); pad.pack(fill="both", expand=True, padx=26, pady=22)
        ctk.CTkLabel(pad, text=f"Use stock  -  {job['job_no']}  ({job['customer']})", font=T.font(21, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(pad, text=("Add every material this entry used. Anything the customer gave (fabric, ink, paper) is taken from his own stock at zero cost; if the factory's own stock was used too, "
                                "add it from the 'in factory stock' line." if self.cust_fabric
                                else "Add every material this entry used. Factory stock leaves inventory at its average cost; what this customer gave (fabric, ink, paper) is listed as '(customer's own)' at zero cost."),
                     font=T.font(13), text_color=T.MUTED, anchor="w", wraplength=900, justify="left").pack(fill="x", pady=(2, 10))
        hdr = ctk.CTkFrame(pad, fg_color="transparent"); hdr.pack(fill="x", padx=(14, 0))
        ctk.CTkLabel(hdr, text="Material", font=T.font(12), text_color=T.MUTED, anchor="w").pack(side="left", fill="x", expand=True, padx=(0, 6))
        for txt, w, an in (("Quantity", 100, "w"), ("Counted in", 132, "w"), ("Cost (approx.)", 112, "e")):
            ctk.CTkLabel(hdr, text=txt, font=T.font(12), text_color=T.MUTED, anchor=an, width=w).pack(side="left", padx=(0, 6))
        ctk.CTkLabel(hdr, text="", width=34).pack(side="left")
        ctk.CTkLabel(hdr, text="", width=16).pack(side="left")                        # room for the list's scroll bar
        # The footer (add button, date, error, buttons) is packed to the BOTTOM first, so on a small screen it is the list in the middle that gets
        # shorter (it scrolls) - the buttons are never pushed off the window.
        row = ctk.CTkFrame(pad, fg_color="transparent"); row.pack(side="bottom", fill="x", pady=(10, 0))
        self.go_btn = self.default_btn = make_button(row, "Take from stock", self.submit, "primary", width=160); self.go_btn.pack(side="right")
        make_button(row, "Cancel", self.destroy, "ghost", width=90).pack(side="right", padx=8)
        self.err = ctk.CTkLabel(pad, text="", text_color=T.BAD, font=T.font(13), wraplength=900, justify="left", anchor="w"); self.err.pack(side="bottom", fill="x", pady=(10, 0))
        dr = ctk.CTkFrame(pad, fg_color="transparent"); dr.pack(side="bottom", fill="x", pady=(12, 0))
        ctk.CTkLabel(dr, text="Date", font=T.font(13), text_color=T.MUTED, width=60, anchor="w").pack(side="left")
        self.on = DateField(dr, date.today(), width=230); self.on.pack(side="left")
        self.total = ctk.CTkLabel(dr, text="", font=T.font(16, "bold"), text_color=T.TEXT); self.total.pack(side="right")
        make_button(pad, "+ Add another material", self.add_row, "ghost", width=190).pack(side="bottom", anchor="w", pady=(6, 0))
        self.box = ctk.CTkScrollableFrame(pad, fg_color="transparent", width=100, height=150)
        self.box.pack(fill="both", expand=True)
        self.box._scrollbar.configure(height=10)             # the scrollbar's own default height (200) would otherwise be the minimum
        self.add_row()
        self.present(True)

    def _add_option(self, m, own, have):
        theirs = own == "customer"
        label = f"{m['name']}  -  {T.qty_full(have, m['base_unit'], m['roll_length_milli'])} " + ("(customer's own)" if theirs else "in factory stock")
        self.mats[label] = dict(m, theirs=theirs, have=have, own=own)

    ROLLS, METERS = "Rolls", "Meters"

    def add_row(self):
        r = {}
        outer = ctk.CTkFrame(self.box, fg_color="transparent"); outer.pack(fill="x", pady=3)
        f = ctk.CTkFrame(outer, fg_color="transparent"); f.pack(fill="x")
        labels = list(self.mats)
        r["frame"] = outer
        r["mat"] = ctk.CTkComboBox(f, values=labels, state="readonly", height=38, dropdown_font=T.font(14), font=T.font(14), command=lambda _v: self._material_changed(r))
        r["mat"].set(labels[0]); r["mat"].pack(side="left", fill="x", expand=True, padx=(0, 6))
        r["qty"] = ctk.CTkEntry(f, width=100, height=38, font=T.font(14), placeholder_text="0"); r["qty"].pack(side="left", padx=(0, 6))
        r["qty"].bind("<KeyRelease>", lambda _e: self._calc())
        # "Counted in": paper gets a Rolls / Meters switch, every other material just shows its own unit
        r["unit_box"] = ctk.CTkFrame(f, fg_color="transparent"); r["unit_box"].pack(side="left", padx=(0, 6))
        r["unit_lbl"] = ctk.CTkLabel(r["unit_box"], text="", width=132, height=38, font=T.font(14), text_color=T.MUTED, anchor="w")
        r["unit_seg"] = ctk.CTkComboBox(r["unit_box"], values=[self.METERS, self.ROLLS], state="readonly", width=132, height=38, font=T.font(14),
                                        dropdown_font=T.font(14), command=lambda _v: self._calc())
        r["unit_seg"].set(self.METERS)                      # paper is counted in meters first; rolls are the other choice
        r["cost"] = ctk.CTkLabel(f, text="-", width=112, anchor="e", font=T.font(14), text_color=T.TEXT); r["cost"].pack(side="left", padx=(0, 6))
        r["x"] = make_button(f, "x", lambda r=r: self.drop_row(r), "ghost", width=34, height=34); r["x"].pack(side="left")
        r["note"] = ctk.CTkLabel(outer, text="", font=T.font(12), text_color=T.MUTED, anchor="w", justify="left")
        self.rows.append(r)
        self._material_changed(r)
        self._resize_box()

    def _resize_box(self):
        sc = self._get_window_scaling() or 1.0
        room = int(self.winfo_screenheight() / sc) - 100 - 330                         # screen minus the title, header, footer and the window frame
        self.box.configure(height=max(150, min(74 * len(self.rows) + 14, room)))       # every line is shown; the list scrolls only when the screen has no more room
        self.present(False)

    def drop_row(self, r):
        if len(self.rows) == 1:
            r["qty"].delete(0, "end"); self._calc(); return
        self.rows.remove(r); r["frame"].destroy()
        self._calc(); self._resize_box()

    def _material_changed(self, r):
        """Show the Rolls / Meters switch for paper, or just the unit for everything else; then recompute."""
        m = self.mats[r["mat"].get()]
        r["paper"] = bool(m["roll_length_milli"]) and m["category"] == "paper"
        if r["paper"]:
            r["unit_lbl"].pack_forget(); r["unit_seg"].pack()
        else:
            r["unit_seg"].pack_forget()
            u = m["base_unit"]
            r["unit_lbl"].configure(text={"L": "litres (L)", "m": "meters (m)", "roll": "rolls"}.get(u, u)); r["unit_lbl"].pack()
        self._calc()

    def _qty_milli(self, r, m):
        """The row's quantity in the material's own unit (milli), turning meters into rolls for paper. Raises on half-typed numbers."""
        text = r["qty"].get().strip() or "0"
        if r["paper"] and r["unit_seg"].get() == self.METERS:
            return inv.meters_to_rolls_milli(text, m["roll_length_milli"])
        return to_milli(text)

    def _estimate(self, m, qty_milli) -> int:
        """Approximate cost of taking qty out at the average cost (the exact figure is booked when saved)."""
        if m["theirs"] or m["qty_milli"] <= 0:
            return 0
        if qty_milli >= m["qty_milli"]:
            return m["value_paisa"]
        return mul_div_round(qty_milli, m["value_paisa"], m["qty_milli"])

    def _calc(self):
        tot = 0
        for r in self.rows:
            m = self.mats[r["mat"].get()]
            note, bad = "", False
            try:
                qm = self._qty_milli(r, m)
            except Exception:  # noqa: BLE001 - half-typed number
                r["cost"].configure(text="?"); self._set_note(r, "", False); continue
            if r["paper"] and qm > 0:
                rl = m["roll_length_milli"]
                note = f"= {T.qty(qm, 'roll')} ({T.meters(mul_div_round(qm, rl, 1000))})   -   1 roll = {T.meters(rl)}"
            elif r["paper"]:
                note = f"1 roll = {T.meters(m['roll_length_milli'])}"
            if qm > m["have"]:
                note = f"Only {T.qty_full(m['have'], m['base_unit'], m['roll_length_milli'])} in stock."
                bad = True
            c = self._estimate(m, qm) if qm > 0 else 0
            tot += c
            r["cost"].configure(text=T.money(c) if qm > 0 else "-", text_color=T.TEXT)
            self._set_note(r, note, bad)
        self.total.configure(text=f"Total about {T.money(tot)}")

    def _set_note(self, r, text, bad):
        if text:
            r["note"].configure(text=text, text_color=T.BAD if bad else T.MUTED)
            if not r["note"].winfo_ismapped():
                r["note"].pack(fill="x", padx=4)
        elif r["note"].winfo_ismapped():
            r["note"].pack_forget()

    def submit(self):
        lines = []
        for n, r in enumerate(self.rows, 1):
            q_ = r["qty"].get().strip()
            if q_:
                m = self.mats[r["mat"].get()]
                lines.append((m["id"], q_, "m" if (r["paper"] and r["unit_seg"].get() == self.METERS) else None, m["own"]))
        if not lines:
            self.err.configure(text="Type a quantity for at least one material."); return
        self.go_btn.configure(state="disabled")
        try:
            js.consume_materials(self.conn, self.user, self.job["id"], lines, on_date=self.on.get())
        except Exception as e:  # noqa: BLE001 - shown here; nothing was taken
            self.err.configure(text=str(e) or type(e).__name__); self.go_btn.configure(state="normal"); return
        self.destroy(); self.on_done()


class InvoiceDialog(_Modal):
    """Create the invoice for an entry: type the price per meter, see the total, the profit and what the customer's advance covers."""

    def __init__(self, parent, conn, user, job, summary, advance_paisa, *, on_done):
        super().__init__(parent, f"Invoice {job['job_no']}", 600)
        self.conn, self.user, self.job, self.on_done = conn, user, job, on_done
        self.stock_cost, self.pieces, self.advance = summary["total_paisa"], job["quantity_pieces"], advance_paisa
        self.locked = settings_service.locked_cost(conn)                      # the cost per meter the owner locked on the Cost Calculator page
        self.cost = self.locked * int(self.pieces) if (self.locked and self.pieces) else self.stock_cost
        btns = ctk.CTkFrame(self, fg_color="transparent"); btns.pack(side="bottom", fill="x", padx=28, pady=(0, 20))          # pinned first: never cut off
        self.err = ctk.CTkLabel(self, text="", text_color=T.BAD, font=T.font(13), wraplength=520, justify="left", anchor="w"); self.err.pack(side="bottom", fill="x", padx=28, pady=(6, 8))
        pad = ctk.CTkFrame(self, fg_color="transparent"); pad.pack(fill="both", expand=True, padx=28, pady=(24, 0))
        ctk.CTkLabel(pad, text=f"Invoice {job['job_no']}", font=T.font(22, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(pad, text=f"{job['customer']}  ·  {job['product_type']}" + (f"  ·  {self.pieces:,} m" if self.pieces else ""), font=T.font(13), text_color=T.MUTED,
                     anchor="w").pack(fill="x", pady=(2, 10))
        cost_box = surface(pad); cost_box.pack(fill="x")
        row = ctk.CTkFrame(cost_box, fg_color="transparent"); row.pack(fill="x", padx=16, pady=12)
        row.grid_columnconfigure((0, 1), weight=1, uniform="c")
        pp = js.cost_per_piece(self.stock_cost, self.pieces)
        if self.locked:
            Stat(row, "Cost per meter", T.money(self.locked)).grid(row=0, column=0, sticky="w")
            Stat(row, "Cost of this entry", T.money(self.cost) if self.pieces else "-").grid(row=0, column=1, sticky="w")
        else:
            Stat(row, "What this entry cost", T.money(self.cost)).grid(row=0, column=0, sticky="w")
            Stat(row, "Cost per meter", "-" if pp is None else T.money(pp)).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(pad, text="Invoice date", font=T.font(13), text_color=T.MUTED, anchor="w").pack(fill="x", pady=(14, 4))
        self.on = DateField(pad, date.today()); self.on.pack(fill="x")
        ctk.CTkLabel(pad, text="Price per meter (Rs)" if self.pieces else "Price per meter (add the meters on the entry to use this)", font=T.font(13), text_color=T.MUTED,
                     anchor="w").pack(fill="x", pady=(14, 4))
        self.rate = ctk.CTkEntry(pad, height=42, font=T.font(16)); self.rate.pack(fill="x")
        ctk.CTkLabel(pad, text="Invoice total (Rs)", font=T.font(13), text_color=T.MUTED, anchor="w").pack(fill="x", pady=(14, 4))
        self.total = ctk.CTkEntry(pad, height=42, font=T.font(16, "bold")); self.total.pack(fill="x")
        if not self.pieces:
            self.rate.configure(state="disabled")
        self.rate.bind("<KeyRelease>", lambda _e: self._from_rate())
        self.total.bind("<KeyRelease>", lambda _e: self._from_total())
        self.sum = ctk.CTkFrame(pad, fg_color="transparent"); self.sum.pack(fill="x", pady=(14, 0))
        self.profit = ctk.CTkLabel(self.sum, text="", font=T.font(15, "bold"), anchor="w", wraplength=520, justify="left"); self.profit.pack(fill="x")
        self.adv = ctk.CTkLabel(self.sum, text="", font=T.font(14), text_color=T.TEXT, anchor="w", wraplength=520, justify="left"); self.adv.pack(fill="x", pady=(6, 0))
        btns.grid_columnconfigure((0, 1), weight=1, uniform="ib")
        make_button(btns, "Cancel", self.destroy, "secondary", height=44, width=10).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.go_btn = self.default_btn = make_button(btns, "Create invoice", self.submit, "primary", height=44, width=10)
        self.go_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self._show(0)
        self.present(True)

    def _from_rate(self):
        try:
            total, _ = js.invoice_amount(self.pieces, rate=self.rate.get().strip() or None)
        except Exception:  # noqa: BLE001
            return self._show(0)
        self.total.delete(0, "end"); self.total.insert(0, str(from_paisa(total)))
        self._show(total)

    def _from_total(self):
        self.rate.configure(state="normal"); self.rate.delete(0, "end")            # a typed lump sum replaces the per-meter rate
        if not self.pieces:
            self.rate.configure(state="disabled")
        try:
            self._show(to_paisa(self.total.get().strip() or "0"))
        except Exception:  # noqa: BLE001
            self._show(0)

    def _show(self, total):
        p = js.invoice_preview(self.cost, total, self.advance)
        if total <= 0:
            self.profit.configure(text="Enter a price to see the profit.", text_color=T.MUTED); self.adv.configure(text=""); return
        m = p["margin_bps"]
        txt = f"Profit {T.money(p['profit_paisa'])}" + (f"  ({T.pct_bps(m)} of the invoice)" if m is not None else "") + ("  at the locked cost" if self.locked and self.pieces else "")
        self.profit.configure(text=txt if p["profit_paisa"] >= 0 else f"Loss {T.money(-p['profit_paisa'])}  -  below the cost of this entry", text_color=T.GOOD if p["profit_paisa"] >= 0 else T.BAD)
        if self.advance > 0:
            self.adv.configure(text=f"Advance held for {self.job['customer']}: {T.money(self.advance)}\nApplied automatically: {T.money(p['advance_applied_paisa'])}   ->   "
                                    f"still to collect: {T.money(p['due_paisa'])}")
        else:
            self.adv.configure(text=f"No advance held for this customer. The full {T.money(total)} will be due.")

    def submit(self):
        self.go_btn.configure(state="disabled")
        try:
            rate = self.rate.get().strip() if self.pieces else ""
            res = js.invoice_job(self.conn, self.user, self.job["id"], invoice_date=self.on.get(), rate=rate or None, total=None if rate else (self.total.get().strip() or None))
        except Exception as e:  # noqa: BLE001
            self.err.configure(text=str(e) or type(e).__name__); self.go_btn.configure(state="normal"); return
        self.destroy(); self.on_done()


class MachinesPage(Page):
    title = "Machines"
    subtitle = "Electricity: add a machine's hourly rate for ESTIMATES, or meter readings + the real FESCO bill for actual cost."

    def build(self):
        self.tabs = make_tabs(self.body)
        self.tabs.pack(fill="both", expand=True)
        a, b, c, d = (self.tabs.add(n) for n in ("Machines", "Runs", "Meter readings", "Electricity bills"))
        bar = ctk.CTkFrame(a, fg_color="transparent"); bar.pack(fill="x", pady=(0, 6))
        self.button(bar, "+ Add machine", self.add, "manage_machines")
        self.button(bar, "Set hourly rate", self.rate, "manage_machines", T.PURPLE)
        self.t = DataTable(a, [Col("name", "Machine", 200), Col("kind", "Kind", 90), Col("rate", "Hourly electricity rate (estimate)", 240, None, "e"), Col("note", "Status", 260)])
        self.t.pack(fill="both", expand=True)
        self.runs = DataTable(b, [Col("job_no", "Job", 90), Col("machine", "Machine", 150), Col("started_at", "Start", 140), Col("ended_at", "End", 140), Col("minutes", "Run min", 80, None, "e"),
                                  Col("downtime_minutes", "Downtime", 80, None, "e"), Col("meters_milli", "Meters", 100, lambda v: T.qty(v) if v else "-", "e")])
        self.runs.pack(fill="both", expand=True)
        bar = ctk.CTkFrame(c, fg_color="transparent"); bar.pack(fill="x", pady=(0, 6))
        self.button(bar, "+ Add reading", self.reading, "manage_machines")
        self.rd = DataTable(c, [Col("machine", "Machine", 160), Col("read_at", "Reading date/time", 160), Col("kwh_milli", "Cumulative kWh", 130, T.qty, "e")])
        self.rd.pack(fill="both", expand=True)
        bar = ctk.CTkFrame(d, fg_color="transparent"); bar.pack(fill="x", pady=(0, 6))
        self.button(bar, "+ Record bill", self.bill, "manage_machines")
        self.bl = DataTable(d, [Col("provider", "Provider", 80), Col("period_start", "From", 100), Col("period_end", "To", 100), Col("units_kwh_milli", "Units (kWh)", 110, T.qty, "e"),
                                Col("total_amount_paisa", "Bill total", 130, T.money, "e"), Col("rate", "Effective Rs/kWh", 120, None, "e"), Col("reference", "Reference", 120)])
        self.bl.pack(fill="both", expand=True)

    def refresh(self):
        rows = []
        for m in q.machines(self.conn):
            rows.append(dict(m, rate=f"{T.money(m['hourly_rate_paisa'])} / hour" if m["hourly_rate_paisa"] is not None else "not set",
                             note="electricity cost will be estimated" if m["hourly_rate_paisa"] is not None else "no rate: runs get NO electricity cost until set (flagged)"))
        self.t.set_rows(rows)
        self.runs.set_rows(q.machine_runs(self.conn)); self.rd.set_rows(q.meter_readings(self.conn))
        self.bl.set_rows([dict(b, rate=f"{Decimal(b['total_amount_paisa']) * 10 / b['units_kwh_milli']:.2f}") for b in q.electricity_bills(self.conn)])

    def _mach(self):
        return [(m["name"], m["id"]) for m in q.machines(self.conn)]

    def add(self):
        self.form("Add machine", [Field("n", "Name"), Field("k", "Kind", "choice", [("Heating", "heating"), ("Printing", "printing"), ("Other", "other")]), Field("r", "Hourly electricity rate (Rs, optional)")],
                  lambda v: ms.add_machine(self.conn, self.user, v["n"], v["k"], v["r"] or None))

    def rate(self):
        r = self.t.selected()
        if self.need(r, "a machine"):
            self.form(f"Hourly rate - {r['name']}", [Field("r", "Rs per hour of running", "money", default="" if r["hourly_rate_paisa"] is None else str(from_paisa(r["hourly_rate_paisa"])))],
                      lambda v: ms.set_hourly_rate(self.conn, self.user, r["id"], v["r"]), intro="Used to ESTIMATE electricity cost per job run. Leave blank to clear.")

    def reading(self):
        sel = self.t.selected()                                                   # the machine selected on the Machines tab starts selected here
        self.form("Add meter reading", [Field("m", "Machine", "choice", self._mach(), sel["id"] if sel else ""), Field("at", "Date/time", default=date.today().isoformat()), Field("k", "Cumulative kWh on the meter")],
                  lambda v: ms.add_meter_reading(self.conn, self.user, v["m"], v["at"], v["k"]))

    def bill(self):
        self.form("Record electricity bill", [Field("a", "Period start", "date"), Field("b", "Period end", "date"), Field("u", "Units billed (kWh)"), Field("t", "Total amount billed (Rs)", "money", hint="Include taxes and fixed charges as billed."), Field("r", "Bill reference")],
                  lambda v: ms.add_electricity_bill(self.conn, self.user, period_start=v["a"], period_end=v["b"], units_kwh=v["u"], total_amount=v["t"], reference=v["r"]),
                  intro="This is a reference for meter-based costing. Record the payment itself on the Expenses page.")
