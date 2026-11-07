"""Whitelisted, validated application settings (owner only, audited)."""
from __future__ import annotations

from database.connection import transaction
from services import audit_service
from services.auth_service import require_permission


class SettingsError(Exception):
    pass


def _month(v):
    if not v.isdigit() or not 1 <= int(v) <= 12:
        raise SettingsError("Must be a month number 1-12.")


def _posint(v):
    if not v.isdigit() or int(v) < 1:
        raise SettingsError("Must be a whole number of at least 1.")


def _enum(*opts):
    def chk(v):
        if v not in opts:
            raise SettingsError(f"Must be one of: {', '.join(opts)}")
    return chk


SCHEMA = {
    "factory_name": lambda v: None, "factory_address": lambda v: None, "factory_phone": lambda v: None,
    "currency": lambda v: None if 2 <= len(v) <= 5 else (_ for _ in ()).throw(SettingsError("Currency code, e.g. PKR")),
    "fiscal_year_start_month": _month, "absence_deduction_basis": _enum("off", "calendar_days"),
    "leave_deduction": _enum("paid", "unpaid"), "auto_accrue_payroll": _enum("0", "1"),
    "backup_retention": _posint, "backup_dir": lambda v: None, "allow_negative_cash": _enum("0", "1"),
}


DEFAULTS = {"currency": "PKR", "fiscal_year_start_month": "7", "absence_deduction_basis": "calendar_days", "leave_deduction": "paid", "auto_accrue_payroll": "1",
            "backup_retention": "30",
            "allow_negative_cash": "0", "backup_dir": "", "factory_name": "", "factory_address": "", "factory_phone": ""}


def get(conn, key, default=None) -> str:
    """Stored value, else the documented default (so screens never show a blank that can't be saved)."""
    r = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    if r:
        return r[0]
    return DEFAULTS.get(key, "") if default is None else default


def set_values(conn, actor, values: dict[str, str]) -> None:
    require_permission(actor, "manage_users")
    clean = {}
    for k, v in values.items():
        if k not in SCHEMA:
            raise SettingsError(f"Unknown setting: {k}")
        v = str(v).strip()
        SCHEMA[k](v)
        clean[k] = v
    with transaction(conn):
        for k, v in clean.items():
            old = get(conn, k)
            if old != v:
                conn.execute("INSERT INTO app_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
                audit_service.log(conn, "setting.change", user=actor, entity="setting", entity_id=k,
                                  details={"from": old, "to": v})


# ------------------------------------------------------------------ locked cost per meter
LOCKED_COST_KEY = "locked_cost_per_meter"
MAX_LOCKED_COST_PAISA = 100_000_000          # Rs 1,000,000 per meter: anything above is a typing mistake


def locked_cost(conn) -> int | None:
    """The cost per meter the owner decided on (paisa), or None when nothing is locked. Shown on Create invoice."""
    r = conn.execute("SELECT value FROM app_settings WHERE key=?", (LOCKED_COST_KEY,)).fetchone()
    return int(r[0]) if r and r[0] and r[0].isdigit() and int(r[0]) > 0 else None


def set_locked_cost(conn, actor, rupees) -> int | None:
    """Lock the cost per meter at the typed figure (e.g. the calculator says 167.97, the owner locks 170). Empty = remove the lock.
    It is only a figure to show: it never posts to the books."""
    from services.auth_service import require_permission as _req
    from services.units import to_paisa
    _req(actor, "manage_invoices")
    raw = str(rupees if rupees is not None else "").replace(",", "").strip()
    if raw == "":
        new = ""
    else:
        try:
            p = to_paisa(raw)
        except Exception:  # noqa: BLE001
            raise SettingsError(f"'{raw}' is not a number.") from None
        if p <= 0:
            raise SettingsError("The locked cost per meter must be more than 0.")
        if p > MAX_LOCKED_COST_PAISA:
            raise SettingsError("That is too large for a cost per meter. Check the figure.")
        new = str(p)
    with transaction(conn):
        old = conn.execute("SELECT value FROM app_settings WHERE key=?", (LOCKED_COST_KEY,)).fetchone()
        old = old[0] if old else ""
        if old != new:
            conn.execute("INSERT INTO app_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (LOCKED_COST_KEY, new))
            audit_service.log(conn, "setting.change", user=actor, entity="setting", entity_id=LOCKED_COST_KEY, details={"from": old, "to": new})
    return int(new) if new else None

