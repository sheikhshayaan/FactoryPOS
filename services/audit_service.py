"""Append-only audit trail. Secrets are redacted before storage."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from database.connection import utc_now

_SECRET_HINTS = ("password", "passwd", "secret", "token", "hash", "key")


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if any(h in str(k).lower() for h in _SECRET_HINTS) else _redact(v))
                for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_redact(v) for v in obj]
    return obj


def log(conn: sqlite3.Connection, action: str, *, user: dict | sqlite3.Row | None = None,
        username: str | None = None, entity: str | None = None, entity_id: Any = None,
        reason: str | None = None, details: dict | None = None) -> None:
    """Write one audit row. Call inside the caller's transaction so the audit
    entry commits or rolls back together with the business change."""
    uid = user["id"] if user else None
    uname = user["username"] if user else username
    conn.execute(
        "INSERT INTO audit_log(ts,user_id,username,action,entity,entity_id,reason,details) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (utc_now(), uid, uname, action, entity,
         None if entity_id is None else str(entity_id), reason,
         json.dumps(_redact(details), default=str) if details else None))
