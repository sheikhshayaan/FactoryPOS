import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config.paths import get_paths  # noqa: E402
from services.startup import initialize_new_database  # noqa: E402


@pytest.fixture
def paths(tmp_path):
    p = get_paths(tmp_path / "app")
    p.ensure_dirs()
    return p


@pytest.fixture
def conn(paths):
    c = initialize_new_database(paths)
    c.execute("INSERT INTO suppliers(id,name,created_at) VALUES (1,'Test Supplier','x')")
    yield c
    c.close()


@pytest.fixture
def owner(conn):
    from services import auth_service as a
    a.create_user(conn, None, "boss", "correct-horse", "owner")
    return a.authenticate(conn, "boss", "correct-horse")


@pytest.fixture(autouse=True)
def _no_registration_gate(monkeypatch):
    """A developer's own services/registration_local.py must not put the code screen in front of every test."""
    from services import registration_service
    monkeypatch.setattr(registration_service, "REGISTER_URL", "")
    monkeypatch.setattr(registration_service, "REGISTER_SECRET", "")
