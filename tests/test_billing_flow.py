"""Customer -> production entry -> several materials at once -> invoice (same number) -> advance applied -> payment.
Every number below is worked out by hand, not copied from the program's output."""
from decimal import Decimal as D

import pytest

from services import accounting_service as acct, auth_service as auth, inventory_service as inv, job_service as js, queries as q
from services.units import to_paisa as P


@pytest.fixture
def w(conn, owner):
    paper = inv.add_material(conn, owner, "Paper", "paper", "roll")
    cloth = inv.add_material(conn, owner, "Kapra", "fabric", "anything")        # the unit follows the category
    ink = inv.add_material(conn, owner, "Reactive ink", "ink", "kg")
    cust = inv.add_customer(conn, owner, "Ali Traders")
    inv.receive_stock(conn, owner, material_id=paper, qty="8", total_cost="40000", supplier_id=1)       # Rs 5,000 per roll
    inv.receive_stock(conn, owner, material_id=cloth, qty="100", unit_price="100", supplier_id=1)      # 100 m x Rs 100 = Rs 10,000
    inv.receive_stock(conn, owner, material_id=ink, qty="2", unit_price="450", supplier_id=1)      # 2 L x Rs 450 = Rs 900
    auth.create_user(conn, owner, "sup", "supervisor-pw", "auditor")
    auth.create_user(conn, owner, "acc", "accountant-pw", "accountant")
    return dict(paper=paper, cloth=cloth, ink=ink, cust=cust, sup=auth.authenticate(conn, "sup", "supervisor-pw"), acc=auth.authenticate(conn, "acc", "accountant-pw"))


def entry(conn, owner, w, no="P-0001", pieces=50, **kw):
    a = dict(job_no=no, customer_id=w["cust"], product_type="Bedsheet", fabric_source="factory", order_date="2026-08-01", quantity_pieces=pieces, status="in_progress")
    a.update(kw)
    return js.create_job(conn, owner, **a)


# ------------------------------------------------------------------ units follow the material
def test_unit_follows_the_category(conn, w):
    unit = lambda mid: conn.execute("SELECT base_unit FROM materials WHERE id=?", (mid,)).fetchone()[0]  # noqa: E731
    assert (unit(w["paper"]), unit(w["cloth"]), unit(w["ink"])) == ("roll", "m", "L")


# ------------------------------------------------------------------ price per unit -> total cost
def test_cost_of_is_exact_and_rounds_half_up():
    assert inv.cost_of("12.5", "38500") == P("481250")             # 12.5 rolls x 38,500
    assert inv.cost_of("250.75", "112.40") == P("28184.30")        # 250.75 m x 112.40 = 28,184.30
    assert inv.cost_of("1500.5", "0.45") == 67523                  # 675.225 -> exactly half a paisa -> rounds UP to 675.23
    assert inv.cost_of("1000", "0.45") == 45000
    assert inv.cost_of("0", "50") == 0


def test_receive_with_unit_price_books_the_computed_total(conn, owner, w):
    lot = inv.receive_stock(conn, owner, material_id=w["cloth"], qty="250.75", unit_price="112.40", supplier_id=1, payment_status="unpaid")
    assert conn.execute("SELECT total_cost_paisa FROM stock_lots WHERE id=?", (lot,)).fetchone()[0] == P("28184.30")
    s = inv.stock_summary(conn, w["cloth"])
    assert s["factory_qty"] == D("350.75") and s["factory_value_paisa"] == P("10000") + P("28184.30")
    assert acct.balance(conn, "1200") == P("40000") + P("10000") + P("900") + P("28184.30")
    assert acct.check_integrity(conn) == []


def test_unit_price_rules(conn, owner, w):
    with pytest.raises(inv.InventoryError):                                          # a total and a price together are ambiguous
        inv.receive_stock(conn, owner, material_id=w["cloth"], qty="1", total_cost="50", unit_price="50", supplier_id=1)
    c = inv.add_customer(conn, owner, "Bilal")
    with pytest.raises(inv.InventoryError):                                          # customer's own cloth has no price
        inv.receive_stock(conn, owner, material_id=w["cloth"], qty="10", unit_price="5", ownership="customer", customer_id=c)
    with pytest.raises(inv.InventoryError):
        inv.receive_stock(conn, owner, material_id=w["cloth"], qty="10", unit_price="-5", supplier_id=1)


# ------------------------------------------------------------------ several materials at once
def test_use_several_materials_in_one_go(conn, owner, w):
    j = entry(conn, owner, w)
    r = js.consume_materials(conn, owner, j, [(w["paper"], "2"), (w["cloth"], "30"), (w["ink"], "0.5")], on_date="2026-08-02")
    # paper 2 x 5,000 = 10,000   cloth 30 x 100 = 3,000   ink 500 x 0.45 = 225   -> 13,225
    assert r["cost_paisa"] == P("13225")
    s = js.job_summary(conn, j)
    assert s["total_paisa"] == P("13225") and s["pieces"] == 50
    assert s["cost_per_piece_paisa"] == P("264.50")                                  # 13,225 / 50
    assert acct.balance(conn, "5000") == P("13225") and acct.balance(conn, "1200") == P("40000") + P("10000") + P("900") - P("13225")
    assert acct.check_integrity(conn) == []


