"""Inventory ledger: lots, physical rolls, weighted-average valuation.

Rules
- Factory-owned stock is valued at weighted-average cost (material_stock).
- Customer-owned (customer property) stock is tracked in lots/movements but has
  zero value and never enters material_stock or valuation.
- Issues (production, wastage, supplier return, negative adjustment) leave stock
  at the current average cost; the final issue of all remaining qty takes the
  entire remaining value, so no rounding residue is left behind.
- Negative stock is always rejected. Movements are append-only.
- Every public mutator runs in ONE transaction (with the audit entry) and honours
  an optional idempotency key so double-clicks cannot post twice.
"""
from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from typing import Sequence

from database.connection import claim_idempotency_key, transaction, utc_now
from services import accounting_service as acct, audit_service
from services.auth_service import require_permission
from services.units import from_milli, mul_div_round, to_milli, to_paisa


class InventoryError(Exception):
    pass


class InsufficientStock(InventoryError):
    pass


class DuplicateSubmission(InventoryError):
    pass


PAPER_UNIT = "roll"
# The unit follows the material, so nobody has to choose (or get wrong) a unit: cloth in meters, ink in litres, paper in rolls.
CATEGORY_UNITS = {"fabric": "m", "ink": "L", "paper": PAPER_UNIT}
DEFAULT_ROLL_METERS = "300"        # a paper roll is usually 300 m; the factory can change it per material
ISSUE_TYPES = {"production", "wastage", "supplier_return", "adjustment_out"}
REASON_REQUIRED = {"wastage", "supplier_return", "adjustment_out", "adjustment_in"}


# ---------------------------------------------------------------- master data
def _roll_length(category, roll_length) -> int | None:
    """Milli-meters in one paper roll (None for other materials). Must be more than zero."""
    if category != "paper":
        return None
    try:
        ml = to_milli(DEFAULT_ROLL_METERS if roll_length in (None, "") else roll_length)
    except Exception as e:  # noqa: BLE001 - say it plainly
        raise InventoryError("Meters in one roll must be a number, for example 300.") from e
    if ml <= 0:
        raise InventoryError("Meters in one roll must be more than zero.")
    return ml


