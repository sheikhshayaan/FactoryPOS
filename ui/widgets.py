"""Reusable widgets: buttons, Stat, DataTable, FormDialog, dialogs, tabs, background worker."""
from __future__ import annotations

import calendar
import logging
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass, field
from datetime import date, timedelta
from tkinter import ttk
from typing import Any, Callable

import customtkinter as ctk

from ui import theme as T

log = logging.getLogger("fms.ui")


# ------------------------------------------------------------------ buttons / small pieces
def make_button(parent, text, command, kind: str = "secondary", width: int = 0, **kw):
    """kind: primary (filled accent) | secondary (outlined) | danger (outlined red) | ghost (text only)."""
    base = dict(text=text, command=command, height=kw.pop("height", 38), corner_radius=10, font=T.font(14, "bold" if kind == "primary" else "normal"))
    base["width"] = width or max(84, len(text) * 9 + 40)
    if kind == "primary":
        base.update(fg_color=T.ACCENT_FILL, hover_color=T.ACCENT_HOVER, text_color=("#ffffff", "#ffffff"), border_width=0)
    elif kind == "danger":
        base.update(fg_color="transparent", hover_color=T.CARD_ALT, text_color=T.BAD, border_width=1, border_color=T.BAD)
    elif kind == "ghost":
        base.update(fg_color="transparent", hover_color=T.CARD_ALT, text_color=T.MUTED, border_width=0)
    else:
        base.update(fg_color="transparent", hover_color=T.CARD_ALT, text_color=T.TEXT, border_width=1, border_color=T.BORDER)
    base.update(kw)
    btn = ctk.CTkButton(parent, **base)
    _keyboard(btn)
    return btn


def _keyboard(btn):
    """Make a CTkButton usable from the keyboard: Tab reaches it (a thin ring shows where you are), Enter or Space presses it."""
    try:
        cv = btn._canvas
        cv.configure(takefocus=1)
        ring = lambda on: cv.configure(highlightthickness=2 if on else 0, highlightbackground=T.rc(T.ACCENT), highlightcolor=T.rc(T.ACCENT))  # noqa: E731
        cv.bind("<FocusIn>", lambda _e: ring(True), add="+")
        cv.bind("<FocusOut>", lambda _e: ring(False), add="+")
        for key in ("<Return>", "<KP_Enter>", "<space>"):
            cv.bind(key, lambda _e, b=btn: (b.invoke() if str(b.cget("state")) != "disabled" else None, "break")[1], add="+")
    except Exception:  # noqa: BLE001 - never let a cosmetic feature stop a button from being created
        pass


def make_tabs(parent, **kw):
    kw.setdefault("fg_color", "transparent")
    kw.setdefault("segmented_button_fg_color", T.CARD_ALT)
    kw.setdefault("segmented_button_selected_color", T.SEG_ON)
    kw.setdefault("segmented_button_selected_hover_color", T.SEG_ON)
    kw.setdefault("segmented_button_unselected_color", T.CARD_ALT)
    kw.setdefault("segmented_button_unselected_hover_color", T.BORDER)
    kw.setdefault("text_color", T.TEXT)
    kw.setdefault("border_width", 0)
    tv = ctk.CTkTabview(parent, **kw)
    tv._segmented_button.configure(font=T.font(14))
    return tv


def surface(parent, **kw):
    """A flat raised panel: subtle border, generous radius."""
    kw.setdefault("fg_color", T.CARD)
    kw.setdefault("corner_radius", 14)
    kw.setdefault("border_width", 1)
    kw.setdefault("border_color", T.BORDER)
    return ctk.CTkFrame(parent, **kw)


class Stat(ctk.CTkFrame):
    """A bold heading + one value, no box of its own - it lives on a surface. (Notes passed to set() are ignored on purpose:
    the dashboard shows headings and numbers only.)"""

    def __init__(self, parent, label: str, value: str = "-", note: str = "", *, big: bool = False, tag: str = ""):
        super().__init__(parent, fg_color="transparent")
        self.big = big
        ctk.CTkLabel(self, text=label, text_color=T.MUTED, font=T.font(14), anchor="w").pack(fill="x")
        self.value = ctk.CTkLabel(self, text=value, text_color=T.TEXT, font=T.font(34 if big else 22, "bold"), anchor="w")
        self.value.pack(fill="x", pady=(6, 0))

    def set(self, value: str, sub: str = "", tone: str | None = None):
        self.value.configure(text=value, text_color={"good": T.GOOD, "bad": T.BAD, "warn": T.WARN}.get(tone, T.TEXT))


# soft drop shadow: Tk has no real shadows, so a few offset, progressively darker rounded layers sit behind the card
_SHADOW_PAD = (0, 10)                                   # extra room on the right and bottom for the shadow
_SHADOW_LAYERS = (                                       # (dx, dy, (light, dark)), far -> near
    (3, 9, ("#eaedf4", "#0a0d18")),
    (2, 6, ("#dfe2ec", "#070a13")),
    (1, 3, ("#d2d6e4", "#04060d")),
)


class StatBox(ctk.CTkFrame):
    """A coloured figure box: tinted background, a border in the same colour as the number, a soft shadow, bold heading and
    coloured number. `kind` is a key of theme.KINDS (good/bad = profit/loss, info = sales, cash ...). `set(value, sub, kind)`
    can change the kind, so a box can turn green or red with the data. Notes (`sub`) are ignored on purpose."""

    def __init__(self, parent, label: str, value: str = "-", note: str = "", *, kind: str = "neutral", big: bool = False, wrap: int = 215):
        super().__init__(parent, fg_color="transparent", corner_radius=0, border_width=0)
        self.kind = kind
        self.big = big
        strong, soft = T.KINDS[kind]
        self.card = ctk.CTkFrame(self, fg_color=soft, corner_radius=14, border_width=2, border_color=strong)
        self.card.pack(fill="both", expand=True, padx=(0, _SHADOW_PAD[1]), pady=(0, _SHADOW_PAD[1]))
        for dx, dy, col in _SHADOW_LAYERS:               # same size as the card, shifted right/down, stacked just below it
            layer = ctk.CTkFrame(self, fg_color=col, corner_radius=14, border_width=0)
            layer.place(in_=self.card, x=dx, y=dy, relwidth=1, relheight=1)
            layer.tk.call("lower", layer._w, self.card._w)
        body = ctk.CTkFrame(self.card, fg_color="transparent"); body.pack(fill="both", expand=True, padx=26, pady=24)
        self.label = ctk.CTkLabel(body, text=label, text_color=T.MUTED, font=T.font(15), anchor="w"); self.label.pack(fill="x")
        self.value = ctk.CTkLabel(body, text=value, text_color=strong, font=T.font(30 if big else 22, "bold"), anchor="w")
        self.value.pack(fill="x", pady=(8, 0))

    def set(self, value: str, sub: str = "", kind: str | None = None, tag: str = ""):
        if kind is not None:
            self.kind = kind
        strong, soft = T.KINDS[self.kind]
        self.card.configure(fg_color=soft, border_color=strong)
        self.value.configure(text=value, text_color=strong)


