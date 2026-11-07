"""Profit sharing in two modes: automatic (= ownership %, moves with withdrawals) and manual. Every figure worked out by hand."""
import pytest

from services import accounting_service as ac, auth_service as auth, inventory_service as inv, profit_service as ps
from services.units import to_paisa as P


@pytest.fixture
def w(conn, owner):
    auth.create_user(conn, owner, "acc", "accountant-pw", "accountant")
    return dict(owner=owner, acc=auth.authenticate(conn, "acc", "accountant-pw"), cust=inv.add_customer(conn, owner, "Ali Traders"))


@pytest.fixture
def firm(conn, w):
    a, b, c = (ps.add_partner(conn, w["owner"], n) for n in "ABC")
    ps.record_capital(conn, w["owner"], a, "600000", "2026-07-01")
    ps.record_capital(conn, w["owner"], b, "300000", "2026-07-01")
    ps.record_capital(conn, w["owner"], c, "100000", "2026-07-01")
    return dict(a=a, b=b, c=c)


def profit(conn, w, amount="100000"):
    i = ac.issue_invoice(conn, w["acc"], invoice_no="P1", customer_id=w["cust"], total=amount, invoice_date="2026-08-01")
    ac.receive_payment(conn, w["acc"], customer_id=w["cust"], amount=amount, paid_on="2026-08-02", invoice_id=i)


def test_default_is_automatic_and_shares_equal_ownership(conn, firm):
    assert ps.share_mode(conn) == "ownership"
    assert ps.shares_effective(conn, "2026-08-01") == {firm["a"]: 6000, firm["b"]: 3000, firm["c"]: 1000}      # 600 : 300 : 100 of 1,000


def test_a_withdrawal_moves_the_shares_by_itself(conn, w, firm):
    ps.record_drawing(conn, w["owner"], firm["c"], "50000", "2026-08-10")                    # C: 100,000 -> 50,000 ; capital 600 : 300 : 50
    s = ps.shares_effective(conn, "2026-08-31")
    # 10000 x 600/950 = 6315.79, 3157.89, 526.32 -> 6315 + 3157 + 526 = 9998; the 2 left over go to the biggest fractions (B .89, A .79)
    assert s == {firm["a"]: 6316, firm["b"]: 3158, firm["c"]: 526} and sum(s.values()) == 10000
    assert ps.shares_effective(conn, "2026-08-09")[firm["c"]] == 1000                          # before the withdrawal he still had 10%


def test_a_partner_who_took_everything_out_gets_zero(conn, w, firm):
    ps.record_drawing(conn, w["owner"], firm["c"], "100000", "2026-08-10")
    s = ps.shares_effective(conn, "2026-08-31")
    assert s[firm["c"]] == 0 and s[firm["a"]] + s[firm["b"]] == 10000 and s[firm["a"]] == 6667       # 600 / 900


def test_typing_shares_switches_to_manual_and_ownership_no_longer_moves_them(conn, w, firm):
    ps.set_profit_shares(conn, w["owner"], "2026-07-01", {firm["a"]: 5000, firm["b"]: 3000, firm["c"]: 2000}, confirmed=True)
    assert ps.share_mode(conn) == "manual"
    ps.record_drawing(conn, w["owner"], firm["a"], "300000", "2026-08-10")
    assert ps.shares_effective(conn, "2026-08-31") == {firm["a"]: 5000, firm["b"]: 3000, firm["c"]: 2000}


def test_switch_back_to_automatic_and_to_manual_again(conn, w, firm):
    ps.set_profit_shares(conn, w["owner"], "2026-07-01", {firm["a"]: 5000, firm["b"]: 3000, firm["c"]: 2000}, confirmed=True)
    ps.set_share_mode(conn, w["owner"], "ownership")
    assert ps.shares_effective(conn, "2026-08-01")[firm["a"]] == 6000
    ps.set_share_mode(conn, w["owner"], "manual")
    assert ps.shares_effective(conn, "2026-08-01")[firm["a"]] == 5000
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='partner.share_mode'").fetchone()[0] == 3     # manual, ownership, manual


def test_manual_needs_typed_shares_first_and_only_owner_switches(conn, w, firm):
    with pytest.raises(ps.ProfitError, match="Set profit-sharing"):
        ps.set_share_mode(conn, w["owner"], "manual")
    with pytest.raises(auth.PermissionDenied):
        ps.set_share_mode(conn, w["acc"], "ownership")
    with pytest.raises(ps.ProfitError):
        ps.set_share_mode(conn, w["owner"], "whatever")


