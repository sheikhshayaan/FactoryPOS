"""Read-only list queries for the UI (no writes, no business rules)."""
from __future__ import annotations

from services import accounting_service as acct


def rows(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def customers(conn):
    return rows(conn, "SELECT c.id, c.name, c.phone, c.address, "
                "COALESCE((SELECT SUM(l.debit_paisa)-SUM(l.credit_paisa) FROM journal_lines l JOIN accounts a ON a.id=l.account_id "
                "WHERE a.code='1100' AND l.customer_id=c.id),0) AS outstanding, "
                "COALESCE((SELECT SUM(l.credit_paisa)-SUM(l.debit_paisa) FROM journal_lines l JOIN accounts a ON a.id=l.account_id "
                "WHERE a.code='2200' AND l.customer_id=c.id),0) AS advance, "
                "(SELECT COUNT(*) FROM jobs j WHERE j.customer_id=c.id AND j.status<>'cancelled') AS entries, "
                "COALESCE((SELECT SUM(total_paisa) FROM invoices i WHERE i.customer_id=c.id),0) AS billed "
                "FROM customers c WHERE c.is_active=1 ORDER BY c.name")


def suppliers(conn):
    return rows(conn, "SELECT s.id, s.name, s.phone, s.address, "
                "COALESCE((SELECT SUM(l.credit_paisa)-SUM(l.debit_paisa) FROM journal_lines l JOIN accounts a ON a.id=l.account_id "
                "WHERE a.code='2000' AND l.supplier_id=s.id),0) AS owed FROM suppliers s WHERE s.is_active=1 ORDER BY s.name")


def materials(conn):
    return rows(conn, "SELECT m.id, m.name, m.category, m.base_unit, m.tracks_rolls, m.reorder_level_milli, m.roll_length_milli, s.qty_milli, s.value_paisa, "
                "(SELECT COUNT(*) FROM stock_rolls r JOIN stock_lots l ON l.id=r.lot_id WHERE l.material_id=m.id AND l.ownership='factory' "
                "AND r.remaining_milli>0) AS rolls, "
                "COALESCE((SELECT SUM(qty_remaining_milli) FROM stock_lots WHERE material_id=m.id AND ownership='customer'),0) AS cust_milli "
                "FROM materials m JOIN material_stock s ON s.material_id=m.id WHERE m.is_active=1 ORDER BY m.name")


def rolls(conn, material_id):
    return rows(conn, "SELECT r.id, r.length_milli, r.remaining_milli, l.ownership, l.received_on FROM stock_rolls r JOIN stock_lots l "
                "ON l.id=r.lot_id WHERE l.material_id=? AND r.remaining_milli>0 ORDER BY l.received_on, r.id", (material_id,))


def movements(conn, material_id=None, search="", limit=200, offset=0):
    q = ("SELECT mv.id, mv.created_at, m.name AS material, mv.movement_type, mv.ownership, mv.qty_milli, mv.value_paisa, "
         "mv.ref_type, mv.ref_id, mv.reason, u.username AS user FROM stock_movements mv JOIN materials m ON m.id=mv.material_id "
         "LEFT JOIN users u ON u.id=mv.created_by WHERE 1=1")
    a = []
    if material_id:
        q += " AND mv.material_id=?"; a.append(material_id)
    if search:
        q += " AND (m.name LIKE ? OR mv.movement_type LIKE ? OR COALESCE(mv.ref_id,'') LIKE ? OR COALESCE(mv.reason,'') LIKE ?)"
        a += [f"%{search}%"] * 4
    return rows(conn, q + " ORDER BY mv.id DESC LIMIT ? OFFSET ?", (*a, limit, offset))


def jobs(conn, status=None):
    q = ("SELECT j.id, j.job_no, c.name AS customer, j.product_type, j.fabric_source, j.status, j.order_date, j.delivery_date, "
         "j.pricing_method, j.quoted_price_paisa, j.final_price_paisa, j.quantity_pieces, "
         "COALESCE((SELECT SUM(amount_paisa) FROM job_costs WHERE job_id=j.id),0) AS cost_paisa "
         "FROM jobs j JOIN customers c ON c.id=j.customer_id")
    return rows(conn, q + (" WHERE j.status=?" if status else " WHERE j.status<>'cancelled'") + " ORDER BY j.id DESC", (status,) if status else ())


def stock_sheet(conn):
    """(headers, rows) of every material for an Excel/CSV export. Amounts are exact decimals (rupees / units), never floats."""
    from services.units import from_milli, from_paisa, mul_div_round
    last = last_purchase_prices(conn)
    headers = ["Material", "Category", "Unit", "Factory stock", "Average cost per unit (Rs)", "Last purchase price per unit (Rs)", "Stock value (Rs)",
               "Customer property", "Reorder level", "Status", "Meters in one roll (paper)", "Meters in stock (paper)"]
    out = []
    for m in materials(conn):
        qty = m["qty_milli"]
        avg = mul_div_round(m["value_paisa"], 1000, qty) if qty > 0 else None
        lp = last.get(m["id"])
        lastp = mul_div_round(lp[0], 1000, lp[1]) if lp and lp[1] > 0 and lp[0] > 0 else None
        low = m["reorder_level_milli"] > 0 and qty <= m["reorder_level_milli"]
        out.append([m["name"], m["category"], m["base_unit"], from_milli(qty), "" if avg is None else from_paisa(avg), "" if lastp is None else from_paisa(lastp),
                    from_paisa(m["value_paisa"]), from_milli(m["cust_milli"]), from_milli(m["reorder_level_milli"]), "LOW" if low else "ok",
                    from_milli(m["roll_length_milli"]) if m["roll_length_milli"] else "",
                    from_milli(mul_div_round(qty, m["roll_length_milli"], 1000)) if m["roll_length_milli"] else ""])
    return headers, out


def production_entries(conn):
    """Production entries with their invoice (same number) and how much of it is paid. bill_status: none | unpaid | part | paid."""
    out = rows(conn, "SELECT j.id, j.job_no, j.customer_id, c.name AS customer, j.product_type, j.fabric_source, j.status, j.order_date, j.quantity_pieces, "
               "COALESCE((SELECT SUM(amount_paisa) FROM job_costs WHERE job_id=j.id),0) AS cost_paisa, "
               "i.id AS invoice_id, i.total_paisa AS invoice_paisa, i.pieces AS invoice_pieces, i.rate_paisa, "
               "COALESCE((SELECT SUM(amount_paisa) FROM invoice_allocations WHERE invoice_id=i.id),0) AS paid_paisa "
               "FROM jobs j JOIN customers c ON c.id=j.customer_id LEFT JOIN invoices i ON i.id=(SELECT MIN(id) FROM invoices WHERE job_id=j.id) "
               "WHERE j.status<>'cancelled' ORDER BY j.id DESC")
    for r in out:
        if r["invoice_id"] is None:
            r["due_paisa"], r["bill_status"] = None, "none"
        else:
            r["due_paisa"] = r["invoice_paisa"] - r["paid_paisa"]
            r["bill_status"] = "paid" if r["due_paisa"] == 0 else "unpaid" if r["paid_paisa"] == 0 else "part"
    return out


def last_purchase_prices(conn):
    """{material_id: (total_cost_paisa, qty_milli, date)} for the most recent (not undone) factory purchase of each material."""
    return {r["material_id"]: (r["total_cost_paisa"], r["qty_received_milli"], r["received_on"]) for r in rows(
        conn, "SELECT l.material_id, l.total_cost_paisa, l.qty_received_milli, l.received_on FROM stock_lots l "
        "WHERE l.ownership='factory' AND l.qty_received_milli>0 AND l.id=(SELECT MAX(l2.id) FROM stock_lots l2 WHERE l2.material_id=l.material_id AND l2.ownership='factory' "
        "AND NOT EXISTS (SELECT 1 FROM stock_movements mv WHERE mv.ref_type='undo_receipt' AND mv.ref_id=CAST(l2.id AS TEXT)))")}


def stock_receipts(conn):
    """Every stock receipt (lot), newest first, with how much of it is left and whether it was undone."""
    return rows(conn, "SELECT l.id, l.received_on, m.name AS material, m.base_unit, m.roll_length_milli, l.ownership, c.name AS customer, s.name AS supplier, l.qty_received_milli, "
                "l.qty_remaining_milli, l.total_cost_paisa, l.invoice_no, l.payment_status, "
                "EXISTS(SELECT 1 FROM stock_movements mv WHERE mv.ref_type='undo_receipt' AND mv.ref_id=CAST(l.id AS TEXT)) AS undone "
                "FROM stock_lots l JOIN materials m ON m.id=l.material_id LEFT JOIN suppliers s ON s.id=l.supplier_id "
                "LEFT JOIN customers c ON c.id=l.customer_id ORDER BY l.id DESC")


def job_costs(conn, job_id):
    return rows(conn, "SELECT jc.id, jc.created_at, jc.kind, m.name AS material, jc.qty_milli, jc.minutes, jc.amount_paisa, jc.is_estimate, jc.basis, "
                "jc.description FROM job_costs jc LEFT JOIN materials m ON m.id=jc.material_id WHERE jc.job_id=? ORDER BY jc.id", (job_id,))


def machines(conn):
    return rows(conn, "SELECT id, name, kind, hourly_rate_paisa, is_active FROM machines ORDER BY id")


def machine_runs(conn, limit=200):
    return rows(conn, "SELECT r.id, j.job_no, m.name AS machine, r.started_at, r.ended_at, r.minutes, r.downtime_minutes, r.meters_milli "
                "FROM machine_runs r JOIN jobs j ON j.id=r.job_id JOIN machines m ON m.id=r.machine_id ORDER BY r.id DESC LIMIT ?", (limit,))


def meter_readings(conn):
    return rows(conn, "SELECT r.id, m.name AS machine, r.read_at, r.kwh_milli FROM machine_meter_readings r JOIN machines m ON m.id=r.machine_id "
                "ORDER BY r.read_at DESC, r.id DESC")


def electricity_bills(conn):
    return rows(conn, "SELECT * FROM electricity_bills ORDER BY period_end DESC")


def employees(conn):
    return rows(conn, "SELECT e.id, e.emp_code, e.name, e.job_role, e.default_shift, e.cost_category, e.is_active, e.joined_on, e.left_on, e.exit_kind, e.exit_reason, "
                "(SELECT monthly_paisa FROM salary_history WHERE employee_id=e.id ORDER BY effective_from DESC LIMIT 1) AS salary_paisa, "
                "COALESCE((SELECT SUM(l.debit_paisa)-SUM(l.credit_paisa) FROM journal_lines l JOIN accounts a ON a.id=l.account_id "
                "WHERE a.code='1300' AND l.employee_id=e.id),0) AS advance_paisa FROM employees e ORDER BY e.is_active DESC, e.emp_code")


def attendance(conn, period):
    return rows(conn, "SELECT a.id, e.emp_code, e.name, a.work_date, a.status, a.shift, a.clock_in, a.clock_out, a.break_minutes, a.worked_minutes, a.auto_out, a.notes "
                "FROM attendance a JOIN employees e ON e.id=a.employee_id WHERE substr(a.work_date,1,7)=? ORDER BY a.work_date DESC, e.emp_code", (period,))


def payslips(conn, period=None):
    q = ("SELECT p.id, p.period, e.emp_code, e.name, p.basic_paisa, p.allowances_paisa, p.deductions_paisa, p.advance_recovered_paisa, p.net_paisa, "
         "p.net_paisa - COALESCE((SELECT SUM(amount_paisa) FROM payslip_payments WHERE payslip_id=p.id),0) AS due_paisa "
         "FROM payslips p JOIN employees e ON e.id=p.employee_id")
    return rows(conn, q + (" WHERE p.period=?" if period else "") + " ORDER BY p.period DESC, e.emp_code", (period,) if period else ())


def invoices(conn):
    return rows(conn, "SELECT i.id, i.invoice_no, c.name AS customer, i.customer_id, i.invoice_date, i.total_paisa, "
                "COALESCE((SELECT SUM(amount_paisa) FROM invoice_allocations WHERE invoice_id=i.id),0) AS paid_paisa, "
                "i.total_paisa - COALESCE((SELECT SUM(amount_paisa) FROM invoice_allocations WHERE invoice_id=i.id),0) AS due_paisa, j.job_no "
                "FROM invoices i JOIN customers c ON c.id=i.customer_id LEFT JOIN jobs j ON j.id=i.job_id ORDER BY i.id DESC")


def receipts(conn, limit=300):
    return rows(conn, "SELECT e.id, e.entry_date, e.description, l.debit_paisa AS amount_paisa, "
                "(SELECT c.name FROM journal_lines l2 JOIN customers c ON c.id=l2.customer_id WHERE l2.entry_id=e.id LIMIT 1) AS customer, "
                "CASE WHEN EXISTS (SELECT 1 FROM invoice_allocations ia WHERE ia.entry_id=e.id) THEN 'against invoice' ELSE 'advance' END AS kind "
                "FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id JOIN accounts a ON a.id=l.account_id "
                "WHERE e.source_type='receipt' AND a.code='1000' ORDER BY e.id DESC LIMIT ?", (limit,))


_EXP_FROM = ("FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id JOIN accounts a ON a.id=l.account_id "
             "WHERE e.source_type='expense' AND l.debit_paisa>0")


def _exp_range(start, end):
    sql, args = "", []
    if start:
        sql += " AND e.entry_date>=?"; args.append(start)
    if end:
        sql += " AND e.entry_date<=?"; args.append(end)
    return sql, args


def expenses(conn, limit=5000, start=None, end=None):
    """Expense entries, newest first, optionally only those dated start..end. `reversed` = 1 when the entry was cancelled by a reversal
    (the original stays in the books, shown in red)."""
    rng, args = _exp_range(start, end)
    return rows(conn, "SELECT e.id, e.entry_date, e.description, e.source_type, a.code, a.name AS account, l.debit_paisa AS amount_paisa, "
                "EXISTS(SELECT 1 FROM journal_entries r WHERE r.reverses_entry_id=e.id) AS reversed " + _EXP_FROM + rng +
                " ORDER BY e.entry_date DESC, e.id DESC LIMIT ?", (*args, limit))


def expense_total(conn, start=None, end=None):
    """(total_paisa, entries) of the expenses dated start..end. Reversed (cancelled) entries are NOT counted."""
    rng, args = _exp_range(start, end)
    t, n = conn.execute("SELECT COALESCE(SUM(l.debit_paisa),0), COUNT(*) " + _EXP_FROM + rng +
                        " AND NOT EXISTS (SELECT 1 FROM journal_entries r WHERE r.reverses_entry_id=e.id)", args).fetchone()
    return t, n


def expenses_by_category(conn, start=None, end=None):
    """[(category name, total_paisa)] of the counted (not reversed) expenses dated start..end, biggest first."""
    rng, args = _exp_range(start, end)
    return [(r[0], r[1]) for r in conn.execute("SELECT a.name, SUM(l.debit_paisa) AS t " + _EXP_FROM + rng +
                                              " AND NOT EXISTS (SELECT 1 FROM journal_entries r WHERE r.reverses_entry_id=e.id) GROUP BY a.id ORDER BY t DESC", args)]


def assets(conn, limit=500):
    """The asset register: records typed in on the Expenses page (not in the books at all), plus capital purchases that were booked
    before the register existed (kind='books' - those did move cash, and can be reversed). `voided` = 1 when cancelled."""
    out = rows(conn, "SELECT id, acquired_on AS on_date, name, price_paisa, COALESCE(notes,'') AS notes, 'record' AS kind, "
               "CASE WHEN voided_at IS NULL THEN 0 ELSE 1 END AS voided, COALESCE(void_reason,'') AS void_reason FROM asset_records")
    out += rows(conn, "SELECT e.id, e.entry_date AS on_date, e.description AS name, l.debit_paisa AS price_paisa, '' AS notes, 'books' AS kind, "
                "EXISTS(SELECT 1 FROM journal_entries r WHERE r.reverses_entry_id=e.id) AS voided, '' AS void_reason "
                "FROM journal_entries e JOIN journal_lines l ON l.entry_id=e.id WHERE e.source_type='capex' AND l.debit_paisa>0")
    out.sort(key=lambda r: (r["on_date"], r["kind"], r["id"]), reverse=True)
    return out[:limit]


def customer_stock(conn, customer_id=None):
    """Customer-owned material (their own fabric etc.) per customer and material: how much was given, how much is still here."""
    q_ = ("SELECT l.customer_id, c.name AS customer, m.id AS material_id, m.name AS material, m.category, m.base_unit, m.roll_length_milli, "
          "SUM(l.qty_received_milli) AS received_milli, SUM(l.qty_remaining_milli) AS left_milli, MAX(l.received_on) AS last_on "
          "FROM stock_lots l JOIN materials m ON m.id=l.material_id JOIN customers c ON c.id=l.customer_id "
          "WHERE l.ownership='customer' AND l.qty_received_milli>0 "
          "AND NOT EXISTS (SELECT 1 FROM stock_movements mv WHERE mv.ref_type='undo_receipt' AND mv.ref_id=CAST(l.id AS TEXT))")
    args: tuple = ()
    if customer_id is not None:
        q_ += " AND l.customer_id=?"; args = (customer_id,)
    return rows(conn, q_ + " GROUP BY l.customer_id, l.material_id ORDER BY c.name, m.name", args)


def customer_lots(conn, customer_id):
    """Every receipt of this customer's own material (newest first), with what is left of it."""
    return rows(conn, "SELECT l.id, l.received_on, m.name AS material, m.base_unit, m.roll_length_milli, l.qty_received_milli, l.qty_remaining_milli, l.reference, "
                "EXISTS(SELECT 1 FROM stock_movements mv WHERE mv.ref_type='undo_receipt' AND mv.ref_id=CAST(l.id AS TEXT)) AS undone "
                "FROM stock_lots l JOIN materials m ON m.id=l.material_id WHERE l.ownership='customer' AND l.customer_id=? AND l.qty_received_milli>0 "
                "ORDER BY l.received_on DESC, l.id DESC", (customer_id,))


def expense_accounts(conn):
    return rows(conn, "SELECT code, name FROM accounts WHERE type='expense' AND code NOT IN ('5000','5300','5900','5100','6100') AND is_active=1 ORDER BY code")


def partners(conn):
    from services import profit_service as ps
    own = ps.ownership(conn)
    shares = ps.shares_effective(conn, "9999-12-31")
    out = []
    for r in conn.execute("SELECT id,name,phone,joined_on FROM partners WHERE is_active=1 ORDER BY name"):
        out.append({"id": r["id"], "name": r["name"], "phone": r["phone"], "joined_on": r["joined_on"],
                    "capital_paisa": own[r["id"]]["capital_paisa"], "ownership_bps": own[r["id"]]["ownership_bps"],
                    "share_bps": shares.get(r["id"])})
    return out


def distributions(conn):
    return rows(conn, "SELECT d.id, d.period_start, d.period_end, d.amount_paisa, d.approved_at, u.username AS approved_by, "
                "(SELECT COALESCE(SUM(paid_paisa),0) FROM distribution_lines WHERE distribution_id=d.id) AS paid_paisa "
                "FROM distributions d JOIN users u ON u.id=d.approved_by ORDER BY d.id DESC")


def distribution_lines(conn, distribution_id):
    return rows(conn, "SELECT dl.partner_id, p.name, dl.share_bps, dl.amount_paisa, dl.paid_paisa FROM distribution_lines dl JOIN partners p ON p.id=dl.partner_id "
                "WHERE dl.distribution_id=? ORDER BY p.name", (distribution_id,))


def audit(conn, search="", limit=100, offset=0):
    where, a = "", []
    if search:
        where = " WHERE action LIKE ? OR COALESCE(username,'') LIKE ? OR COALESCE(entity,'') LIKE ? OR COALESCE(details,'') LIKE ? OR COALESCE(reason,'') LIKE ?"
        a = [f"%{search}%"] * 5
    total = conn.execute("SELECT COUNT(*) FROM audit_log" + where, a).fetchone()[0]
    return total, rows(conn, "SELECT id, ts, username, action, entity, entity_id, reason, details FROM audit_log" + where +
                       " ORDER BY id DESC LIMIT ? OFFSET ?", (*a, limit, offset))


def users(conn):
    return rows(conn, "SELECT id, username, full_name, role, is_active, created_at, "
                      "CASE WHEN security_answer_hash IS NULL THEN 0 ELSE 1 END AS has_recovery FROM users ORDER BY username")


def backups(conn):
    return rows(conn, "SELECT id, created_at, kind, path, size_bytes, status, error FROM backup_history ORDER BY id DESC")


def trial_balance(conn, as_of=None):
    return acct.trial_balance(conn, as_of)
