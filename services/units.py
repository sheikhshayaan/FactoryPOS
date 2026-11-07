"""Exact conversions. Money -> integer paisa, quantities -> integer milli-units."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


class UnitError(ValueError):
    pass


def _dec(v) -> Decimal:
    if isinstance(v, float):
        raise UnitError("Floats are not accepted; pass Decimal or str.")
    try:
        d = Decimal(str(v).strip().replace(",", ""))
    except InvalidOperation as e:
        raise UnitError(f"Not a number: {v!r}") from e
    if not d.is_finite():
        raise UnitError("Not a finite number.")
    return d


def to_milli(qty) -> int:
    """Quantity -> milli-units; rejects more than 3 decimal places (no silent rounding)."""
    d = _dec(qty)
    m = d * 1000
    if m != m.to_integral_value():
        raise UnitError("At most 3 decimal places allowed for quantities.")
    return int(m)


def from_milli(m: int) -> Decimal:
    return (Decimal(m) / 1000).quantize(Decimal("0.001"))


def to_paisa(rupees) -> int:
    """Rupees -> paisa, rounding half-up to the nearest paisa."""
    return int((_dec(rupees) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_paisa(p: int) -> Decimal:
    return (Decimal(p) / 100).quantize(Decimal("0.01"))


def mul_div_round(a: int, b: int, c: int) -> int:
    """round_half_up(a*b/c) using integers only (a,b >= 0, c > 0)."""
    if c <= 0 or a < 0 or b < 0:
        raise UnitError("bad arguments")
    return (2 * a * b + c) // (2 * c)