def test_approval_in_automatic_mode_splits_by_ownership_and_keeps_it(conn, w, firm):
    profit(conn, w)
    ps.record_drawing(conn, w["owner"], firm["c"], "50000", "2026-08-10")
    d = ps.approve_distribution(conn, w["owner"], period_start="2026-08-01", period_end="2026-08-31", amount="100000", on="2026-08-31")
    lines = {r["partner_id"]: (r["share_bps"], r["amount_paisa"]) for r in conn.execute("SELECT * FROM distribution_lines WHERE distribution_id=?", (d,))}
    assert lines == {firm["a"]: (6316, P("63160")), firm["b"]: (3158, P("31580")), firm["c"]: (526, P("5260"))}      # 100,000 x each %
    ps.record_drawing(conn, w["owner"], firm["a"], "100000", "2026-09-05")                                            # later changes never touch it
    assert {r["partner_id"]: r["share_bps"] for r in conn.execute("SELECT * FROM distribution_lines WHERE distribution_id=?", (d,))}[firm["a"]] == 6316


def test_preview_equals_what_approval_books(conn, w, firm):
    profit(conn, w)
    pre = ps.preview_split(conn, P("12345.67"), "2026-08-31")
    assert sum(x["amount_paisa"] for x in pre) == P("12345.67") and [x["name"] for x in pre] == ["A", "B", "C"]
    d = ps.approve_distribution(conn, w["owner"], period_start="2026-08-01", period_end="2026-08-31", amount="12345.67", on="2026-08-31")
    booked = {r["partner_id"]: r["amount_paisa"] for r in conn.execute("SELECT * FROM distribution_lines WHERE distribution_id=?", (d,))}
    assert booked == {x["partner_id"]: x["amount_paisa"] for x in pre}


def test_the_same_profit_cannot_be_paid_out_under_overlapping_periods(conn, w, firm):
    profit(conn, w)                                                                                  # Rs 100,000 profit, dated 1 August
    ps.approve_distribution(conn, w["owner"], period_start="2026-08-01", period_end="2026-08-31", amount="60000", on="2026-08-31")
    with pytest.raises(ps.ProfitError):                                                              # 1-15 Aug overlaps: only 40,000 is left
        ps.approve_distribution(conn, w["owner"], period_start="2026-08-01", period_end="2026-08-15", amount="40000.01", on="2026-08-31")
    ps.approve_distribution(conn, w["owner"], period_start="2026-08-01", period_end="2026-08-15", amount="40000", on="2026-08-31")
    with pytest.raises(ps.ProfitError):
        ps.approve_distribution(conn, w["owner"], period_start="2026-08-10", period_end="2026-08-20", amount="0.01", on="2026-08-31")


def test_pay_all_pays_every_partner_in_one_go(conn, w, firm):
    profit(conn, w)
    d = ps.approve_distribution(conn, w["owner"], period_start="2026-08-01", period_end="2026-08-31", amount="60000", on="2026-08-31")
    ps.pay_distribution(conn, w["owner"], distribution_id=d, partner_id=firm["a"], amount="10000", on="2026-09-01")      # A already got 10,000 of 36,000
    total = ps.pay_all_distribution(conn, w["owner"], distribution_id=d, on="2026-09-02")
    assert total == P("50000")                                                                                          # 60,000 - 10,000 already paid
    assert ac.balance(conn, "2400") == 0 and ac.balance(conn, "1000") == P("1040000") and ac.check_integrity(conn) == []
    assert conn.execute("SELECT SUM(paid_paisa) FROM distribution_lines WHERE distribution_id=?", (d,)).fetchone()[0] == P("60000")
    with pytest.raises(ps.ProfitError, match="already paid"):
        ps.pay_all_distribution(conn, w["owner"], distribution_id=d, on="2026-09-03")


def test_pay_all_is_all_or_nothing_when_cash_is_short(conn, w, firm):
    profit(conn, w)
    d = ps.approve_distribution(conn, w["owner"], period_start="2026-08-01", period_end="2026-08-31", amount="60000", on="2026-08-31")
    ac.record_expense(conn, w["acc"], account_code="6000", amount="1050000", on="2026-09-05", description="big bill")       # cash 1,100,000 (capital + sale) -> 50,000, less than the 60,000 owed
    with pytest.raises(ac.InsufficientCash):
        ps.pay_all_distribution(conn, w["owner"], distribution_id=d, on="2026-09-06")
    assert conn.execute("SELECT SUM(paid_paisa) FROM distribution_lines WHERE distribution_id=?", (d,)).fetchone()[0] == 0
    assert ac.balance(conn, "1000") == P("50000")


def test_manual_mode_still_works_for_approval(conn, w, firm):
    profit(conn, w)
    ps.set_profit_shares(conn, w["owner"], "2026-07-01", {firm["a"]: 5000, firm["b"]: 3000, firm["c"]: 2000}, confirmed=True)
    d = ps.approve_distribution(conn, w["owner"], period_start="2026-08-01", period_end="2026-08-31", amount="100000", on="2026-08-31")
    assert sorted(r["amount_paisa"] for r in conn.execute("SELECT * FROM distribution_lines WHERE distribution_id=?", (d,))) == [P("20000"), P("30000"), P("50000")]
