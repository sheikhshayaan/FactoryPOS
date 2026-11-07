"""Partners, capital, profit-sharing and distributions.

- Ownership % = partner net capital / total net capital (from the ledger, never typed in).
- Profit-sharing has TWO modes. "ownership" (the default): every partner's profit share simply IS his ownership % on the date that
  matters, so a withdrawal or a new contribution moves the shares automatically. "manual": the owner types the shares
  (effective-dated basis points, 10000 = 100%). Switching is always possible; approved distributions keep the shares they were
  approved with.
- Distribution amount can never exceed the period's recognised net profit (a loss distributes nothing)
  nor what earlier distributions of that period already took.
- Approval (owner only) posts Dr Profit Distributions / Cr Distributions Payable per partner; paying
  later is cash only, so approval and payment are separate facts. Cash must exist to pay.
"""
from __future__ import annotations

import sqlite3

from database.connection import claim_idempotency_key, transaction, utc_now
from services import accounting_service as acct, audit_service
from services.auth_service import require_permission
from services.units import to_paisa


class ProfitError(Exception):
    pass


def add_partner(conn, actor, name, phone="", joined_on=None) -> int:
    require_permission(actor, "change_ownership")
    with transaction(conn):
        try:
            cur = conn.execute("INSERT INTO partners(name,phone,joined_on,created_at) VALUES (?,?,?,?)",
                               (name.strip(), phone, joined_on, utc_now()))
        except sqlite3.IntegrityError as e:
            raise ProfitError("Partner already exists.") from e
        audit_service.log(conn, "partner.add", user=actor, entity="partner", entity_id=cur.lastrowid)
        return cur.lastrowid


def record_capital(conn, actor, partner_id, amount, on, note="") -> int:
    """Capital contribution: Dr Cash / Cr Partner Capital. Never revenue."""
    require_permission(actor, "record_capital")
    p = to_paisa(amount)
    if p <= 0:
        raise ProfitError("Amount must be positive.")
    with transaction(conn):
        e = acct.post(conn, entry_date=on, description=f"Capital contribution {note}".strip(), user=actor,
                      source_type="capital", source_id=partner_id,
                      lines=[{"account": "1000", "debit": p}, {"account": "3000", "credit": p, "partner_id": partner_id}])
        audit_service.log(conn, "partner.capital", user=actor, entity="partner", entity_id=partner_id, details={"paisa": p})
        return e


def record_drawing(conn, actor, partner_id, amount, on, note="") -> int:
    """Withdrawal of capital/drawings: equity, never an expense."""
    require_permission(actor, "change_ownership")
    p = to_paisa(amount)
    if p <= 0:
        raise ProfitError("Amount must be positive.")
    with transaction(conn):
        e = acct.post(conn, entry_date=on, description=f"Partner drawing {note}".strip(), user=actor,
                      source_type="drawing", source_id=partner_id,
                      lines=[{"account": "3100", "debit": p, "partner_id": partner_id}, {"account": "1000", "credit": p}])
        audit_service.log(conn, "partner.drawing", user=actor, entity="partner", entity_id=partner_id, details={"paisa": p})
        return e


