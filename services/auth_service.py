"""Authentication (Argon2id) and role-based authorisation."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from database.connection import transaction, utc_now
from services import audit_service

ROLES = ("owner", "accountant", "auditor")
MAX_FAILED = 5
LOCKOUT_MINUTES = 5
MIN_PASSWORD_LEN = 8
MIN_ANSWER_LEN = 3
RESET_MAX_FAILED = 5
RESET_LOCKOUT_MINUTES = 15
CUSTOM_QUESTION = "Write my own question"
SECURITY_QUESTIONS = (
    "What was the name of your first school?",
    "In which city or village were you born?",
    "What is the name of your oldest cousin?",
    "What was your first mobile phone model?",
    "What was the name of your childhood best friend?",
    "What is the name of your favourite teacher?",
)

# Permissions are explicit; anything not listed is denied.
PERMISSIONS: dict[str, frozenset[str]] = {
    # owner: everything
    "owner": frozenset({"*"}),
    # accountant: everything the owner does in the day-to-day work (money, stock, production, people, salaries), but NOT the Dashboard, Insights,
    # Partners, Profit Distribution, Reports or the system pages (Settings, Users, Audit Log, Backup)
    "accountant": frozenset({
        "view_all", "post_journal", "record_payment", "record_expense", "manage_payroll", "manage_invoices", "record_capital",
        "view_inventory", "receive_stock", "adjust_stock", "manage_suppliers", "manage_jobs", "consume_stock",
        "manage_attendance", "manage_machines", "finalize_job"}),
    # auditor: sees everything except the Dashboard and Insights (and owner-only Settings / Users), changes nothing
    "auditor": frozenset({
        "view_all", "view_reports", "export_reports", "view_audit", "view_partners", "view_profit", "view_backup"}),
}
# Only these actions are ever restricted to owner, even though owner has '*'.
OWNER_ONLY = frozenset({"approve_distribution", "restore_backup", "manage_users", "change_ownership", "create_backup"})

_ph = PasswordHasher()


class AuthError(Exception):
    pass


class PermissionDenied(AuthError):
    pass


def has_permission(role: str, permission: str) -> bool:
    perms = PERMISSIONS.get(role, frozenset())
    if permission in OWNER_ONLY:
        return role == "owner"
    return "*" in perms or permission in perms


def require_permission(user, permission: str) -> None:
    if user is None or not user["is_active"] or not has_permission(user["role"], permission):
        raise PermissionDenied(f"Not allowed: {permission}")


def _validate_password(pw: str) -> None:
    if len(pw) < MIN_PASSWORD_LEN:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")


def _norm_answer(answer: str) -> str:
    """Case, spacing and surrounding blanks must not make a right answer fail ('  Lahore ' == 'lahore')."""
    return " ".join((answer or "").split()).casefold()


def _validate_security(question: str, answer: str) -> tuple[str, str]:
    question = " ".join((question or "").split())
    if len(question) < 8:
        raise AuthError("Choose a security question (or write one of at least 8 characters).")
    norm = _norm_answer(answer)
    if len(norm) < MIN_ANSWER_LEN:
        raise AuthError(f"The security answer must be at least {MIN_ANSWER_LEN} characters.")
    return question, norm


def user_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(conn: sqlite3.Connection, actor, username: str, password: str, role: str,
                full_name: str = "", security_question: str | None = None,
                security_answer: str | None = None) -> int:
    """actor=None is allowed only for the very first (owner) account."""
    if role not in ROLES:
        raise AuthError(f"Unknown role: {role}")
    username = username.strip()
    if not username:
        raise AuthError("Username required.")
    _validate_password(password)
    sec_q = sec_hash = None
    if security_question is not None or security_answer is not None:
        q, norm = _validate_security(security_question or "", security_answer or "")
        sec_q, sec_hash = q, _ph.hash(norm)
    with transaction(conn):
        if user_count(conn) == 0:
            if role != "owner":
                raise AuthError("The first account must be the owner.")
        else:
            require_permission(actor, "manage_users")
        try:
            cur = conn.execute(
                "INSERT INTO users(username,full_name,password_hash,role,created_at,security_question,security_answer_hash) "
                "VALUES (?,?,?,?,?,?,?)",
                (username, full_name, _ph.hash(password), role, utc_now(), sec_q, sec_hash))
        except sqlite3.IntegrityError as e:
            raise AuthError("That username already exists.") from e
        audit_service.log(conn, "user.create", user=actor, username=username if not actor else None,
                          entity="user", entity_id=cur.lastrowid, details={"role": role, "security_question": bool(sec_q)})
        return cur.lastrowid


def authenticate(conn: sqlite3.Connection, username: str, password: str):
    """Returns the user row on success, else raises AuthError (generic message).

    Failure bookkeeping (attempt counter, lockout, audit row) is committed
    BEFORE the error is raised; raising inside the transaction would roll it back."""
    failure: AuthError | None = None
    uid = None
    with transaction(conn):
        u = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
        now = datetime.now(timezone.utc)
        if u is None:
            audit_service.log(conn, "login.failed", username=username[:64], details={"why": "unknown_user"})
            failure = AuthError("Invalid username or password.")
        elif u["locked_until"] and datetime.strptime(u["locked_until"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc) > now:
            audit_service.log(conn, "login.blocked", user=u)
            failure = AuthError("Account temporarily locked. Try again later.")
        else:
            ok = False
            if u["is_active"]:
                try:
                    ok = _ph.verify(u["password_hash"], password)
                except (VerifyMismatchError, InvalidHashError):
                    ok = False
            if not ok:
                fails = u["failed_attempts"] + 1
                locked = None
                if fails >= MAX_FAILED:
                    locked = (now + timedelta(minutes=LOCKOUT_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    fails = 0
                conn.execute("UPDATE users SET failed_attempts=?, locked_until=? WHERE id=?",
                             (fails, locked, u["id"]))
                audit_service.log(conn, "login.failed", user=u, details={"locked": bool(locked)})
                failure = AuthError("Invalid username or password.")
            else:
                conn.execute("UPDATE users SET failed_attempts=0, locked_until=NULL WHERE id=?", (u["id"],))
                if _ph.check_needs_rehash(u["password_hash"]):
                    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (_ph.hash(password), u["id"]))
                audit_service.log(conn, "login.ok", user=u)
                uid = u["id"]
    if failure:
        raise failure
    return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def change_password(conn: sqlite3.Connection, actor, user_id: int, new_password: str) -> None:
    _validate_password(new_password)
    if actor is None or (actor["id"] != user_id):
        require_permission(actor, "manage_users")
    with transaction(conn):
        conn.execute("UPDATE users SET password_hash=?, failed_attempts=0, locked_until=NULL WHERE id=?",
                     (_ph.hash(new_password), user_id))
        audit_service.log(conn, "user.password_change", user=actor, entity="user", entity_id=user_id)


def set_security_question(conn: sqlite3.Connection, actor, user_id: int, question: str, answer: str,
                          current_password: str | None = None) -> None:
    """A person changes their OWN question only after re-entering their password; the owner (manage_users) may set anyone's."""
    q, norm = _validate_security(question, answer)
    if actor is None:
        raise PermissionDenied("Sign in first.")
    if actor["id"] == user_id:
        row = conn.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
        try:
            ok = bool(row) and _ph.verify(row["password_hash"], current_password or "")
        except (VerifyMismatchError, InvalidHashError):
            ok = False
        if not ok:
            raise AuthError("Current password is not correct.")
    else:
        require_permission(actor, "manage_users")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is None:
            raise AuthError("User not found.")
        conn.execute("UPDATE users SET security_question=?, security_answer_hash=?, reset_failed=0, reset_locked_until=NULL "
                     "WHERE id=?", (q, _ph.hash(norm), user_id))
        audit_service.log(conn, "user.security_question_set", user=actor, entity="user", entity_id=user_id)


