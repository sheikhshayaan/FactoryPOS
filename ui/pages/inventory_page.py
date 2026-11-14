"""Inventory & Purchases: stock position, receiving (price per unit -> total), wastage/adjustments, ledger.
The unit follows the material: cloth in meters, ink in litres, paper in rolls (and meters)."""
from __future__ import annotations

import re
from datetime import date
from tkinter import filedialog

import customtkinter as ctk

from reports import exporters
from services import inventory_service as inv, queries as q
from services.units import from_milli, mul_div_round, to_milli
from ui import theme as T
from ui.base import Page
from ui.widgets import Col, DataTable, Field, make_tabs, show_error, show_info, surface, Stat

UNITS = [("Meter (m)", "m"), ("Litre (L)", "L"), ("Millilitre (ml)", "ml"), ("Roll", "roll"), ("Piece (pcs)", "pcs"), ("Kilogram (kg)", "kg"), ("Gram (g)", "g")]
UNIT_WORD = {"m": "meter", "ml": "ml", "roll": "roll", "pcs": "piece", "kg": "kg", "g": "gram", "L": "litre", "l": "litre"}
UNIT_PLURAL = {"m": "meters", "ml": "ml", "roll": "rolls", "pcs": "pieces", "kg": "kg", "g": "grams", "L": "litres", "l": "litres"}
CATEGORIES = [("Fabric (kapra)  -  counted in meters", "fabric"), ("Ink  -  counted in litres (L)", "ink"), ("Paper  -  counted in rolls and meters", "paper"), ("Other (pieces, kg ...)", "consumable")]


def parse_rolls(text: str) -> list[str]:
    """'1000,1000,500' or '20x1000, 5x500' -> list of roll lengths (meters, as strings). Legacy roll-tracked materials only."""
    out = []
    for part in re.split(r"[,\s;]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*[x*]\s*([\d.]+)", part, re.I)
        if m:
            out += [m.group(2)] * int(m.group(1))
        else:
            out.append(part)
    return out


def _rate(cost_paisa: int, qty_milli: int):
    """Price per ONE unit in paisa (round-half-up), or None when there is nothing to divide by."""
    return mul_div_round(cost_paisa, 1000, qty_milli) if qty_milli > 0 and cost_paisa > 0 else None