Card = Stat   # legacy alias


# ------------------------------------------------------------------ table
@dataclass
class Col:
    key: str
    title: str
    width: int = 120
    fmt: Callable[[Any], str] | None = None
    anchor: str = "w"


_KEEP_FONTS: list = []              # see _style: the table fonts must not be garbage-collected


def _style(scale: float = 1.0):
    """(Re)apply the table style for the current theme/scale. Safe to call repeatedly."""
    s = ttk.Style()
    try:
        s.theme_use("clam")
    except tk.TclError:
        pass
    px = lambda n: -max(1, round(n * scale))  # noqa: E731  negative = pixels, so it follows Windows display scaling
    # These Font objects MUST stay referenced: Tk deletes a named font when its Python object is collected, and the table then
    # silently falls back to the default font (which is why bold never showed). Sizes match what the tables have always looked like.
    body_font = tkfont.Font(family=T.FAMILY, size=px(12))
    head_font = tkfont.Font(family=T.FAMILY, size=px(12))
    bold_font = tkfont.Font(family=T.FAMILY, size=px(12), weight="bold")
    _KEEP_FONTS[:] = [body_font, head_font, bold_font]
    for name, fnt in (("FMS.Treeview", body_font), ("FMS.Bold.Treeview", bold_font)):       # the Bold one is for lists whose entries should stand out
        s.layout(name, [("Treeview.treearea", {"sticky": "nswe"})])                        # no border
        s.configure(name, background=T.rc(T.CARD), fieldbackground=T.rc(T.CARD), foreground=T.rc(T.TEXT), rowheight=round(52 * scale),
                    borderwidth=0, font=fnt)
        s.configure(name + ".Heading", background=T.rc(T.CARD), foreground=T.rc(T.MUTED), font=head_font, relief="flat", borderwidth=0,
                    padding=(round(12 * scale), round(12 * scale)))
        s.map(name, background=[("selected", T.rc(T.ACCENT_SOFT))], foreground=[("selected", T.rc(T.TEXT))])
        s.map(name + ".Heading", background=[("active", T.rc(T.CARD_ALT)), ("!active", T.rc(T.CARD))], foreground=[("!disabled", T.rc(T.MUTED))])


def _scale_of(widget) -> float:
    try:
        from customtkinter.windows.widgets.scaling import CTkScalingBaseClass  # noqa: F401
        return float(ctk.ScalingTracker.get_widget_scaling(widget))
    except Exception:  # noqa: BLE001 - fall back to 1.0 rather than fail to draw
        return 1.0


def restyle(widget):
    """Call after switching light/dark so tables pick up the new colours."""
    _style(_scale_of(widget))


