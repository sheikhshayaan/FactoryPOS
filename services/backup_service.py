"""Consistent live backups (SQLite backup API), verification and safe restore."""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from config.paths import AppPaths
from database import connection
from database.migrations import latest_version
from services import audit_service

REQUIRED_TABLES = {"users", "audit_log", "app_settings", "backup_history",
                   "idempotency_keys", "schema_migrations"}
DEFAULT_RETENTION_AUTO = 30


class BackupError(Exception):
    pass


@dataclass
class BackupInfo:
    path: Path
    kind: str
    size_bytes: int
    sha256: str
    created_at: str


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_database_file(path: Path, *, required_tables=REQUIRED_TABLES) -> None:
    """Raises BackupError unless the file passes integrity, FK and schema checks."""
    if not Path(path).is_file():
        raise BackupError(f"File not found: {path}")
    try:
        conn = connection.connect(path, readonly=True)
    except (sqlite3.Error, connection.DatabaseError) as e:
        raise BackupError(f"Cannot open as a database: {e}") from e
    try:
        res = conn.execute("PRAGMA integrity_check").fetchall()
        if [r[0] for r in res] != ["ok"]:
            raise BackupError("integrity_check failed: " + "; ".join(str(r[0]) for r in res[:5]))
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise BackupError(f"foreign_key_check found {len(fk)} violation(s)")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = required_tables - tables
        if missing:
            raise BackupError(f"Missing required tables: {sorted(missing)}")
        ver = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
        if ver > latest_version():
            raise BackupError(f"Backup schema v{ver} is newer than this application.")
    except sqlite3.DatabaseError as e:
        raise BackupError(f"Database check failed: {e}") from e
    finally:
        conn.close()


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def create_backup(paths: AppPaths, kind: str = "manual", *, dest_dir: Path | None = None,
                  record_conn: sqlite3.Connection | None = None, user=None) -> BackupInfo:
    """Backup the live DB via sqlite3 backup API (safe with active WAL writers).
    The file is written as .tmp, verified, then atomically renamed."""
    if kind not in ("auto", "manual", "pre_migration", "pre_restore"):
        raise BackupError("bad kind")
    dest_dir = Path(dest_dir) if dest_dir else paths.backups
    final = dest_dir / f"factory_{_stamp()}_{kind}.db"
    tmp = final.with_suffix(".db.tmp")
    err: str | None = None
    info: BackupInfo | None = None
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)           # a missing / unplugged folder is a failed backup, reported as one
        src = connection.connect(paths.db)
        try:
            dst = sqlite3.connect(str(tmp))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        verify_database_file(tmp)
        os.replace(tmp, final)
        info = BackupInfo(final, kind, final.stat().st_size, _sha256(final),
                          connection.utc_now())
    except (OSError, sqlite3.Error, BackupError, connection.DatabaseError) as e:
        err = str(e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass                                               # the folder itself is unreachable: nothing to clean up
    if record_conn is not None:
        _record(record_conn, kind, info, final, err, user)
    if err:
        raise BackupError(f"Backup failed: {err}")
    return info  # type: ignore[return-value]


def _record(conn, kind, info, final, err, user) -> None:
    try:
        with connection.transaction(conn):
            conn.execute(
                "INSERT INTO backup_history(created_at,kind,path,size_bytes,sha256,status,error) "
                "VALUES (?,?,?,?,?,?,?)",
                (connection.utc_now(), kind, str(final), info.size_bytes if info else None,
                 info.sha256 if info else None, "ok" if info else "failed", err))
            audit_service.log(conn, "backup.create" if info else "backup.failed", user=user,
                              entity="backup", details={"kind": kind, "path": str(final)})
    except sqlite3.Error:
        pass  # history is best effort; the backup file itself is what matters


def record_backup(conn, kind: str, info: BackupInfo | None, error: str | None = None, user=None, where=None) -> None:
    """Public history recorder for backups made on a worker thread (the connection stays on the UI thread).
    `where` names the folder a FAILED backup was meant for (shown in the history)."""
    _record(conn, kind, info, info.path if info else Path(where or "-"), error, user)


def last_successful_backup(conn: sqlite3.Connection) -> str | None:
    r = conn.execute("SELECT MAX(created_at) FROM backup_history WHERE status='ok'").fetchone()
    return r[0]


def daily_backup_if_due(paths: AppPaths, conn: sqlite3.Connection, *,
                        retention: int = DEFAULT_RETENTION_AUTO) -> BackupInfo | None:
    """Create an 'auto' backup unless a successful one exists for today (UTC date)."""
    today = connection.utc_now()[:10]
    due = conn.execute(
        "SELECT 1 FROM backup_history WHERE kind='auto' AND status='ok' AND substr(created_at,1,10)=?",
        (today,)).fetchone() is None
    if not due:
        return None
    info = create_backup(paths, "auto", record_conn=conn)
    prune_auto(paths, retention)
    return info


# ------------------------------------------------------------------ automatic backups (app folder + the optional extra folder)
def _ok_today(conn, folder: str | None, inside: bool) -> bool:
    """Is there a successful auto backup made today inside `folder` (inside=True) or outside it (inside=False)?"""
    today = connection.utc_now()[:10]
    sql = "SELECT 1 FROM backup_history WHERE kind='auto' AND status='ok' AND substr(created_at,1,10)=?"
    args: tuple = (today,)
    if folder:
        pre = str(Path(folder))
        sql += " AND substr(path,1,?)" + ("=?" if inside else "<>?")
        args += (len(pre), pre)
    return conn.execute(sql, args).fetchone() is not None


def auto_backup_due(conn, extra_dir: str | None) -> dict:
    """What is still missing today: the copy in the app's folder and, if an extra folder is set, the copy there. Checked again every 30 minutes,
    so a USB drive plugged in later in the day still gets today's copy."""
    extra = (extra_dir or "").strip() or None
    return {"local": not _ok_today(conn, extra, inside=False),
            "extra": bool(extra) and not _ok_today(conn, extra, inside=True),
            "extra_dir": extra}


def run_auto_backup(paths: AppPaths, due: dict) -> dict:
    """Make the missing copies. Safe on a worker thread (no database connection of the UI is used). A problem with the extra folder never stops the local copy."""
    out = {"local": None, "local_err": None, "extra": None, "extra_err": None, "extra_dir": due.get("extra_dir")}
    if due["local"]:
        try:
            out["local"] = create_backup(paths, "auto")
        except BackupError as e:
            out["local_err"] = str(e)
    if due["extra"]:
        try:
            out["extra"] = create_backup(paths, "auto", dest_dir=due["extra_dir"])
        except BackupError as e:
            out["extra_err"] = str(e)
    return out


def record_auto_result(conn, res: dict, retention: int = DEFAULT_RETENTION_AUTO, paths: AppPaths | None = None) -> None:
    """History + pruning for run_auto_backup's result. A failed extra copy is written once a day, not every 30 minutes."""
    if res["local"] or res["local_err"]:
        record_backup(conn, "auto", res["local"], res["local_err"])
    if res["extra"]:
        record_backup(conn, "auto", res["extra"])
    elif res["extra_err"]:
        pre = str(Path(res["extra_dir"]))
        seen = conn.execute("SELECT 1 FROM backup_history WHERE kind='auto' AND status='failed' AND substr(created_at,1,10)=? AND substr(path,1,?)=?",
                            (connection.utc_now()[:10], len(pre), pre)).fetchone()
        if not seen:
            record_backup(conn, "auto", None, res["extra_err"], where=pre)
    if paths is not None:
        if res["local"]:
            prune_auto(paths, retention)
        if res["extra"]:
            prune_auto(paths, retention, directory=res["extra_dir"])


def prune_auto(paths: AppPaths, keep: int, directory=None) -> list[Path]:
    """Delete only old *auto* backups beyond `keep` (in the app's backups folder, or in `directory`). Manual/pre_* are never pruned."""
    files = sorted(Path(directory).glob("factory_*_auto.db")) if directory else sorted(paths.backups.glob("factory_*_auto.db"))
    removed = []
    for f in files[:-keep] if keep > 0 else files:
        f.unlink(missing_ok=True)
        removed.append(f)
    return removed


def restore_backup(paths: AppPaths, backup_file: Path, *, confirmed: bool, user=None) -> Path:
    """Validated restore. Caller MUST close all connections to paths.db first.

    Steps: confirm -> verify backup -> copy to temp (via backup API) -> verify temp
    -> pre_restore backup of current -> move current aside into recovery/ -> activate.
    The live database is only replaced after every check passed."""
    if not confirmed:
        raise BackupError("Restore requires explicit confirmation.")
    if user is not None:
        from services.auth_service import require_permission
        require_permission(user, "restore_backup")
    backup_file = Path(backup_file)
    verify_database_file(backup_file)
    paths.ensure_dirs()
    tmp = paths.data_dir / f"restore_{_stamp()}.tmp"
    try:
        s = connection.connect(backup_file, readonly=True)
        try:
            d = sqlite3.connect(str(tmp))
            try:
                s.backup(d)
            finally:
                d.close()
        finally:
            s.close()
        verify_database_file(tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    preserved: Path | None = None
    if paths.db.exists():
        try:
            create_backup(paths, "pre_restore")
        except BackupError:
            pass  # damaged live DB: still preserved byte-for-byte below
        preserved = paths.recovery / f"factory_before_restore_{_stamp()}.db"
        try:
            shutil.copy2(paths.db, preserved)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise BackupError(f"Could not preserve current database: {e}") from e
    try:
        for ext in ("-wal", "-shm"):
            Path(str(paths.db) + ext).unlink(missing_ok=True)
        os.replace(tmp, paths.db)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise BackupError(f"Could not activate restored database (is the app still open?): {e}") from e
    return preserved or paths.db
