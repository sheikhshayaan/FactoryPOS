"""Explicit application data paths.

The database, logs and backups live NEXT TO THE SOFTWARE (in the program's own folder), never relative to the current
working directory, so the same database is found from a shortcut, another cwd, or a packaged .exe.

Program folder : the folder of FactoryManagement.exe (packaged) or of main.py (running from source).
Data           : <program folder>\\{data,backups,recovery,logs,exports}   (the database is data\\factory.db)
Override       : env var FMS_DATA_DIR (tests / special installs).
Fallback       : if the program folder cannot be written to (e.g. C:\\Program Files), %APPDATA%\\FactoryManagementSystem is used.
An older database in %APPDATA%\\FactoryManagementSystem is COPIED (never moved or deleted) into the program folder on first start.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "FactoryManagementSystem"
DB_FILENAME = "factory.db"


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @property
    def db(self) -> Path:
        return self.root / "data" / DB_FILENAME

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def recovery(self) -> Path:
        """Previous/damaged databases are preserved here, never deleted."""
        return self.root / "recovery"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.backups, self.recovery, self.logs, self.exports):
            p.mkdir(parents=True, exist_ok=True)


def program_dir() -> Path:
    """Folder that holds the software: the .exe when packaged, else the project folder (parent of config/)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def legacy_root() -> Path:
    """Where earlier versions kept their data (per-user application data)."""
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return (Path(base) / APP_DIR_NAME).resolve()
    return (Path.home() / ".local" / "share" / APP_DIR_NAME).resolve()


def _writable(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_test"
        probe.write_text("x"); probe.unlink()
        return True
    except OSError:
        return False


def _copy_legacy_data(old: Path, new: Path) -> None:
    """One-time, non-destructive: bring an older database (and backups) next to the software. The old folder is left untouched."""
    import shutil
    import sqlite3
    old_db, new_db = old / "data" / DB_FILENAME, new / "data" / DB_FILENAME
    if new_db.exists() or not old_db.exists() or old.resolve() == new.resolve():
        return
    new_db.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{old_db}?mode=ro", uri=True)       # backup API gives a consistent copy even if -wal exists
    try:
        dst = sqlite3.connect(new_db)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    for sub in ("backups", "recovery"):
        if (old / sub).is_dir() and not (new / sub).exists():
            shutil.copytree(old / sub, new / sub)
    if (old / "ui.json").exists() and not (new / "ui.json").exists():
        shutil.copy2(old / "ui.json", new / "ui.json")


def default_root() -> Path:
    env = os.environ.get("FMS_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    here, old = program_dir(), legacy_root()
    if not _writable(here):
        return old
    try:
        _copy_legacy_data(old, here)
    except Exception:  # noqa: BLE001 - a failed copy must never stop the app; the old data stays where it is
        if not (here / "data" / DB_FILENAME).exists() and (old / "data" / DB_FILENAME).exists():
            return old
    return here


def get_paths(root: str | Path | None = None) -> AppPaths:
    return AppPaths(Path(root).resolve() if root else default_root())
