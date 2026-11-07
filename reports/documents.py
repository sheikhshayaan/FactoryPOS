"""Printable documents: invoice and salary slip (PDF)."""
from __future__ import annotations

from pathlib import Path

from services import settings_service
from services.units import from_paisa


def _rs(p: int) -> str:
    return f"{from_paisa(p):,.2f}"


def _doc(path, title_lines, table_rows, footer_lines, widths=None):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    st = getSampleStyleSheet()
    p = Path(path); p.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(p), pagesize=A4, title=title_lines[0])
    story = [Paragraph(title_lines[0], st["Title"])] + [Paragraph(x, st["Normal"]) for x in title_lines[1:]] + [Spacer(1, 10)]
    t = Table(table_rows, colWidths=widths)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                           ("ALIGN", (-1, 0), (-1, -1), "RIGHT")]))
    story += [t, Spacer(1, 10)] + [Paragraph(x, st["Normal"]) for x in footer_lines]
    doc.build(story)
    return p


def _esc(s):
    return str(s or "").replace("&", "&amp;").replace("<", "&lt;")


def invoice_pdf(conn, invoice_id: int, path) -> Path:
    i = conn.execute("SELECT i.*, c.name cname, c.address caddr, j.job_no, j.product_type FROM invoices i JOIN customers c ON c.id=i.customer_id "
                     "LEFT JOIN jobs j ON j.id=i.job_id WHERE i.id=?", (invoice_id,)).fetchone()
    if not i:
        raise ValueError("Invoice not found")
    paid = conn.execute("SELECT COALESCE(SUM(amount_paisa),0) FROM invoice_allocations WHERE invoice_id=?", (invoice_id,)).fetchone()[0]
    cur = settings_service.get(conn, "currency", "PKR")
    head = [f"{_esc(settings_service.get(conn, 'factory_name', 'Factory'))} - INVOICE", f"Invoice No: {_esc(i['invoice_no'])} &nbsp; Date: {i['invoice_date']}",
            f"Customer: {_esc(i['cname'])}", _esc(i["caddr"] or "")]
    rows = [["Description", f"Amount ({cur})"],
            [_esc(f"Printing services{' - job ' + i['job_no'] if i['job_no'] else ''}{' (' + i['product_type'] + ')' if i['product_type'] else ''}"), _rs(i["total_paisa"])]]
    if i["pieces"] and i["rate_paisa"]:
        rows.append([_esc(f"{i['pieces']:,} m x {cur} {_rs(i['rate_paisa'])} per meter"), ""])
    foot = [f"<b>Total: {cur} {_rs(i['total_paisa'])}</b>", f"Received: {cur} {_rs(paid)}", f"<b>Balance due: {cur} {_rs(i['total_paisa'] - paid)}</b>"]
    return _doc(path, head, rows, foot, widths=[380, 120])


def payslip_pdf(conn, payslip_id: int, path) -> Path:
    s = conn.execute("SELECT p.*, e.name, e.emp_code, e.job_role FROM payslips p JOIN employees e ON e.id=p.employee_id WHERE p.id=?", (payslip_id,)).fetchone()
    if not s:
        raise ValueError("Payslip not found")
    paid = conn.execute("SELECT COALESCE(SUM(amount_paisa),0) FROM payslip_payments WHERE payslip_id=?", (payslip_id,)).fetchone()[0]
    cur = settings_service.get(conn, "currency", "PKR")
    head = [f"{_esc(settings_service.get(conn, 'factory_name', 'Factory'))} - SALARY SLIP", f"Employee: {_esc(s['name'])} ({_esc(s['emp_code'])}) {_esc(s['job_role'])}",
            f"Period: {s['period']}"]
    rows = [["Item", f"Amount ({cur})"], ["Basic salary", _rs(s["basic_paisa"])], ["Allowances", _rs(s["allowances_paisa"])],
            [f"Deductions ({_esc(s['deduction_notes'])})" if s["deduction_notes"] else "Deductions", "-" + _rs(s["deductions_paisa"])],
            ["Advance recovered", "-" + _rs(s["advance_recovered_paisa"])], ["Net pay", _rs(s["net_paisa"])]]
    foot = [f"Paid so far: {cur} {_rs(paid)}", f"<b>Balance payable: {cur} {_rs(s['net_paisa'] - paid)}</b>"]
    return _doc(path, head, rows, foot, widths=[380, 120])


def expenses_pdf(conn, start: str | None, end: str | None, path) -> Path:
    """Printable list of the expenses dated start..end (all dates when both are None) with the total and the total per category.
    Reversed (cancelled) entries are left out of the list and the total; the number of them is stated."""
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph
    from services import queries as q
    cur = settings_service.get(conn, "currency", "PKR")
    st = getSampleStyleSheet()["Normal"]
    st.fontSize = 8
    allrows = q.expenses(conn, limit=100000, start=start, end=end)
    rows = [r for r in allrows if not r["reversed"]]
    total, n = q.expense_total(conn, start, end)
    period = f"{start} to {end}" if start and end else "All dates"
    head = [f"{_esc(settings_service.get(conn, 'factory_name', 'Factory'))} - EXPENSES", f"Period: {period}"]
    table = [["Date", "Category", "Description", f"Amount ({cur})"]]
    for r in sorted(rows, key=lambda r: (r["entry_date"], r["id"])):
        table.append([r["entry_date"], Paragraph(_esc(r["account"]), st), Paragraph(_esc(r["description"]), st), _rs(r["amount_paisa"])])
    table.append(["", "", "TOTAL", _rs(total)])
    foot = [f"<b>Total expenses: {cur} {_rs(total)}</b> ({n} entr{'y' if n == 1 else 'ies'})"]
    cats = q.expenses_by_category(conn, start, end)
    if cats:
        foot += ["", "<b>By category</b>"] + [f"{_esc(name)}: {cur} {_rs(t)}" for name, t in cats]
    gone = len(allrows) - len(rows)
    if gone:
        foot += ["", f"{gone} reversed (cancelled) entr{'y is' if gone == 1 else 'ies are'} not included."]
    foot += ["", f"Printed on {__import__('datetime').date.today().isoformat()}"]
    return _doc(path, head, table, foot, widths=[62, 120, 220, 90])