class DataTable(ctk.CTkFrame):
    """Searchable, sortable, paginated table. Rows are dicts."""

    def __init__(self, parent, columns: list[Col], *, page_size: int = 40, height: int = 12, on_select=None, searchable: bool = True,
                 empty_text: str = "Nothing here yet.", row_tags: Callable[[dict], str | None] | None = None, bold: bool = False):
        super().__init__(parent, fg_color="transparent")
        self.row_tags = row_tags            # row -> "good" | "bad" | "warn" | "muted" | None : colours the row's text
        self.scale = _scale_of(self)
        _style(self.scale)
        self.columns, self.page_size, self.on_select, self.empty_text = columns, page_size, on_select, empty_text
        self.rows: list[dict] = []
        self.view: list[dict] = []
        self.page = 0
        self.sort_key, self.sort_rev = None, False
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", pady=(0, 8))

        class _NoSearch:
            @staticmethod
            def get():
                return ""
        self.search_var = _NoSearch()
        if searchable:
            e = ctk.CTkEntry(top, placeholder_text="Search this list", width=300, height=38, font=T.font(14))
            e.pack(side="left")
            e.bind("<KeyRelease>", lambda _e: self._apply(reset=True))
            self.search_var = e
        self.count = ctk.CTkLabel(top, text="", text_color=T.MUTED, font=T.font(13))
        self.count.pack(side="right")
        body = surface(self)
        body.pack(fill="both", expand=True)
        self.body = body
        self.tree = ttk.Treeview(body, columns=[c.key for c in columns], show="headings", style="FMS.Bold.Treeview" if bold else "FMS.Treeview", height=height, selectmode="browse")
        for c in columns:                                    # every heading and every entry sits in the middle of its column: readable and tidy
            self.tree.heading(c.key, text=c.title, anchor="center", command=lambda k=c.key: self._sort(k))
            self.tree.column(c.key, width=round(c.width * 1.2 * self.scale), anchor="center", stretch=True)
        self.bold = bold
        self.tree.bind("<Configure>", self._fit, add="+")
        self.vs = ctk.CTkScrollbar(body, orientation="vertical", command=self.tree.yview, width=12, height=10)     # its default height (200) would be the table's minimum height
        self.hs = ctk.CTkScrollbar(body, orientation="horizontal", command=self.tree.xview, height=12)
        self.tree.configure(yscrollcommand=self._yset, xscrollcommand=self._xset)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.vs.grid(row=0, column=1, sticky="ns", pady=6)
        self.hs.grid(row=1, column=0, sticky="ew", padx=6)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.empty = ctk.CTkLabel(body, text=empty_text, text_color=T.MUTED, font=T.font(15))
        self.nav = nav = ctk.CTkFrame(self, fg_color="transparent")
        self.prev_b = make_button(nav, "‹  Previous", lambda: self._go(-1), "ghost", width=100)
        self.next_b = make_button(nav, "Next  ›", lambda: self._go(1), "ghost", width=90)
        self.prev_b.pack(side="left"); self.next_b.pack(side="left", padx=6)
        self.page_lbl = ctk.CTkLabel(nav, text="", text_color=T.MUTED, font=T.font(13))
        self.page_lbl.pack(side="left", padx=6)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.on_select and self.on_select(self.selected()))

    def _bar(self, bar, a, b):
        """Set a scrollbar and show it only when there is something to scroll. CTkScrollbar redraws with update_idletasks(), which can fire
        this very callback again; without the guard (and the 'only when it changes' check) a short table made the two scrollbars flip in and
        out forever and froze the window."""
        if getattr(self, "_in_bar", False):
            return
        self._in_bar = True
        try:
            bar.set(a, b)
            show = not (float(a) <= 0 and float(b) >= 1)
            if getattr(bar, "_shown", None) is not show:
                bar._shown = show
                (bar.grid if show else bar.grid_remove)()
        finally:
            self._in_bar = False

    def _yset(self, a, b):
        self._bar(self.vs, a, b)

    def _xset(self, a, b):
        self._bar(self.hs, a, b)

    def _fit(self, _e=None):
        """Make the columns share the table's width: when there is not enough room they shrink together (down to what their heading
        needs) instead of pushing the last columns out of sight behind a scroll bar."""
        avail = self.tree.winfo_width() - 4
        if avail < 50:
            return
        sc = self.scale
        want = [round(c.width * 1.2 * sc) for c in self.columns]
        body, head = _KEEP_FONTS[2 if self.bold else 0], _KEEP_FONTS[1]
        shown = self.view[self.page * self.page_size:(self.page + 1) * self.page_size]
        floor = []
        for w, c in zip(want, self.columns):
            need = head.measure(c.title) + 2 * round(14 * sc)                               # the heading (and room for the sort arrow)
            for r in shown[:60]:
                need = max(need, body.measure(self._text(c, r)) + 2 * round(10 * sc))        # the longest entry on this page
            floor.append(min(w, max(round(48 * sc), need)))                                 # never asks for more than the width given to it
        w = list(want)
        if sum(want) > avail:
            f = avail / sum(want)
            w = [max(fl, round(x * f)) for x, fl in zip(want, floor)]
            for _ in range(5):
                over = sum(w) - avail
                if over <= 0:
                    break
                room = [(i, w[i] - floor[i]) for i in range(len(w)) if w[i] > floor[i]]
                total = sum(r for _i, r in room)
                if total <= 0:
                    break
                for i, r in room:
                    w[i] = max(floor[i], w[i] - max(1, round(over * r / total)))
        for c, x in zip(self.columns, w):
            self.tree.column(c.key, width=x, stretch=True)

    def set_rows(self, rows: list[dict]):
        self.rows = rows
        self._apply(reset=False)

    def _text(self, c: Col, r: dict) -> str:
        v = r.get(c.key)
        if c.fmt:
            try:
                return c.fmt(v)
            except Exception:  # noqa: BLE001 - display must never crash on odd data
                return str(v)
        return "" if v is None else str(v)

    def _apply(self, reset=True):
        try:
            self._keep = self.selected()                    # the row that is selected now, so a refresh does not make the selection vanish
        except Exception:  # noqa: BLE001
            self._keep = None
        term = self.search_var.get().strip().lower()
        v = self.rows
        if term:
            v = [r for r in v if any(term in self._text(c, r).lower() for c in self.columns)]
        if self.sort_key:
            v = sorted(v, key=lambda r: (r.get(self.sort_key) is None, r.get(self.sort_key) if isinstance(r.get(self.sort_key), (int, float)) else str(r.get(self.sort_key)).lower()),
                       reverse=self.sort_rev)
        self.view = v
        if reset:
            self.page = 0
        self.page = min(self.page, max(0, (len(v) - 1) // self.page_size))
        self._render()

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        for name, col in (("good", T.GOOD), ("bad", T.BAD), ("warn", T.WARN), ("muted", T.MUTED)):
            self.tree.tag_configure(name, foreground=T.rc(col))
        s = self.page * self.page_size
        for i, r in enumerate(self.view[s:s + self.page_size]):
            tag = self.row_tags(r) if self.row_tags else None
            self.tree.insert("", "end", iid=str(s + i), values=[self._text(c, r) for c in self.columns], tags=(tag,) if tag else ())
        keep, self._keep = getattr(self, "_keep", None), None
        if keep is not None:
            for i, r in enumerate(self.view[s:s + self.page_size]):
                if r == keep or (keep.get("id") is not None and r.get("id") == keep.get("id")):
                    self.tree.selection_set(str(s + i)); self.tree.see(str(s + i))
                    break
        for c in self.columns:
            arrow = (" ▲" if not self.sort_rev else " ▼") if c.key == self.sort_key else ""
            self.tree.heading(c.key, text=c.title + arrow)
        pages = max(1, -(-len(self.view) // self.page_size))
        self.page_lbl.configure(text=f"Page {self.page + 1} of {pages}" if pages > 1 else "")
        if self.view:
            self.empty.place_forget()
            self.count.configure(text=f"{len(self.view)} of {len(self.rows)}" if len(self.view) != len(self.rows) else f"{len(self.rows)} rows")
        else:
            self.empty.configure(text="No matches." if self.rows else self.empty_text)
            self.empty.place(relx=0.5, rely=0.55, anchor="center")
            self.count.configure(text="")
        if pages > 1:
            self.nav.pack(fill="x", pady=(8, 0))
        else:
            self.nav.pack_forget()
        self.prev_b.configure(state="normal" if self.page > 0 else "disabled")
        self.next_b.configure(state="normal" if self.page < pages - 1 else "disabled")
        self._fit()

    def _go(self, d):
        self.page += d
        self._render()

    def _sort(self, key):
        self.sort_rev = (not self.sort_rev) if self.sort_key == key else False
        self.sort_key = key
        self._apply(reset=False)

    def selected(self) -> dict | None:
        sel = self.tree.selection()
        return self.view[int(sel[0])] if sel else None


# ------------------------------------------------------------------ dialogs
@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"            # text | money | date | time | choice | multiline | readonly | password
    choices: list[tuple[str, Any]] = field(default_factory=list)
    default: Any = ""
    hint: str = ""
    advanced: bool = False        # hidden behind "More options" so the common case stays short
    live_hint: bool = False       # reserve a hint line that a `live` callback can rewrite while the person types


class _Modal(ctk.CTkToplevel):
    def __init__(self, parent, title, width=540):
        super().__init__(parent)
        self.title(title)
        self._min_w = width
        self.configure(fg_color=T.CARD)
        self.geometry(f"{width}x10")
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.default_btn = None                      # the dialog's main button: Enter presses it (set by each dialog)
        self.bind("<Return>", self._on_enter); self.bind("<KP_Enter>", self._on_enter)
        self.bind("<Escape>", lambda _e: self.destroy())

    def _on_enter(self, _e=None):
        """Enter presses the dialog's main button - except inside a multi-line box, where it must start a new line."""
        w = self.focus_get()
        if isinstance(w, tk.Text):
            return None
        b = self.default_btn
        if b is not None and b.winfo_exists() and str(b.cget("state")) != "disabled":
            b.invoke()
            return "break"
        return None

    def present(self, modal=True):
        self.update_idletasks()
        w = max(self.winfo_reqwidth(), min(self._min_w, self.winfo_screenwidth() - 40))
        h = min(self.winfo_reqheight(), max(300, self.winfo_screenheight() - 150))
        top = self.master.winfo_toplevel()
        x = top.winfo_rootx() + max(0, (top.winfo_width() - w) // 2)
        y = max(10, min(top.winfo_rooty() + max(0, (top.winfo_height() - h) // 4), self.winfo_screenheight() - h - 90))
        self.geometry(f"{w}x{h}+{x}+{y}")
        if modal:
            try:
                self.wait_visibility(); self.grab_set(); self.focus_force()
            except tk.TclError:
                pass


class CalendarPopup(_Modal):
    """A small month calendar. Pick a day with the mouse - nobody has to type a date."""

    def __init__(self, anchor, current: date | None, on_pick: Callable[[date], None]):
        super().__init__(anchor, "Pick a date", 344)
        self.on_pick, self.today = on_pick, date.today()
        self.sel = current
        self.year, self.month = (current or self.today).year, (current or self.today).month
        pad = ctk.CTkFrame(self, fg_color="transparent"); pad.pack(padx=16, pady=14)
        head = ctk.CTkFrame(pad, fg_color="transparent"); head.pack(fill="x")
        nav = lambda txt, d: make_button(head, txt, lambda: self._shift(d), "ghost", width=38, height=34)  # noqa: E731
        nav("«", -12).pack(side="left"); nav("‹", -1).pack(side="left")
        nav("»", 12).pack(side="right"); nav("›", 1).pack(side="right")
        self.title_lbl = ctk.CTkLabel(head, text="", font=T.font(16, "bold"), text_color=T.TEXT); self.title_lbl.pack(expand=True)
        grid = ctk.CTkFrame(pad, fg_color="transparent"); grid.pack(pady=(10, 0))
        for i, n in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):      # weekday names live in the same grid, so they line up with the days
            ctk.CTkLabel(grid, text=n, font=T.font(12), text_color=T.MUTED).grid(row=0, column=i, pady=(0, 4))
        self.cells = []
        for r in range(6):
            for c in range(7):
                b = ctk.CTkButton(grid, text="", width=42, height=36, corner_radius=18, font=T.font(14), fg_color="transparent", hover_color=T.CARD_ALT,
                                  text_color=T.TEXT, border_width=0)
                b.grid(row=r + 1, column=c, padx=1, pady=1)
                self.cells.append(b)
        foot = ctk.CTkFrame(pad, fg_color="transparent"); foot.pack(fill="x", pady=(10, 0))
        make_button(foot, "Today", lambda: self._pick(self.today), "secondary", width=90, height=34).pack(side="left")
        make_button(foot, "Yesterday", lambda: self._pick(self.today - timedelta(days=1)), "secondary", width=100, height=34).pack(side="left", padx=8)
        make_button(foot, "Close", self._close, "ghost", width=70, height=34).pack(side="right")
        self.bind("<Escape>", lambda _e: self._close())
        self._render()
        self.present(False)
        try:                                           # sit just under the field instead of in the middle of the window
            self.update_idletasks()
            x = anchor.winfo_rootx(); y = anchor.winfo_rooty() + anchor.winfo_height() + 4
            x = max(0, min(x, self.winfo_screenwidth() - self.winfo_width() - 8))
            if y + self.winfo_height() > self.winfo_screenheight() - 60:
                y = max(0, anchor.winfo_rooty() - self.winfo_height() - 4)
            self.geometry(f"+{x}+{y}")
            self.wait_visibility(); self.grab_set(); self.focus_force()
        except tk.TclError:
            pass

    def _shift(self, months: int):
        t = self.year * 12 + (self.month - 1) + months
        self.year, self.month = t // 12, t % 12 + 1
        self._render()

    def _render(self):
        self.title_lbl.configure(text=f"{calendar.month_name[self.month]} {self.year}")
        first = date(self.year, self.month, 1)
        start = first - timedelta(days=first.weekday())                       # the Monday on or before the 1st
        for i, b in enumerate(self.cells):
            d = start + timedelta(days=i)
            inside = d.month == self.month
            if d == self.sel:
                b.configure(text=str(d.day), fg_color=T.ACCENT_FILL, hover_color=T.ACCENT_HOVER, text_color=("#ffffff", "#ffffff"), border_width=0)
            elif d == self.today:
                b.configure(text=str(d.day), fg_color="transparent", hover_color=T.CARD_ALT, text_color=T.ACCENT, border_width=2, border_color=T.ACCENT)
            else:
                b.configure(text=str(d.day), fg_color="transparent", hover_color=T.CARD_ALT, text_color=T.TEXT if inside else T.MUTED, border_width=0)
            b.configure(command=lambda d=d: self._pick(d))

    def _pick(self, d: date):
        self._close()
        self.on_pick(d)

    def _close(self):
        top = self.master.winfo_toplevel()
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()
        if isinstance(top, ctk.CTkToplevel):                                  # give the form underneath its modal focus back
            try:
                top.grab_set()
            except tk.TclError:
                pass


class DateField(ctk.CTkFrame):
    """A date the person picks on a calendar (no typing). `get()` returns an ISO date (YYYY-MM-DD) like an entry would."""

    def __init__(self, parent, value: str | date | None = None, *, width: int = 0, height: int = 40, command: Callable[[], None] | None = None, compact: bool = False):
        super().__init__(parent, fg_color="transparent")
        self._iso, self._cmd, self.compact = "", command, compact
        self.btn = ctk.CTkButton(self, text="", height=height, anchor="w", corner_radius=10, fg_color=T.CARD_ALT, hover_color=T.BORDER, text_color=T.TEXT,
                                 border_width=1, border_color=T.BORDER, font=T.font(14), command=self.open, **({"width": width} if width else {}))
        self.btn.pack(fill="x")
        self.set_date(value if value is not None else date.today())

    # -- the small subset of the Entry API that forms and tests use
    def get(self) -> str:
        return self._iso

    def delete(self, *_a):
        self._set("")

    def insert(self, _idx, text):
        self.set_date(text)

    def configure(self, **kw):
        kw.pop("state", None)
        if kw:
            super().configure(**kw)

    def set_date(self, v):
        if isinstance(v, date):
            v = v.isoformat()
        self._set(str(v or "").strip())

    def _set(self, iso: str):
        self._iso = iso
        try:
            d = date.fromisoformat(iso)
            txt = d.strftime("%d %b %Y") if self.compact else d.strftime("%a, %d %b %Y") + ("   (today)" if d == date.today() else "")
        except ValueError:
            txt = iso or "Pick a date"
        self.btn.configure(text=f"{txt}    ▾")

    def open(self):
        try:
            cur = date.fromisoformat(self._iso)
        except ValueError:
            cur = None

        def picked(d: date):
            self.set_date(d)
            if self._cmd:
                self._cmd()
        CalendarPopup(self.btn, cur, picked)


class TimeField(ctk.CTkFrame):
    """A time of day picked from two lists (hour, minute): nobody types it. `get()` returns "HH:MM", or "" while left blank."""
    BLANK = "--"

    def __init__(self, parent, value: str = "", *, command: Callable[[], None] | None = None, width: int = 84):
        super().__init__(parent, fg_color="transparent")
        self._cmd = command
        mk = lambda vals: ctk.CTkComboBox(self, values=vals, state="readonly", width=width, height=40, font=T.font(14),  # noqa: E731
                                          dropdown_font=T.font(14), command=lambda _v: self._fire())
        self.h = mk([self.BLANK] + [f"{i:02d}" for i in range(24)])
        self.m = mk([self.BLANK] + [f"{i:02d}" for i in range(0, 60, 5)])
        self.h.pack(side="left")
        ctk.CTkLabel(self, text=":", font=T.font(16, "bold"), text_color=T.MUTED).pack(side="left", padx=6)
        self.m.pack(side="left")
        self.set_time(value)

    def _fire(self):
        if self.h.get() == self.BLANK:
            self.m.set(self.BLANK)
        elif self.m.get() == self.BLANK:
            self.m.set("00")
        if self._cmd:
            self._cmd()

    def set_time(self, v: str):
        v = (v or "").strip()
        if len(v) >= 5 and v[2] == ":" and v[:2].isdigit() and v[3:5].isdigit():
            hh, mm = v[:2], v[3:5]
            vals = list(self.m.cget("values"))
            if mm not in vals:                               # an existing record may have a minute that is not a multiple of 5
                self.m.configure(values=[self.BLANK] + sorted(x for x in vals + [mm] if x != self.BLANK))
            self.h.set(hh); self.m.set(mm)
        else:
            self.h.set(self.BLANK); self.m.set(self.BLANK)

    def get(self) -> str:
        return "" if self.h.get() == self.BLANK else f"{self.h.get()}:{self.m.get() if self.m.get() != self.BLANK else '00'}"

    # the small Entry-like API that forms and tests use
    def delete(self, *_a):
        self.set_time("")

    def insert(self, _idx, text):
        self.set_time(text)

    def configure(self, **kw):
        kw.pop("state", None)
        if kw:
            super().configure(**kw)


class RowList(ctk.CTkFrame):
    """A scrolling list whose rows hold real widgets (chips, buttons) - for screens where every row has its own action buttons.
    columns = [(title, width, anchor)]; width 0 = takes the spare room. `set_rows(rows, render)` calls render(cells, row) for each row;
    `cells` is one empty frame per column to put widgets into. `replace(i, row)` redraws a single row."""

    def __init__(self, parent, columns: list[tuple[str, int, str]], *, empty_text: str = "Nothing here yet.", pad_right: int = 46,
                 header_in_scroll: bool = False):
        """header_in_scroll=True puts the column titles inside the scrolling area, so titles and rows are always exactly the same width
        (the columns can never drift apart); the titles then scroll away with a long list."""
        super().__init__(parent, fg_color="transparent")
        self.columns, self.empty_text = columns, empty_text
        self.rows: list[dict] = []
        self.items: list[tuple[ctk.CTkFrame, list[ctk.CTkFrame]]] = []
        self._render_fn: Callable | None = None
        self._border_fn: Callable | None = None
        self._sig_fn: Callable | None = None
        self._keys: list = []
        self._sigs: list = []
        self.scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        head = self._head = ctk.CTkFrame(self.scroll if header_in_scroll else self, fg_color="transparent")
        head.pack(fill="x", padx=(1, 5) if header_in_scroll else (14, pad_right), pady=(0, 2))
        self._in_scroll, self._sync_job = header_in_scroll, None
        self._grid_cols(head)
        for i, (title, _w, anchor) in enumerate(columns):
            ctk.CTkLabel(head, text=title, text_color=T.MUTED, font=T.font(12), anchor="center").grid(
                row=0, column=i, sticky="ew", padx=(12 if i == 0 else 4, 4))
        self.scroll.pack(fill="both", expand=True)
        self.bind("<Configure>", lambda _e: self._sync_later(), add="+")
        try:
            self.scroll._scrollbar.configure(height=10)
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
        self.empty = ctk.CTkLabel(self.scroll, text="", text_color=T.MUTED, font=T.font(15))

    def _sync_later(self):
        if self._sync_job:
            self.after_cancel(self._sync_job)
        self._sync_job = self.after(40, self._sync)

    def _sync(self):
        """Keep the column titles exactly above their columns: every row and the title line get the same column widths (a column that
        needs more room for its buttons widens everywhere) and the title line gets the same left and right edges as the row cards."""
        self._sync_job = None
        try:
            self.update_idletasks()
            widths = [w for _t, w, _a in self.columns]
            for i, w in enumerate(widths):
                if w:
                    need = max((cells[i].winfo_reqwidth() + (12 if i == 0 else 4) + 4 for _f, cells in self.items), default=0)
                    widths[i] = max(w, need)
            for f in [self._head] + [f for f, _c in self.items]:
                for i, w in enumerate(widths):
                    if w:
                        f.grid_columnconfigure(i, minsize=w)
            if self.items and not self._in_scroll:
                f = self.items[0][0]
                left = f.winfo_rootx() - self.winfo_rootx()
                right = self.winfo_width() - left - f.winfo_width()
                if f.winfo_width() > 20:
                    self._head.pack_configure(padx=(max(left, 0), max(right, 0)))
        except tk.TclError:
            pass

    def _grid_cols(self, f):
        for i, (_t, w, _a) in enumerate(self.columns):
            f.grid_columnconfigure(i, minsize=w, weight=1 if w == 0 else 0)

    def _pack(self, frame):
        frame.pack(fill="x", pady=3, padx=(0, 4))

    def _build(self, row: dict, frame=None):
        border = self._border_fn(row) if self._border_fn else T.BORDER
        if frame is None:
            frame = ctk.CTkFrame(self.scroll, fg_color=T.CARD, corner_radius=12, border_width=1, border_color=border)
            self._pack(frame)
            self._grid_cols(frame)
        else:
            frame.configure(border_color=border)
            for c in frame.winfo_children():
                c.destroy()
        cells = []
        for i, (_t, _w, anchor) in enumerate(self.columns):
            c = ctk.CTkFrame(frame, fg_color="transparent", height=1)
            c.grid(row=0, column=i, sticky="nsew", padx=(12 if i == 0 else 4, 4), pady=9)
            cells.append(c)
        self._render_fn(cells, row)
        self._centre(cells)
        return frame, cells

    @staticmethod
    def _centre(cells):
        """Whatever a page put in a cell (text, chips, buttons) is centred in its column, like the headings."""
        def walk(w):
            for ch in w.winfo_children():
                if isinstance(ch, ctk.CTkLabel):
                    ch.configure(anchor="center", justify="center")
                if ch.winfo_manager() == "pack":
                    ch.pack_configure(anchor="center")
                walk(ch)
        for cell in cells:
            walk(cell)

    def _signature(self, row: dict):
        """What decides whether a row has to be redrawn: its data (minus anything the page says is volatile), its outline colour, and the day."""
        data = self._sig_fn(row) if self._sig_fn else dict(row)
        return (data, self._border_fn(row) if self._border_fn else None, date.today())

    def set_rows(self, rows: list[dict], render: Callable[[list, dict], None], border: Callable[[dict], Any] | None = None, *,
                 key: Callable[[dict], Any] | None = None, sig: Callable[[dict], Any] | None = None):
        """Show `rows`, redrawing ONLY the rows whose data changed (no flicker, the scroll position stays).
        border(row) -> a theme colour for the row's outline (e.g. green while someone is IN).
        key(row) -> a stable id (default: the row's position); sig(row) -> what to compare to decide a row changed (default: the whole row)."""
        same_render = self._render_fn is not None and getattr(render, "__func__", render) == getattr(self._render_fn, "__func__", self._render_fn) \
            and getattr(render, "__self__", None) is getattr(self._render_fn, "__self__", None)
        self._render_fn, self._border_fn, self._sig_fn = render, border, sig
        new_keys = [key(r) if key else i for i, r in enumerate(rows)]
        new_sigs = [self._signature(r) for r in rows]
        old = {k: (it, sg) for k, it, sg in zip(self._keys, self.items, self._sigs)} if same_render else {}
        reused = {k for k in new_keys if k in old}
        for k, (it, _sg) in old.items():                               # rows that are gone
            if k not in reused:
                it[0].destroy()
        for k, it in zip(self._keys, self.items):
            if not same_render:
                it[0].destroy()
        self.rows = rows
        if not rows:
            self.items, self._keys, self._sigs = [], [], []
            self.empty.configure(text=self.empty_text)
            self.empty.pack(pady=40)
            return
        self.empty.pack_forget()
        items = []
        for k, r, sg in zip(new_keys, rows, new_sigs):
            if k in old:
                it, old_sg = old[k]
                items.append(it if old_sg == sg else self._build(r, it[0]))
            else:
                items.append(self._build(r))
        order_changed = [k for k in self._keys if k in reused] != [k for k in new_keys if k in reused] or any(k not in old for k in new_keys)
        self.items, self._keys, self._sigs = items, new_keys, new_sigs
        if order_changed:                                              # only when rows moved, were added or the list shrank
            for f, _c in items:
                f.pack_forget()
            for f, _c in items:
                self._pack(f)
        self._sync_later()

    def replace(self, index: int, row: dict):
        self.rows[index] = row
        frame, _cells = self.items[index]
        self.items[index] = self._build(row, frame)
        self._sigs[index] = self._signature(row)
        self._sync_later()


class FormDialog(_Modal):
    """Generic form. on_submit(values: dict) may raise to show an error and keep the dialog open."""

    def __init__(self, parent, title: str, fields: list[Field], on_submit: Callable[[dict], Any], *, submit_text="Save",
                 on_done: Callable[[], None] | None = None, intro: str = "", modal=True,
                 live: Callable[[dict, "FormDialog"], None] | None = None):
        super().__init__(parent, title)
        self.fields, self.on_submit, self.on_done, self.live = fields, on_submit, on_done, live
        self.widgets: dict[str, Any] = {}
        self.maps: dict[str, dict[str, Any]] = {}
        self.labels: dict[str, Any] = {}
        self.hints: dict[str, Any] = {}
        pad = ctk.CTkFrame(self, fg_color="transparent")
        pad.pack(fill="both", expand=True, padx=28, pady=24)
        ctk.CTkLabel(pad, text=title, font=T.font(21, "bold"), text_color=T.TEXT, anchor="w").pack(fill="x")
        if intro:
            ctk.CTkLabel(pad, text=intro, text_color=T.MUTED, font=T.font(13), wraplength=480, justify="left", anchor="w").pack(fill="x", pady=(4, 0))
        # fields live in a scrollable area so a long form never runs off the screen; title and buttons stay visible
        self.body = body = ctk.CTkScrollableFrame(pad, fg_color="transparent", width=470, height=100)
        body.pack(fill="both", expand=True)
        try:
            body._scrollbar.configure(height=10)       # its default height (200) would make every short form at least that tall
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
        self.adv = ctk.CTkFrame(body, fg_color="transparent")
        self.boxes: dict[str, Any] = {}          # one frame per field (label + input + hint) so a field can be hidden as a whole
        self._hidden: set[str] = set()
        for f in fields:
            outer = self.adv if f.advanced else body
            host = ctk.CTkFrame(outer, fg_color="transparent"); host.pack(fill="x")
            self.boxes[f.key] = (host, outer)
            lab = ctk.CTkLabel(host, text=f.label, text_color=T.MUTED, font=T.font(13), anchor="w"); lab.pack(fill="x", pady=(14, 4))
            self.labels[f.key] = lab
            if f.kind == "choice":
                labels = [c[0] for c in f.choices]
                self.maps[f.key] = dict(f.choices)
                w = ctk.CTkComboBox(host, values=labels or [""], state="readonly", height=40, dropdown_font=T.font(14), font=T.font(14), command=lambda _v: self._changed())
                dflt = next((c[0] for c in f.choices if c[1] == f.default), labels[0] if labels else "")
                w.set(dflt)
            elif f.kind == "multiline":
                w = ctk.CTkTextbox(host, height=84)
                w.insert("1.0", str(f.default or ""))
            elif f.kind == "date":
                w = DateField(host, f.default if f.default not in ("", None) else date.today(), command=self._changed)
            elif f.kind == "time":
                w = TimeField(host, f.default or "", command=self._changed)
            else:
                w = ctk.CTkEntry(host, show="•", height=40) if f.kind == "password" else ctk.CTkEntry(host, height=40)
                w.insert(0, "" if f.default is None else str(f.default))
                if f.kind == "readonly":
                    w.configure(state="disabled")
                else:
                    w.bind("<KeyRelease>", lambda _e: self._changed())
            w.pack(fill="x")
            if f.hint or f.live_hint:
                h = ctk.CTkLabel(host, text=f.hint, text_color=T.MUTED, font=T.font(12), anchor="w", wraplength=480, justify="left")
                h.pack(fill="x", pady=(4, 0))
                self.hints[f.key] = h
            self.widgets[f.key] = w
        self.more_btn = None
        if any(f.advanced for f in fields):
            self.more_btn = make_button(body, "More options  ▾", self._toggle_more, "ghost", width=150)
            self.more_btn.pack(anchor="w", pady=(10, 0))
        self.err = ctk.CTkLabel(pad, text="", text_color=T.BAD, font=T.font(13), wraplength=480, justify="left", anchor="w")
        self.err.pack(fill="x", pady=(14, 0))
        row = ctk.CTkFrame(pad, fg_color="transparent")
        row.pack(fill="x", pady=(14, 0))
        self.submit_btn = make_button(row, submit_text, self.submit, "primary", width=130)
        self.submit_btn.pack(side="right")
        self.default_btn = self.submit_btn
        make_button(row, "Cancel", self.destroy, "ghost", width=90).pack(side="right", padx=8)
        self._changed()
        self._ready = True
        self._fit()
        self.present(modal)
        first = next((self.widgets[f.key] for f in fields if f.kind in ("text", "money", "password") and f.key not in self._hidden), None)
        if first is not None and modal:
            try:
                first.focus_set()                    # type straight away; Enter then saves
            except tk.TclError:
                pass

    # -- live behaviour: a `live(values, dialog)` callback can recompute fields while the person types or picks
    def _changed(self):
        if self.live is None or not self.widgets or len(self.widgets) < len(self.fields):
            return
        try:
            self.live(self.values(), self)
        except Exception:  # noqa: BLE001 - half-typed input must never break the form
            pass
        if getattr(self, "_dirty", False) and getattr(self, "_ready", False):
            self._dirty = False
            self._fit()

    def set_value(self, key: str, text: str):
        w = self.widgets[key]
        f = next(f for f in self.fields if f.key == key)
        if f.kind == "choice":
            label = next((l for l, v in f.choices if v == text or l == text), None)
            if label is not None:
                was = w.cget("state")
                w.configure(state="normal"); w.set(label); w.configure(state=was)        # a locked (disabled) box ignores set()
        elif f.kind == "date":
            w.set_date(text)
        elif f.kind == "time":
            w.set_time(text)
        else:
            was = w.cget("state")
            w.configure(state="normal"); w.delete(0, "end"); w.insert(0, text); w.configure(state=was)

    def set_label(self, key: str, text: str):
        if self.labels[key].cget("text") != text:
            self.labels[key].configure(text=text)

    def show(self, key: str, visible: bool = True):
        """Show or hide a whole field (label, input and hint). Hidden fields keep their value, so they can still be read.
        Only that one field is touched (nothing else is re-packed), and the window is resized once per change, so the form does not flicker."""
        if visible == (key not in self._hidden):
            return
        box, outer = self.boxes[key]
        if visible:
            self._hidden.discard(key)
            later = next((self.boxes[f.key][0] for f in self.fields[self.fields.index(next(x for x in self.fields if x.key == key)) + 1:]
                          if self.boxes[f.key][1] is outer and f.key not in self._hidden), None)
            box.pack(fill="x", before=later) if later is not None else box.pack(fill="x")
        else:
            self._hidden.add(key)
            box.pack_forget()
        self._dirty = True

    def set_choices(self, key: str, choices: list, keep=None):
        """Replace the list of a choice field; keeps the current pick when it is still there, else picks the first."""
        f = next(f for f in self.fields if f.key == key)
        w = self.widgets[key]
        current = self.maps[key].get(w.get())
        f.choices = list(choices)
        self.maps[key] = dict(f.choices)
        labels = [c[0] for c in f.choices] or [""]
        was = w.cget("state")
        w.configure(state="normal", values=labels)
        pick = next((c[0] for c in f.choices if c[1] == (current if keep is None else keep)), labels[0])
        w.set(pick); w.configure(state=was)

    def set_hint(self, key: str, text: str, bad: bool = False):
        h, col = self.hints[key], (T.BAD if bad else T.MUTED)
        if h.cget("text") != text or h.cget("text_color") != col:
            h.configure(text=text, text_color=col)

    def lock(self, key: str, locked: bool = True):
        w = self.widgets[key]
        f = next(f for f in self.fields if f.key == key)
        if f.kind == "choice":
            w.configure(state="disabled" if locked else "readonly")
        elif f.kind not in ("date", "time"):
            w.configure(state="disabled" if locked else "normal")

    def _fit(self):
        """Give the scrollable field area its natural height, but never more than the screen allows."""
        self.update_idletasks()
        sc = _scale_of(self)
        inner = self.body.winfo_reqheight()
        want = max(100, int(inner / sc) + 8)
        if abs(self.body.cget("height") - want) > 1:
            self.body.configure(height=want)
        self.update_idletasks()
        over = self.winfo_reqheight() - (self.winfo_screenheight() - 150)
        if over > 0:
            self.body.configure(height=max(100, int((inner - over) / sc)))
            self.update_idletasks()

    def _toggle_more(self):
        if self.adv.winfo_manager():
            self.adv.pack_forget()
            self.more_btn.configure(text="More options  ▾")
        else:
            self.adv.pack(fill="x", before=self.more_btn)
            self.more_btn.configure(text="Fewer options  ▴")
        self._fit()
        self.present(False)

    def values(self) -> dict:
        out = {}
        for f in self.fields:
            w = self.widgets[f.key]
            if f.kind == "choice":
                out[f.key] = self.maps[f.key].get(w.get())
            elif f.kind == "multiline":
                out[f.key] = w.get("1.0", "end").strip()
            else:
                out[f.key] = w.get().strip()
        return out

    def submit(self):
        if getattr(self, "_busy", False):          # guard against double clicks
            return
        self._busy = True
        self.submit_btn.configure(state="disabled")
        try:
            self.on_submit(self.values())
        except Exception as e:  # noqa: BLE001 - shown to the user, dialog stays open
            log.info("form rejected: %s: %s", type(e).__name__, e)
            self.err.configure(text=str(e) or type(e).__name__)
            self._busy = False
            self.submit_btn.configure(state="normal")
            return
        self.destroy()
        if self.on_done:
            self.on_done()


class Message(_Modal):
    def __init__(self, parent, title, text, color=None, modal=True):
        super().__init__(parent, title, 600)
        pad = ctk.CTkFrame(self, fg_color="transparent")
        pad.pack(fill="both", expand=True, padx=28, pady=24)
        ctk.CTkLabel(pad, text=title, font=T.font(20, "bold"), text_color=color or T.TEXT, anchor="w").pack(fill="x")
        box = ctk.CTkTextbox(pad, width=540, height=min(440, 60 + 22 * (text.count("\n") + 1 + len(text) // 70)), wrap="word", font=T.font(14))
        box.pack(fill="both", expand=True, pady=12)
        box.insert("1.0", text); box.configure(state="disabled")
        self.default_btn = make_button(pad, "Close", self.destroy, "primary", width=110); self.default_btn.pack(anchor="e")
        self.present(modal)


def show_error(parent, text: str):
    Message(parent, "Cannot complete", text, T.BAD)


def show_info(parent, title: str, text: str):
    Message(parent, title, text)


def confirm(parent, title: str, text: str, *, yes="Confirm", danger=False) -> bool:
    d = _Modal(parent, title, 500)
    result = {"v": False}
    pad = ctk.CTkFrame(d, fg_color="transparent")
    pad.pack(fill="both", expand=True, padx=28, pady=24)
    ctk.CTkLabel(pad, text=title, font=T.font(20, "bold"), text_color=T.WARN if danger else T.TEXT, anchor="w").pack(fill="x")
    ctk.CTkLabel(pad, text=text, wraplength=440, justify="left", text_color=T.TEXT, font=T.font(14), anchor="w").pack(fill="x", pady=14)
    row = ctk.CTkFrame(pad, fg_color="transparent"); row.pack(fill="x")

    def ok():
        result["v"] = True
        d.destroy()
    yes_btn = make_button(row, yes, ok, "danger" if danger else "primary", width=130); yes_btn.pack(side="right")
    if not danger:
        d.default_btn = yes_btn                      # a dangerous action (delete...) is never confirmed by a stray Enter: it needs a click
    make_button(row, "Cancel", d.destroy, "ghost", width=90).pack(side="right", padx=8)
    d.present(True)
    d.wait_window()
    return result["v"]


# ------------------------------------------------------------------ background work
def run_bg(widget, fn: Callable[[], Any], done: Callable[[Any, Exception | None], None]):
    """Run fn on a worker thread; `done(result, error)` is called on the UI thread (never touch widgets from fn)."""
    q: queue.Queue = queue.Queue()

    def work():
        try:
            q.put((fn(), None))
        except Exception as e:  # noqa: BLE001
            q.put((None, e))
    threading.Thread(target=work, daemon=True).start()

    def poll():
        try:
            res, err = q.get_nowait()
        except queue.Empty:
            widget.after(80, poll)
            return
        done(res, err)
    widget.after(80, poll)