def add_material(conn, actor, name, category, base_unit, *, tracks_rolls=False, reorder_level="0", roll_length=None) -> int:
    require_permission(actor, "receive_stock")
    if category == "paper" and tracks_rolls:
        raise InventoryError("Paper is counted in rolls; per-roll length tracking is not used.")
    base_unit = CATEGORY_UNITS.get(category, base_unit)            # fabric -> m, ink -> ml, paper -> roll; only "other" materials choose
    if tracks_rolls and base_unit != "m":
        raise InventoryError("Roll-tracked materials must use meters ('m') as base unit.")
    if not (base_unit or "").strip():
        raise InventoryError("A unit is required (for example m, ml, g, pcs).")
    roll_ml = _roll_length(category, roll_length)
    with transaction(conn):
        try:
            cur = conn.execute(
                "INSERT INTO materials(name,category,base_unit,tracks_rolls,reorder_level_milli,roll_length_milli,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (name.strip(), category, base_unit.strip(), int(tracks_rolls), to_milli(reorder_level), roll_ml, utc_now()))
        except sqlite3.IntegrityError as e:
            raise InventoryError(f"Cannot add material: {e}") from e
        conn.execute("INSERT INTO material_stock(material_id) VALUES (?)", (cur.lastrowid,))
        audit_service.log(conn, "material.create", user=actor, entity="material", entity_id=cur.lastrowid,
                          details={"name": name, "category": category})
        return cur.lastrowid


def add_supplier(conn, actor, name, phone="", address="") -> int:
    require_permission(actor, "manage_suppliers")
    with transaction(conn):
        try:
            cur = conn.execute("INSERT INTO suppliers(name,phone,address,created_at) VALUES (?,?,?,?)",
                               (name.strip(), phone, address, utc_now()))
        except sqlite3.IntegrityError as e:
            raise InventoryError("Supplier already exists.") from e
        audit_service.log(conn, "supplier.create", user=actor, entity="supplier", entity_id=cur.lastrowid)
        return cur.lastrowid


def add_customer(conn, actor, name, phone="", address="") -> int:
    require_permission(actor, "manage_jobs")
    name = (name or "").strip()
    if not name:
        raise InventoryError("Customer name is required.")
    with transaction(conn):
        old = conn.execute("SELECT id, is_active FROM customers WHERE name=?", (name,)).fetchone()
        if old and not old["is_active"]:                                    # a customer who was removed earlier comes back with his history
            conn.execute("UPDATE customers SET is_active=1, phone=?, address=? WHERE id=?", (phone, address, old["id"]))
            audit_service.log(conn, "customer.restore", user=actor, entity="customer", entity_id=old["id"])
            return old["id"]
        try:
            cur = conn.execute("INSERT INTO customers(name,phone,address,created_at) VALUES (?,?,?,?)",
                               (name, phone, address, utc_now()))
        except sqlite3.IntegrityError as e:
            raise InventoryError("Customer already exists.") from e
        audit_service.log(conn, "customer.create", user=actor, entity="customer", entity_id=cur.lastrowid)
        return cur.lastrowid


def update_customer(conn, actor, customer_id, *, name, phone="", address="") -> None:
    require_permission(actor, "manage_jobs")
    name = (name or "").strip()
    if not name:
        raise InventoryError("Customer name is required.")
    with transaction(conn):
        c = conn.execute("SELECT * FROM customers WHERE id=? AND is_active=1", (customer_id,)).fetchone()
        if not c:
            raise InventoryError("Customer not found.")
        try:
            conn.execute("UPDATE customers SET name=?, phone=?, address=? WHERE id=?", (name, phone, address, customer_id))
        except sqlite3.IntegrityError as e:
            raise InventoryError("Another customer already has this name.") from e
        audit_service.log(conn, "customer.update", user=actor, entity="customer", entity_id=customer_id,
                          details={"before": {k: c[k] for k in ("name", "phone", "address")}, "after": {"name": name, "phone": phone, "address": address}})


def delete_customer(conn, actor, customer_id) -> str:
    """Returns 'deleted' (never used) or 'archived' (has history: hidden, history kept). Refused while money is owed either way
    (an unpaid invoice or an advance we hold) or while the customer's own fabric is still in the factory."""
    require_permission(actor, "manage_jobs")
    with transaction(conn):
        c = conn.execute("SELECT * FROM customers WHERE id=? AND is_active=1", (customer_id,)).fetchone()
        if not c:
            raise InventoryError("Customer not found.")
        if acct.balance(conn, "1100", customer_id=customer_id) != 0:
            raise InventoryError("This customer still owes money (unpaid invoices). Collect or settle it first.")
        if acct.balance(conn, "2200", customer_id=customer_id) != 0:
            raise InventoryError("The factory still holds an advance for this customer. Use it against an invoice first.")
        if conn.execute("SELECT 1 FROM stock_lots WHERE customer_id=? AND ownership='customer' AND qty_remaining_milli>0", (customer_id,)).fetchone():
            raise InventoryError("This customer's own fabric is still in the factory.")
        if conn.execute("SELECT 1 FROM jobs WHERE customer_id=? AND status NOT IN ('cancelled','finalized','completed')", (customer_id,)).fetchone():
            raise InventoryError("This customer still has an open production entry. Delete or finish it first.")
        used = any(conn.execute(f"SELECT 1 FROM {t} WHERE customer_id=? LIMIT 1", (customer_id,)).fetchone()
                   for t in ("jobs", "invoices", "stock_lots", "stock_movements", "journal_lines"))
        if used:
            conn.execute("UPDATE customers SET is_active=0 WHERE id=?", (customer_id,))
            res = "archived"
        else:
            conn.execute("DELETE FROM customers WHERE id=?", (customer_id,))
            res = "deleted"
        audit_service.log(conn, "customer.delete" if res == "deleted" else "customer.archive", user=actor, entity="customer", entity_id=customer_id,
                          details={"name": c["name"]})
        return res


def material_has_history(conn, material_id) -> bool:
    return any(conn.execute(f"SELECT 1 FROM {t} WHERE material_id=?", (material_id,)).fetchone()
               for t in ("stock_lots", "stock_movements", "job_costs", "job_estimates"))


def update_material(conn, actor, material_id, *, name, category, base_unit, reorder_level="0", roll_length=None) -> None:
    """Edit a material. Name and reorder level can always change; category and unit only while the material has no history
    (changing a unit later would silently change the meaning of every past quantity)."""
    require_permission(actor, "receive_stock")
    with transaction(conn):
        m = conn.execute("SELECT * FROM materials WHERE id=? AND is_active=1", (material_id,)).fetchone()
        if not m:
            raise InventoryError("Unknown or archived material.")
        base_unit = m["base_unit"] if (category == m["category"] and material_has_history(conn, material_id)) else CATEGORY_UNITS.get(category, base_unit)
        if not (name or "").strip() or not (base_unit or "").strip():
            raise InventoryError("Name and unit are required.")
        if (category != m["category"] or base_unit.strip() != m["base_unit"]) and material_has_history(conn, material_id):
            raise InventoryError("Category and unit cannot change once the material has stock history. Add a new material instead.")
        roll_ml = _roll_length(category, roll_length if roll_length not in (None, "") else
                               (from_milli(m["roll_length_milli"]) if m["roll_length_milli"] else None))
        try:
            conn.execute("UPDATE materials SET name=?, category=?, base_unit=?, reorder_level_milli=?, roll_length_milli=? WHERE id=?",
                         (name.strip(), category, base_unit.strip(), to_milli(reorder_level), roll_ml, material_id))
        except sqlite3.IntegrityError as e:
            raise InventoryError(f"Cannot save: {e}") from e
        audit_service.log(conn, "material.update", user=actor, entity="material", entity_id=material_id,
                          details={"before": {k: m[k] for k in ("name", "category", "base_unit", "reorder_level_milli")},
                                   "after": {"name": name.strip(), "category": category, "base_unit": base_unit.strip(),
                                             "reorder_level_milli": to_milli(reorder_level)},
                                   "roll_length_milli": roll_ml})


def delete_material(conn, actor, material_id) -> str:
    """Returns 'deleted' (never used, removed completely) or 'archived' (has history: hidden from lists, history kept).
    Refused while any stock - the factory's or a customer's - is still on hand."""
    require_permission(actor, "receive_stock")
    with transaction(conn):
        m = conn.execute("SELECT * FROM materials WHERE id=? AND is_active=1", (material_id,)).fetchone()
        if not m:
            raise InventoryError("Unknown or already archived material.")
        ms = conn.execute("SELECT qty_milli FROM material_stock WHERE material_id=?", (material_id,)).fetchone()
        cust = conn.execute("SELECT COALESCE(SUM(qty_remaining_milli),0) FROM stock_lots WHERE material_id=? AND ownership='customer'",
                            (material_id,)).fetchone()[0]
        if (ms and ms["qty_milli"] > 0) or cust > 0:
            raise InventoryError("Stock is still on hand. Use it up, or write it off with Wastage / adjustment, before deleting the material.")
        if material_has_history(conn, material_id):
            conn.execute("UPDATE materials SET is_active=0 WHERE id=?", (material_id,))
            result = "archived"
        else:
            conn.execute("DELETE FROM material_stock WHERE material_id=?", (material_id,))
            conn.execute("DELETE FROM materials WHERE id=?", (material_id,))
            result = "deleted"
        audit_service.log(conn, "material.delete", user=actor, entity="material", entity_id=material_id,
                          details={"name": m["name"], "result": result})
        return result


# ---------------------------------------------------------------- rolls <-> meters (paper)
def roll_meters(rolls_milli: int, roll_length_milli: int) -> int:
    """Milli-meters in `rolls_milli` milli-rolls (round-half-up, integers only)."""
    return mul_div_round(rolls_milli, roll_length_milli, 1000)


def meters_to_rolls_milli(meters, roll_length_milli: int) -> int:
    """Milli-rolls that `meters` of paper is worth (round-half-up). 150 m of a 300 m roll = 500 milli-rolls = half a roll."""
    m = to_milli(meters)
    if m < 0:
        raise InventoryError("Quantity cannot be negative.")
    return mul_div_round(m, 1000, roll_length_milli)


def to_base_qty(conn, material_id: int, qty, unit: str | None = None) -> str:
    """Turn a quantity typed in `unit` into the material's own unit. Only paper has a choice: 'roll' (its own unit) or 'm' (meters,
    worked out with the material's roll length). Everything else must be typed in its own unit."""
    if unit in (None, ""):
        return qty
    mat = conn.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
    if not mat:
        raise InventoryError("Unknown material.")
    if unit == mat["base_unit"]:
        return qty
    if mat["category"] == "paper" and mat["roll_length_milli"] and unit == "m":
        rolls = meters_to_rolls_milli(qty, mat["roll_length_milli"])
        if rolls <= 0:
            raise InventoryError("That is less than 0.001 of a roll - type a larger length.")
        return str(from_milli(rolls))
    raise InventoryError(f"{mat['name']} cannot be counted in {unit}.")


# ---------------------------------------------------------------- receiving
def cost_of(qty, unit_price) -> int:
    """Total cost in paisa of `qty` units at `unit_price` rupees per unit: round-half-up(qty_milli x price_paisa / 1000), integers only.
    The Receive-stock form uses this same function for its live total, so what is shown is what is booked."""
    q, p = to_milli(qty), to_paisa(unit_price)
    if q < 0 or p < 0:
        raise InventoryError("Quantity and price cannot be negative.")
    return mul_div_round(q, p, 1000)


def receive_stock(conn: sqlite3.Connection, actor, *, material_id: int, received_on: str | None = None,
                  qty=None, rolls: Sequence | None = None, total_cost="0", supplier_id: int | None = None,
                  ownership: str = "factory", customer_id: int | None = None,
                  movement_type: str | None = None, reference="", invoice_no="", location="",
                  payment_status: str | None = None, reason: str | None = None,
                  idem_key: str | None = None, unit_price=None, roll_length=None) -> int:
    """Receive stock as one lot. For roll materials pass `rolls=[length,...]` (mixed lengths OK);
    otherwise pass `qty`. Give either `total_cost` or `unit_price` (rupees per unit - the total is then worked out exactly). Returns the lot id."""
    require_permission(actor, "receive_stock")
    if ownership not in ("factory", "customer"):
        raise InventoryError("ownership must be 'factory' or 'customer'")
    mtype = movement_type or "purchase"
    if mtype not in ("purchase", "opening", "customer_return", "adjustment_in"):
        raise InventoryError(f"Invalid receipt type: {mtype}")
    if mtype in REASON_REQUIRED and not reason:
        raise InventoryError("A reason is required for this receipt type.")
    received_on = received_on or date.today().isoformat()
    cost = to_paisa(total_cost)
    if cost < 0:
        raise InventoryError("Cost cannot be negative.")
    if ownership == "customer":
        if not customer_id:
            raise InventoryError("Customer-owned stock requires a customer.")
        if cost:
            raise InventoryError("Customer-owned stock has no factory cost.")
    with transaction(conn):
        if idem_key and not claim_idempotency_key(conn, idem_key, "receive_stock"):
            raise DuplicateSubmission("This receipt was already recorded.")
        mat = conn.execute("SELECT * FROM materials WHERE id=? AND is_active=1", (material_id,)).fetchone()
        if not mat:
            raise InventoryError("Unknown or inactive material.")
        if roll_length not in (None, "") and mat["category"] == "paper":
            new_len = _roll_length("paper", roll_length)
            if new_len != mat["roll_length_milli"]:
                conn.execute("UPDATE materials SET roll_length_milli=? WHERE id=?", (new_len, material_id))
                audit_service.log(conn, "material.roll_length", user=actor, entity="material", entity_id=material_id,
                                  details={"before": mat["roll_length_milli"], "after": new_len})
        if mat["tracks_rolls"]:
            if not rolls:
                raise InventoryError("This material is received in rolls: provide roll lengths.")
            lengths = [to_milli(r) for r in rolls]
            if any(l <= 0 for l in lengths):
                raise InventoryError("Roll lengths must be positive.")
            total = sum(lengths)
            if qty is not None and to_milli(qty) != total:
                raise InventoryError("qty does not match the sum of roll lengths.")
        else:
            if rolls:
                raise InventoryError("This material is not tracked in rolls.")
            lengths = []
            total = to_milli(qty) if qty is not None else 0
            if total <= 0:
                raise InventoryError("Quantity must be positive.")
        if unit_price is not None:
            if to_paisa(total_cost) != 0:
                raise InventoryError("Give either a total cost or a price per unit, not both.")
            if to_paisa(unit_price) < 0:
                raise InventoryError("Price cannot be negative.")
            cost = mul_div_round(total, to_paisa(unit_price), 1000)
            if ownership == "customer" and cost:
                raise InventoryError("Customer-owned stock has no factory cost.")
        cur = conn.execute(
            "INSERT INTO stock_lots(material_id,ownership,customer_id,supplier_id,received_on,"
            "qty_received_milli,qty_remaining_milli,total_cost_paisa,reference,invoice_no,location,"
            "payment_status,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (material_id, ownership, customer_id, supplier_id, received_on, total, total, cost,
             reference, invoice_no, location, payment_status, actor["id"], utc_now()))
        lot_id = cur.lastrowid
        for l in lengths:
            conn.execute("INSERT INTO stock_rolls(lot_id,length_milli,remaining_milli) VALUES (?,?,?)",
                         (lot_id, l, l))
        conn.execute(
            "INSERT INTO stock_movements(material_id,ownership,customer_id,lot_id,movement_type,qty_milli,"
            "value_paisa,ref_type,ref_id,reason,created_by,created_at,moved_on) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (material_id, ownership, customer_id, lot_id, mtype, total, cost, "receipt", reference or None,
             reason, actor["id"], utc_now(), received_on))
        if ownership == "factory":
            conn.execute("UPDATE material_stock SET qty_milli=qty_milli+?, value_paisa=value_paisa+? "
                         "WHERE material_id=?", (total, cost, material_id))
        if ownership == "factory" and cost > 0:
            if mtype == "purchase":
                if payment_status == "paid":
                    credit = {"account": "1000", "credit": cost}
                else:
                    if not supplier_id:
                        raise InventoryError("A supplier is required for purchases that are not fully paid.")
                    credit = {"account": "2000", "credit": cost, "supplier_id": supplier_id}
            elif mtype == "opening":
                credit = {"account": "3500", "credit": cost}
            else:
                credit = {"account": "5900", "credit": cost}
            acct.post(conn, entry_date=received_on, description=f"Stock receipt lot {lot_id}", user=actor,
                      source_type="stock", source_id=lot_id,
                      lines=[{"account": "1200", "debit": cost}, credit])
        audit_service.log(conn, "stock.receive", user=actor, entity="stock_lot", entity_id=lot_id,
                          reason=reason, details={"material_id": material_id, "qty_milli": total,
                                                  "cost_paisa": cost, "ownership": ownership})
        return lot_id


