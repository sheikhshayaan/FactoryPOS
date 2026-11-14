"""Cost Calculator: what one linear metre of printing costs. Every figure is typed in by the user; nothing is read from the books.

Layout follows the owner's reference: three result cards, a "Production inputs" card with a Calculate button, the step-by-step breakdown,
the cost split and the ink-use scenarios. It re-arranges itself to the window width (three columns -> stacked)."""
from __future__ import annotations

import json
import logging
import tkinter as tk

import customtkinter as ctk
from matplotlib.figure import Figure

from services import cost_calculator as cc, settings_service as st
from services.units import from_paisa
from ui import smooth, theme as T
from ui.base import Page
from ui.widgets import Col, DataTable, make_button, show_error, surface

log = logging.getLogger("fms.ui")

PART_COLOURS = {"Paper": "#3b82f6", "Ink": "#a855f7", "Labour": "#10b981", "Electricity": "#f59e0b"}   # one colour per cost part, everywhere
TEAL, GREEN, INDIGO, AMBER = T.CASH, T.GOOD, T.ACCENT, T.WARN
# key -> (label, prefix inside the box, suffix inside the box)
UI = {"paper": ("Paper cost per linear metre", "Rs", ""), "ink_price": ("Ink price per litre", "Rs", ""), "ink_use": ("Ink consumption", "", "ml/m²"),
      "width": ("Print width", "", "m"), "power": ("Machine power", "", "kW"), "per_day": ("Production per day", "", "m"),
      "days": ("Working days per month", "", "days"), "hours": ("Machine hours per day", "", "hours"),
      "rate": ("Electricity rate (per kWh)", "Rs", ""), "labour": ("Labour cost per month", "Rs", "")}
ORDER = [["paper"], ["ink_price", "ink_use"], ["width", "power"], ["per_day", "days"], ["hours", "rate"], ["labour"]]
INK_ROWS = [("Ink price per ml", lambda r: cc.money(r["ink_per_ml"])), ("Area per linear metre", lambda r: cc.num(r["width"]) + " m²"),
            ("Ink per linear metre", lambda r: cc.num(r["ink_ml_per_m"]) + " ml")]
RUN_ROWS = [("Monthly production", lambda r: cc.num(r["monthly_m"], 0) + " m"), ("Labour cost per metre", lambda r: cc.money(r["labour"])),
            ("Power cost per hour", lambda r: cc.money(r["power_per_hour"])), ("Production per hour", lambda r: cc.num(r["per_hour"]) + " m")]


def _ghost(cv: tk.Canvas, kind: str, tint: str):
    """A big faded drawing in the corner of a result card (a ruler, a calendar)."""
    bg = T.rc(T.CARD)
    if kind == "ruler":
        cv.create_rectangle(8, 38, 122, 80, fill=tint, outline="")
        for i in range(8):
            x = 20 + i * 13
            cv.create_line(x, 38, x, 62 if i % 2 == 0 else 52, fill=bg, width=3)
    else:
        cv.create_rectangle(22, 14, 112, 92, fill=tint, outline="")
        cv.create_rectangle(22, 14, 112, 34, fill=tint, outline=bg, width=2)
        for x in (44, 90):
            cv.create_rectangle(x - 3, 6, x + 3, 22, fill=tint, outline=bg, width=2)
        for i in range(3):
            for j in range(2):
                cv.create_rectangle(32 + i * 26, 44 + j * 24, 48 + i * 26, 60 + j * 24, fill=bg, outline="")


