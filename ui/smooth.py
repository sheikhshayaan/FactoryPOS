"""Smoother window resizing.

CustomTkinter redraws every one of its rounded widgets on EVERY size step of a window drag (a page has hundreds), so dragging the window
edge felt choppy. Here the redraw is postponed while the window size is changing and done once when it settles (about 0.12 s after the last
step). During the drag a card's rounded outline may lag a little behind its content; it snaps into place when the drag stops.
Charts (matplotlib) are redrawn once after the resize too, instead of on every step."""
from __future__ import annotations

import tkinter as tk

from customtkinter.windows.widgets.core_widget_classes.ctk_base_class import CTkBaseClass

SETTLE_MS = 120
CHART_SETTLE_MS = 160


class _State:
    active = False
    pending: dict = {}
    job = None
    size = None


_orig = CTkBaseClass._update_dimensions_event


def _patched(self, event):
    if _State.active:
        _State.pending[self] = event                       # the newest size wins; it is applied when the window stops changing
        return
    _orig(self, event)


def install() -> None:
    """Call once, before any widget is created (widgets bind the method when they are made)."""
    if CTkBaseClass._update_dimensions_event is not _patched:
        CTkBaseClass._update_dimensions_event = _patched


def watch(root) -> None:
    def flush():
        _State.active, _State.job = False, None
        pending, _State.pending = _State.pending, {}
        for w, ev in pending.items():
            try:
                if w.winfo_exists():
                    _orig(w, ev)
            except tk.TclError:
                pass                                       # the widget was destroyed meanwhile

    def on_configure(e):
        if e.widget is not root:
            return
        size = (e.width, e.height)
        if size == _State.size:
            return                                         # moved, not resized
        _State.size = size
        _State.active = True
        if _State.job:
            root.after_cancel(_State.job)
        _State.job = root.after(SETTLE_MS, flush)
    root.bind("<Configure>", on_configure, add="+")


def embed_chart(fig, master, **pack):
    """Put a matplotlib figure into `master`: it follows the width of its holder, but is redrawn once the size has settled, not on every step."""
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    c = FigureCanvasTkAgg(fig, master=master)
    w = c.get_tk_widget()
    w.focus_set = lambda *a, **k: None                     # charts do not need the keyboard; matplotlib would otherwise grab the focus
    w.unbind("<Configure>")                                # matplotlib's own handler redraws on every step
    job = {"id": None, "ev": None}

    def later(e):
        job["ev"] = e
        if job["id"]:
            w.after_cancel(job["id"])
        job["id"] = w.after(CHART_SETTLE_MS, apply)

    def apply():
        job["id"] = None
        e = job["ev"]
        if e is not None and e.width > 20 and e.height > 20 and w.winfo_exists():
            c.resize(e)
    w.bind("<Configure>", later, add="+")
    w.pack(**pack)
    return c


def bold_all_text(fig) -> None:
    """Bold every text of the figure (tick labels included) with ONE cheap layout pass, no real drawing."""
    from matplotlib.text import Text
    fig.draw_without_rendering()                           # makes the tick labels exist so they can be bolded too
    for t in fig.findobj(Text):
        t.set_fontweight("bold")
