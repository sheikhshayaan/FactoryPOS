import pytest

from services import electricity_service as el
from services.units import to_milli, to_paisa


def test_meter_cost_uses_real_bill_rate():
    # bill Rs 100,000 for 5,000 kWh -> Rs 20/kWh effective; machine used 120 kWh -> Rs 2,400
    c = el.cost_from_meter(to_milli("1000"), to_milli("1120"), to_paisa("100000"), to_milli("5000"))
    assert c.paisa == to_paisa("2400") and not c.is_estimate


def test_hourly_is_labelled_estimate():
    c = el.cost_from_hourly(90, to_paisa("300"))
    assert c.paisa == to_paisa("450") and c.is_estimate and "ESTIMATE" in c.basis


def test_missing_data_raises_not_zero():
    with pytest.raises(el.ElectricityDataMissing):
        el.cost_from_hourly(60, None)


def test_meter_backwards_rejected():
    with pytest.raises(el.ElectricityDataMissing):
        el.cost_from_meter(500, 100, 1000, 1000)


def test_bill_allocation_no_double_count():
    r = el.allocate_bill_by_machine(to_paisa("100000"), to_milli("5000"),
                                    {1: to_milli("1200"), 2: to_milli("800")})
    assert r["machines"] == {1: to_paisa("24000"), 2: to_paisa("16000")}
    assert r["unallocated"] == to_paisa("60000")          # stays factory overhead
    assert sum(r["machines"].values()) + r["unallocated"] == to_paisa("100000")


def test_over_metered_rejected():
    with pytest.raises(el.ElectricityDataMissing):
        el.allocate_bill_by_machine(1000, to_milli("10"), {1: to_milli("11")})


def test_meter_usage_from_db_and_missing(conn):
    mid = conn.execute("SELECT id FROM machines WHERE kind='printing'").fetchone()[0]
    with pytest.raises(el.ElectricityDataMissing):
        el.meter_usage(conn, mid, "2026-10-01", "2026-10-31")
    for ts, k in (("2026-10-01", 1000000), ("2026-10-31", 1500000)):
        conn.execute("INSERT INTO machine_meter_readings(machine_id,read_at,kwh_milli) VALUES (?,?,?)", (mid, ts, k))
    assert el.meter_usage(conn, mid, "2026-10-01", "2026-10-31") == (1000000, 1500000)


def test_two_default_machines_and_settings(conn):
    names = [r[0] for r in conn.execute("SELECT name FROM machines ORDER BY id")]
    assert names == ["Heating Machine", "Printing Machine"]
    assert conn.execute("SELECT value FROM app_settings WHERE key='fiscal_year_start_month'").fetchone()[0] == "7"
    assert conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0] == 0   # no demo data
