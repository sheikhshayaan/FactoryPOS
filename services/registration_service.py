"""Registration approval by e-mail code (OTP). Used only when a NEW factory is registered on a computer.

The program asks a small web address (a Google Apps Script the software owner deployed, see docs/registration_mail.gs) to e-mail a
6-digit code to the OWNER OF THE SOFTWARE. The person registering must phone him, get the code and type it in.

What is sent, and only at that moment: the business/contact name typed on the screen, the 6-digit code, and an anonymous computer code
(a hash - it does not reveal the computer's name). Nothing is sent afterwards; the program runs fully offline.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid

# Set these two in services/registration_local.py (copy of this pair, never pushed), from the Apps Script deployment; then rebuild the .exe.
REGISTER_URL = ""
REGISTER_SECRET = ""
try:                                             # your own values live in services/registration_local.py (kept out of GitHub)
    from services.registration_local import REGISTER_SECRET, REGISTER_URL  # noqa: F401
except ImportError:
    pass

CODE_LIFETIME_S = 10 * 60
MAX_TRIES = 5
RESEND_AFTER_S = 30
TIMEOUT_S = 20


class RegistrationError(Exception):
    pass


def required() -> bool:
    """A build with a URL asks for the code. A built .exe WITHOUT one refuses to register (fails closed);
    only a source checkout run for development skips the check."""
    return bool(REGISTER_URL) or bool(getattr(sys, "frozen", False))


def computer_code() -> str:
    raw = ""
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)) as k:
                raw = str(winreg.QueryValueEx(k, "MachineGuid")[0])
        else:
            for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                try:
                    raw = open(p, encoding="utf8").read().strip()
                except OSError:
                    continue
                if raw:
                    break
    except OSError:
        pass
    raw = raw or f"mac-{uuid.getnode():012x}"
    h = hashlib.sha256(b"fms-computer|" + raw.encode()).hexdigest()[:12].upper()
    return "-".join(h[i:i + 4] for i in range(0, 12, 4))


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def request_code(who: str, code: str) -> None:
    """E-mail `code` to the software owner. Raises RegistrationError with a message the user can act on."""
    if not REGISTER_URL:
        raise RegistrationError("This build has no registration address, so new factories cannot be registered. Rebuild the program after "
                                "setting REGISTER_URL in services/registration_local.py.")
    body = json.dumps({"secret": REGISTER_SECRET, "who": who.strip()[:120], "code": code, "computer": computer_code()}).encode()
    req = urllib.request.Request(REGISTER_URL, data=body, headers={"Content-Type": "text/plain;charset=utf-8"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            text = r.read().decode("utf8", "replace")
    except urllib.error.HTTPError as e:
        raise RegistrationError(f"The registration service answered with an error ({e.code}). Try again in a few minutes.") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RegistrationError("Could not reach the internet. Connect this computer to the internet for a minute, then press Send code again. "
                                "(Only registration needs the internet; the program itself works offline.)") from e
    try:
        reply = json.loads(text)
        ok, why = reply.get("ok") is True, str(reply.get("msg", ""))[:100]
    except (ValueError, AttributeError):
        ok, why = False, "the address did not answer like the registration script (is it deployed for 'Anyone'? is it the /exec address?)"
    if not ok:
        raise RegistrationError(f"The registration service did not accept the request ({why}). Check with the person who gave you the software.")


class Challenge:
    """One pending code: valid for 10 minutes and 5 wrong tries, then a new code must be requested."""

    def __init__(self, code: str, now=time.time):
        self.code, self._now, self.created, self.tries = code, now, now(), 0

    def check(self, typed: str) -> None:
        if self._now() - self.created > CODE_LIFETIME_S:
            raise RegistrationError("This code has expired. Press Send code to get a new one.")
        if self.tries >= MAX_TRIES:
            raise RegistrationError("Too many wrong tries. Press Send code to get a new one.")
        t = "".join(ch for ch in (typed or "") if ch.isdigit())
        if not t:
            raise RegistrationError("Type the 6-digit code.")
        if not secrets.compare_digest(t, self.code):
            self.tries += 1
            left = MAX_TRIES - self.tries
            raise RegistrationError("That code is not correct." + (f" {left} tries left." if left else " Press Send code to get a new one."))