NO_RECOVERY_MSG = "Password recovery is not set up for this account. Ask the owner to reset the password."


def security_question_for(conn: sqlite3.Connection, username: str) -> str:
    """Returns the question to show on the Forgot-password screen. Unknown names, deactivated accounts and
    accounts without a question all get the SAME message, so the screen does not reveal which usernames exist."""
    u = conn.execute("SELECT security_question, security_answer_hash, is_active FROM users WHERE username=?",
                     ((username or "").strip(),)).fetchone()
    if u is None or not u["is_active"] or not u["security_question"] or not u["security_answer_hash"]:
        raise AuthError(NO_RECOVERY_MSG)
    return u["security_question"]


def reset_password_with_answer(conn: sqlite3.Connection, username: str, answer: str, new_password: str) -> None:
    """Forgot-password: a right answer sets the new password. Wrong answers are counted per account and
    lock recovery for RESET_LOCKOUT_MINUTES after RESET_MAX_FAILED tries. Failures are committed before raising."""
    _validate_password(new_password)
    failure: AuthError | None = None
    with transaction(conn):
        u = conn.execute("SELECT * FROM users WHERE username=?", ((username or "").strip(),)).fetchone()
        now = datetime.now(timezone.utc)
        if u is None or not u["is_active"] or not u["security_question"] or not u["security_answer_hash"]:
            audit_service.log(conn, "password_reset.failed", username=(username or "")[:64], details={"why": "unavailable"})
            failure = AuthError(NO_RECOVERY_MSG)
        elif u["reset_locked_until"] and datetime.strptime(u["reset_locked_until"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc) > now:
            audit_service.log(conn, "password_reset.blocked", user=u)
            failure = AuthError("Too many wrong answers. Password recovery is locked for a while. Ask the owner to reset the password.")
        else:
            try:
                ok = _ph.verify(u["security_answer_hash"], _norm_answer(answer))
            except (VerifyMismatchError, InvalidHashError):
                ok = False
            if not ok:
                fails = u["reset_failed"] + 1
                locked = None
                if fails >= RESET_MAX_FAILED:
                    locked = (now + timedelta(minutes=RESET_LOCKOUT_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
                    fails = 0
                conn.execute("UPDATE users SET reset_failed=?, reset_locked_until=? WHERE id=?", (fails, locked, u["id"]))
                audit_service.log(conn, "password_reset.failed", user=u, details={"why": "wrong_answer", "locked": bool(locked)})
                failure = AuthError("That answer is not correct.")
            else:
                conn.execute("UPDATE users SET password_hash=?, failed_attempts=0, locked_until=NULL, "
                             "reset_failed=0, reset_locked_until=NULL WHERE id=?", (_ph.hash(new_password), u["id"]))
                audit_service.log(conn, "password_reset.ok", user=u, entity="user", entity_id=u["id"])
    if failure:
        raise failure


def set_active(conn: sqlite3.Connection, actor, user_id: int, active: bool) -> None:
    require_permission(actor, "manage_users")
    with transaction(conn):
        if active:
            role = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
            if role and role["role"] not in ROLES:
                raise AuthError("This user has an old role that no longer exists. Create a new user with the owner, accountant or auditor role instead.")
        if not active:
            owners = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='owner' AND is_active=1 AND id<>?", (user_id,)
            ).fetchone()[0]
            target = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
            if target and target["role"] == "owner" and owners == 0:
                raise AuthError("Cannot deactivate the last active owner.")
        conn.execute("UPDATE users SET is_active=? WHERE id=?", (1 if active else 0, user_id))
        audit_service.log(conn, "user.activate" if active else "user.deactivate",
                          user=actor, entity="user", entity_id=user_id)
