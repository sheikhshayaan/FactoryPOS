"""Design tokens (light, dark) + formatting helpers.

Direction: calm "indigo ink" dark theme and cool-paper light theme, ONE accent colour; green/red only mean good/bad.
Every token is a (light, dark) pair that CustomTkinter resolves automatically; use rc() for non-CTk widgets (ttk, matplotlib).
"""
from __future__ import annotations

import sys
from decimal import Decimal

from services.units import mul_div_round

import customtkinter as ctk

# ---- surfaces
BG = ("#f4f5f9", "#0e1220")          # window
PANEL = ("#fbfbfd", "#0a0d19")       # sidebar
CARD = ("#ffffff", "#151a2c")        # raised surface
CARD_ALT = ("#eef0f6", "#1c2238")    # inputs, hover, secondary fills
BORDER = ("#e1e4ee", "#272e49")
SEG_ON = ("#ffffff", "#2c3558")   # selected segment
# ---- text
TEXT = ("#14172b", "#e9ebf5")
MUTED = ("#6a7090", "#8d95b3")
# ---- accent (the one colour)
ACCENT = ("#3d4fdc", "#8b99ff")          # text/links/active
ACCENT_FILL = ("#3d4fdc", "#5565f0")     # filled buttons
ACCENT_HOVER = ("#3243c4", "#6978f5")
ACCENT_SOFT = ("#e4e8ff", "#232b57")     # selected rows, active nav
# ---- meaning only
GOOD = ("#0f8a5f", "#46d19a")
BAD = ("#d6343a", "#ff7a7a")
WARN = ("#a96b00", "#f0b95a")
# ---- dashboard colour language: kind -> (strong colour for numbers/bars, soft tint for the box, meaning)
SOFT_GOOD = ("#e2f5ec", "#12312a")
SOFT_BAD = ("#fde7e8", "#3b1b22")
INFO = ("#1f6fd1", "#6db0ff");    SOFT_INFO = ("#e2eefc", "#14294a")      # money coming in
COST = ("#a96b00", "#f0b95a");    SOFT_COST = ("#fff1d6", "#35290f")      # costs and expenses
OWE = ("#c2500c", "#ff9a55");     SOFT_OWE = ("#fee9da", "#3a2010")       # money you owe
CASH = ("#0a8a8f", "#4fd6d9");    SOFT_CASH = ("#dcf4f4", "#0f2f33")      # cash in hand
STOCK = ("#7a45d6", "#b99aff");   SOFT_STOCK = ("#eee5fd", "#2a1f4d")     # stock and owners' capital
NEUTRAL_SOFT = ("#eef0f6", "#1c2238")
KINDS = {
    "good": (GOOD, SOFT_GOOD), "bad": (BAD, SOFT_BAD), "info": (INFO, SOFT_INFO), "cost": (COST, SOFT_COST),
    "owe": (OWE, SOFT_OWE), "cash": (CASH, SOFT_CASH), "stock": (STOCK, SOFT_STOCK), "neutral": (TEXT, NEUTRAL_SOFT),
}
# legacy names used by pages (colour is now conveyed by button *kind*, not hue)
BLUE, PURPLE, GREEN, AMBER, RED = ACCENT, ACCENT, GOOD, WARN, BAD

FAMILY = "Segoe UI"
FONT = FAMILY            # legacy alias


def rc(c):
    """Resolve a (light, dark) token to the colour for the current appearance mode."""
    if isinstance(c, (tuple, list)):
        return c[1] if ctk.get_appearance_mode() == "Dark" else c[0]
    return c


def font(size: int = 14, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=FAMILY, size=size, weight=weight)


