import csv

import pytest

from reports import documents, exporters
from services import queries as q, settings_service as st, auth_service as auth, machine_service as ms
from services import costing_service as cs, job_service as js
from tests.test_dashboard import scenario  # noqa: F401  (populated database fixture)


def test_all_list_queries_run_on_populated_db(conn, owner, scenario):
    ms.add_meter_reading(conn, owner, 1, "2026-07-01", "100")
    ms.add_electricity_bill(conn, owner, period_start="2026-07-01", period_end="2026-07-31", units_kwh="5000", total_amount="100000")
    for fn in (q.customers, q.suppliers, q.materials, q.jobs, q.machines, q.machine_runs, q.meter_readings, q.electricity_bills,
               q.employees, q.invoices, q.receipts, q.expenses, q.expense_accounts, q.partners, q.distributions, q.users, q.backups):
        assert isinstance(fn(conn), list)
    assert q.payslips(conn, "2026-07")[0]["due_paisa"] == 2000000
    assert q.attendance(conn, "2026-07") == []
    total, rows = q.audit(conn, "login", 10, 0)
    assert total >= 1 and len(rows) <= 10
    assert q.movements(conn, search="Paper")[0]["material"] == "Paper"
    assert q.job_costs(conn, 1)[0]["kind"] == "material"
    assert q.rolls(conn, 1) == [] and q.invoices(conn)[0]["due_paisa"] in (4000000, 1000000)   # paper is counted in rolls, not tracked roll by roll
    assert {c["name"]: c["outstanding"] for c in q.customers(conn)}["Ali Traders"] == 4000000
    assert {p["name"]: p["ownership_bps"] for p in q.partners(conn)} == {"A": 7500, "B": 2500}


def test_settings_validation_and_owner_only(conn, owner):
    st.set_values(conn, owner, {"factory_name": "Test Mills", "fiscal_year_start_month": "7"})
    assert st.get(conn, "factory_name") == "Test Mills"
    with pytest.raises(st.SettingsError):
        st.set_values(conn, owner, {"fiscal_year_start_month": "13"})
    with pytest.raises(st.SettingsError):
        st.set_values(conn, owner, {"evil": "1"})
    auth.create_user(conn, owner, "acc", "accountant-pw", "accountant")
    with pytest.raises(auth.PermissionDenied):
        st.set_values(conn, auth.authenticate(conn, "acc", "accountant-pw"), {"factory_name": "x"})


def test_machine_service(conn, owner):
    ms.set_hourly_rate(conn, owner, 1, "250")
    assert conn.execute("SELECT hourly_rate_paisa FROM machines WHERE id=1").fetchone()[0] == 25000
    ms.set_hourly_rate(conn, owner, 1, "")
    assert conn.execute("SELECT hourly_rate_paisa FROM machines WHERE id=1").fetchone()[0] is None
    ms.add_meter_reading(conn, owner, 1, "2026-07-01", "100")
    with pytest.raises(ms.MachineError):
        ms.add_meter_reading(conn, owner, 1, "2026-07-02", "99")


def test_set_quote(conn, owner, scenario):
    js.set_quote(conn, owner, 1, cs.price_target_margin(100000, "20"))
    r = conn.execute("SELECT pricing_method, quoted_price_paisa FROM jobs WHERE id=1").fetchone()
    assert r[0] == "target_margin" and r[1] == 125000


def test_exports_all_formats_and_formula_injection(tmp_path):
    hdr, rows = ["Name", "Amount"], [["=HYPERLINK(\"http://x\")", 5], ["Normal & <b>", 7]]
    for ext in ("csv", "xlsx", "pdf"):
        p = exporters.export_rows(tmp_path / f"r.{ext}", "Test", hdr, rows, subtitle="sub")
        assert p.exists() and p.stat().st_size > 20
    with open(tmp_path / "r.csv", encoding="utf-8-sig") as f:
        assert list(csv.reader(f))[1][0].startswith("'=")
    from openpyxl import load_workbook
    ws = load_workbook(tmp_path / "r.xlsx").active
    assert str(ws["A5"].value).startswith("'=")


def test_invoice_and_payslip_pdf(conn, scenario, tmp_path):
    p = documents.invoice_pdf(conn, 1, tmp_path / "inv.pdf")
    s = documents.payslip_pdf(conn, 1, tmp_path / "slip.pdf")
    assert p.read_bytes()[:4] == b"%PDF" and s.read_bytes()[:4] == b"%PDF"
