"""The daily automatic backup also goes to the optional extra folder, and is retried while the app is open."""
from pathlib import Path

from services import backup_service as bk


def _run(conn, paths, extra):
    due = bk.auto_backup_due(conn, extra)
    res = bk.run_auto_backup(paths, due) if (due["local"] or due["extra"]) else None
    if res:
        bk.record_auto_result(conn, res, 30, paths)
    return due, res


def test_no_extra_folder_only_local_once_a_day(conn, paths):
    due, res = _run(conn, paths, "")
    assert due["local"] and not due["extra"] and res["local"] and res["extra"] is None
    due, res = _run(conn, paths, "")
    assert not due["local"] and not due["extra"] and res is None


def test_both_copies_are_made(conn, paths, tmp_path):
    usb = tmp_path / "usb"
    usb.mkdir()
    due, res = _run(conn, paths, str(usb))
    assert due["local"] and due["extra"]
    assert res["local"].path.parent == paths.backups and res["extra"].path.parent == usb
    assert len(list(usb.glob("factory_*_auto.db"))) == 1
    due, _ = _run(conn, paths, str(usb))
    assert not due["local"] and not due["extra"]


def test_unplugged_usb_never_blocks_local_and_is_retried_later(conn, paths, tmp_path):
    blocker = tmp_path / "drive"                # stands in for a drive letter that is not there: its folder cannot be created
    blocker.write_text("x")
    usb = blocker / "usb"
    due, res = _run(conn, paths, str(usb))
    assert res["local"] and res["extra"] is None and "Backup failed" in res["extra_err"]
    # the local copy is recorded as ok, the extra failure is recorded once
    rows = conn.execute("SELECT status, path FROM backup_history ORDER BY id").fetchall()
    assert [r["status"] for r in rows] == ["ok", "failed"] and rows[1]["path"] == str(usb)
    # 30 minutes later: still unplugged -> only the extra is due, and no new failed row piles up
    due, res = _run(conn, paths, str(usb))
    assert not due["local"] and due["extra"] and res["extra_err"]
    assert conn.execute("SELECT COUNT(*) FROM backup_history WHERE status='failed'").fetchone()[0] == 1
    # USB plugged in
    blocker.unlink(); blocker.mkdir()
    due, res = _run(conn, paths, str(usb))
    assert not due["local"] and due["extra"] and res["extra"] and res["extra_err"] is None
    assert len(list(usb.glob("factory_*_auto.db"))) == 1
    due, _ = _run(conn, paths, str(usb))
    assert not due["local"] and not due["extra"]


def test_setting_an_extra_folder_later_in_the_day_adds_only_that_copy(conn, paths, tmp_path):
    _run(conn, paths, "")
    usb = tmp_path / "usb"; usb.mkdir()
    due, res = _run(conn, paths, str(usb))
    assert not due["local"] and due["extra"] and res["extra"] and res["local"] is None


def test_pruning_applies_to_both_folders_and_keeps_manual(conn, paths, tmp_path):
    usb = tmp_path / "usb"; usb.mkdir()
    for d in (paths.backups, usb):
        for i in range(5):
            (d / f"factory_2020010{i}_000000_auto.db").write_bytes(b"x")
        (d / "factory_20200101_000000_manual.db").write_bytes(b"x")
    due = {"local": True, "extra": True, "extra_dir": str(usb)}
    res = bk.run_auto_backup(paths, due)
    bk.record_auto_result(conn, res, 2, paths)
    assert len(list(paths.backups.glob("factory_*_auto.db"))) == 2 and len(list(usb.glob("factory_*_auto.db"))) == 2
    assert (usb / "factory_20200101_000000_manual.db").exists() and (paths.backups / "factory_20200101_000000_manual.db").exists()
