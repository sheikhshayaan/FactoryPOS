"""Pure, deterministic pricing maths (no database). All money in integer paisa.

markup = profit / cost            margin = profit / selling price
target margin price = cost / (1 - margin)    (margin in [0, 100) percent)
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from services.units import UnitError, _dec


class PricingError(ValueError):
    pass


@dataclass(frozen=True)
class PriceQuote:
    method: str
    price_paisa: int
    cost_paisa: int
    explanation: str

    @property
    def profit_paisa(self) -> int:
        return self.price_paisa - self.cost_paisa


def _round(d: Decimal) -> int:
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _pct(v, name) -> Decimal:
    try:
        return _dec(v)
    except UnitError as e:
        raise PricingError(f"{name}: {e}") from e


def markup_pct(cost_paisa: int, price_paisa: int) -> Decimal | None:
    return None if cost_paisa <= 0 else Decimal(price_paisa - cost_paisa) * 100 / cost_paisa


def margin_pct(cost_paisa: int, price_paisa: int) -> Decimal | None:
    return None if price_paisa <= 0 else Decimal(price_paisa - cost_paisa) * 100 / price_paisa


def price_cost_plus(cost_paisa: int, markup_percent) -> PriceQuote:
    m = _pct(markup_percent, "markup")
    if m < 0:
        raise PricingError("Markup cannot be negative.")
    price = _round(Decimal(cost_paisa) * (1 + m / 100))
    return PriceQuote("cost_plus", price, cost_paisa,
                      f"price = cost x (1 + markup) = {cost_paisa/100:.2f} x (1 + {m}%) = {price/100:.2f}")


def price_target_margin(cost_paisa: int, margin_percent) -> PriceQuote:
    m = _pct(margin_percent, "margin")
    if m < 0 or m >= 100:
        raise PricingError("Target margin must be >= 0% and < 100%.")
    price = _round(Decimal(cost_paisa) / (1 - m / 100))
    return PriceQuote("target_margin", price, cost_paisa,
                      f"price = cost / (1 - margin) = {cost_paisa/100:.2f} / (1 - {m}%) = {price/100:.2f}")


def price_per_meter(cost_paisa: int, meters, rate_per_meter_rupees) -> PriceQuote:
    mt, r = _pct(meters, "meters"), _pct(rate_per_meter_rupees, "rate")
    if mt <= 0 or r < 0:
        raise PricingError("Meters must be positive and rate non-negative.")
    price = _round(mt * r * 100)
    return PriceQuote("per_meter", price, cost_paisa, f"price = {mt} m x Rs {r}/m = {price/100:.2f}")


def price_fixed(cost_paisa: int, charge_paisa: int) -> PriceQuote:
    if charge_paisa < 0:
        raise PricingError("Charge cannot be negative.")
    return PriceQuote("fixed", charge_paisa, cost_paisa, f"fixed service charge {charge_paisa/100:.2f}")


def price_manual(cost_paisa: int, price_paisa: int) -> PriceQuote:
    if price_paisa < 0:
        raise PricingError("Price cannot be negative.")
    return PriceQuote("manual", price_paisa, cost_paisa, f"manually approved price {price_paisa/100:.2f}")
