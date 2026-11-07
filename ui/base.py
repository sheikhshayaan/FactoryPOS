"""Shared page plumbing: Context (session) and Page (header, toolbar, guarded actions)."""
from __future__ import annotations

import logging
from typing import Any, Callable

import customtkinter as ctk

from services import auth_service as auth
from ui import theme as T
from ui.widgets import FormDialog, Field, confirm, show_error, make_button

log = logging.getLogger("fms.ui")


class Context:
    def __init__(self, paths):
        self.paths = paths
        self.conn = None
        self.user = None

    def can(self, perm: str) -> bool:
        return self.user is not None and bool(self.user["is_active"]) and auth.has_permission(self.user["role"], perm)


class Page(ctk.CTkFrame):
    title = ""
    subtitle = ""

    def __init__(self, parent, app):
        super().__init__(parent, fg_color="transparent")
        self.app, self.ctx = app, app.ctx
        self.buttons: list[tuple[str, ctk.CTkButton, bool]] = []
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=32, pady=(26, 6))
        ctk.CTkLabel(head, text=self.title, font=T.font(28, "bold"), text_color=T.TEXT).pack(anchor="w")
        if self.subtitle:
            ctk.CTkLabel(head, text=self.subtitle, text_color=T.MUTED, font=T.font(14), wraplength=920, justify="left").pack(anchor="w", pady=(2, 0))
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=32, pady=(10, 20))
        self.build()

    # --- to override
    def build(self): ...
    def refresh(self): ...

    # --- helpers
    @property
    def conn(self):
        return self.ctx.conn

    @property
    def user(self):
        return self.ctx.user

    def toolbar(self, parent=None):
        f = ctk.CTkFrame(parent or self.body, fg_color="transparent")
        f.pack(fill="x", pady=(0, 12))
        f._has_primary = False
        return f

    def button(self, bar, text: str, cmd: Callable, perm: str | None = None, color=None, width: int = 0):
        """Style by role, not hue: the first positive action on a bar is the filled primary; destructive ones are red outlines."""
        if color == T.RED:
            kind = "danger"
        elif color in (T.GREEN, T.BLUE) and not getattr(bar, "_has_primary", False):
            kind, bar._has_primary = "primary", True
        else:
            kind = "secondary"
        allowed = not (perm and not self.ctx.can(perm))
        b = make_button(bar, text, cmd, kind, width)
        if not allowed:
            b.configure(state="disabled")
        b.pack(side="left", padx=(0, 8))
        self.buttons.append((text, b, allowed))
        return b

    def go(self, key: str):
        self.app.show(key)

    def menu_button(self, bar, text: str, items: list[tuple[str, Callable, str | None]]):
        """A 'More' button that pops up secondary actions, so a toolbar shows only the common ones."""
        import tkinter as tk
        m = tk.Menu(self, tearoff=0, font=(T.FAMILY, 12), bg=T.rc(T.CARD), fg=T.rc(T.TEXT), activebackground=T.rc(T.ACCENT_SOFT), activeforeground=T.rc(T.TEXT), bd=0)
        for label, cmd, perm in items:
            allowed = not (perm and not self.ctx.can(perm))
            m.add_command(label=label, command=cmd, state="normal" if allowed else "disabled")
            self.buttons.append((label, m, allowed))
        b = make_button(bar, text + "  ▾", lambda: m.tk_popup(b.winfo_rootx(), b.winfo_rooty() + b.winfo_height()), "secondary")
        b.pack(side="left", padx=(0, 8))
        return b

    def form(self, title: str, fields: list[Field], submit: Callable[[dict], Any], *, intro="", submit_text="Save", live=None):
        return FormDialog(self, title, fields, submit, submit_text=submit_text, intro=intro, on_done=self.refresh, live=live)

    def guard(self, fn: Callable, *, done: Callable | None = None):
        """Run a service call; any error is shown to the user (and logged without data), UI refreshes on success."""
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            log.warning("action failed: %s: %s", type(e).__name__, e)
            show_error(self, str(e) or type(e).__name__)
            return None
        (done or self.refresh)()
        return res

    def need(self, row, what="a row") -> bool:
        if row is None:
            show_error(self, f"Select {what} in the table first.")
            return False
        return True

    def confirm(self, title, text, **kw) -> bool:
        return confirm(self, title, text, **kw)