def apply_ctk_theme():
    """Restyle every CustomTkinter widget in one place (call before creating the first window)."""
    th = ctk.ThemeManager.theme
    th["CTk"]["fg_color"] = BG
    th["CTkToplevel"]["fg_color"] = BG
    th["CTkFrame"].update(corner_radius=14, border_width=0, fg_color=CARD, top_fg_color=CARD_ALT, border_color=BORDER)
    th["CTkButton"].update(corner_radius=10, border_width=0, fg_color=ACCENT_FILL, hover_color=ACCENT_HOVER, border_color=BORDER,
                           text_color=("#ffffff", "#ffffff"), text_color_disabled=MUTED)
    th["CTkLabel"].update(text_color=TEXT)
    th["CTkEntry"].update(corner_radius=10, border_width=1, fg_color=CARD_ALT, border_color=BORDER, text_color=TEXT, placeholder_text_color=MUTED)
    th["CTkComboBox"].update(corner_radius=10, border_width=1, fg_color=CARD_ALT, border_color=BORDER, button_color=CARD_ALT,
                             button_hover_color=BORDER, text_color=TEXT, text_color_disabled=MUTED)
    th["CTkOptionMenu"].update(corner_radius=10, fg_color=CARD_ALT, button_color=CARD_ALT, button_hover_color=BORDER, text_color=TEXT)
    th["CTkTextbox"].update(corner_radius=10, border_width=1, fg_color=CARD_ALT, border_color=BORDER, text_color=TEXT,
                            scrollbar_button_color=BORDER, scrollbar_button_hover_color=MUTED)
    th["CTkScrollbar"].update(corner_radius=8, border_spacing=3, fg_color="transparent", button_color=BORDER, button_hover_color=MUTED)
    th["CTkScrollableFrame"].update(label_fg_color=CARD_ALT)
    th["CTkSegmentedButton"].update(corner_radius=10, border_width=3, fg_color=CARD_ALT, selected_color=SEG_ON,
                                    selected_hover_color=SEG_ON, unselected_color=CARD_ALT, unselected_hover_color=BORDER,
                                    text_color=TEXT, text_color_disabled=MUTED)
    th["DropdownMenu"].update(fg_color=CARD, hover_color=CARD_ALT, text_color=TEXT)
    for plat in ("macOS", "Windows", "Linux"):
        th["CTkFont"][plat] = {"family": FAMILY, "size": 14, "weight": "normal"}


def money(paisa: int | None, *, sign: bool = False) -> str:
    if paisa is None:
        return "-"
    neg = paisa < 0
    s = f"{abs(Decimal(paisa)) / 100:,.2f}"
    return ("-" if neg else ("+" if sign and paisa > 0 else "")) + "Rs " + s


def qty(milli: int | None, unit: str = "") -> str:
    if milli is None:
        return "-"
    d = Decimal(milli) / 1000
    s = f"{d:,.3f}".rstrip("0").rstrip(".")
    if unit == "roll" and d != 1:
        unit = "rolls"
    return f"{s} {unit}".strip()


def meters(milli: int | None) -> str:
    """Milli-meters as '3,000 m' / '99.9 m' (one decimal at most, trailing zeros dropped)."""
    if milli is None:
        return "-"
    s = f"{(Decimal(milli) / 1000).quantize(Decimal('0.1'), rounding='ROUND_HALF_UP'):,.1f}".rstrip("0").rstrip(".")
    return f"{s} m"


def qty_full(milli: int | None, unit: str = "", roll_length_milli: int | None = None) -> str:
    """Like qty(), and for paper (a roll length is known) the same amount in meters too: '9.5 rolls (2,850 m)'."""
    text = qty(milli, unit)
    if milli is None or not roll_length_milli or unit != "roll":
        return text
    return f"{text} ({meters(mul_div_round(milli, roll_length_milli, 1000))})"


def pct_bps(bps: int | None) -> str:
    return "-" if bps is None else f"{Decimal(bps) / 100:.2f}%"


def when(ts: str | None) -> str:
    """UTC ISO timestamp -> local date/time."""
    if not ts:
        return "-"
    from datetime import datetime, timezone
    try:
        d = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone()
        return d.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts
