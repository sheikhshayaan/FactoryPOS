"""Cost Calculator: what one linear metre of printing costs, from figures the user types in.

This is a stand-alone planning tool. It never reads the database and never writes to the books: every number comes from the
person using it. Exact Decimal arithmetic; results are estimates shown to 2 decimals.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

# key, label, unit, short help  (order = order on screen)
FIELDS = [
    ("paper", "Paper cost per linear metre", "Rs / m", "What one metre of paper costs you."),
    ("ink_price", "Ink price per litre", "Rs / litre", "Price you pay for one litre of ink."),
    ("ink_use", "Ink used per square metre", "ml / m²", "How much ink the design uses on one square metre."),
    ("width", "Print width", "m", "Width of the printed cloth or paper."),
    ("power", "Machine power", "kW", "Power the machine draws while running."),
    ("per_day", "Production per day", "m / day", "Linear metres printed in one working day."),
    ("days", "Working days per month", "days", "Days the machine runs in a month."),
    ("hours", "Machine hours per day", "hours", "Hours the machine actually runs in a day."),
    ("rate", "Electricity rate", "Rs / kWh", "Your electricity price per unit."),
    ("labour", "Labour cost per month", "Rs / month", "Wages of the people working on this machine in a month."),
]
LABEL = {k: label for k, label, _u, _h in FIELDS}
MUST_BE_MORE_THAN_ZERO = ("per_day", "days", "hours")        # these divide: zero would make the answer meaningless
LEVELS = (10, 12, 15, 18, 20, 22, 25, 30, 35, 40)           # ink coverage steps for the scenario table (ml / m²)


_BASE = dict(paper="60", ink_price="2000", ink_use="20", width="2.6", power="6", per_day="2000", days="30", hours="20", rate="38.44", labour="100000")
# starting points taken from the owner's calculator sheet; they are examples to start from, not measured figures of this factory
PRESETS = {
    "standard": ("Standard preset", dict(_BASE)),
    "high_speed": ("High speed", {**_BASE, "ink_use": "15", "per_day": "2800", "power": "6.5"}),
    "heavy_ink": ("Heavy ink pass", {**_BASE, "ink_use": "30", "per_day": "1600"}),
}


class CostError(ValueError):
    pass


def parse(values: dict) -> dict[str, Decimal]:
    """Turn what was typed into Decimals. Commas are allowed (1,50,000). Raises CostError saying which field is wrong or missing."""
    out: dict[str, Decimal] = {}
    missing = []
    for key, label, _u, _h in FIELDS:
        raw = str(values.get(key, "") or "").replace(",", "").strip()
        if raw == "":
            missing.append(label); continue
        try:
            d = Decimal(raw)
        except InvalidOperation:
            raise CostError(f"{label}: '{values.get(key)}' is not a number.") from None
        if not d.is_finite() or d < 0:
            raise CostError(f"{label}: use a number that is not negative.")
        if d == 0 and key in MUST_BE_MORE_THAN_ZERO:
            raise CostError(f"{label} must be more than 0.")
        out[key] = d
    if missing:
        raise CostError("Still to fill in: " + ", ".join(missing) + ".")
    return out


def _cost_for(v: dict[str, Decimal], ink_use: Decimal) -> dict[str, Decimal]:
    ink_per_ml = v["ink_price"] / 1000
    ink_ml_per_m = ink_use * v["width"]                           # one linear metre is `width` square metres
    ink = ink_ml_per_m * ink_per_ml
    monthly_m = v["per_day"] * v["days"]
    labour = v["labour"] / monthly_m
    power_per_hour = v["rate"] * v["power"]
    per_hour = v["per_day"] / v["hours"]
    elec = power_per_hour / per_hour
    total = v["paper"] + ink + labour + elec
    return {"ink_per_ml": ink_per_ml, "ink_ml_per_m": ink_ml_per_m, "ink": ink, "labour": labour, "power_per_hour": power_per_hour,
            "per_hour": per_hour, "elec": elec, "total": total, "monthly_m": monthly_m}


def calculate(values: dict) -> dict:
    """All results for the typed figures (see parse for errors)."""
    v = parse(values)
    r = _cost_for(v, v["ink_use"])
    r["paper"] = v["paper"]
    r["per_day_cost"] = r["total"] * v["per_day"]
    r["per_month_cost"] = r["total"] * r["monthly_m"]
    r["per_day_m"], r["width"], r["ink_use"] = v["per_day"], v["width"], v["ink_use"]
    parts = [("Paper", r["paper"]), ("Ink", r["ink"]), ("Labour", r["labour"]), ("Electricity", r["elec"])]
    r["shares"] = [(name, amt, (amt / r["total"] * 100) if r["total"] > 0 else Decimal(0)) for name, amt in parts]
    levels = sorted(set(LEVELS) | {v["ink_use"]})
    rows = []
    for lv in levels:
        c = _cost_for(v, Decimal(lv))
        rows.append({"coverage": Decimal(lv), "ink_ml": c["ink_ml_per_m"], "ink_cost": c["ink"], "total": c["total"],
                     "per_day": c["total"] * v["per_day"], "diff": c["total"] - r["total"], "current": Decimal(lv) == v["ink_use"]})
    r["scenarios"] = rows
    return r


def money(d: Decimal | float | int, places: int = 2) -> str:
    return f"Rs {Decimal(d):,.{places}f}"


def num(d: Decimal | float | int, places: int = 2) -> str:
    return f"{Decimal(d):,.{places}f}"