# ---------------------------------------------------------------- issuing
def issue_stock(conn: sqlite3.Connection, actor, *, material_id: int, qty, movement_type: str = "production",
                ownership: str = "factory", customer_id: int | None = None, roll_id: int | None = None,
                ref_type: str | None = None, ref_id: str | None = None, reason: str | None = None,
                idem_key: str | None = None, on_date: str | None = None, supplier_id: int | None = None,
                unit: str | None = None) -> dict:
    """Consume stock. Returns {'qty_milli','cost_paisa','movement_ids'}.
    cost_paisa is the weighted-average cost of factory-owned stock (0 for customer property).
    `unit` ('m' for paper) says the quantity is in meters, not rolls; it is turned into rolls with the material's roll length."""
    if movement_type not in ISSUE_TYPES:
        raise InventoryError(f"Invalid issue type: {movement_type}")
    require_permission(actor, "adjust_stock" if movement_type != "production" else "consume_stock")
    if movement_type in REASON_REQUIRED and not reason:
        raise InventoryError("A reason is required for this movement type.")
    q = to_milli(to_base_qty(conn, material_id, qty, unit))
    if q <= 0:
        raise InventoryError("Quantity must be positive.")
    if ownership == "customer" and not customer_id:
        raise InventoryError("Customer-owned issue requires customer_id.")
    with transaction(conn):
        if idem_key and not claim_idempotency_key(conn, idem_key, "issue_stock"):
            raise DuplicateSubmission("This issue was already recorded.")
        mat = conn.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
        if not mat:
            raise InventoryError("Unknown material.")
        lot_filter = "material_id=? AND ownership=? AND qty_remaining_milli>0"
        args: list = [material_id, ownership]
        if ownership == "customer":
            lot_filter += " AND customer_id=?"
            args.append(customer_id)
        lots = conn.execute(f"SELECT * FROM stock_lots WHERE {lot_filter} ORDER BY received_on, id", args).fetchall()
        available = sum(l["qty_remaining_milli"] for l in lots)
        if roll_id is not None:
            r = conn.execute("SELECT r.* FROM stock_rolls r JOIN stock_lots l ON l.id=r.lot_id "
                             "WHERE r.id=? AND l.material_id=? AND l.ownership=?",
                             (roll_id, material_id, ownership)).fetchone()
            if not r:
                raise InventoryError("Roll not found for this material/ownership.")
            if r["remaining_milli"] < q:
                raise InsufficientStock(f"Roll has only {from_milli(r['remaining_milli'])} left.")
        if available < q:
            raise InsufficientStock(f"Insufficient stock: need {from_milli(q)}, have {from_milli(available)}.")

        cost = 0
        if ownership == "factory":
            ms = conn.execute("SELECT * FROM material_stock WHERE material_id=?", (material_id,)).fetchone()
            if ms["qty_milli"] < q:
                raise InsufficientStock("Insufficient stock.")
            cost = ms["value_paisa"] if q == ms["qty_milli"] else mul_div_round(ms["value_paisa"], q, ms["qty_milli"])
            conn.execute("UPDATE material_stock SET qty_milli=qty_milli-?, value_paisa=value_paisa-? "
                         "WHERE material_id=?", (q, cost, material_id))

        # Allocate physically (specific roll, else FIFO) and write one movement per lot touched.
        need = q
        per_lot: dict[int, int] = {}
        per_roll: list[tuple[int, int, int]] = []  # (lot, roll, take)
        if roll_id is not None:
            r = conn.execute("SELECT * FROM stock_rolls WHERE id=?", (roll_id,)).fetchone()
            per_roll.append((r["lot_id"], r["id"], q))
        elif mat["tracks_rolls"]:
            rq = ("SELECT r.* FROM stock_rolls r JOIN stock_lots l ON l.id=r.lot_id "
                  "WHERE l.material_id=? AND l.ownership=? AND r.remaining_milli>0")
            rargs: list = [material_id, ownership]
            if ownership == "customer":
                rq += " AND l.customer_id=?"
                rargs.append(customer_id)
            rq += " ORDER BY l.received_on, l.id, r.id"
            for r in conn.execute(rq, rargs):
                take = min(need, r["remaining_milli"])
                per_roll.append((r["lot_id"], r["id"], take))
                need -= take
                if need == 0:
                    break
        else:
            for l in lots:
                take = min(need, l["qty_remaining_milli"])
                per_lot[l["id"]] = take
                need -= take
                if need == 0:
                    break
        for lot_id, rid, take in per_roll:
            conn.execute("UPDATE stock_rolls SET remaining_milli=remaining_milli-? WHERE id=?", (take, rid))
            per_lot[lot_id] = per_lot.get(lot_id, 0)  # ensure present
        if per_roll:
            tot_by_lot: dict[int, int] = {}
            for lot_id, _, take in per_roll:
                tot_by_lot[lot_id] = tot_by_lot.get(lot_id, 0) + take
            per_lot = tot_by_lot
        for lot_id, take in per_lot.items():
            conn.execute("UPDATE stock_lots SET qty_remaining_milli=qty_remaining_milli-? WHERE id=?",
                         (take, lot_id))
        # movements (value split proportionally; last gets remainder)
        ids, remaining_cost, remaining_q = [], cost, q
        items = list(per_lot.items())
        roll_for_lot = {lot: rid for lot, rid, _ in per_roll} if len(per_roll) == len(per_lot) else {}
        for i, (lot_id, take) in enumerate(items):
            v = remaining_cost if i == len(items) - 1 else mul_div_round(remaining_cost, take, remaining_q)
            remaining_cost -= v
            remaining_q -= take
            cur = conn.execute(
                "INSERT INTO stock_movements(material_id,ownership,customer_id,lot_id,roll_id,movement_type,"
                "qty_milli,value_paisa,ref_type,ref_id,reason,created_by,created_at,moved_on) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (material_id, ownership, customer_id, lot_id, roll_for_lot.get(lot_id), movement_type,
                 -take, -v, ref_type, ref_id, reason, actor["id"], utc_now(), on_date or date.today().isoformat()))
            ids.append(cur.lastrowid)
        if ownership == "factory" and cost > 0:
            if movement_type == "supplier_return" and not supplier_id:
                raise InventoryError("A supplier is required for a supplier return.")
            debit = {"production": {"account": "5000"}, "wastage": {"account": "5300"},
                     "adjustment_out": {"account": "5900"},
                     "supplier_return": {"account": "2000", "supplier_id": supplier_id}}[movement_type]
            acct.post(conn, entry_date=on_date or date.today().isoformat(),
                      description=f"Stock {movement_type} material {material_id}", user=actor,
                      source_type="stock", source_id=ids[0] if ids else None,
                      lines=[dict(debit, debit=cost), {"account": "1200", "credit": cost}])
        audit_service.log(conn, f"stock.{movement_type}", user=actor, entity="material", entity_id=material_id,
                          reason=reason, details={"qty_milli": q, "cost_paisa": cost, "ref": f"{ref_type}:{ref_id}"})
        return {"qty_milli": q, "cost_paisa": cost, "movement_ids": ids}