class InventoryPage(Page):
    title = "Inventory & Purchases"
    subtitle = "Cloth is counted in meters, ink in litres and paper in rolls (with the meters shown too). Type the price of ONE unit when stock arrives - the total is worked out for you."

    def build(self):
        self.tabs = make_tabs(self.body)
        self.tabs.pack(fill="both", expand=True)
        st, rc, lg = self.tabs.add("Stock"), self.tabs.add("Receipts"), self.tabs.add("Ledger")
        # figures
        self.figs = {}
        strip = ctk.CTkFrame(st, fg_color="transparent"); strip.pack(fill="x", pady=(0, 10))
        for i, (k, t) in enumerate([("value", "Stock value"), ("count", "Materials"), ("low", "Low on stock")]):
            strip.grid_columnconfigure(i, weight=1, uniform="s")
            box = surface(strip); box.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
            self.figs[k] = Stat(box, t, "-"); self.figs[k].pack(fill="x", padx=18, pady=12)
        bar = ctk.CTkFrame(st, fg_color="transparent"); bar.pack(fill="x", pady=(0, 6))
        self.button(bar, "+ New material", self.new_material, "receive_stock")
        self.button(bar, "Receive stock", self.receive, "receive_stock", T.GREEN)
        self.button(bar, "Edit", self.edit_material, "receive_stock")
        self.button(bar, "Delete", self.delete_material, "receive_stock", T.RED)
        bar2 = ctk.CTkFrame(st, fg_color="transparent"); bar2.pack(fill="x", pady=(0, 8))
        self.button(bar2, "Wastage / adjustment / return", self.issue, "adjust_stock")
        self.button(bar2, "Export to Excel", self.export, None)
        self.low_only = ctk.CTkSwitch(bar2, text="Low stock only", command=self.refresh, font=T.font(14), text_color=T.TEXT)
        self.low_only.pack(side="left", padx=14)
        self.t = DataTable(st, [Col("name", "Material", 120), Col("category", "Kind", 60), Col("qty", "In stock", 150, None, "e"), Col("avg", "Avg cost/unit", 105, None, "e"),
                                Col("last", "Last price", 108, None, "e"), Col("value", "Value", 100, None, "e"), Col("cust", "Customer's", 92, None, "e"), Col("flag", "Status", 55)],
                           row_tags=lambda r: "bad" if r["flag"] == "LOW" else None, bold=True)
        self.t.pack(fill="both", expand=True)
        rbar = ctk.CTkFrame(rc, fg_color="transparent"); rbar.pack(fill="x", pady=(0, 6))
        self.button(rbar, "Undo receipt", self.undo_receipt, "adjust_stock", T.RED)
        self.rec = DataTable(rc, [Col("received_on", "Date", 90), Col("material", "Material", 120), Col("got", "Received", 150, None, "e"), Col("left", "Left", 150, None, "e"),
                                  Col("rate", "Price / unit", 100, None, "e"), Col("total_cost_paisa", "Total cost", 115, T.money, "e"), Col("from_", "From", 120), Col("state", "Status", 90)], bold=True)
        self.rec.pack(fill="both", expand=True)
        self.led = DataTable(lg, [Col("id", "#", 55), Col("created_at", "When", 130, T.when), Col("material", "Material", 150), Col("movement_type", "Type", 110), Col("ownership", "Owner", 80),
                                  Col("qty", "Qty", 100, None, "e"), Col("value_paisa", "Value", 110, T.money, "e"), Col("ref", "Reference", 120), Col("reason", "Reason", 160), Col("user", "By", 80)], page_size=60, bold=True)
        self.led.pack(fill="both", expand=True)

    def refresh(self):
        mats = q.materials(self.conn)
        last = q.last_purchase_prices(self.conn)
        rows, low_n = [], 0
        for m in mats:
            low = m["reorder_level_milli"] > 0 and m["qty_milli"] <= m["reorder_level_milli"]
            low_n += low
            avg = _rate(m["value_paisa"], m["qty_milli"])
            lp = last.get(m["id"])
            lastp = _rate(lp[0], lp[1]) if lp else None
            rows.append(dict(m, qty=T.qty_full(m["qty_milli"], m["base_unit"], m["roll_length_milli"]), avg="-" if avg is None else T.money(avg), last="-" if lastp is None else T.money(lastp),
                             value=T.money(m["value_paisa"]), cust=T.qty(m["cust_milli"], m["base_unit"]) if m["cust_milli"] else "-", flag="LOW" if low else "ok"))
        self.mats = {m["id"]: m for m in mats}
        self.figs["value"].set(T.money(sum(m["value_paisa"] for m in mats)))
        self.figs["count"].set(str(len(mats)))
        self.figs["low"].set(str(low_n), "", "bad" if low_n else None)
        self.t.set_rows([r for r in rows if r["flag"] == "LOW"] if self.low_only.get() else rows)
        rec = []
        for r in q.stock_receipts(self.conn):
            state = "Undone" if r["undone"] else "In stock" if r["qty_remaining_milli"] == r["qty_received_milli"] else "Partly used" if r["qty_remaining_milli"] else "Used up"
            rt = _rate(r["total_cost_paisa"], r["qty_received_milli"])
            rec.append(dict(r, got=T.qty_full(r["qty_received_milli"], r["base_unit"], r["roll_length_milli"]), left=T.qty_full(r["qty_remaining_milli"], r["base_unit"], r["roll_length_milli"]), rate="-" if rt is None else T.money(rt),
                            from_=(r["supplier"] or (f"{r['customer']} (customer)" if r["customer"] else "-")), state=state))
        self.rec.set_rows(rec)
        self.led.set_rows([dict(r, qty=T.qty(r["qty_milli"]), ref=f"{r['ref_type'] or ''} {r['ref_id'] or ''}".strip()) for r in q.movements(self.conn, limit=2000)])

    # ------------------------------------------------------------------ materials
    @staticmethod
    def _unit_live(dlg, locked_history=False):
        """Category decides the unit: fabric m, ink L, paper roll. Only 'Other' lets the person pick. Paper also asks how many meters one roll has."""
        def live(v, d):
            paper = v["cat"] == "paper"
            d.show("rl", paper)                                   # meters in one roll: paper only
            d.show("unit", v["cat"] == "consumable")              # the unit is fixed for fabric, ink and paper, so it is only asked for 'Other'
            d.lock("rl", not paper)
            if paper and not v["rl"]:
                d.set_value("rl", inv.DEFAULT_ROLL_METERS)
            elif not paper and v["rl"]:
                d.set_value("rl", "")
            fixed = inv.CATEGORY_UNITS.get(v["cat"])
            if fixed:
                d.set_value("unit", fixed); d.lock("unit", True)
            else:
                d.lock("unit", locked_history)
            unit = fixed or v["unit"] or "pcs"
            d.set_label("reorder", f"Alert me when stock falls to (in {UNIT_PLURAL.get(unit, unit)})")
            if locked_history:
                d.lock("cat", True)
        return live

    def new_material(self):
        def submit(v):
            inv.add_material(self.conn, self.user, v["name"], v["cat"], v["unit"], reorder_level=v["reorder"] or "0", roll_length=v["rl"] or None)
        self.form("New material", [Field("name", "Name"), Field("cat", "What kind of material is it?", "choice", CATEGORIES, "fabric"),
                                   Field("unit", "Counted in", "choice", UNITS, "m", hint="Chosen for you. Only 'Other' materials can pick their own unit."),
                                   Field("rl", "Meters in one roll", default="", hint="Usually 300. Stock arrives in rolls; this turns rolls into meters and back."),
                                   Field("reorder", "Alert me when stock falls to", default="0", hint="Optional. Shows a LOW flag and a count on this page.")], submit,
                  submit_text="Add material", live=self._unit_live(None))

    def edit_material(self):
        m = self._mat()
        if not m:
            return
        history = inv.material_has_history(self.conn, m["id"])

        def submit(v):
            inv.update_material(self.conn, self.user, m["id"], name=v["name"], category=v["cat"], base_unit=v["unit"], reorder_level=v["reorder"] or "0",
                                roll_length=v["rl"] or None)
        self.form(f"Edit {m['name']}", [Field("name", "Name", default=m["name"]), Field("cat", "What kind of material is it?", "choice", CATEGORIES, m["category"]),
                                        Field("unit", "Counted in", "choice", UNITS + ([] if m["base_unit"] in dict((u, 0) for _l, u in UNITS) else [(m["base_unit"], m["base_unit"])]), m["base_unit"],
                                              hint="Kind and unit are locked once stock has been received." if history else "Chosen for you from the kind of material."),
                                        Field("rl", "Meters in one roll", default=T.qty(m["roll_length_milli"]) if m["roll_length_milli"] else "",
                                              hint="Changing this changes the meters shown for the paper already in stock (the rolls stay the same)."),
                                        Field("reorder", "Alert me when stock falls to", default=T.qty(m["reorder_level_milli"]))], submit, submit_text="Save changes",
                  live=self._unit_live(None, history))

    def _mat(self):
        r = self.t.selected()
        return r if self.need(r, "a material") else None

    def delete_material(self):
        m = self._mat()
        if not m:
            return
        if not self.confirm("Delete material", f"Delete {m['name']}?\n\nIt can only be deleted when no stock is left. A material that was used before is hidden, "
                            "not erased, so the stock history stays correct.", yes="Delete", danger=True):
            return
        res = self.guard(lambda: inv.delete_material(self.conn, self.user, m["id"]))
        if res == "archived":
            show_info(self, "Material hidden", f"{m['name']} has stock history, so it was hidden from the lists instead of erased.")

    # ------------------------------------------------------------------ receiving
    KINDS = [("Fabric (kapra)", "fabric"), ("Ink", "ink"), ("Paper", "paper"), ("Other", "consumable")]

    def _picked(self, mats, kinds):
        """(kind, material id) to start a form on: the material selected in the Stock table, else the first kind (nothing is forced)."""
        r = self.t.selected()
        m = self.mats.get(r["id"]) if r else None
        if m is not None and any(k[1] == m["category"] for k in kinds):
            return m["category"], m["id"]
        return kinds[0][1], None

    def _kind_choices(self, mats):
        """The kinds that have at least one material, so the list never offers an empty choice."""
        have = {m["category"] for m in mats}
        return [k for k in self.KINDS if k[1] in have] or self.KINDS[:1]

    def _mat_choices(self, mats, kind):
        out = [(f"{m['name']}  ({UNIT_WORD.get(m['base_unit'], m['base_unit'])})", m["id"]) for m in mats if m["category"] == kind]
        return out or [("- add this kind of material first -", None)]

    def receive(self):
        mats = list(self.mats.values())
        if not mats:
            return self.need(None, "a material (create one first)")
        sup = [("- none -", None)] + [(s["name"], s["id"]) for s in q.suppliers(self.conn)]
        cus = [(c["name"], c["id"]) for c in q.customers(self.conn)] or [("- add a customer first -", None)]
        kinds = self._kind_choices(mats)

        def qty_of(m, text):
            if m["tracks_rolls"]:                                     # older roll-by-roll materials: the quantity is the sum of the roll lengths
                return sum((from_milli(to_milli(x)) for x in parse_rolls(text)), from_milli(0))
            return text

        seen = {"kind": None, "mat": None}

        def live(v, d):
            if seen["kind"] != v["kind"]:                             # a different kind was picked: list only that kind's materials
                seen["kind"] = v["kind"]
                d.set_choices("mat", self._mat_choices(mats, v["kind"]))
                v = d.values()
            if v["mat"] is None:
                for k in ("rl", "price", "cost"):
                    d.show(k, False)
                return
            m = self.mats[v["mat"]]
            u = m["base_unit"]
            paper = bool(m["roll_length_milli"]) and m["category"] == "paper"
            theirs = v["own"] == "customer"
            d.show("cus", theirs)
            for k in ("price", "cost", "sup", "pay"):                 # the customer's own stock is not bought: no price, supplier or payment
                d.show(k, not theirs)
            d.show("rl", paper)                                       # meters in one roll: paper only
            d.set_label("qty", f"Quantity received (in {UNIT_PLURAL.get(u, u)})")
            d.set_label("price", f"Price of one {UNIT_WORD.get(u, u)} (Rs)")
            if seen["mat"] != v["mat"]:                               # a different material was picked: show ITS roll length
                seen["mat"] = v["mat"]
                d.set_value("rl", T.qty(m["roll_length_milli"]) if paper else "")
                v = dict(v, rl=T.qty(m["roll_length_milli"]) if paper else "")
            if paper:
                try:
                    rolls = to_milli(v["qty"] or "0")
                    per = to_milli(v["rl"] or "0")
                    d.set_hint("rl", f"{T.qty(rolls, 'roll')} x {T.meters(per)} = {T.meters(mul_div_round(rolls, per, 1000))} of paper" if rolls and per
                               else "How many meters one roll has (usually 300).")
                except Exception:  # noqa: BLE001 - half-typed numbers
                    d.set_hint("rl", "Type a number, for example 300.", bad=True)
            if theirs:
                return
            try:
                total = inv.cost_of(qty_of(m, v["qty"] or "0"), v["price"] or "0")
            except Exception as e:  # noqa: BLE001 - half-typed numbers
                d.set_value("cost", "-")
                return d.set_hint("cost", str(e) if v["qty"] and v["price"] else "Type the quantity and the price of one unit.", bad=bool(v["qty"] and v["price"]))
            d.set_value("cost", T.money(total))
            d.set_hint("cost", f"{qty_of(m, v['qty'])} {UNIT_PLURAL.get(u, u)}  x  Rs {v['price']}  =  {T.money(total)}" if total else "")

        def submit(v):
            if v["mat"] is None:
                raise ValueError("Add this kind of material on the Inventory page first.")
            m = self.mats[v["mat"]]
            theirs = v["own"] == "customer"
            if theirs and v["cus"] is None:
                raise ValueError("Choose the customer whose stock this is.")
            kw = dict(material_id=v["mat"], received_on=v["on"], supplier_id=None if theirs else v["sup"], ownership=v["own"], customer_id=v["cus"] if theirs else None,
                      payment_status=None if theirs else v["pay"], movement_type="purchase")
            price = "0" if theirs else v["price"]
            if price == "":
                raise ValueError("Type the price of one unit (use 0 only for stock you did not pay for).")
            if m["tracks_rolls"]:
                inv.receive_stock(self.conn, self.user, rolls=parse_rolls(v["qty"]), unit_price=price, **kw)
            else:
                inv.receive_stock(self.conn, self.user, qty=v["qty"], unit_price=price, roll_length=(v["rl"] or None) if m["category"] == "paper" else None, **kw)
        first, pick = self._picked(mats, kinds)
        self.form("Receive stock", [
            Field("kind", "What are you receiving?", "choice", kinds, first),
            Field("mat", "Material", "choice", self._mat_choices(mats, first), pick),
            Field("own", "Whose stock is it?", "choice", [("The factory's (bought)", "factory"), ("Customer's own", "customer")]),
            Field("cus", "Customer", "choice", cus),
            Field("qty", "Quantity received"),
            Field("rl", "Meters in one roll", default="", live_hint=True),
            Field("price", "Price of one unit (Rs)", "money"),
            Field("cost", "Total cost (worked out)", "readonly", default="-", live_hint=True),
            Field("sup", "Supplier", "choice", sup), Field("on", "Date received", "date"),
            Field("pay", "Payment", "choice", [("Not paid yet (we owe the supplier)", "unpaid"), ("Paid in cash", "paid"), ("Partly paid (still owed in full until you record payments)", "partial")])],
            submit, submit_text="Receive stock", live=live)

    def issue(self):
        mats = list(self.mats.values())
        if not mats:
            return self.need(None, "a material")
        sup = [(s["name"], s["id"]) for s in q.suppliers(self.conn)] or [("- add a supplier first -", None)]
        cus = [(c["name"], c["id"]) for c in q.customers(self.conn)] or [("- add a customer first -", None)]
        kinds = self._kind_choices(mats)
        seen = {"kind": None}

        def live(v, d):
            if seen["kind"] != v["kind"]:
                seen["kind"] = v["kind"]
                d.set_choices("mat", self._mat_choices(mats, v["kind"]))
                v = d.values()
            d.show("cus", v["own"] == "customer")
            d.show("sup", v["type"] == "supplier_return")
            if v["mat"] is None:
                return d.show("unit", False)
            m = self.mats[v["mat"]]
            u = m["base_unit"]
            paper = bool(m["roll_length_milli"]) and m["category"] == "paper"
            d.show("unit", paper)
            if not paper:
                d.set_value("unit", "roll")
                d.set_label("qty", f"Quantity (in {UNIT_PLURAL.get(u, u)})")
                return d.set_hint("qty", "")
            d.set_label("unit", "Count the paper in")
            in_m = v["unit"] == "m"
            d.set_label("qty", "Quantity (in meters)" if in_m else "Quantity (in rolls - a part roll such as 0.5 is fine)")
            try:
                if not (v["qty"] or "").strip():
                    return d.set_hint("qty", f"1 roll = {T.meters(m['roll_length_milli'])}")
                rolls = inv.meters_to_rolls_milli(v["qty"], m["roll_length_milli"]) if in_m else to_milli(v["qty"])
                d.set_hint("qty", f"= {T.qty(rolls, 'roll')} ({T.meters(mul_div_round(rolls, m['roll_length_milli'], 1000))})   -   {T.qty(m['qty_milli'], 'roll')} in stock")
            except Exception as e:  # noqa: BLE001 - half-typed numbers
                d.set_hint("qty", str(e), bad=True)

        def submit(v):
            if v["mat"] is None:
                raise ValueError("Add this kind of material on the Inventory page first.")
            m = self.mats[v["mat"]]
            theirs = v["own"] == "customer"
            if theirs and v["cus"] is None:
                raise ValueError("Choose the customer whose property this is.")
            ret = v["type"] == "supplier_return"
            if ret and v["sup"] is None:
                raise ValueError("Choose the supplier it goes back to.")
            inv.issue_stock(self.conn, self.user, material_id=v["mat"], qty=v["qty"], movement_type=v["type"], ownership=v["own"], customer_id=v["cus"] if theirs else None,
                            reason=v["reason"], on_date=v["on"], supplier_id=v["sup"] if ret else None, unit="m" if (m["category"] == "paper" and v["unit"] == "m") else None)
        first, pick = self._picked(mats, kinds)
        self.form("Wastage / adjustment / supplier return", [
            Field("kind", "What is it?", "choice", kinds, first),
            Field("mat", "Material", "choice", self._mat_choices(mats, first), pick),
            Field("type", "Type", "choice", [("Wastage", "wastage"), ("Stock adjustment (loss)", "adjustment_out"), ("Return to supplier", "supplier_return")]),
            Field("sup", "Supplier it goes back to", "choice", sup),
            Field("own", "Whose stock is it?", "choice", [("The factory's", "factory"), ("Customer's own", "customer")]),
            Field("cus", "Customer", "choice", cus),
            Field("unit", "Count the paper in", "choice", [("Rolls", "roll"), ("Meters", "m")], "roll"),
            Field("qty", "Quantity", live_hint=True),
            Field("on", "Date", "date"), Field("reason", "Reason (required)", "multiline")], submit, submit_text="Post",
            intro="Stock leaves at average cost. Posted movements can't be edited or deleted.", live=live)

    # ------------------------------------------------------------------ receipts / export
    def undo_receipt(self):
        r = self.rec.selected()
        if not self.need(r, "a receipt"):
            return

        def submit(v):
            if not self.confirm("Undo receipt", f"Take {r['got']} of {r['material']} out again ({T.money(r['total_cost_paisa'])})?\n\n"
                                "The stock and the accounts go back to how they were before this receipt.", yes="Undo receipt", danger=True):
                raise ValueError("Cancelled.")
            inv.undo_receipt(self.conn, self.user, r["id"], v["reason"], on_date=v["on"])
        self.form(f"Undo receipt of {r['material']}", [Field("on", "Date", "date"), Field("reason", "Why is it being undone?", "multiline")], submit, submit_text="Review...",
                  intro="Only works while none of this stock has been used and nothing else was issued from the material since. Otherwise use Wastage / adjustment.")

    def export(self):
        f = filedialog.asksaveasfilename(defaultextension=".xlsx", initialfile=f"stock_{date.today().isoformat()}.xlsx", filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv")])
        if not f:
            return
        try:
            headers, rows = q.stock_sheet(self.conn)
            exporters.export_rows(f, "Stock", headers, rows, subtitle=f"As of {date.today().isoformat()}")
        except Exception as e:  # noqa: BLE001
            return show_error(self, str(e))
        show_info(self, "Saved", f)
