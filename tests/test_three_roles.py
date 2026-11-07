"""The program has three roles: owner, accountant, auditor. What each may see and do."""
from pathlib import Path

import pytest

from services import auth_service as auth

PAGE_PERMS = ("view_dashboard", "view_insights", "view_partners", "view_profit", "view_reports", "view_audit", "view_backup", "manage_users")
DAILY_WORK = ("post_journal", "record_payment", "record_expense", "manage_payroll", "manage_invoices", "record_capital", "view_inventory", "receive_stock",
              "adjust_stock", "manage_suppliers", "manage_jobs", "consume_stock", "manage_attendance", "manage_machines", "finalize_job")
OWNER_ONLY = ("approve_distribution", "restore_backup", "manage_users", "change_ownership", "create_backup")


def test_only_three_roles_exist(conn, owner):
    assert auth.ROLES == ("owner", "accountant", "auditor")
    for old in ("inventory_manager", "production_supervisor", "clerk"):
        with pytest.raises(auth.AuthError):
            auth.create_user(conn, owner, "x" + old[:3], "password-123", old)


def test_owner_can_do_everything():
    for p in PAGE_PERMS + DAILY_WORK + OWNER_ONLY + ("view_all", "export_reports"):
        assert auth.has_permission("owner", p), p


def test_accountant_does_the_daily_work_but_sees_none_of_the_hidden_pages():
    for p in DAILY_WORK + ("view_all",):
        assert auth.has_permission("accountant", p), p
    for p in PAGE_PERMS + OWNER_ONLY + ("export_reports",):          # Dashboard, Insights, Partners, Profit, Reports, Audit, Backup, Settings/Users
        assert not auth.has_permission("accountant", p), p


def test_auditor_looks_at_everything_except_dashboard_and_insights_and_changes_nothing():
    for p in ("view_all", "view_partners", "view_profit", "view_reports", "export_reports", "view_audit", "view_backup"):
        assert auth.has_permission("auditor", p), p
    for p in ("view_dashboard", "view_insights", "manage_users") + DAILY_WORK + OWNER_ONLY:
        assert not auth.has_permission("auditor", p), p


def test_people_with_a_removed_role_are_switched_off_not_deleted(conn, owner):
    conn.execute("INSERT INTO users(username,password_hash,full_name,role,is_active,created_at) VALUES ('old1','x','Old One','inventory_manager',1,'x')")
    conn.execute("INSERT INTO users(username,password_hash,full_name,role,is_active,created_at) VALUES ('old2','x','Old Two','production_supervisor',1,'x')")
    sql = (Path(__file__).resolve().parents[1] / "database" / "migrations" / "0016_three_roles.sql").read_text(encoding="utf-8")
    conn.executescript(sql)
    rows = {r["username"]: r for r in conn.execute("SELECT username, role, is_active FROM users WHERE username IN ('old1','old2')")}
    assert rows["old1"]["is_active"] == 0 and rows["old2"]["is_active"] == 0 and rows["old1"]["role"] == "inventory_manager"      # still on file
    assert conn.execute("SELECT is_active FROM users WHERE username='boss'").fetchone()[0] == 1                                   # nobody else is touched
    uid = conn.execute("SELECT id FROM users WHERE username='old1'").fetchone()[0]
    with pytest.raises(auth.AuthError, match="old role"):
        auth.set_active(conn, owner, uid, True)                                                                                  # cannot be switched back on with no role