def undo_receipt(conn, actor, lot_id, reason, *, on_date=None) -> int:
    """Cancel a receipt that was entered wrongly. Allowed only while none of it has been used and nothing has been issued
    from that material since (so the average cost is untouched). The stock leaves at the cost it came in at, and the books
    get the exact mirror of the original entry. Nothing is edited or deleted; the undo is its own movement."""
    require_permission(actor, "adjust_stock")
    if not (reason or "").strip():
        raise InventoryError("A reason is required.")
    on_date = on_date or date.today().isoformat()
    with transaction(conn):
        lot = conn.execute("SELECT * FROM stock_lots WHERE id=?", (lot_id,)).fetchone()
        if not lot:
            raise InventoryError("Receipt not found.")
        if conn.execute("SELECT 1 FROM stock_movements WHERE ref_type='undo_receipt' AND ref_id=?", (str(lot_id),)).fetchone():
            raise InventoryError("This receipt was already undone.")
        if lot["qty_remaining_milli"] != lot["qty_received_milli"]:
            raise InventoryError("Some of this stock has already been used. Use Wastage / adjustment to correct the quantity instead.")
        first_mv = conn.execute("SELECT MIN(id) FROM stock_movements WHERE lot_id=? AND movement_type IN "
                                "('purchase','opening','customer_return','adjustment_in')", (lot_id,)).fetchone()[0]
        later_issue = conn.execute("SELECT 1 FROM stock_movements WHERE material_id=? AND id>? AND qty_milli<0 AND ownership=?",
                                   (lot["material_id"], first_mv, lot["ownership"])).fetchone()
        if later_issue:
            raise InventoryError("Stock of this material was issued after this receipt, so it can no longer be undone safely. "
                                 "Use Wastage / adjustment to correct the quantity.")
        qty, cost = lot["qty_received_milli"], lot["total_cost_paisa"]
        if lot["ownership"] == "factory":
            ms = conn.execute("SELECT * FROM material_stock WHERE material_id=?", (lot["material_id"],)).fetchone()
            if ms["qty_milli"] < qty or ms["value_paisa"] < cost:
                raise InventoryError("The stock figures no longer allow this to be undone. Use Wastage / adjustment instead.")
            conn.execute("UPDATE material_stock SET qty_milli=qty_milli-?, value_paisa=value_paisa-? WHERE material_id=?",
                         (qty, cost, lot["material_id"]))
        conn.execute("UPDATE stock_lots SET qty_remaining_milli=0 WHERE id=?", (lot_id,))
        conn.execute("UPDATE stock_rolls SET remaining_milli=0 WHERE lot_id=?", (lot_id,))
        cur = conn.execute(
            "INSERT INTO stock_movements(material_id,ownership,customer_id,lot_id,movement_type,qty_milli,value_paisa,ref_type,ref_id,"
            "reason,created_by,created_at,moved_on) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lot["material_id"], lot["ownership"], lot["customer_id"], lot_id, "adjustment_out", -qty, -cost if lot["ownership"] == "factory" else 0,
             "undo_receipt", str(lot_id), reason.strip(), actor["id"], utc_now(), on_date))
        original = conn.execute("SELECT id FROM journal_entries WHERE source_type='stock' AND source_id=? AND reverses_entry_id IS NULL "
                                "AND description LIKE 'Stock receipt lot%'", (str(lot_id),)).fetchone()
        if original:
            lines = [{"account": r["code"], "debit": r["credit_paisa"], "credit": r["debit_paisa"], "customer_id": r["customer_id"],
                      "supplier_id": r["supplier_id"]} for r in conn.execute(
                "SELECT a.code, l.debit_paisa, l.credit_paisa, l.customer_id, l.supplier_id FROM journal_lines l "
                "JOIN accounts a ON a.id=l.account_id WHERE l.entry_id=? ORDER BY l.id", (original["id"],))]
            acct.post(conn, entry_date=on_date, description=f"Undo stock receipt lot {lot_id}", user=actor, source_type="stock",
                      source_id=lot_id, lines=lines, reverses_entry_id=original["id"])
        audit_service.log(conn, "stock.undo_receipt", user=actor, entity="stock_lot", entity_id=lot_id, reason=reason,
                          details={"qty_milli": qty, "cost_paisa": cost})
        return cur.lastrowid


