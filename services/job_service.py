"""Production jobs: estimates, actual cost capture, costing summary, finalisation.

Costing rules
- Factory-owned material  -> cost at weighted average (from inventory ledger).
- Customer-owned fabric   -> zero cost; kept separate (customer property).
- Wastage                 -> its own cost line (the stock issue is posted once, as wastage,
                             so it is never also counted as normal material).
- Electricity             -> hourly-rate ESTIMATE (machine rate) or bill-based from meter readings;
                             unknown if neither is configured (flagged, never invented).
- Cost lines are append-only; finalisation snapshots the cost and locks the job.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from database.connection import transaction, utc_now
from services import accounting_service as acct, audit_service, electricity_service as el, inventory_service as inv
from services.auth_service import has_permission, require_permission
from services.units import from_paisa, mul_div_round, to_milli, to_paisa


class JobError(Exception):
    pass


OPEN_STATES = ("approved", "in_progress")


def _job(conn, job_id, *, for_costs=False):
    j = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not j:
        raise JobError("Job not found.")
    if for_costs and j["status"] not in OPEN_STATES + ("completed",):
        raise JobError(f"Costs cannot be added while job is '{j['status']}'.")
    return j


def create_job(conn, actor, *, job_no, customer_id, product_type, fabric_source, order_date,
               delivery_date=None, quantity_pieces=None, fabric_length=None, specs="", status="quotation") -> int:
    require_permission(actor, "manage_jobs")
    if fabric_source not in ("factory", "customer"):
        raise JobError("fabric_source must be 'factory' or 'customer'.")
    if status not in ("quotation", "approved", "in_progress"):
        raise JobError("A new job can only start as quotation, approved or in_progress.")
    with transaction(conn):
        try:
            cur = conn.execute(
                "INSERT INTO jobs(job_no,customer_id,order_date,delivery_date,product_type,fabric_source,"
                "quantity_pieces,fabric_length_milli,specs,status,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_no.strip(), customer_id, order_date, delivery_date, product_type, fabric_source,
                 quantity_pieces, to_milli(fabric_length) if fabric_length is not None else None,
                 specs, status, actor["id"], utc_now()))
        except sqlite3.IntegrityError as e:
            raise JobError(f"Cannot create job (duplicate number or unknown customer): {e}") from e
        audit_service.log(conn, "job.create", user=actor, entity="job", entity_id=cur.lastrowid)
        return cur.lastrowid


def set_status(conn, actor, job_id, status):
    require_permission(actor, "manage_jobs")
    if status in ("finalized",):
        raise JobError("Use finalize_job().")
    with transaction(conn):
        j = _job(conn, job_id)
        if j["status"] in ("finalized", "cancelled"):
            raise JobError(f"Job is {j['status']}.")
        conn.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
        audit_service.log(conn, "job.status", user=actor, entity="job", entity_id=job_id,
                          details={"from": j["status"], "to": status})


def add_estimate(conn, actor, job_id, kind, *, material_id=None, qty=None, minutes=0, amount="0", description=""):
    require_permission(actor, "manage_jobs")
    with transaction(conn):
        j = _job(conn, job_id)
        if j["status"] in ("finalized", "cancelled"):
            raise JobError("Job is closed.")
        conn.execute("INSERT INTO job_estimates(job_id,kind,material_id,qty_milli,minutes,amount_paisa,description) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (job_id, kind, material_id, to_milli(qty) if qty is not None else 0, minutes,
                      to_paisa(amount), description))


def _add_cost(conn, actor, job_id, kind, amount_paisa, **kw) -> int:
    cur = conn.execute(
        "INSERT INTO job_costs(job_id,kind,material_id,qty_milli,minutes,amount_paisa,is_estimate,basis,description,"
        "machine_id,stock_movement_ids,created_by,created_at,reverses_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, kind, kw.get("material_id"), kw.get("qty_milli", 0), kw.get("minutes", 0), amount_paisa,
         int(kw.get("is_estimate", False)), kw.get("basis"), kw.get("description"), kw.get("machine_id"),
         kw.get("movement_ids"), actor["id"], utc_now(), kw.get("reverses_id")))
    return cur.lastrowid


def consume_material(conn, actor, job_id, material_id, qty, *, ownership=None, wastage=False, reason=None,
                     roll_id=None, idem_key=None, on_date=None, unit=None) -> dict:
    """Issue stock to a job and record the cost line in the SAME transaction. For paper, `unit='m'` means `qty` is meters."""
    require_permission(actor, "consume_stock")
    with transaction(conn):
        j = _job(conn, job_id, for_costs=True)
        mat = conn.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if not mat:
            raise JobError("Unknown material.")
        if ownership is None:
            ownership = "customer" if (mat["category"] == "fabric" and j["fabric_source"] == "customer") else "factory"
        if wastage:
            require_permission(actor, "adjust_stock")
        res = inv.issue_stock(
            conn, actor, material_id=material_id, qty=qty,
            movement_type="wastage" if wastage else "production", ownership=ownership,
            customer_id=j["customer_id"] if ownership == "customer" else None, roll_id=roll_id,
            ref_type="job", ref_id=j["job_no"], reason=reason, idem_key=idem_key, on_date=on_date, unit=unit)
        _add_cost(conn, actor, job_id, "wastage" if wastage else "material", res["cost_paisa"],
                  material_id=material_id, qty_milli=res["qty_milli"],
                  basis=f"{ownership}-owned stock" + ("" if ownership == "factory" else " (customer property, no cost)"),
                  movement_ids=",".join(map(str, res["movement_ids"])))
        if j["status"] == "approved":
            conn.execute("UPDATE jobs SET status='in_progress' WHERE id=?", (job_id,))
        return res


def consume_materials(conn, actor, job_id, lines, *, on_date=None, wastage=False) -> dict:
    """Use several materials on one entry in ONE transaction: if any line fails (say, not enough stock), nothing at all is taken.
    `lines` is a list of (material_id, qty), (material_id, qty, unit) or (material_id, qty, unit, ownership) - unit 'm' counts paper in meters;
    ownership 'customer' / 'factory' says whose fabric is taken (default: the entry's fabric source). Returns {'results': [...], 'cost_paisa': total}."""
    if not lines:
        raise JobError("Add at least one material.")
    with transaction(conn):
        results = []
        for n, line in enumerate(lines, 1):
            material_id, qty = line[0], line[1]
            unit = line[2] if len(line) > 2 else None
            ownership = line[3] if len(line) > 3 else None
            name = (conn.execute("SELECT name FROM materials WHERE id=?", (material_id,)).fetchone() or ["?"])[0]
            try:
                results.append(consume_material(conn, actor, job_id, material_id, qty, on_date=on_date, wastage=wastage, unit=unit, ownership=ownership))
            except PermissionError:
                raise
            except Exception as e:  # noqa: BLE001 - say which line failed; the whole batch rolls back
                raise JobError(f"Line {n} ({name}): {e}") from e
        return {"results": results, "cost_paisa": sum(r["cost_paisa"] for r in results)}


def job_invoice(conn, job_id):
    """The invoice raised for this entry, or None."""
    return conn.execute("SELECT * FROM invoices WHERE job_id=? ORDER BY id LIMIT 1", (job_id,)).fetchone()


def invoice_amount(pieces, rate=None, total=None) -> tuple[int, int | None]:
    """(total_paisa, rate_paisa) of an invoice: meters x rate per meter when a rate is given, else the lump total."""
    if rate not in (None, ""):
        if not pieces:
            raise JobError("This entry has no meters. Edit the entry to add the meters produced, or type the total instead.")
        rate_paisa = to_paisa(rate)
        if rate_paisa <= 0:
            raise JobError("The price per meter must be more than zero.")
        return pieces * rate_paisa, rate_paisa
    if total not in (None, ""):
        return to_paisa(total), None
    raise JobError("Enter a price per meter or a total.")


def invoice_preview(cost_paisa, total_paisa, advance_paisa=0) -> dict:
    """What an invoice of `total_paisa` means for a job that cost `cost_paisa`, given the customer's advance. margin_bps is None if total is 0."""
    profit = total_paisa - cost_paisa
    margin = None if total_paisa <= 0 else (1 if profit >= 0 else -1) * mul_div_round(abs(profit), 10000, total_paisa)
    applied = min(max(advance_paisa, 0), max(total_paisa, 0))
    return {"profit_paisa": profit, "margin_bps": margin, "advance_applied_paisa": applied, "due_paisa": total_paisa - applied}


def invoice_job(conn, actor, job_id, *, invoice_date, rate=None, total=None, apply_advance=True) -> dict:
    """Raise the invoice for a production entry. The invoice number IS the entry number. Price it either per piece (`rate`, rupees,
    times the entry's pieces) or as a lump `total`. Any advance the customer already paid is applied to it straight away.
    Returns {'invoice_id','invoice_no','total_paisa','advance_applied_paisa','due_paisa'}."""
    require_permission(actor, "manage_invoices")
    with transaction(conn):
        j = _job(conn, job_id)
        if j["status"] == "cancelled":
            raise JobError("This entry was deleted.")
        if job_invoice(conn, job_id):
            raise JobError("This entry already has an invoice.")
        pieces = j["quantity_pieces"]
        total_paisa, rate_paisa = invoice_amount(pieces, rate, total)
        if total_paisa <= 0:
            raise JobError("The invoice total must be more than zero.")
        inv_id = acct.issue_invoice(conn, actor, invoice_no=j["job_no"], customer_id=j["customer_id"], total=str(from_paisa(total_paisa)),
                                    invoice_date=invoice_date, job_id=job_id, pieces=pieces if rate_paisa else None, rate_paisa=rate_paisa)
        if j["status"] in ("quotation", "approved", "in_progress"):
            conn.execute("UPDATE jobs SET status='completed' WHERE id=?", (job_id,))      # billed = done
        applied = 0
        if apply_advance and has_permission(actor["role"], "record_payment"):
            held = acct.balance(conn, "2200", customer_id=j["customer_id"])
            if held > 0:
                applied = min(held, total_paisa)
                acct.apply_advance(conn, actor, customer_id=j["customer_id"], invoice_id=inv_id, amount=str(from_paisa(applied)), on=invoice_date)
        return {"invoice_id": inv_id, "invoice_no": j["job_no"], "total_paisa": total_paisa, "advance_applied_paisa": applied, "due_paisa": total_paisa - applied}


def record_machine_run(conn, actor, job_id, machine_id, started_at: str, ended_at: str, *,
                       downtime_minutes=0, meters=None, kwh_start=None, kwh_end=None,
                       bill_total=None, bill_units_kwh=None) -> dict:
    """Records the run. Electricity cost: meter-based if kWh readings AND bill given (actual),
    else the machine's hourly rate (ESTIMATE), else no cost line and 'unknown' is reported."""
    require_permission(actor, "manage_machines")
    t0, t1 = datetime.fromisoformat(started_at), datetime.fromisoformat(ended_at)
    if t1 < t0:
        raise JobError("End time is before start time.")
    gross = int((t1 - t0).total_seconds() // 60)
    if downtime_minutes < 0 or downtime_minutes > gross:
        raise JobError("Downtime must be between 0 and the run length.")
    minutes = gross - downtime_minutes
    with transaction(conn):
        _job(conn, job_id, for_costs=True)
        m = conn.execute("SELECT * FROM machines WHERE id=?", (machine_id,)).fetchone()
        if not m:
            raise JobError("Unknown machine.")
        cur = conn.execute(
            "INSERT INTO machine_runs(job_id,machine_id,started_at,ended_at,minutes,downtime_minutes,meters_milli,"
            "created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, machine_id, started_at, ended_at, minutes, downtime_minutes,
             to_milli(meters) if meters is not None else None, actor["id"], utc_now()))
        cost = None
        if kwh_start is not None and kwh_end is not None and bill_total is not None and bill_units_kwh is not None:
            cost = el.cost_from_meter(to_milli(kwh_start), to_milli(kwh_end), to_paisa(bill_total), to_milli(bill_units_kwh))
        elif m["hourly_rate_paisa"] is not None:
            cost = el.cost_from_hourly(minutes, m["hourly_rate_paisa"])
        line_id = None
        if cost is not None:
            line_id = _add_cost(conn, actor, job_id, "electricity", cost.paisa, minutes=minutes,
                                is_estimate=cost.is_estimate, basis=cost.basis, machine_id=machine_id,
                                description=f"{m['name']} run #{cur.lastrowid}")
        if cost is None:
            audit_service.log(conn, "job.electricity_unknown", user=actor, entity="job", entity_id=job_id)
        return {"run_id": cur.lastrowid, "minutes": minutes, "cost": cost, "cost_line_id": line_id}


def add_cost(conn, actor, job_id, kind, amount, *, minutes=0, description="", basis="manual entry") -> int:
    """Labor / machine maintenance / other direct cost entered by an authorised user.
    Negative amounts are allowed ONLY as corrections and require a description."""
    if kind not in ("labor", "machine", "other"):
        raise JobError("kind must be labor, machine or other.")
    require_permission(actor, "manage_jobs")
    p = to_paisa(amount)
    if p < 0 and not description:
        raise JobError("Corrections need a description.")
    with transaction(conn):
        _job(conn, job_id, for_costs=True)
        lid = _add_cost(conn, actor, job_id, kind, p, minutes=minutes, description=description, basis=basis)
        audit_service.log(conn, "job.cost_add", user=actor, entity="job", entity_id=job_id,
                          details={"kind": kind, "paisa": p})
        return lid


@dataclass
class Costing:
    by_kind: dict[str, int] = field(default_factory=dict)
    total_paisa: int = 0
    includes_estimates: bool = False
    warnings: list[str] = field(default_factory=list)


def costing(conn, job_id) -> Costing:
    c = Costing()
    for r in conn.execute("SELECT kind, SUM(amount_paisa) s, MAX(is_estimate) e FROM job_costs WHERE job_id=? GROUP BY kind", (job_id,)):
        c.by_kind[r["kind"]] = r["s"]
        c.includes_estimates |= bool(r["e"])
    c.total_paisa = sum(c.by_kind.values())
    unknown = conn.execute(
        "SELECT COUNT(*) FROM machine_runs r WHERE r.job_id=? AND NOT EXISTS "
        "(SELECT 1 FROM job_costs c WHERE c.job_id=r.job_id AND c.machine_id=r.machine_id AND c.kind='electricity' "
        "AND c.description LIKE '%run #' || r.id)", (job_id,)).fetchone()[0]
    if unknown:
        c.warnings.append(f"{unknown} machine run(s) have no electricity cost (no rate/meter configured) - total is understated.")
    if c.includes_estimates:
        c.warnings.append("Total includes ESTIMATED electricity (hourly rate).")
    return c


def estimate_vs_actual(conn, job_id) -> dict:
    est = {r["kind"]: r["s"] for r in conn.execute(
        "SELECT kind, SUM(amount_paisa) s FROM job_estimates WHERE job_id=? GROUP BY kind", (job_id,))}
    act = costing(conn, job_id).by_kind
    kinds = sorted(set(est) | set(act))
    return {k: {"estimated": est.get(k, 0), "actual": act.get(k, 0), "variance": act.get(k, 0) - est.get(k, 0)} for k in kinds}


def finalize_job(conn, actor, job_id, final_price) -> dict:
    require_permission(actor, "finalize_job")
    price = to_paisa(final_price)
    if price < 0:
        raise JobError("Price cannot be negative.")
    with transaction(conn):
        j = _job(conn, job_id)
        if j["status"] != "completed":
            raise JobError("Only completed jobs can be finalised.")
        c = costing(conn, job_id)
        conn.execute("UPDATE jobs SET status='finalized', final_price_paisa=?, final_cost_paisa=?, finalized_by=?, "
                     "finalized_at=? WHERE id=?", (price, c.total_paisa, actor["id"], utc_now(), job_id))
        audit_service.log(conn, "job.finalize", user=actor, entity="job", entity_id=job_id,
                          details={"price": price, "cost": c.total_paisa, "warnings": c.warnings})
        return {"price_paisa": price, "cost_paisa": c.total_paisa, "profit_paisa": price - c.total_paisa,
                "warnings": c.warnings}


def set_quote(conn, actor, job_id, quote) -> None:
    """Store a PriceQuote (from costing_service) as the job's quoted price. Not allowed once finalised."""
    require_permission(actor, "manage_jobs")
    with transaction(conn):
        j = _job(conn, job_id)
        if j["status"] in ("finalized", "cancelled"):
            raise JobError("Job is closed.")
        conn.execute("UPDATE jobs SET pricing_method=?, quoted_price_paisa=? WHERE id=?",
                     (quote.method, quote.price_paisa, job_id))
        audit_service.log(conn, "job.quote", user=actor, entity="job", entity_id=job_id,
                          details={"method": quote.method, "price": quote.price_paisa, "basis": quote.explanation})


def cost_per_piece(total_paisa: int, pieces) -> int | None:
    """Total cost / pieces in whole paisa, rounded half away from zero. None when pieces are unknown."""
    if not pieces or pieces <= 0:
        return None
    q = mul_div_round(abs(total_paisa), 1, int(pieces))
    return -q if total_paisa < 0 else q


def job_summary(conn, job_id) -> dict:
    """Everything the Production 'Calculation' view shows, computed from the cost lines and stock movements."""
    j = conn.execute("SELECT j.*, c.name AS customer FROM jobs j JOIN customers c ON c.id=j.customer_id WHERE j.id=?", (job_id,)).fetchone()
    if not j:
        raise JobError("Job not found.")
    lines = []
    for r in conn.execute(
            "SELECT jc.*, m.name AS material, m.base_unit AS unit FROM job_costs jc LEFT JOIN materials m ON m.id=jc.material_id "
            "WHERE jc.job_id=? ORDER BY jc.id", (job_id,)):
        day = r["created_at"][:10]
        first = (r["stock_movement_ids"] or "").split(",")[0]
        if first.isdigit():
            mv = conn.execute("SELECT COALESCE(moved_on, substr(created_at,1,10)) FROM stock_movements WHERE id=?", (int(first),)).fetchone()
            day = mv[0] if mv else day
        qty = r["qty_milli"]
        customer_owned = (r["basis"] or "").startswith("customer-owned")
        lines.append({
            "id": r["id"], "reverses_id": r["reverses_id"], "date": day, "kind": r["kind"], "item": r["material"] or r["description"] or r["kind"], "unit": r["unit"] or "",
            "qty_milli": qty, "cost_paisa": r["amount_paisa"], "customer_owned": customer_owned,
            "rate_paisa_per_unit": mul_div_round(abs(r["amount_paisa"]), 1000, abs(qty)) if qty and r["amount_paisa"] and not customer_owned else None,
            "estimate": bool(r["is_estimate"])})
    reversed_ids = {l["reverses_id"] for l in lines if l["reverses_id"]}
    for l in lines:
        l["returned"] = l["id"] in reversed_ids
    c = costing(conn, job_id)
    stock = sum(l["cost_paisa"] for l in lines if l["kind"] in ("material", "wastage"))
    return {"job": dict(j), "lines": lines, "by_kind": c.by_kind, "stock_cost_paisa": stock, "other_cost_paisa": c.total_paisa - stock,
            "total_paisa": c.total_paisa, "pieces": j["quantity_pieces"],
            "cost_per_piece_paisa": cost_per_piece(c.total_paisa, j["quantity_pieces"]), "warnings": c.warnings}


# ------------------------------------------------------------------ edit / give stock back / delete
def update_job(conn, actor, job_id, *, customer_id, product_type, order_date, quantity_pieces=None, specs="", fabric_source=None):
    """Edit an entry's details. The entry number never changes (stock movements refer to it).
    Customer and fabric source are locked once stock has been used or an invoice exists, because they decide whose fabric was used."""
    require_permission(actor, "manage_jobs")
    with transaction(conn):
        j = _job(conn, job_id)
        if j["status"] in ("finalized", "cancelled"):
            raise JobError(f"This entry is {j['status']} and cannot be edited.")
        used = conn.execute("SELECT COUNT(*) FROM job_costs WHERE job_id=?", (job_id,)).fetchone()[0]
        invoiced = conn.execute("SELECT COUNT(*) FROM invoices WHERE job_id=?", (job_id,)).fetchone()[0]
        fabric_source = fabric_source or j["fabric_source"]
        if fabric_source not in ("factory", "customer"):
            raise JobError("fabric_source must be 'factory' or 'customer'.")
        if (used or invoiced) and (customer_id != j["customer_id"] or fabric_source != j["fabric_source"]):
            raise JobError("Customer and fabric source cannot change after stock was used or an invoice was made. Return the stock first.")
        if quantity_pieces is not None and quantity_pieces <= 0:
            raise JobError("Meters must be more than zero.")
        if not (product_type or "").strip():
            raise JobError("Product is required.")
        try:
            conn.execute("UPDATE jobs SET customer_id=?, product_type=?, order_date=?, quantity_pieces=?, specs=?, fabric_source=? WHERE id=?",
                         (customer_id, product_type.strip(), order_date, quantity_pieces, specs, fabric_source, job_id))
        except sqlite3.IntegrityError as e:
            raise JobError(f"Cannot save: {e}") from e
        audit_service.log(conn, "job.update", user=actor, entity="job", entity_id=job_id, details={
            "before": {k: j[k] for k in ("customer_id", "product_type", "order_date", "quantity_pieces", "fabric_source")},
            "after": {"customer_id": customer_id, "product_type": product_type, "order_date": order_date,
                      "quantity_pieces": quantity_pieces, "fabric_source": fabric_source}})


def return_material(conn, actor, job_id, cost_line_id, reason, *, on_date=None) -> dict:
    """Give the stock of one cost line back to inventory (a mistake, or leftover paper). Nothing is edited: the same quantity
    returns to the same lots at the SAME cost it left at, the books get the mirror entry, and a negative cost line records it."""
    require_permission(actor, "consume_stock")
    if not (reason or "").strip():
        raise JobError("A reason is required.")
    on_date = on_date or date.today().isoformat()
    with transaction(conn):
        j = _job(conn, job_id, for_costs=True)
        line = conn.execute("SELECT * FROM job_costs WHERE id=? AND job_id=?", (cost_line_id, job_id)).fetchone()
        if not line or line["kind"] not in ("material", "wastage") or not line["stock_movement_ids"] or line["reverses_id"]:
            raise JobError("That line is not stock that can be given back.")
        if conn.execute("SELECT 1 FROM job_costs WHERE reverses_id=?", (cost_line_id,)).fetchone():
            raise JobError("This stock was already given back.")
        ids = [int(x) for x in line["stock_movement_ids"].split(",")]
        new_ids, total_cost = [], 0
        for mid in ids:
            mv = conn.execute("SELECT * FROM stock_movements WHERE id=?", (mid,)).fetchone()
            take, value = -mv["qty_milli"], -mv["value_paisa"]
            conn.execute("UPDATE stock_lots SET qty_remaining_milli=qty_remaining_milli+? WHERE id=?", (take, mv["lot_id"]))
            if mv["roll_id"]:
                conn.execute("UPDATE stock_rolls SET remaining_milli=remaining_milli+? WHERE id=?", (take, mv["roll_id"]))
            if mv["ownership"] == "factory":
                conn.execute("UPDATE material_stock SET qty_milli=qty_milli+?, value_paisa=value_paisa+? WHERE material_id=?",
                             (take, value, mv["material_id"]))
            cur = conn.execute(
                "INSERT INTO stock_movements(material_id,ownership,customer_id,lot_id,roll_id,movement_type,qty_milli,value_paisa,"
                "ref_type,ref_id,reason,created_by,created_at,moved_on) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (mv["material_id"], mv["ownership"], mv["customer_id"], mv["lot_id"], mv["roll_id"], "adjustment_in", take, value,
                 "job_return", j["job_no"], reason.strip(), actor["id"], utc_now(), on_date))
            new_ids.append(cur.lastrowid)
            total_cost += value
        if total_cost > 0:
            acct.post(conn, entry_date=on_date, description=f"Stock returned from {j['job_no']}", user=actor, source_type="stock",
                      source_id=new_ids[0], lines=[{"account": "1200", "debit": total_cost},
                                                   {"account": "5300" if line["kind"] == "wastage" else "5000", "credit": total_cost}])
        _add_cost(conn, actor, job_id, line["kind"], -line["amount_paisa"], material_id=line["material_id"], qty_milli=-line["qty_milli"],
                  basis=f"returned to stock (line #{cost_line_id})", description=reason.strip(), movement_ids=",".join(map(str, new_ids)),
                  reverses_id=cost_line_id)
        audit_service.log(conn, "job.return_stock", user=actor, entity="job", entity_id=job_id, reason=reason,
                          details={"cost_line": cost_line_id, "cost_paisa": total_cost})
        return {"cost_paisa": total_cost, "qty_milli": line["qty_milli"]}


