"""Safe application start: open existing DB, migrate with backup, daily backup."""
from __future__ import annotations

import sqlite3

from config.paths import AppPaths
from database import connection
from database.migrations import migrate
from services import backup_service


def initialize_new_database(paths: AppPaths) -> sqlite3.Connection:
    """Explicit first-run creation. Refuses if a database already exists."""
    paths.ensure_dirs()
    if paths.db.exists():
        raise connection.DatabaseError(f"Database already exists: {paths.db}")
    conn = connection.connect(paths.db, create=True)
    migrate(conn)
    return conn


def open_database(paths: AppPaths) -> sqlite3.Connection:
    """Open the existing DB (never creates one), migrate if needed, daily backup."""
    paths.ensure_dirs()
    conn = connection.connect(paths.db)  # raises DatabaseMissing -> UI offers first-run/restore
    try:
        migrate(conn, backup_hook=lambda: backup_service.create_backup(
            paths, "pre_migration", record_conn=None))
        try:
            backup_service.daily_backup_if_due(paths, conn)
        except backup_service.BackupError:
            pass  # surfaced in UI via backup_history 'failed' rows
    except Exception:
        conn.close()
        raise
    return conn
