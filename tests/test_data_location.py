"""The database lives next to the software; an older per-user database is copied, never moved."""
import sqlite3

from config import paths as P


def _mkdb(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t(v TEXT)"); c.execute("INSERT INTO t VALUES (?)", (value,)); c.commit(); c.close()


def _val(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT v FROM t").fetchone()[0]
    finally:
        c.close()


def test_default_is_program_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("FMS_DATA_DIR", raising=False)
    app, old = tmp_path / "app", tmp_path / "old"
    app.mkdir()
    monkeypatch.setattr(P, "program_dir", lambda: app)
    monkeypatch.setattr(P, "legacy_root", lambda: old)
    p = P.get_paths()
    assert p.root == app and p.db == app / "data" / "factory.db"


def test_env_override_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("FMS_DATA_DIR", str(tmp_path / "custom"))
    assert P.get_paths().root == (tmp_path / "custom").resolve()


def test_old_database_is_copied_not_moved(tmp_path, monkeypatch):
    monkeypatch.delenv("FMS_DATA_DIR", raising=False)
    app, old = tmp_path / "app", tmp_path / "old"
    app.mkdir()
    _mkdb(old / "data" / "factory.db", "real")
    (old / "backups").mkdir(); (old / "backups" / "b1.db").write_bytes(b"x")
    monkeypatch.setattr(P, "program_dir", lambda: app)
    monkeypatch.setattr(P, "legacy_root", lambda: old)
    p = P.get_paths()
    assert _val(p.db) == "real" and (app / "backups" / "b1.db").exists()
    assert _val(old / "data" / "factory.db") == "real"            # the old copy is untouched
    _mkdb(p.db.with_name("x.db"), "n")                            # a second start must not overwrite the new database
    sqlite3.connect(p.db).execute("UPDATE t SET v='changed'").connection.commit()
    P.get_paths()
    assert _val(p.db) == "changed"


def test_unwritable_program_folder_falls_back(tmp_path, monkeypatch):
    monkeypatch.delenv("FMS_DATA_DIR", raising=False)
    old = tmp_path / "old"
    monkeypatch.setattr(P, "program_dir", lambda: tmp_path / "app")
    monkeypatch.setattr(P, "legacy_root", lambda: old)
    monkeypatch.setattr(P, "_writable", lambda f: False)
    assert P.get_paths().root == old


def test_real_program_folder_is_the_project_folder():
    assert (P.program_dir() / "main.py").exists()
