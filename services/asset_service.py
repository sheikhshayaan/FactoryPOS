"""Asset register: plain records of what the factory owns (machines, equipment) with their price.

These are NOT accounting entries. Nothing here posts to the journal, so cash in hand, profit, expenses and every report are untouched.
A wrong record is voided with a reason (it stays visible); it is never edited or deleted.
"""
from __future__ import annotations

from datetime import date as _date

from database.connection import transaction, utc_now
from services import audit_service
from services.auth_service import require_permission
from services.units import to_paisa


class AssetError(Exception):
    pass


def add_asset(conn, actor, *, name: str, price, on: str, notes: str = "") -> int:
    require_permission(actor, "record_expense")
    name = (name or "").strip()
    if not name:
        raise AssetError("Write what the asset is (for example 'Rotary printing machine').")
    p = to_paisa(price)
    if p <= 0:
        raise AssetError("The price must be more than zero.")
    try:
        _date.fromisoformat(on)
    except (ValueError, TypeError) as e:
        raise AssetError(f"Invalid date: {on!r} (use YYYY-MM-DD)") from e
    with transaction(conn):
        cur = conn.execute("INSERT INTO asset_records(acquired_on,name,price_paisa,notes,created_by,created_at) VALUES (?,?,?,?,?,?)",
                           (on, name, p, (notes or "").strip() or None, actor["id"], utc_now()))
        audit_service.log(conn, "asset.add", user=actor, entity="asset", entity_id=cur.lastrowid, details={"name": name, "paisa": p})
        return cur.lastrowid


def void_asset(conn, actor, asset_id: int, reason: str) -> None:
    require_permission(actor, "record_expense")
    if not (reason or "").strip():
        raise AssetError("A reason is required.")
    with transaction(conn):
        r = conn.execute("SELECT * FROM asset_records WHERE id=?", (asset_id,)).fetchone()
        if r is None:
            raise AssetError("Asset record not found.")
        if r["voided_at"]:
            raise AssetError("This record is already voided.")
        conn.execute("UPDATE asset_records SET voided_at=?, voided_by=?, void_reason=? WHERE id=?", (utc_now(), actor["id"], reason.strip(), asset_id))
        audit_service.log(conn, "asset.void", user=actor, entity="asset", entity_id=asset_id, reason=reason.strip())


def total_value(conn) -> int:
    """Sum of the active (not voided) records - a list total only, never part of the books."""
    return conn.execute("SELECT COALESCE(SUM(price_paisa),0) FROM asset_records WHERE voided_at IS NULL").fetchone()[0]
