"""Security question / forgot-password: hashing, matching rules, lockout, audit, migration."""
import sqlite3

import pytest

from database import migrations
from services import auth_service as auth

Q = "What was the name of your first school?"


def _owner(conn, answer="City Model School"):
    auth.create_user(conn, None, "boss", "correct-horse", "owner", security_question=Q, security_answer=answer)


def test_migration_adds_columns_and_old_users_survive(tmp_path):
    c = sqlite3.connect(tmp_path / "old.db"); c.row_factory = sqlite3.Row
    ms = [m for m in migrations.load_migrations() if m.version < 9]
    migrations.migrate(c, migrations=ms)
    c.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES ('old','h','owner','x')")
    migrations.migrate(c)                                             # now apply 0009 on a database that already has a user
    row = c.execute("SELECT security_question, security_answer_hash, reset_failed FROM users").fetchone()
    assert row[0] is None and row[1] is None and row[2] == 0
    assert migrations.current_version(c) == migrations.latest_version()


def test_answer_is_stored_only_as_hash(conn):
    _owner(conn)
    q, h = conn.execute("SELECT security_question, security_answer_hash FROM users").fetchone()
    assert q == Q and h.startswith("$argon2id$") and "city" not in h.lower()


def test_correct_answer_resets_password_ignoring_case_and_spacing(conn):
    _owner(conn)
    assert auth.security_question_for(conn, "boss") == Q
    auth.reset_password_with_answer(conn, " BOSS ", "  city   MODEL school ", "a-new-password")
    assert auth.authenticate(conn, "boss", "a-new-password")["username"] == "boss"
    with pytest.raises(auth.AuthError):
        auth.authenticate(conn, "boss", "correct-horse")


def test_wrong_answer_changes_nothing_and_is_audited(conn):
    _owner(conn)
    with pytest.raises(auth.AuthError, match="not correct"):
        auth.reset_password_with_answer(conn, "boss", "Wrong School", "a-new-password")
    assert auth.authenticate(conn, "boss", "correct-horse")           # old password still works
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='password_reset.failed'").fetchone()[0] == 1


def test_five_wrong_answers_lock_recovery_even_for_the_right_answer(conn):
    _owner(conn)
    for _ in range(auth.RESET_MAX_FAILED):
        with pytest.raises(auth.AuthError):
            auth.reset_password_with_answer(conn, "boss", "nope", "a-new-password")
    with pytest.raises(auth.AuthError, match="locked"):
        auth.reset_password_with_answer(conn, "boss", "City Model School", "a-new-password")
    assert auth.authenticate(conn, "boss", "correct-horse")           # nothing changed
    conn.execute("UPDATE users SET reset_locked_until='2000-01-01T00:00:00Z'")      # lock time passes
    auth.reset_password_with_answer(conn, "boss", "City Model School", "a-new-password")


def test_reset_also_clears_a_login_lockout(conn):
    _owner(conn)
    for _ in range(auth.MAX_FAILED):
        with pytest.raises(auth.AuthError):
            auth.authenticate(conn, "boss", "bad-password")
    with pytest.raises(auth.AuthError, match="locked"):
        auth.authenticate(conn, "boss", "correct-horse")
    auth.reset_password_with_answer(conn, "boss", "city model school", "a-new-password")
    assert auth.authenticate(conn, "boss", "a-new-password")


def test_unknown_user_no_question_and_inactive_all_look_the_same(conn, owner):
    # 'owner' fixture has no question; add an inactive user with one
    auth.create_user(conn, owner, "gone", "longenough1", "accountant", security_question=Q, security_answer="abc")
    auth.set_active(conn, owner, conn.execute("SELECT id FROM users WHERE username='gone'").fetchone()[0], False)
    msgs = set()
    for name in ("nobody", "boss", "gone"):
        with pytest.raises(auth.AuthError) as e:
            auth.security_question_for(conn, name)
        msgs.add(str(e.value))
        with pytest.raises(auth.AuthError) as e2:
            auth.reset_password_with_answer(conn, name, "abc", "a-new-password")
        msgs.add(str(e2.value))
    assert msgs == {auth.NO_RECOVERY_MSG}


def test_new_password_still_needs_8_characters(conn):
    _owner(conn)
    with pytest.raises(auth.AuthError, match="at least 8"):
        auth.reset_password_with_answer(conn, "boss", "City Model School", "short")
    assert auth.authenticate(conn, "boss", "correct-horse")


def test_question_and_answer_validation(conn):
    with pytest.raises(auth.AuthError):
        auth.create_user(conn, None, "boss", "correct-horse", "owner", security_question="short", security_answer="abc")
    with pytest.raises(auth.AuthError, match="at least 3"):
        auth.create_user(conn, None, "boss", "correct-horse", "owner", security_question=Q, security_answer=" a ")
    assert auth.user_count(conn) == 0                                  # nothing half-created


def test_set_own_question_needs_current_password(conn, owner):
    with pytest.raises(auth.AuthError, match="Current password"):
        auth.set_security_question(conn, owner, owner["id"], Q, "Moti", "wrong-password")
    auth.set_security_question(conn, owner, owner["id"], Q, "Moti", "correct-horse")
    auth.reset_password_with_answer(conn, "boss", "moti", "a-new-password")


def test_only_owner_may_set_someone_elses_question(conn, owner):
    auth.create_user(conn, owner, "acc", "longenough1", "accountant", security_question=Q, security_answer="abc")
    acc = auth.authenticate(conn, "acc", "longenough1")
    with pytest.raises(auth.PermissionDenied):
        auth.set_security_question(conn, acc, owner["id"], Q, "hacked", None)
    auth.set_security_question(conn, owner, acc["id"], Q, "newanswer")
    auth.reset_password_with_answer(conn, "acc", "NewAnswer", "a-new-password")


def test_audit_never_contains_the_answer(conn):
    _owner(conn, "Super Secret Answer")
    with pytest.raises(auth.AuthError):
        auth.reset_password_with_answer(conn, "boss", "Another Guess", "a-new-password")
    dump = " ".join(str(tuple(r)) for r in conn.execute("SELECT * FROM audit_log"))
    assert "super secret" not in dump.lower() and "another guess" not in dump.lower()