class Hero(ctk.CTkFrame):
    """One big result card: heading, the number, its unit, a one-line note, a faded drawing in the corner."""

    def __init__(self, parent, title, colour, unit, ghost, tint):
        super().__init__(parent, fg_color=T.CARD, corner_radius=16, border_width=1, border_color=T.BORDER)
        self.cv = tk.Canvas(self, width=130, height=100, highlightthickness=0, bd=0, bg=T.rc(T.CARD))
        _ghost(self.cv, ghost, T.rc(tint))
        inner = self.inner = ctk.CTkFrame(self, fg_color="transparent"); inner.pack(side="left", anchor="nw", padx=24, pady=(20, 18))
        ctk.CTkLabel(inner, text=title, font=T.font(12, "bold"), text_color=T.MUTED, anchor="w").pack(anchor="w")
        row = ctk.CTkFrame(inner, fg_color="transparent"); row.pack(anchor="w", pady=(6, 2))
        self.size, self.unit_text = 32, unit
        self.value = ctk.CTkLabel(row, text="-", font=T.font(32, "bold"), text_color=colour); self.value.pack(side="left")
        ctk.CTkLabel(row, text=unit, font=T.font(13), text_color=T.MUTED).pack(side="left", padx=(8, 0), pady=(10, 0))
        self.note = ctk.CTkLabel(inner, text="", font=T.font(13), text_color=T.MUTED, anchor="w", justify="left"); self.note.pack(anchor="w", pady=(2, 0))


    def autosize(self, scale: float):
        """The three cards always share one row, so the big number shrinks to fit its card (never cut off)."""
        w = self.winfo_width() / scale
        if w < 120:
            return
        text = self.value.cget("text")
        size = int((w - 48 - len(self.unit_text) * 7 - 16) / (max(len(text), 1) * 0.66))
        size = max(15, min(32, size))
        if size != self.size:
            self.size = size
            self.value.configure(font=T.font(size, "bold"))

    def fit(self):
        """The faded drawing is shown only when it fits beside the text; it never sits under the number."""
        try:
            room = self.winfo_width() >= self.inner.winfo_reqwidth() + 48 + 150
        except tk.TclError:
            return
        if room and not self.cv.winfo_ismapped():
            self.cv.place(relx=1, rely=1, x=-16, y=-12, anchor="se")
        elif not room and self.cv.winfo_ismapped():
            self.cv.place_forget()