def delete_job(conn, actor, job_id) -> str:
    """Remove an entry. Returns 'deleted' (nothing was ever recorded against it) or 'cancelled' (it has history, which is kept).
    Refused while stock is still out on the entry, or when an invoice points at it."""
    require_permission(actor, "manage_jobs")
    with transaction(conn):
        j = _job(conn, job_id)
        if j["status"] == "finalized":
            raise JobError("A finalised entry is locked and cannot be deleted.")
        if conn.execute("SELECT 1 FROM invoices WHERE job_id=?", (job_id,)).fetchone():
            raise JobError("An invoice was made for this entry, so it cannot be deleted.")
        open_stock = conn.execute(
            "SELECT COUNT(*) FROM job_costs c WHERE c.job_id=? AND c.kind IN ('material','wastage') AND c.reverses_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM job_costs r WHERE r.reverses_id=c.id)", (job_id,)).fetchone()[0]
        if open_stock:
            raise JobError("Stock is still used on this entry. Give it back to inventory first (Return stock).")
        has_history = any(conn.execute(f"SELECT 1 FROM {t} WHERE job_id=?", (job_id,)).fetchone()
                          for t in ("job_costs", "job_estimates", "machine_runs"))
        if has_history:
            if j["status"] == "cancelled":
                raise JobError("This entry is already deleted.")
            conn.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (job_id,))
            result = "cancelled"
        else:
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            result = "deleted"
        audit_service.log(conn, "job.delete", user=actor, entity="job", entity_id=job_id, details={"job_no": j["job_no"], "result": result})
        return result