def ownership(conn, as_of=None) -> dict[int, dict]:
    """{partner_id: {'capital_paisa', 'ownership_bps'}} computed from contributions less drawings."""
    caps = {}
    for r in conn.execute("SELECT id FROM partners WHERE is_active=1"):
        # 3100 (drawings) is a debit-natured equity account: its credit-side balance is already negative.
        caps[r["id"]] = acct.balance(conn, "3000", partner_id=r["id"], as_of=as_of) \
                        + acct.balance(conn, "3100", partner_id=r["id"], as_of=as_of)
    total = sum(v for v in caps.values() if v > 0)
    return {pid: {"capital_paisa": c, "ownership_bps": (c * 10000 // total) if total > 0 and c > 0 else 0}
            for pid, c in caps.items()}


SHARE_MODES = ("ownership", "manual")
_MODE_KEY = "profit_share_mode"


def share_mode(conn) -> str:
    r = conn.execute("SELECT value FROM app_settings WHERE key=?", (_MODE_KEY,)).fetchone()
    return r[0] if r and r[0] in SHARE_MODES else "ownership"


def _write_mode(conn, actor, mode: str) -> None:
    old = share_mode(conn)
    if old == mode:
        return
    conn.execute("INSERT INTO app_settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_MODE_KEY, mode))
    audit_service.log(conn, "partner.share_mode", user=actor, entity="partners", details={"from": old, "to": mode})


def set_share_mode(conn, actor, mode: str) -> None:
    """Switch profit sharing between 'ownership' (automatic) and 'manual'. Manual uses the last shares that were typed in
    (set_profit_shares); if none exist yet, set them first."""
    require_permission(actor, "change_ownership")
    if mode not in SHARE_MODES:
        raise ProfitError("Unknown profit-sharing mode.")
    with transaction(conn):
        if mode == "manual" and not conn.execute("SELECT 1 FROM partner_profit_shares LIMIT 1").fetchone():
            raise ProfitError("No manual profit shares have been entered yet. Use 'Set profit-sharing %' first.")
        _write_mode(conn, actor, mode)


def _apportion(total: int, weights: dict[int, int]) -> dict[int, int]:
    """Largest-remainder split of `total` by integer `weights` (any scale): the parts always add up to exactly `total`."""
    wsum = sum(weights.values())
    if wsum <= 0:
        return {k: 0 for k in weights}
    base = {k: total * w // wsum for k, w in weights.items()}
    rem = total - sum(base.values())
    for k in sorted(weights, key=lambda k: (-(total * weights[k] % wsum), k))[:rem]:
        base[k] += 1
    return base


def ownership_shares(conn, as_of=None) -> dict[int, int]:
    """Profit shares that follow ownership: each partner's share of the net capital on `as_of`, in basis points that total exactly
    10000 (largest remainder). Partners with no capital left get 0. Empty if nobody has any capital."""
    own = ownership(conn, as_of)
    caps = {pid: v["capital_paisa"] for pid, v in own.items() if v["capital_paisa"] > 0}
    if not caps:
        return {}
    bps = _apportion(10000, caps)
    return {pid: bps.get(pid, 0) for pid in own}


def set_profit_shares(conn, actor, effective_from: str, shares_bps: dict[int, int], *, confirmed: bool) -> None:
    """Owner-confirmed, effective-dated; earlier rows are untouched. Must total exactly 10000 bps. Also switches profit sharing to manual."""
    require_permission(actor, "change_ownership")
    if not confirmed:
        raise ProfitError("Changing profit-sharing requires explicit confirmation.")
    if sum(shares_bps.values()) != 10000 or any(v < 0 for v in shares_bps.values()):
        raise ProfitError("Profit shares must be non-negative and total exactly 100%.")
    with transaction(conn):
        active = {r["id"] for r in conn.execute("SELECT id FROM partners WHERE is_active=1")}
        if set(shares_bps) != active:
            raise ProfitError("Shares must be given for every active partner and only those.")
        for pid, bps in shares_bps.items():
            try:
                conn.execute("INSERT INTO partner_profit_shares(partner_id,effective_from,share_bps,approved_by,created_at) "
                             "VALUES (?,?,?,?,?)", (pid, effective_from, bps, actor["id"], utc_now()))
            except sqlite3.IntegrityError as e:
                raise ProfitError("Shares already set for that effective date; use a later date.") from e
        audit_service.log(conn, "partner.shares", user=actor, entity="partners",
                          details={"effective_from": effective_from, "shares_bps": shares_bps})
        _write_mode(conn, actor, "manual")


def shares_effective(conn, as_of: str) -> dict[int, int]:
    """Profit shares in force on `as_of`: ownership % in automatic mode, the typed shares in manual mode."""
    if share_mode(conn) == "ownership":
        return ownership_shares(conn, None if as_of >= "9999" else as_of)
    rows = conn.execute(
        "SELECT p.partner_id, p.share_bps FROM partner_profit_shares p WHERE p.effective_from = "
        "(SELECT MAX(effective_from) FROM partner_profit_shares q WHERE q.partner_id=p.partner_id AND q.effective_from<=?)",
        (as_of,)).fetchall()
    return {r["partner_id"]: r["share_bps"] for r in rows}


def allocate(amount_paisa: int, shares_bps: dict[int, int]) -> dict[int, int]:
    """Largest-remainder split: parts always sum to exactly amount_paisa."""
    if sum(shares_bps.values()) != 10000:
        raise ProfitError("Shares must total 100%.")
    return _apportion(amount_paisa, shares_bps)


def distributable(conn, start: str, end: str) -> int:
    """Period net profit minus everything already approved for any period that overlaps it (so the same profit can never be paid out
    twice under two slightly different date ranges); never negative."""
    net = acct.income_statement(conn, start, end)["net_profit"]
    used = conn.execute("SELECT COALESCE(SUM(amount_paisa),0) FROM distributions WHERE period_start<=? AND period_end>=?",
                        (end, start)).fetchone()[0]
    return max(0, net - used)


def preview_split(conn, amount_paisa: int, period_end: str) -> list[dict]:
    """What approving `amount_paisa` for a period ending `period_end` would give each partner (the same split approve_distribution books)."""
    shares = shares_effective(conn, period_end)
    if not shares or sum(shares.values()) != 10000:
        return []
    split = allocate(amount_paisa, shares)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id,name FROM partners")}
    return [{"partner_id": pid, "name": names[pid], "share_bps": shares[pid], "amount_paisa": split[pid]} for pid in sorted(shares, key=lambda k: names[k])]


def approve_distribution(conn, actor, *, period_start, period_end, amount, on, note="", idem_key=None) -> int:
    require_permission(actor, "approve_distribution")
    p = to_paisa(amount)
    if p <= 0:
        raise ProfitError("Amount must be positive.")
    with transaction(conn):
        if idem_key and not claim_idempotency_key(conn, idem_key, "approve_distribution"):
            raise ProfitError("This distribution was already approved.")
        if p > distributable(conn, period_start, period_end):
            raise ProfitError("Amount exceeds the period's distributable profit (losses are never distributed).")
        shares = shares_effective(conn, period_end)
        if not shares or sum(shares.values()) != 10000:
            raise ProfitError("No complete profit-sharing arrangement is effective for this period.")
        split = allocate(p, shares)
        e = acct.post(conn, entry_date=on, description=f"Profit distribution {period_start}..{period_end}", user=actor,
                      source_type="distribution",
                      lines=[{"account": "3200", "debit": p}] +
                            [{"account": "2400", "credit": a, "partner_id": pid} for pid, a in split.items() if a > 0])
        cur = conn.execute("INSERT INTO distributions(period_start,period_end,amount_paisa,shares_as_of,note,approved_by,"
                           "approved_at,entry_id) VALUES (?,?,?,?,?,?,?,?)",
                           (period_start, period_end, p, period_end, note, actor["id"], utc_now(), e))
        for pid, a in split.items():
            conn.execute("INSERT INTO distribution_lines(distribution_id,partner_id,share_bps,amount_paisa) VALUES (?,?,?,?)",
                         (cur.lastrowid, pid, shares[pid], a))
        audit_service.log(conn, "distribution.approve", user=actor, entity="distribution", entity_id=cur.lastrowid,
                          details={"paisa": p, "split": split})
        return cur.lastrowid


def pay_distribution(conn, actor, *, distribution_id, partner_id, amount, on, idem_key=None) -> int:
    require_permission(actor, "approve_distribution")
    p = to_paisa(amount)
    with transaction(conn):
        if idem_key and not claim_idempotency_key(conn, idem_key, "pay_distribution"):
            raise ProfitError("This payment was already recorded.")
        ln = conn.execute("SELECT * FROM distribution_lines WHERE distribution_id=? AND partner_id=?",
                          (distribution_id, partner_id)).fetchone()
        if not ln or p <= 0 or p > ln["amount_paisa"] - ln["paid_paisa"]:
            raise ProfitError("Payment must be positive and not exceed the unpaid distribution.")
        e = acct.post(conn, entry_date=on, description="Distribution payment", user=actor,
                      source_type="distribution_payment", source_id=distribution_id,
                      lines=[{"account": "2400", "debit": p, "partner_id": partner_id}, {"account": "1000", "credit": p}])
        conn.execute("UPDATE distribution_lines SET paid_paisa=paid_paisa+? WHERE id=?", (p, ln["id"]))
        audit_service.log(conn, "distribution.pay", user=actor, entity="distribution", entity_id=distribution_id,
                          details={"partner": partner_id, "paisa": p})
        return e


def pay_all_distribution(conn, actor, *, distribution_id, on) -> int:
    """Pay every partner what is still unpaid of one distribution, in one go: all payments or none (cash must cover the total).
    Returns the total paid in paisa."""
    require_permission(actor, "approve_distribution")
    with transaction(conn):
        lines = conn.execute("SELECT partner_id, amount_paisa - paid_paisa AS due FROM distribution_lines WHERE distribution_id=? "
                             "AND amount_paisa > paid_paisa ORDER BY partner_id", (distribution_id,)).fetchall()
        if not lines:
            raise ProfitError("Everything in this distribution is already paid.")
        for ln in lines:
            pay_distribution(conn, actor, distribution_id=distribution_id, partner_id=ln["partner_id"], amount=_rupees(ln["due"]), on=on)
        return sum(l["due"] for l in lines)


def _rupees(paisa: int) -> str:
    return f"{paisa // 100}.{paisa % 100:02d}"
