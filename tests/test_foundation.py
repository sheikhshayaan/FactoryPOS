import os
import sqlite3
import threading

import pytest

from config.paths import get_paths
from database import connection, migrations
from database.connection import transaction
from services import audit_service, auth_service as auth, backup_service as bk
from services.startup import initialize_new_database, open_database


# ---------- paths / connection ----------
def test_paths_independent_of_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("FMS_DATA_DIR", str(tmp_path / "x"))
    a = get_paths()
    monkeypatch.chdir(tmp_path)
    assert get_paths().db == a.db and a.db.is_absolute()


def test_open_never_creates_second_db(paths):
    with pytest.raises(connection.DatabaseMissing):
        open_database(paths)
    assert not paths.db.exists()


def test_pragmas(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_fk_rejected(conn):
    conn.execute("CREATE TABLE p(id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE c(id INTEGER PRIMARY KEY, p INTEGER REFERENCES p(id))")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO c(p) VALUES (99)")


def test_rollback_on_failure(conn):
    n = conn.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0]
    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute("INSERT INTO app_settings VALUES ('a','1')")
            raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM app_settings").fetchone()[0] == n


def test_idempotency_blocks_duplicates(conn):
    with transaction(conn):
        assert connection.claim_idempotency_key(conn, "pay-1", "payment") is True
    with transaction(conn):
        assert connection.claim_idempotency_key(conn, "pay-1", "payment") is False


def test_committed_data_survives_restart(paths):
    c = initialize_new_database(paths)
    with transaction(c):
        c.execute("INSERT INTO app_settings VALUES ('k','v')")
    c.close()
    c2 = open_database(paths)
    assert c2.execute("SELECT value FROM app_settings WHERE key='k'").fetchone()[0] == "v"
    c2.close()


# ---------- migrations ----------
def test_migrations_idempotent(conn):
    assert migrations.current_version(conn) == migrations.latest_version()
    assert migrations.migrate(conn) == []


def test_failed_migration_rolls_back_and_backup_taken(paths):
    c = initialize_new_database(paths)
    good = migrations.load_migrations()
    v = good[-1].version
    bad = good + [migrations.Migration(
        v + 1, "bad", "CREATE TABLE t2(x); INSERT INTO nonexistent VALUES (1);")]
    calls = []
    with pytest.raises(migrations.MigrationError):
        migrations.migrate(c, migrations=bad, backup_hook=lambda: calls.append(bk.create_backup(paths, "pre_migration")))
    assert migrations.current_version(c) == v
    assert c.execute("SELECT 1 FROM sqlite_master WHERE name='t2'").fetchone() is None
    assert len(calls) == 1 and calls[0].path.exists()
    bk.verify_database_file(calls[0].path)
    c.close()


def test_newer_schema_refused(conn):
    conn.execute("INSERT INTO schema_migrations VALUES (99,'future','x')")
    with pytest.raises(migrations.MigrationError):
        migrations.migrate(conn)


# ---------- auth ----------
def test_password_not_plaintext(conn, owner):
    h = conn.execute("SELECT password_hash FROM users").fetchone()[0]
    assert "correct-horse" not in h and h.startswith("$argon2")


def test_first_user_must_be_owner(conn):
    with pytest.raises(auth.AuthError):
        auth.create_user(conn, None, "x", "password123", "accountant")


def test_bad_login_generic_and_lockout(conn, owner):
    for _ in range(auth.MAX_FAILED):
        with pytest.raises(auth.AuthError, match="Invalid"):
            auth.authenticate(conn, "boss", "wrong-password")
    with pytest.raises(auth.AuthError, match="locked"):
        auth.authenticate(conn, "boss", "correct-horse")


def test_short_password_rejected(conn):
    with pytest.raises(auth.AuthError):
        auth.create_user(conn, None, "boss", "short", "owner")


def test_non_owner_cannot_approve_distribution_or_manage_users(conn, owner):
    auth.create_user(conn, owner, "acc", "accountant-pw", "accountant")
    acc = auth.authenticate(conn, "acc", "accountant-pw")
    for perm in ("approve_distribution", "restore_backup", "manage_users"):
        with pytest.raises(auth.PermissionDenied):
            auth.require_permission(acc, perm)
    with pytest.raises(auth.PermissionDenied):
        auth.create_user(conn, acc, "z", "password123", "auditor")
    auth.require_permission(owner, "approve_distribution")


def test_auditor_is_read_only():
    assert not auth.has_permission("auditor", "record_payment")
    assert auth.has_permission("auditor", "view_reports")


def test_cannot_deactivate_last_owner(conn, owner):
    with pytest.raises(auth.AuthError):
        auth.set_active(conn, owner, owner["id"], False)