def test_one_bad_line_takes_nothing(conn, owner, w):
    j = entry(conn, owner, w)
    before = inv.stock_summary(conn, w["paper"])["factory_qty"], inv.stock_summary(conn, w["cloth"])["factory_qty"]
    with pytest.raises(js.JobError, match="Line 2"):
        js.consume_materials(conn, owner, j, [(w["paper"], "1"), (w["cloth"], "101")])           # only 100 m exist
    assert (inv.stock_summary(conn, w["paper"])["factory_qty"], inv.stock_summary(conn, w["cloth"])["factory_qty"]) == before
    assert js.job_summary(conn, j)["total_paisa"] == 0
    with pytest.raises(js.JobError):
        js.consume_materials(conn, owner, j, [])
    assert acct.check_integrity(conn) == []


# ------------------------------------------------------------------ invoice = entry number, advance applied
def test_invoice_has_the_entry_number_and_uses_the_advance(conn, owner, w):
    acct.receive_payment(conn, owner, customer_id=w["cust"], amount="5000", paid_on="2026-07-30")            # advance, before any invoice
    j = entry(conn, owner, w)
    js.consume_materials(conn, owner, j, [(w["paper"], "2"), (w["cloth"], "30"), (w["ink"], "0.5")])
    r = js.invoice_job(conn, owner, j, invoice_date="2026-08-05", rate="400")
    assert r["invoice_no"] == "P-0001"
    assert r["total_paisa"] == P("20000")                                           # 50 pieces x Rs 400
    assert r["advance_applied_paisa"] == P("5000") and r["due_paisa"] == P("15000")
    assert acct.balance(conn, "2200", customer_id=w["cust"]) == 0                  # advance used up
    assert acct.balance(conn, "1100", customer_id=w["cust"]) == P("15000")         # what the customer still owes
    assert acct.balance(conn, "4000") == P("20000")                                # revenue
    row = q.production_entries(conn)[0]
    assert (row["bill_status"], row["invoice_paisa"], row["paid_paisa"], row["due_paisa"]) == ("part", P("20000"), P("5000"), P("15000"))
    inv_row = conn.execute("SELECT invoice_no, pieces, rate_paisa FROM invoices WHERE job_id=?", (j,)).fetchone()
    assert tuple(inv_row) == ("P-0001", 50, P("400"))
    acct.receive_payment(conn, owner, customer_id=w["cust"], amount="15000", paid_on="2026-08-10", invoice_id=r["invoice_id"])
    assert q.production_entries(conn)[0]["bill_status"] == "paid"
    assert acct.balance(conn, "1100") == 0 and acct.balance(conn, "1000") == P("5000") + P("15000")
    assert acct.check_integrity(conn) == []
    with pytest.raises(js.JobError):                                               # one invoice per entry
        js.invoice_job(conn, owner, j, invoice_date="2026-08-06", rate="400")


def test_a_big_advance_only_pays_the_invoice(conn, owner, w):
    acct.receive_payment(conn, owner, customer_id=w["cust"], amount="30000", paid_on="2026-07-30")
    j = entry(conn, owner, w)
    r = js.invoice_job(conn, owner, j, invoice_date="2026-08-05", rate="400")
    assert (r["advance_applied_paisa"], r["due_paisa"]) == (P("20000"), 0)
    assert acct.balance(conn, "2200", customer_id=w["cust"]) == P("10000")        # the rest stays as advance
    assert q.production_entries(conn)[0]["bill_status"] == "paid"


def test_no_advance_means_unpaid(conn, owner, w):
    j = entry(conn, owner, w)
    r = js.invoice_job(conn, owner, j, invoice_date="2026-08-05", total="12345.67")
    assert (r["total_paisa"], r["advance_applied_paisa"]) == (P("12345.67"), 0)
    assert q.production_entries(conn)[0]["bill_status"] == "unpaid"
    assert q.production_entries(conn)[0]["rate_paisa"] is None                      # lump sum: no rate recorded


def test_invoice_input_rules(conn, owner, w):
    j = entry(conn, owner, w, pieces=None)
    with pytest.raises(js.JobError):
        js.invoice_job(conn, owner, j, invoice_date="2026-08-05", rate="400")      # no pieces -> cannot price per piece
    with pytest.raises(js.JobError):
        js.invoice_job(conn, owner, j, invoice_date="2026-08-05")                  # nothing to price with
    with pytest.raises(js.JobError):
        js.invoice_job(conn, owner, j, invoice_date="2026-08-05", total="0")
    with pytest.raises(auth.PermissionDenied):                                     # a supervisor cannot raise invoices
        js.invoice_job(conn, w["sup"], j, invoice_date="2026-08-05", total="100")
    assert js.invoice_job(conn, w["acc"], j, invoice_date="2026-08-05", total="100")["invoice_no"] == "P-0001"


