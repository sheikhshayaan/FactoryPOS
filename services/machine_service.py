"""Machines, meter readings and electricity bills."""
from __future__ import annotations

import sqlite3

from database.connection import transaction, utc_now
from services import audit_service
from services.auth_service import require_permission
from services.units import to_milli, to_paisa


class MachineError(Exception):
    pass


def add_machine(conn, actor, name, kind="other", hourly_rate=None) -> int:
    require_permission(actor, "manage_machines")
    with transaction(conn):
        try:
            cur = conn.execute("INSERT INTO machines(name,kind,hourly_rate_paisa) VALUES (?,?,?)",
                               (name.strip(), kind, to_paisa(hourly_rate) if hourly_rate not in (None, "") else None))
        except sqlite3.IntegrityError as e:
            raise MachineError(f"Cannot add machine: {e}") from e
        audit_service.log(conn, "machine.add", user=actor, entity="machine", entity_id=cur.lastrowid)
        return cur.lastrowid


def set_hourly_rate(conn, actor, machine_id, hourly_rate) -> None:
    """Hourly electricity rate (Rs/hour) used for ESTIMATED electricity cost. Blank clears it."""
    require_permission(actor, "manage_machines")
    rate = None if hourly_rate in (None, "") else to_paisa(hourly_rate)
    if rate is not None and rate < 0:
        raise MachineError("Rate cannot be negative.")
    with transaction(conn):
        old = conn.execute("SELECT hourly_rate_paisa FROM machines WHERE id=?", (machine_id,)).fetchone()
        if not old:
            raise MachineError("Unknown machine.")
        conn.execute("UPDATE machines SET hourly_rate_paisa=? WHERE id=?", (rate, machine_id))
        audit_service.log(conn, "machine.rate", user=actor, entity="machine", entity_id=machine_id,
                          details={"from": old[0], "to": rate})


def add_meter_reading(conn, actor, machine_id, read_at, kwh) -> int:
    require_permission(actor, "manage_machines")
    k = to_milli(kwh)
    with transaction(conn):
        last = conn.execute("SELECT kwh_milli FROM machine_meter_readings WHERE machine_id=? AND read_at<=? "
                            "ORDER BY read_at DESC LIMIT 1", (machine_id, read_at)).fetchone()
        if last and k < last[0]:
            raise MachineError("Reading is lower than the previous one.")
        try:
            cur = conn.execute("INSERT INTO machine_meter_readings(machine_id,read_at,kwh_milli,recorded_by) VALUES (?,?,?,?)",
                               (machine_id, read_at, k, actor["id"]))
        except sqlite3.IntegrityError as e:
            raise MachineError("A reading already exists for that time.") from e
        return cur.lastrowid


def add_electricity_bill(conn, actor, *, period_start, period_end, units_kwh, total_amount, reference="", provider="FESCO") -> int:
    """Record a real utility bill (reference for meter-based costing). Expense is booked separately via Expenses."""
    require_permission(actor, "manage_machines")
    u, t = to_milli(units_kwh), to_paisa(total_amount)
    if u <= 0 or t < 0 or period_end < period_start:
        raise MachineError("Check the units, amount and dates.")
    with transaction(conn):
        cur = conn.execute("INSERT INTO electricity_bills(provider,period_start,period_end,units_kwh_milli,total_amount_paisa,reference,created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (provider, period_start, period_end, u, t, reference, utc_now()))
        audit_service.log(conn, "electricity.bill", user=actor, entity="bill", entity_id=cur.lastrowid, details={"paisa": t})
        return cur.lastrowid