# ---------- audit ----------
def test_audit_append_only_and_redacted(conn, owner):
    with transaction(conn):
        audit_service.log(conn, "t", user=owner, details={"password": "hunter2", "ok": 1})
    row = conn.execute("SELECT details FROM audit_log WHERE action='t'").fetchone()
    assert "hunter2" not in row[0] and "***" in row[0]
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("UPDATE audit_log SET action='x'")
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM audit_log")
    log_text = " ".join(str(r[0]) for r in conn.execute("SELECT details FROM audit_log") if r[0])
    assert "correct-horse" not in log_text


# ---------- backup / restore ----------
def test_backup_passes_integrity(paths, conn, owner):
    info = bk.create_backup(paths, "manual", record_conn=conn)
    bk.verify_database_file(info.path)
    assert bk.last_successful_backup(conn) is not None
    assert not list(paths.backups.glob("*.tmp"))


def test_backup_during_active_writes(paths, conn):
    stop = threading.Event()
    errs = []

    def writer():
        c = connection.connect(paths.db)
        i = 0
        while not stop.is_set():
            try:
                with transaction(c):
                    c.execute("INSERT OR REPLACE INTO app_settings VALUES (?,?)", (f"k{i % 50}", str(i)))
            except Exception as e:  # noqa
                errs.append(e)
            i += 1
        c.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        for _ in range(5):
            bk.verify_database_file(bk.create_backup(paths, "manual").path)
    finally:
        stop.set()
        t.join()
    assert not errs


def test_daily_backup_once_per_day(paths, conn):
    assert bk.daily_backup_if_due(paths, conn) is not None
    assert bk.daily_backup_if_due(paths, conn) is None


def test_prune_only_auto(paths, conn):
    for _ in range(3):
        bk.create_backup(paths, "auto")
    m = bk.create_backup(paths, "manual")
    removed = bk.prune_auto(paths, 1)
    assert len(removed) == 2 and m.path.exists()


def test_restore_roundtrip(paths, conn):
    with transaction(conn):
        conn.execute("INSERT INTO app_settings VALUES ('v','before')")
    good = bk.create_backup(paths, "manual")
    with transaction(conn):
        conn.execute("UPDATE app_settings SET value='after' WHERE key='v'")
    conn.close()
    with pytest.raises(bk.BackupError):
        bk.restore_backup(paths, good.path, confirmed=False)
    preserved = bk.restore_backup(paths, good.path, confirmed=True)
    c = connection.connect(paths.db)
    assert c.execute("SELECT value FROM app_settings WHERE key='v'").fetchone()[0] == "before"
    c.close()
    p = connection.connect(preserved)
    assert p.execute("SELECT value FROM app_settings WHERE key='v'").fetchone()[0] == "after"
    p.close()


def test_restore_corrupt_backup_preserves_live_db(paths, conn):
    with transaction(conn):
        conn.execute("INSERT INTO app_settings VALUES ('v','live')")
    conn.close()
    bad = paths.backups / "factory_bad.db"
    bad.write_bytes(b"this is not a sqlite database" * 100)
    before = paths.db.read_bytes()
    with pytest.raises(bk.BackupError):
        bk.restore_backup(paths, bad, confirmed=True)
    c = connection.connect(paths.db)
    assert c.execute("SELECT value FROM app_settings WHERE key='v'").fetchone()[0] == "live"
    c.close()
    assert not list(paths.data_dir.glob("*.tmp"))


def test_restore_wrong_schema_rejected(paths, conn):
    conn.close()
    other = paths.backups / "factory_other.db"
    o = sqlite3.connect(other); o.execute("CREATE TABLE x(a)"); o.commit(); o.close()
    with pytest.raises(bk.BackupError, match="Missing required tables"):
        bk.restore_backup(paths, other, confirmed=True)


def test_restore_requires_owner(paths, conn, owner):
    auth.create_user(conn, owner, "acc", "accountant-pw", "accountant")
    acc = auth.authenticate(conn, "acc", "accountant-pw")
    good = bk.create_backup(paths, "manual")
    conn.close()
    with pytest.raises(auth.PermissionDenied):
        bk.restore_backup(paths, good.path, confirmed=True, user=acc)


def test_corrupted_live_db_detected(paths, conn):
    conn.close()
    paths.db.write_bytes(b"garbage" * 1000)
    with pytest.raises(Exception):
        c = open_database(paths)
        c.execute("SELECT * FROM users").fetchall()
    assert paths.db.read_bytes().startswith(b"garbage")  # not overwritten


def test_failed_logins_are_audited(conn, owner):
    with pytest.raises(auth.AuthError):
        auth.authenticate(conn, "boss", "nope-nope-nope")
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='login.failed'").fetchone()[0] == 1