def test_entry_with_an_invoice_cannot_be_deleted(conn, owner, w):
    j = entry(conn, owner, w)
    js.invoice_job(conn, owner, j, invoice_date="2026-08-05", total="100")
    with pytest.raises(js.JobError):
        js.delete_job(conn, owner, j)


# ------------------------------------------------------------------ customers
def test_customer_edit_and_delete_rules(conn, owner, w):
    inv.update_customer(conn, owner, w["cust"], name="Ali Traders Ltd", phone="0300", address="Faisalabad")
    assert conn.execute("SELECT name, phone FROM customers WHERE id=?", (w["cust"],)).fetchone()[:] == ("Ali Traders Ltd", "0300")
    other = inv.add_customer(conn, owner, "Zaid")
    with pytest.raises(inv.InventoryError):
        inv.update_customer(conn, owner, other, name="ali traders ltd")             # names are unique, ignoring case
    assert inv.delete_customer(conn, owner, other) == "deleted"                    # never used
    # a customer with history is hidden, not erased, and comes back (with his history) if added again
    j = entry(conn, owner, w)
    with pytest.raises(inv.InventoryError):                                        # open entry
        inv.delete_customer(conn, owner, w["cust"])
    js.delete_job(conn, owner, j)
    acct.receive_payment(conn, owner, customer_id=w["cust"], amount="100", paid_on="2026-08-01")
    with pytest.raises(inv.InventoryError):                                        # holds an advance
        inv.delete_customer(conn, owner, w["cust"])
    j2 = entry(conn, owner, w, no="P-0002")
    js.invoice_job(conn, owner, j2, invoice_date="2026-08-05", total="100")        # the advance pays it exactly
    assert inv.delete_customer(conn, owner, w["cust"]) == "archived"
    assert "Ali Traders Ltd" not in [c["name"] for c in q.customers(conn)]
    again = inv.add_customer(conn, owner, "Ali Traders Ltd", "0311", "New address")
    assert again == w["cust"] and q.customers(conn)[0]["entries"] == 1 and q.customers(conn)[0]["billed"] == P("100")


def test_customer_with_unpaid_invoice_cannot_be_removed(conn, owner, w):
    j = entry(conn, owner, w)
    js.invoice_job(conn, owner, j, invoice_date="2026-08-05", total="100")
    with pytest.raises(inv.InventoryError, match="owes"):
        inv.delete_customer(conn, owner, w["cust"])


# ------------------------------------------------------------------ the numbers the invoice dialog shows
def test_invoice_preview_numbers():
    p = js.invoice_preview(P("13225"), P("20000"), P("5000"))
    assert p["profit_paisa"] == P("6775") and p["margin_bps"] == 3388             # 6,775 / 20,000 = 33.875% -> half-up to 33.88%
    assert (p["advance_applied_paisa"], p["due_paisa"]) == (P("5000"), P("15000"))
    loss = js.invoice_preview(P("7500"), P("5000"), 0)
    assert loss["profit_paisa"] == -P("2500") and loss["margin_bps"] == -5000 and loss["due_paisa"] == P("5000")
    big = js.invoice_preview(P("100"), P("20000"), P("30000"))
    assert (big["advance_applied_paisa"], big["due_paisa"]) == (P("20000"), 0)     # an advance never pays more than the invoice
    assert js.invoice_preview(0, 0, 0)["margin_bps"] is None
    assert js.invoice_amount(50, rate="400") == (P("20000"), P("400")) and js.invoice_amount(None, total="123.45") == (P("123.45"), None)


# ------------------------------------------------------------------ stock export
def test_stock_sheet_and_excel_export(conn, owner, w, tmp_path):
    inv.receive_stock(conn, owner, material_id=w["paper"], qty="4", unit_price="6000", supplier_id=1)            # last price Rs 6,000; average (40,000+24,000)/12
    headers, rows = q.stock_sheet(conn)
    paper = next(r for r in rows if r[0] == "Paper")
    assert paper[3] == D("12.000") and paper[6] == D("64000.00")                    # 12 rolls, Rs 64,000
    assert paper[4] == D("5333.33") and paper[5] == D("6000.00")                    # 64,000 / 12 = 5,333.33 ; last price 6,000
    ink = next(r for r in rows if r[0] == "Reactive ink")
    assert ink[2] == "L" and ink[4] == D("450.00") and ink[6] == D("900.00")
    from reports import exporters
    f = tmp_path / "stock.xlsx"
    exporters.export_rows(f, "Stock", headers, rows, subtitle="test")
    from openpyxl import load_workbook
    ws = load_workbook(f).active
    vals = [[c.value for c in r] for r in ws.iter_rows()]
    assert headers[0] in vals[3] and any(r and r[0] == "Paper" for r in vals)
