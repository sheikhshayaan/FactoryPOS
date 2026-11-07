"""SQLite connection helpers: safe pragmas, explicit transactions, error mapping."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

BUSY_TIMEOUT_MS = 30_000


class DatabaseError(Exception):
    """Base for database problems surfaced to the UI."""


class DatabaseBusy(DatabaseError):
    pass


class DatabaseDiskFull(DatabaseError):
    pass


class DatabaseCorrupt(DatabaseError):
    pass


class DatabaseMissing(DatabaseError):
    pass


def translate_error(exc: sqlite3.Error) -> DatabaseError | sqlite3.Error:
    msg = str(exc).lower()
    if "locked" in msg or "busy" in msg:
        return DatabaseBusy("The database is busy. Try again in a moment.")
    if "disk is full" in msg or "database or disk is full" in msg:
        return DatabaseDiskFull("The disk is full. Free space before continuing.")
    if "malformed" in msg or "not a database" in msg or "corrupt" in msg:
        return DatabaseCorrupt("The database file appears to be damaged. Do not write; use Restore.")
    return exc


def connect(db_path: str | Path, *, create: bool = False, readonly: bool = False) -> sqlite3.Connection:
    """Open a connection with integrity-oriented pragmas.

    create=False refuses to silently make a new empty database (which would
    happen if the working directory changed). Initialisation is explicit.
    """
    p = Path(db_path)
    if readonly:
        if not p.exists():
            raise DatabaseMissing(f"Database not found: {p}")
        conn = sqlite3.connect(f"{p.resolve().as_uri()}?mode=ro", uri=True,
                               timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    else:
        if not p.exists() and not create:
            raise DatabaseMissing(f"Database not found: {p}")
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
    return conn


_sp_counter = 0


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Atomic unit of work. Outermost call: BEGIN IMMEDIATE ... COMMIT/ROLLBACK.
    Nested calls (a service called from another service's transaction) use a
    SAVEPOINT, so an inner failure rolls back its part and, if re-raised, the
    whole outer unit rolls back too."""
    global _sp_counter
    if conn.in_transaction:
        _sp_counter += 1
        name = f"sp_{_sp_counter}"
        conn.execute(f"SAVEPOINT {name}")
        try:
            yield conn
        except BaseException:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
            raise
        else:
            conn.execute(f"RELEASE {name}")
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as e:
        raise translate_error(e) from e
    try:
        yield conn
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    else:
        try:
            conn.execute("COMMIT")
        except sqlite3.Error as e:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise translate_error(e) from e


def claim_idempotency_key(conn: sqlite3.Connection, key: str, operation: str) -> bool:
    """Call inside a transaction before posting money/stock. Returns False if
    this key was already used (duplicate submission) so the caller must skip."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO idempotency_keys(key, operation, created_at) VALUES (?,?,?)",
        (key, operation, utc_now()),
    )
    return cur.rowcount == 1


def utc_now() -> str:
    """All timestamps: UTC, ISO-8601 'YYYY-MM-DDTHH:MM:SSZ'."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
