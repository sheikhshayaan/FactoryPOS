"""Electricity cost allocation. Never fabricates data.

Methods
  meter  : kWh used (from machine meter readings) x effective rate -> actual
  hourly : hours run x configured hourly rate                      -> ESTIMATE
Effective rate comes from a real utility bill (FESCO/WAPDA-successor in Faisalabad):
bill total / billed units, so taxes and fixed charges as billed are included.
Anything not allocated to machines stays in factory overhead (no double count).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from services.units import mul_div_round


class ElectricityDataMissing(Exception):
    pass


@dataclass(frozen=True)
class ElectricityCost:
    paisa: int
    is_estimate: bool
    basis: str


def effective_rate_paisa_per_kwh(bill_total_paisa: int, billed_units_kwh_milli: int) -> Decimal:
    if billed_units_kwh_milli <= 0:
        raise ElectricityDataMissing("Billed units must be positive.")
    return Decimal(bill_total_paisa) * 1000 / Decimal(billed_units_kwh_milli)


def cost_from_meter(kwh_start_milli: int, kwh_end_milli: int, bill_total_paisa: int,
                    billed_units_kwh_milli: int) -> ElectricityCost:
    if kwh_end_milli < kwh_start_milli:
        raise ElectricityDataMissing("Meter reading went backwards; correct the readings.")
    used = kwh_end_milli - kwh_start_milli
    paisa = mul_div_round(bill_total_paisa, used, billed_units_kwh_milli)
    return ElectricityCost(paisa, False, f"meter {used/1000:.3f} kWh x bill effective rate")


def cost_from_hourly(minutes_run: int, hourly_rate_paisa: int | None) -> ElectricityCost:
    if hourly_rate_paisa is None:
        raise ElectricityDataMissing("No hourly rate configured for this machine.")
    if minutes_run < 0:
        raise ElectricityDataMissing("Negative run time.")
    return ElectricityCost(mul_div_round(hourly_rate_paisa, minutes_run, 60), True,
                           f"ESTIMATE: {minutes_run} min x hourly rate")


def allocate_bill_by_machine(bill_total_paisa: int, billed_units_kwh_milli: int,
                             machine_kwh_milli: dict[int, int]) -> dict:
    """Split a real bill across machines by metered kWh. Returns per-machine paisa plus
    'unallocated' (remaining bill -> factory overhead, NOT job cost)."""
    total_metered = sum(machine_kwh_milli.values())
    if total_metered > billed_units_kwh_milli:
        raise ElectricityDataMissing("Machines metered more kWh than the bill covers; check readings.")
    per = {m: mul_div_round(bill_total_paisa, k, billed_units_kwh_milli) for m, k in machine_kwh_milli.items()}
    return {"machines": per, "unallocated": bill_total_paisa - sum(per.values())}


def meter_usage(conn: sqlite3.Connection, machine_id: int, start: str, end: str) -> tuple[int, int]:
    """(kwh_start, kwh_end) from recorded readings at or before each timestamp. Raises if absent."""
    def at(ts: str):
        return conn.execute("SELECT kwh_milli FROM machine_meter_readings WHERE machine_id=? AND read_at<=? "
                            "ORDER BY read_at DESC LIMIT 1", (machine_id, ts)).fetchone()
    s, e = at(start), at(end)
    if not s or not e:
        raise ElectricityDataMissing("No meter reading available for that period; cost is unknown.")
    return s[0], e[0]