# ---------------------------------------------------------------- reporting
def stock_summary(conn: sqlite3.Connection, material_id: int) -> dict:
    mat = conn.execute("SELECT * FROM materials WHERE id=?", (material_id,)).fetchone()
    ms = conn.execute("SELECT * FROM material_stock WHERE material_id=?", (material_id,)).fetchone()
    out = {
        "material": mat["name"], "unit": mat["base_unit"],
        "factory_qty": from_milli(ms["qty_milli"]), "factory_value_paisa": ms["value_paisa"],
        "avg_cost_paisa_per_unit": (Decimal(ms["value_paisa"]) * 1000 / ms["qty_milli"]) if ms["qty_milli"] else None,
        "low_stock": ms["qty_milli"] <= mat["reorder_level_milli"] and mat["reorder_level_milli"] > 0,
    }
    if mat["roll_length_milli"]:
        out["roll_length_m"] = from_milli(mat["roll_length_milli"])
        out["factory_meters"] = from_milli(roll_meters(ms["qty_milli"], mat["roll_length_milli"]))
    cust = conn.execute(
        "SELECT customer_id, SUM(qty_remaining_milli) q FROM stock_lots WHERE material_id=? AND ownership='customer' "
        "GROUP BY customer_id HAVING q>0", (material_id,)).fetchall()
    out["customer_owned"] = {r["customer_id"]: from_milli(r["q"]) for r in cust}
    if mat["tracks_rolls"]:
        rolls = conn.execute(
            "SELECT r.id, r.length_milli, r.remaining_milli, l.ownership FROM stock_rolls r JOIN stock_lots l ON l.id=r.lot_id "
            "WHERE l.material_id=? AND r.remaining_milli>0 ORDER BY r.id", (material_id,)).fetchall()
        fac = [r for r in rolls if r["ownership"] == "factory"]
        out["factory_rolls_total"] = len(fac)
        out["factory_rolls_full"] = sum(1 for r in fac if r["remaining_milli"] == r["length_milli"])
        out["factory_rolls_partial"] = [(r["id"], from_milli(r["remaining_milli"])) for r in fac
                                        if r["remaining_milli"] < r["length_milli"]]
    return out


def low_stock_materials(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id, m.name, s.qty_milli, m.reorder_level_milli FROM materials m JOIN material_stock s "
        "ON s.material_id=m.id WHERE m.is_active=1 AND m.reorder_level_milli>0 AND s.qty_milli<=m.reorder_level_milli"
    ).fetchall()
