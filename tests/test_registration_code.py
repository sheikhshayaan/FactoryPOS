"""Registration approval by e-mailed 6-digit code (the e-mail itself is faked by a local web server shaped like Apps Script: POST -> 302 -> JSON)."""
import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from services import registration_service as reg


class Fake(BaseHTTPRequestHandler):
    mode = "ok"
    got: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        Fake.got.append(json.loads(self.rfile.read(n)))
        if Fake.mode == "500":
            self.send_response(500); self.end_headers(); return
        self.send_response(302); self.send_header("Location", "/result"); self.end_headers()

    def do_GET(self):
        body = json.dumps({"ok": Fake.mode == "ok"}).encode() if Fake.mode != "html" else b"<html>sign in</html>"
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)


@pytest.fixture
def server(monkeypatch):
    Fake.mode, Fake.got = "ok", []
    s = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    monkeypatch.setattr(reg, "REGISTER_URL", f"http://127.0.0.1:{s.server_port}/exec")
    monkeypatch.setattr(reg, "REGISTER_SECRET", "s3cret")
    yield s
    s.shutdown()


def test_code_is_six_digits_and_varies():
    codes = {reg.new_code() for _ in range(50)}
    assert all(len(c) == 6 and c.isdigit() for c in codes) and len(codes) > 40


def test_request_sends_name_code_computer_and_secret(server):
    reg.request_code("Ali, Ali Textiles", "123456")
    d = Fake.got[0]
    assert d["secret"] == "s3cret" and d["who"] == "Ali, Ali Textiles" and d["code"] == "123456" and d["computer"] == reg.computer_code()
    assert len(reg.computer_code()) == 14


@pytest.mark.parametrize("mode", ["denied", "500", "html"])
def test_service_refusal_gives_a_clear_error(server, mode):
    Fake.mode = mode
    with pytest.raises(reg.RegistrationError):
        reg.request_code("x", "111111")


def test_no_internet_is_explained(monkeypatch):
    monkeypatch.setattr(reg, "REGISTER_URL", "http://127.0.0.1:9/exec")          # nothing listens here
    with pytest.raises(reg.RegistrationError, match="internet"):
        reg.request_code("x", "111111")


def test_required_rules(monkeypatch):
    monkeypatch.setattr(reg, "REGISTER_URL", "")
    assert not reg.required()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert reg.required()                                           # an .exe without an address fails closed
    with pytest.raises(reg.RegistrationError):
        reg.request_code("x", "111111")


def test_challenge_rules():
    t = [1000.0]
    c = reg.Challenge("424242", now=lambda: t[0])
    with pytest.raises(reg.RegistrationError):
        c.check("")
    for _ in range(5):
        with pytest.raises(reg.RegistrationError, match="not correct"):
            c.check("000000")
    with pytest.raises(reg.RegistrationError, match="Too many"):
        c.check("000000")
    with pytest.raises(reg.RegistrationError, match="Too many"):
        c.check("424242")                                           # locked even for the right code: a new one is needed
    c2 = reg.Challenge("424242", now=lambda: t[0]); c2.check(" 424 242 ")
    t[0] += reg.CODE_LIFETIME_S + 1
    with pytest.raises(reg.RegistrationError, match="expired"):
        c2.check("424242")


# ---------------------------------------------------------------- the screen
tk = pytest.importorskip("tkinter")


def _wait(app, cond):
    import time
    for _ in range(120):
        app.update()
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_registration_needs_the_emailed_code(tmp_path, server):
    from config.paths import get_paths
    from ui import app as appmod
    paths = get_paths(tmp_path / "fresh")
    a = appmod.App(paths); a.update()
    try:
        s = a.screen
        assert isinstance(s, appmod.RegisterScreen) and not paths.db.exists()
        s.go(); assert "Send code first" in s.msg.cget("text")
        s.send(); assert "name and business" in s.msg.cget("text") and not Fake.got
        s.who.insert(0, "Ali, Ali Textiles"); s.send()
        _wait(a, lambda: s.challenge is not None)
        assert Fake.got[0]["code"] == s.challenge.code
        s.send(); assert "Wait" in s.msg.cget("text") and len(Fake.got) == 1          # no mail flooding from the button
        s.code.insert(0, "000000" if s.challenge.code != "000000" else "111111"); s.go(); a.update()
        assert isinstance(a.screen, appmod.RegisterScreen) and not paths.db.exists() and "not correct" in s.msg.cget("text")
        s.code.insert(0, s.challenge.code); s.go(); a.update()
        assert isinstance(a.screen, appmod.FirstRunScreen)
        f = a.screen
        f.fn.insert(0, "Ali Textiles"); f.un.insert(0, "owner1"); f.full.insert(0, "O"); f.pw.insert(0, "longenough1"); f.pw2.insert(0, "longenough1")
        f.sq_answer.insert(0, "Faisalabad"); f.go(); a.update()
        assert isinstance(a.screen, appmod.LoginScreen) and paths.db.exists()
    finally:
        a.on_close()


def test_no_internet_keeps_registration_closed(tmp_path, monkeypatch):
    from config.paths import get_paths
    from ui import app as appmod
    monkeypatch.setattr(reg, "REGISTER_URL", "http://127.0.0.1:9/exec")
    paths = get_paths(tmp_path / "fresh")
    a = appmod.App(paths); a.update()
    try:
        s = a.screen
        s.who.insert(0, "Ali"); s.send()
        _wait(a, lambda: "internet" in s.msg.cget("text"))
        assert s.challenge is None and s.send_btn.cget("state") == "normal"
        s.go(); assert isinstance(a.screen, appmod.RegisterScreen) and not paths.db.exists()
    finally:
        a.on_close()


def test_existing_factory_is_never_asked_for_a_code(tmp_path, server):
    from config.paths import get_paths
    from services.startup import initialize_new_database
    from ui import app as appmod
    paths = get_paths(tmp_path / "old"); paths.ensure_dirs(); initialize_new_database(paths).close()
    a = appmod.App(paths); a.update()
    try:
        assert not isinstance(a.screen, (appmod.RegisterScreen, appmod.FirstRunScreen)) and not Fake.got
    finally:
        a.on_close()