class CostCalculatorPage(Page):
    title = "Cost Calculator"
    subtitle = "Cost of one linear metre of printing. Type your own figures and press Calculate cost; nothing is taken from your books or recorded in them."

    # ------------------------------------------------------------------ layout
    def build(self):
        self.vars = {k: ctk.StringVar(value="") for k, *_ in cc.FIELDS}
        self.entries, self.boxes, self.share, self.cells = {}, {}, {}, {}
        self.result, self.canvas, self._shown, self._layout_key, self._job = None, None, None, None, None
        self._file = self.ctx.paths.root / "cost_calculator.json"
        self._load()
        self.scroll = ctk.CTkScrollableFrame(self.body, fg_color="transparent"); self.scroll.pack(fill="both", expand=True)
        S = self.scroll
        # 1. three result cards
        self.hero = ctk.CTkFrame(S, fg_color="transparent"); self.hero.pack(fill="x", pady=(2, 0))
        specs = [("metre", "TOTAL COST PER LINEAR METRE", TEAL, "/ linear m", "ruler", ("#e2f0f2", "#18292f")),
                 ("day", "DAILY PRODUCTION COST", GREEN, "/ day", "calendar", ("#e1f1ea", "#16302a")),
                 ("month", "MONTHLY PRODUCTION COST", INDIGO, "/ month", "calendar", ("#e6e8f6", "#1d2142"))]
        self.hero_cards = []
        for k, t, col, unit, ghost, tint in specs:
            h = Hero(self.hero, t, col, unit, ghost, tint); self.boxes[k] = h; self.hero_cards.append(h)
        self.boxes["metre"].note.configure(text="ⓘ  Paper, ink, labour, power")
        # 2. inputs (left) and workings (right)
        self.main = ctk.CTkFrame(S, fg_color="transparent"); self.main.pack(fill="x", pady=(18, 0))
        self.inputs = self._inputs(self.main)
        self.right = ctk.CTkFrame(self.main, fg_color="transparent")
        self.breakdown = self._breakdown(self.right)
        self.distribution = self._distribution(self.right)
        # 3. scenarios
        self.scen = self._scenarios(S)
        self.scen.pack(fill="x", pady=(18, 8))
        for v in self.vars.values():
            v.trace_add("write", lambda *_a: self._edited())
        self.body.bind("<Configure>", lambda _e: self._resize_later(), add="+")
        self._show(None)
        self._apply_layout()
        self.after(100, self._fit_cards)
        if any(v.get() for v in self.vars.values()):
            self.calculate(quiet=True)                                     # figures remembered from last time: show their answer straight away

    def _card(self, parent, title, icon, colour, right_text="", chip=False):
        card = surface(parent, corner_radius=16)
        inner = ctk.CTkFrame(card, fg_color="transparent"); inner.pack(fill="both", expand=True, padx=22, pady=(18, 20))
        head = ctk.CTkFrame(inner, fg_color="transparent"); head.pack(fill="x")
        ctk.CTkLabel(head, text=icon, font=T.font(18, "bold"), text_color=colour).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(head, text=title, font=T.font(18, "bold"), text_color=T.TEXT, anchor="w").pack(side="left")
        tail = None
        if right_text:
            tail = ctk.CTkLabel(head, text=right_text, font=T.font(12 if chip else 13), text_color=T.MUTED,
                                **({"fg_color": T.CARD_ALT, "corner_radius": 12, "width": 110, "height": 26} if chip else {}))
            tail.pack(side="right")
        ctk.CTkFrame(inner, height=1, fg_color=T.BORDER).pack(fill="x", pady=(12, 14))
        card.inner, card.head, card.tail = inner, head, tail
        return card

    def _lock_block(self, parent):
        """The 'lock cost per meter' box: sits under the Calculate / Clear buttons inside the inputs card."""
        can = self.ctx.can("manage_invoices")
        blk = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkFrame(blk, height=1, fg_color=T.BORDER).pack(fill="x", pady=(0, 14))
        ctk.CTkLabel(blk, text="Lock cost per meter", font=T.font(15, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x")
        box = ctk.CTkFrame(blk, height=44, corner_radius=11, fg_color=T.CARD, border_width=1, border_color=T.BORDER)
        box.pack(fill="x", pady=(8, 0)); box.pack_propagate(False)
        ctk.CTkLabel(box, text="Rs", font=T.font(12), text_color=T.MUTED, width=26).pack(side="left", padx=(12, 0))
        ctk.CTkLabel(box, text="/ linear m", font=T.font(12), text_color=T.MUTED).pack(side="right", padx=(0, 12))
        self.lock_var = ctk.StringVar(value="")
        self.lock_entry = ctk.CTkEntry(box, textvariable=self.lock_var, font=T.font(16, "bold"), border_width=0, fg_color="transparent", height=38,
                                       placeholder_text="Cost per meter you decide")
        self.lock_entry.pack(side="left", fill="x", expand=True, padx=(8, 6))
        self.lock_entry.bind("<Return>", lambda _e: (self.lock_cost(), "break")[1], add="+")
        row = ctk.CTkFrame(blk, fg_color="transparent"); row.pack(fill="x", pady=(8, 0))
        row.grid_columnconfigure((0, 1), weight=1, uniform="lk")
        self.lock_btn = make_button(row, "LOCK", self.lock_cost, "primary", height=42, width=10)
        self.lock_btn.configure(font=T.font(14, "bold")); self.lock_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.unlock_btn = make_button(row, "Unlock", self.unlock_cost, "secondary", height=42, width=10)
        self.unlock_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        if not can:
            for w in (self.lock_btn, self.unlock_btn):
                w.configure(state="disabled")
            self.lock_entry.configure(state="disabled")
        self.lock_msg = ctk.CTkLabel(blk, text="", font=T.font(13, "bold"), anchor="w", justify="left", wraplength=400)
        self.lock_msg.pack(fill="x", pady=(10, 0))
        self.lock_hint = ctk.CTkLabel(blk, text="", font=T.font(12), text_color=T.MUTED, anchor="w", justify="left", wraplength=400)
        self.lock_hint.pack(fill="x", pady=(2, 0))
        if not can:
            self.lock_hint.configure(text="Only the owner and the accountant can lock the cost.")
        blk.bind("<Configure>", lambda e: [t.configure(wraplength=max(200, int(e.width / self._scale()) - 8)) for t in (self.lock_msg, self.lock_hint)], add="+")
        self._lock_show()
        return blk

    def _lock_show(self):
        cur = st.locked_cost(self.conn)
        if cur:
            self.lock_var.set(f"{from_paisa(cur):.0f}" if cur % 100 == 0 else f"{from_paisa(cur):.2f}")
            self.lock_msg.configure(text=f"Locked at {cc.money(from_paisa(cur), 2 if cur % 100 else 0)} per meter. Create invoice shows this figure.", text_color=T.GOOD)
        else:
            self.lock_msg.configure(text="Not locked. Create invoice shows the stock (inventory) cost per meter.", text_color=T.MUTED)
        self._lock_hint()

    def _lock_hint(self):
        if not self.ctx.can("manage_invoices"):
            return
        r = self.result
        self.lock_hint.configure(text=f"Calculated from your figures: {cc.money(r['total'])} per meter." if r else
                                 "Press Calculate cost to see the calculated figure here, or just type the cost you want.")

    def lock_cost(self):
        try:
            got = st.set_locked_cost(self.conn, self.user, self.lock_var.get())
        except Exception as e:  # noqa: BLE001
            return show_error(self, str(e) or type(e).__name__)
        if got is None:
            self.lock_msg.configure(text="Type the cost per meter first.", text_color=T.BAD); return
        self._lock_show()

    def unlock_cost(self):
        try:
            st.set_locked_cost(self.conn, self.user, "")
        except Exception as e:  # noqa: BLE001
            return show_error(self, str(e) or type(e).__name__)
        self.lock_var.set(""); self._lock_show()

    def _field(self, parent, key):
        label, prefix, suffix = UI[key]
        cell = ctk.CTkFrame(parent, fg_color="transparent")
        ctk.CTkLabel(cell, text=label, font=T.font(13, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x")
        box = ctk.CTkFrame(cell, height=42, corner_radius=11, fg_color=T.CARD, border_width=1, border_color=T.BORDER)
        box.pack(fill="x", pady=(4, 0)); box.pack_propagate(False)
        if prefix:
            ctk.CTkLabel(box, text=prefix, font=T.font(12), text_color=T.MUTED, width=26).pack(side="left", padx=(12, 0))
        if suffix:
            ctk.CTkLabel(box, text=suffix, font=T.font(12), text_color=T.MUTED).pack(side="right", padx=(0, 12))
        e = ctk.CTkEntry(box, textvariable=self.vars[key], font=T.font(15, "bold"), border_width=0, fg_color="transparent", height=36)
        e.pack(side="left", fill="x", expand=True, padx=(8 if prefix else 12, 6))
        e.bind("<FocusIn>", lambda _e, b=box: b.configure(border_color=T.ACCENT, border_width=2), add="+")
        e.bind("<FocusOut>", lambda _e, b=box: b.configure(border_color=T.BORDER, border_width=1), add="+")
        e.bind("<Return>", lambda _e: (self.calculate(), "break")[1], add="+")
        self.entries[key] = e
        return cell

    def _inputs(self, parent):
        card = self._card(parent, "Production inputs", "☰", TEAL, "Enter values", chip=True)
        self.pre = ctk.CTkFrame(card.inner, fg_color="transparent"); self.pre.pack(fill="x", pady=(0, 14))
        self.preset_btns = {}
        for key, (label, _vals) in cc.PRESETS.items():
            self.preset_btns[key] = make_button(self.pre, label, lambda k=key: self.apply_preset(k), "secondary", height=34, width=10)
        self.in_grid = ctk.CTkFrame(card.inner, fg_color="transparent"); self.in_grid.pack(fill="x")
        for row in ORDER:
            for k in row:
                self.cells[k] = self._field(self.in_grid, k)
        foot = ctk.CTkFrame(card.inner, fg_color="transparent"); foot.pack(fill="x", pady=(16, 0))
        ctk.CTkFrame(foot, height=1, fg_color=T.BORDER).pack(fill="x", pady=(0, 14))
        bar = ctk.CTkFrame(foot, corner_radius=12, fg_color=T.NEUTRAL_SOFT, border_width=1, border_color=TEAL); bar.pack(fill="x")
        bi = ctk.CTkFrame(bar, fg_color="transparent"); bi.pack(fill="x", padx=16, pady=10)
        left = ctk.CTkFrame(bi, fg_color="transparent"); left.pack(side="left")
        ctk.CTkLabel(left, text="TOTAL COST / METRE", font=T.font(12, "bold"), text_color=TEAL, anchor="w").pack(anchor="w")
        ctk.CTkLabel(left, text="Paper + ink + labour + electricity", font=T.font(11), text_color=T.MUTED, anchor="w").pack(anchor="w")
        self.total_left = ctk.CTkLabel(bi, text="-", font=T.font(22, "bold"), text_color=TEAL); self.total_left.pack(side="right")
        self.calc_btn = make_button(foot, "CALCULATE COST", self.calculate, "primary", height=48, width=10)       # full width, one under the other:
        self.calc_btn.configure(font=T.font(15, "bold")); self.calc_btn.pack(fill="x", pady=(12, 0))             # always fits, at any screen scaling
        self.clear_btn = make_button(foot, "CLEAR CALCULATIONS", self.clear, "secondary", height=42, width=10)
        self.clear_btn.configure(font=T.font(14, "bold")); self.clear_btn.pack(fill="x", pady=(8, 0))
        self.status = ctk.CTkLabel(foot, text="", font=T.font(13), text_color=T.MUTED, wraplength=420, justify="center")
        self.status.pack(pady=(10, 0))
        ctk.CTkLabel(foot, text="The answer updates when you press Calculate cost or Enter. Clear calculations empties every figure and the answer (a locked cost stays locked).",
                     font=T.font(12), text_color=T.MUTED, wraplength=440, justify="center").pack(pady=(2, 0))
        self.lock_card = self._lock_block(foot); self.lock_card.pack(fill="x", pady=(16, 0))      # below Calculate and Clear
        return card

    def _breakdown(self, parent):
        card = self._card(parent, "Step-by-step cost breakdown", "√", GREEN)
        self.bd_grid = ctk.CTkFrame(card.inner, fg_color="transparent"); self.bd_grid.pack(fill="x")
        self.kv, self.bd_cards = {}, []
        for title, rows, last, colour in [("INK METRICS", INK_ROWS, "Ink cost per metre", TEAL), ("PRODUCTION AND UTILITY", RUN_ROWS, "Electricity cost per metre", AMBER)]:
            c = ctk.CTkFrame(self.bd_grid, corner_radius=12, fg_color=T.NEUTRAL_SOFT, border_width=1, border_color=T.BORDER)
            ci = ctk.CTkFrame(c, fg_color="transparent"); ci.pack(fill="both", expand=True, padx=16, pady=14)
            ctk.CTkLabel(ci, text=title, font=T.font(11, "bold"), text_color=colour, anchor="w").pack(fill="x", pady=(0, 6))
            for label, f in rows:
                r = ctk.CTkFrame(ci, fg_color="transparent"); r.pack(fill="x", pady=3)
                ctk.CTkLabel(r, text=label, font=T.font(13), text_color=T.MUTED, anchor="w").pack(side="left")
                v = ctk.CTkLabel(r, text="-", font=T.font(13, "bold"), text_color=T.TEXT, anchor="e"); v.pack(side="right")
                self.kv[label] = (v, f)
            ctk.CTkFrame(ci, height=1, fg_color=T.BORDER).pack(fill="x", pady=(8, 6))
            r = ctk.CTkFrame(ci, fg_color="transparent"); r.pack(fill="x")
            ctk.CTkLabel(r, text=last, font=T.font(13, "bold"), text_color=T.TEXT, anchor="w").pack(side="left")
            v = ctk.CTkLabel(r, text="-", font=T.font(14, "bold"), text_color=colour, anchor="e"); v.pack(side="right")
            self.kv[last] = (v, (lambda r: cc.money(r["ink"])) if last.startswith("Ink") else (lambda r: cc.money(r["elec"])))
            self.bd_cards.append(c)
        bar = ctk.CTkFrame(card.inner, corner_radius=12, fg_color=T.NEUTRAL_SOFT, border_width=1, border_color=TEAL); bar.pack(fill="x", pady=(14, 0))
        bi = ctk.CTkFrame(bar, fg_color="transparent"); bi.pack(fill="x", padx=16, pady=12)
        left = ctk.CTkFrame(bi, fg_color="transparent"); left.pack(side="left")
        ctk.CTkLabel(left, text="TOTAL COST PER LINEAR METRE", font=T.font(12, "bold"), text_color=T.TEXT, anchor="w").pack(anchor="w")
        self.total_formula = ctk.CTkLabel(left, text="Paper + ink + labour + electricity", font=T.font(12), text_color=T.MUTED, anchor="w"); self.total_formula.pack(anchor="w")
        self.total_main = ctk.CTkLabel(bi, text="-", font=T.font(26, "bold"), text_color=TEAL); self.total_main.pack(side="right")
        return card

    def _distribution(self, parent):
        card = self._card(parent, "Cost distribution", "◔", PART_COLOURS["Ink"], "Percentage share per part")
        self.ds_grid = ctk.CTkFrame(card.inner, fg_color="transparent"); self.ds_grid.pack(fill="x")
        self.chart_holder = ctk.CTkFrame(self.ds_grid, fg_color="transparent", width=230, height=200)
        self.chart_holder.pack_propagate(False)
        self.share_box = ctk.CTkFrame(self.ds_grid, fg_color="transparent")
        for name, colour in PART_COLOURS.items():
            row = ctk.CTkFrame(self.share_box, corner_radius=12, fg_color=T.NEUTRAL_SOFT, border_width=1, border_color=T.BORDER); row.pack(fill="x", pady=4)
            ri = ctk.CTkFrame(row, fg_color="transparent"); ri.pack(fill="x", padx=14, pady=10)
            ctk.CTkLabel(ri, text="■", text_color=colour, font=T.font(15), width=22).pack(side="left")
            ctk.CTkLabel(ri, text=f"{name} cost", text_color=T.TEXT, font=T.font(13, "bold"), anchor="w").pack(side="left")
            pct = ctk.CTkLabel(ri, text="", text_color=T.MUTED, font=T.font(12), anchor="e"); pct.pack(side="right")
            amt = ctk.CTkLabel(ri, text="-", text_color=T.TEXT, font=T.font(13, "bold"), anchor="e"); amt.pack(side="right", padx=(0, 8))
            self.share[name] = (amt, pct)
        return card

    def _scenarios(self, parent):
        card = self._card(parent, "Ink consumption scenarios", "▦", TEAL, "Width: -", chip=True)
        self.width_chip = card.tail
        ctk.CTkLabel(card.inner, text="What the cost would be at other ink uses (10 to 40 ml/m²), with all your other figures kept. Green costs less than your figure, red costs more.",
                     font=T.font(13), text_color=T.MUTED, anchor="w", justify="left", wraplength=900).pack(fill="x", pady=(0, 8))
        self.table = DataTable(card.inner, [Col("cov", "Ink use (ml/m²)", 130), Col("ink_ml", "Ink per metre", 130, anchor="e"), Col("ink_cost", "Ink cost per metre", 160, anchor="e"),
                                            Col("total", "Total cost per metre", 170, anchor="e"), Col("per_day", "Total cost per day", 170, anchor="e"),
                                            Col("change", "Compared with your figure", 210, anchor="e")],
                               height=10, page_size=20, searchable=False, bold=True, row_tags=lambda r: r.get("tag"),
                               empty_text="Press Calculate cost to see this table.")
        self.table.pack(fill="x")
        return card

    # ------------------------------------------------------------------ responsive layout
    def _scale(self) -> float:
        try:
            return float(ctk.ScalingTracker.get_widget_scaling(self)) or 1.0
        except Exception:  # noqa: BLE001
            return 1.0

    def _resize_later(self):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(120, self._resized)

    def _resized(self):
        self._apply_layout()
        self.after_idle(self._fit_cards)                                    # after the layout pass, without forcing one here

    def _fit_presets(self):
        """The three preset buttons share one row when their text fits; otherwise they sit one under the other (never clipped)."""
        sc = self._scale()
        need = sum((len(b.cget("text")) * 9 + 34) for b in self.preset_btns.values()) + 16
        row = self.pre.winfo_width() / sc >= need if self.pre.winfo_width() > 1 else True
        if row == getattr(self, "_pre_row", None):
            return
        self._pre_row = row
        for i, b in enumerate(self.preset_btns.values()):
            b.grid_forget()
            if row:
                b.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 4, 0 if i == 2 else 4))
            else:
                b.grid(row=i, column=0, sticky="ew", pady=(0 if i == 0 else 6, 0))
        for i in range(3):
            self.pre.grid_columnconfigure(i, weight=1 if (row or i == 0) else 0, uniform="pre" if row else "")

    def _apply_layout(self):
        """Three result cards in a row and inputs beside the workings when there is room; stacked when the window is narrow."""
        self._job = None
        w = max(self.body.winfo_width() / self._scale() - 34, 320)                 # logical pixels: the same on a 100% and a 150% screen
        wide = w >= 980
        right_w = (w - 18) * 7 / 12 if wide else w
        left_w = (w - 18) * 5 / 12 if wide else w
        key = (3, wide, 2 if left_w >= 400 else 1, 2 if right_w >= 640 else 1, 2 if right_w >= 560 else 1)
        if key == self._layout_key:
            return
        self._layout_key = key
        hero_cols, _wide, pair_cols, bd_cols, ds_cols = key
        for f in (self.hero, self.main, self.in_grid, self.bd_grid, self.ds_grid):
            for c in f.winfo_children():
                c.grid_forget() if c.winfo_manager() == "grid" else None
            for i in range(4):
                f.grid_columnconfigure(i, weight=0, uniform="")
        for i, h in enumerate(self.hero_cards):
            r, c = divmod(i, hero_cols)
            h.grid(row=r, column=c, sticky="nsew", padx=(0 if c == 0 else 7, 0 if c == hero_cols - 1 else 7), pady=(0 if r == 0 else 12, 0))
        for i in range(hero_cols):
            self.hero.grid_columnconfigure(i, weight=1, uniform="hero")
        if wide:
            self.main.grid_columnconfigure(0, weight=5, uniform="main"); self.main.grid_columnconfigure(1, weight=7, uniform="main")
            self.inputs.grid(row=0, column=0, sticky="nsew", padx=(0, 9)); self.right.grid(row=0, column=1, sticky="nsew", padx=(9, 0))
        else:
            self.main.grid_columnconfigure(0, weight=1)
            self.inputs.grid(row=0, column=0, sticky="nsew"); self.right.grid(row=1, column=0, sticky="nsew", pady=(18, 0))
        self.in_grid.grid_columnconfigure(0, weight=1, uniform="in"); self.in_grid.grid_columnconfigure(1, weight=1, uniform="in") if pair_cols == 2 else None
        r = 0
        for row in ORDER:
            if pair_cols == 2:
                if len(row) == 1:
                    self.cells[row[0]].grid(row=r, column=0, columnspan=2, sticky="ew", pady=(0, 12))
                else:
                    for j, k in enumerate(row):
                        self.cells[k].grid(row=r, column=j, sticky="ew", padx=(0 if j == 0 else 7, 7 if j == 0 else 0), pady=(0, 12))
                r += 1
            else:
                for k in row:
                    self.cells[k].grid(row=r, column=0, sticky="ew", pady=(0, 12)); r += 1
        for c in self.right.winfo_children():
            c.pack_forget()
        self.breakdown.pack(fill="x"); self.distribution.pack(fill="x", pady=(18, 0))
        for i, c in enumerate(self.bd_cards):
            if bd_cols == 2:
                c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 7, 7 if i == 0 else 0))
                self.bd_grid.grid_columnconfigure(i, weight=1, uniform="bd")
            else:
                c.grid(row=i, column=0, sticky="nsew", pady=(0 if i == 0 else 10, 0)); self.bd_grid.grid_columnconfigure(0, weight=1)
        if ds_cols == 2:
            self.chart_holder.grid(row=0, column=0, sticky="n", padx=(0, 14)); self.share_box.grid(row=0, column=1, sticky="ew")
            self.ds_grid.grid_columnconfigure(1, weight=1)
        else:
            self.chart_holder.grid(row=0, column=0); self.share_box.grid(row=1, column=0, sticky="ew", pady=(8, 0)); self.ds_grid.grid_columnconfigure(0, weight=1)
        self.after(80, self._fit_cards)

    # ------------------------------------------------------------------ behaviour
    def refresh(self):
        pass

    def _values(self):
        return {k: v.get() for k, v in self.vars.items()}

    def _edited(self):
        """Typing does not change the answer (that is what Calculate cost is for), but the page says so when the answer is out of date."""
        if self.result is not None and self._shown is not None:
            stale = self._values() != self._shown
            self.status.configure(text="You changed a figure. Press Calculate cost to update the answer." if stale else "", text_color=T.WARN)

    def clear(self):
        for v in self.vars.values():
            v.set("")
        try:
            self._file.unlink()
        except OSError:
            pass
        self.result, self._shown = None, None
        self.status.configure(text="")
        self._show(None)
        self._lock_hint()                                                  # the locked cost itself stays: it is a decision, not a calculation

    def apply_preset(self, key):
        """Fill every figure from a preset and calculate. Replaces what is typed (the figures can be changed again afterwards)."""
        for k, v in cc.PRESETS[key][1].items():
            self.vars[k].set(v)
        self.calculate()

    def _load(self):
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            for k, v in data.items():
                if k in self.vars and isinstance(v, str):
                    self.vars[k].set(v)
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            self._file.write_text(json.dumps(self._values()), encoding="utf-8")
        except OSError as e:
            log.warning("could not remember the calculator figures: %s", e)

    def calculate(self, quiet: bool = False):
        values = self._values()
        try:
            r = cc.calculate(values)
        except cc.CostError as e:
            self.result, self._shown = None, None
            self._show(None)
            self._lock_hint()
            if not quiet:
                self.status.configure(text=str(e), text_color=T.MUTED if str(e).startswith("Still to fill in") else T.BAD)
            return None
        self.result, self._shown = r, values
        self._save()
        self.status.configure(text="")
        self._show(r)
        self._lock_hint()
        return r

    def _show(self, r):
        b = self.boxes
        if r is None:
            for h in b.values():
                h.value.configure(text="-")
            b["day"].note.configure(text="Based on - metres / day"); b["month"].note.configure(text="Based on - metres in total")
            self.total_left.configure(text="-"); self.total_main.configure(text="-")
            for a, p in self.share.values():
                a.configure(text="-"); p.configure(text="")
            for v, _f in self.kv.values():
                v.configure(text="-")
            self.total_formula.configure(text="Paper + ink + labour + electricity"); self.width_chip.configure(text="Width: -")
            self._donut(None); self.table.set_rows([])
            return
        for k, amount in (("metre", r["total"]), ("day", r["per_day_cost"]), ("month", r["per_month_cost"])):
            b[k].value.configure(text=cc.money(amount))
        b["day"].note.configure(text=f"Based on {cc.num(r['per_day_m'], 0)} metres / day"); b["month"].note.configure(text=f"Based on {cc.num(r['monthly_m'], 0)} total metres")
        self.total_left.configure(text=cc.money(r["total"])); self.total_main.configure(text=cc.money(r["total"]))
        self.total_formula.configure(text=f"Paper ({cc.money(r['paper'])}) + ink + labour + electricity")
        for name, amt, pct in r["shares"]:
            a, p = self.share[name]
            a.configure(text=cc.money(amt)); p.configure(text=f"({pct:.1f}%)")
        for v, f in self.kv.values():
            v.configure(text=f(r))
        self.width_chip.configure(text=f"Width: {cc.num(r['width'], 2).rstrip('0').rstrip('.')} m")
        self._donut(r)
        rows = []
        for s in r["scenarios"]:
            d = s["diff"]
            rows.append({"cov": f"{cc.num(s['coverage'], 0 if s['coverage'] == s['coverage'].to_integral() else 2)} ml/m²", "ink_ml": f"{cc.num(s['ink_ml'], 1)} ml",
                         "ink_cost": cc.money(s["ink_cost"]), "total": cc.money(s["total"]), "per_day": cc.money(s["per_day"], 0),
                         "change": "Active input" if s["current"] else (f"+{cc.money(d)}" if d > 0 else f"-{cc.money(-d)}" if d < 0 else "Same"),
                         "tag": None if s["current"] else ("bad" if d > 0 else "good" if d < 0 else None)})
        self.table.set_rows(rows)
        self.after_idle(self._fit_cards)

    def _fit_cards(self):
        for h in self.hero_cards:
            h.autosize(self._scale())
            h.fit()
        self._fit_presets()

    def _donut(self, r):
        if self.canvas is not None:
            if self.focus_get() is self.canvas.get_tk_widget():
                self.focus_set()
            self.canvas.get_tk_widget().destroy()
            self.canvas = None
        fig = Figure(figsize=(2.3, 2.0), dpi=100, facecolor=T.rc(T.CARD))
        ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
        ax.set_facecolor(T.rc(T.CARD)); ax.set_axis_off()
        if r is None:
            ax.text(0.5, 0.5, "Press Calculate cost\nto see the split", ha="center", va="center", color=T.rc(T.MUTED), fontsize=10, fontweight="bold", transform=ax.transAxes)
        else:
            ax.pie([float(a) for _n, a, _p in r["shares"]], colors=[PART_COLOURS[n] for n, _a, _p in r["shares"]], startangle=90, counterclock=False,
                   wedgeprops=dict(width=0.3, edgecolor=T.rc(T.CARD), linewidth=2))
            ax.set_aspect("equal")
        c = smooth.embed_chart(fig, self.chart_holder, fill="both", expand=True)
        c.draw_idle()                                                       # one drawing (it was two, on a brand-new figure every Calculate)
        self.canvas = c
