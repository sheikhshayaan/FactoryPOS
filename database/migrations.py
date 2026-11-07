"""Versioned, transactional migrations. Files: migrations/NNNN_name.sql.

Never re-runs schema blindly: applies only versions above the stored one.
Takes a consistent backup first when migrating an existing database.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .connection import utc_now

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    out = []
    for f in sorted(directory.glob("*.sql")):
        m = _NAME.match(f.name)
        if not m:
            raise MigrationError(f"Bad migration filename: {f.name}")
        out.append(Migration(int(m.group(1)), m.group(2), f.read_text(encoding="utf-8")))
    versions = [m.version for m in out]
    if versions != sorted(set(versions)):
        raise MigrationError("Duplicate or unordered migration versions")
    return out


def current_version(conn: sqlite3.Connection) -> int:
    has = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if not has:
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return row[0] or 0


def latest_version(migrations: Sequence[Migration] | None = None) -> int:
    ms = migrations if migrations is not None else load_migrations()
    return ms[-1].version if ms else 0


def migrate(conn: sqlite3.Connection, *, migrations: Sequence[Migration] | None = None,
            backup_hook: Callable[[], object] | None = None) -> list[int]:
    """Apply pending migrations; each in its own transaction. Returns applied versions.

    backup_hook is called once before the first pending migration if the
    database already has data (version > 0)."""
    ms = list(migrations) if migrations is not None else load_migrations()
    cur = current_version(conn)
    if cur > latest_version(ms):
        raise MigrationError(
            f"Database schema v{cur} is newer than this application (v{latest_version(ms)}). "
            "Update the application; refusing to open.")
    pending = [m for m in ms if m.version > cur]
    if not pending:
        return []
    if cur > 0 and backup_hook:
        backup_hook()  # raises -> abort before touching anything
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    applied = []
    for m in pending:
        script = (f"BEGIN IMMEDIATE;\n{m.sql}\n"
                  f"INSERT INTO schema_migrations(version,name,applied_at) "
                  f"VALUES ({m.version},'{m.name}','{utc_now()}');\nCOMMIT;")
        try:
            conn.executescript(script)
        except sqlite3.Error as e:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"Migration {m.version}_{m.name} failed and was rolled back: {e}") from e
        applied.append(m.version)
    return applied
