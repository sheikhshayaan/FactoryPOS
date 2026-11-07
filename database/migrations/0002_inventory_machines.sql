-- Money: integer paisa. Quantities: integer milli-units (1 m = 1000).
CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phone TEXT, address TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE suppliers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phone TEXT, address TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE materials (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    category TEXT NOT NULL CHECK (category IN ('fabric','paper','ink','consumable')),
    base_unit TEXT NOT NULL,                       -- 'm', 'ml', 'g', 'pcs' ...
    tracks_rolls INTEGER NOT NULL DEFAULT 0 CHECK (tracks_rolls IN (0,1)),
    reorder_level_milli INTEGER NOT NULL DEFAULT 0 CHECK (reorder_level_milli >= 0),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
-- Factory-owned valuation (weighted average). Customer property is NEVER included here.
CREATE TABLE material_stock (
    material_id INTEGER PRIMARY KEY REFERENCES materials(id),
    qty_milli INTEGER NOT NULL DEFAULT 0 CHECK (qty_milli >= 0),
    value_paisa INTEGER NOT NULL DEFAULT 0 CHECK (value_paisa >= 0),
    CHECK (qty_milli > 0 OR value_paisa = 0)
);
CREATE TABLE stock_lots (
    id INTEGER PRIMARY KEY,
    material_id INTEGER NOT NULL REFERENCES materials(id),
    ownership TEXT NOT NULL CHECK (ownership IN ('factory','customer')),
    customer_id INTEGER REFERENCES customers(id),
    supplier_id INTEGER REFERENCES suppliers(id),
    received_on TEXT NOT NULL,
    qty_received_milli INTEGER NOT NULL CHECK (qty_received_milli > 0),
    qty_remaining_milli INTEGER NOT NULL CHECK (qty_remaining_milli >= 0),
    total_cost_paisa INTEGER NOT NULL DEFAULT 0 CHECK (total_cost_paisa >= 0),
    reference TEXT, invoice_no TEXT, location TEXT,
    payment_status TEXT CHECK (payment_status IN ('unpaid','partial','paid')),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    CHECK (ownership = 'factory' OR customer_id IS NOT NULL),
    CHECK (ownership = 'factory' OR total_cost_paisa = 0)
);
CREATE INDEX idx_lots_mat ON stock_lots(material_id, ownership, received_on, id);
CREATE TABLE stock_rolls (
    id INTEGER PRIMARY KEY,
    lot_id INTEGER NOT NULL REFERENCES stock_lots(id),
    length_milli INTEGER NOT NULL CHECK (length_milli > 0),
    remaining_milli INTEGER NOT NULL CHECK (remaining_milli >= 0 AND remaining_milli <= length_milli)
);
CREATE INDEX idx_rolls_lot ON stock_rolls(lot_id, id);
CREATE TABLE stock_movements (
    id INTEGER PRIMARY KEY,
    material_id INTEGER NOT NULL REFERENCES materials(id),
    ownership TEXT NOT NULL CHECK (ownership IN ('factory','customer')),
    customer_id INTEGER REFERENCES customers(id),
    lot_id INTEGER REFERENCES stock_lots(id),
    roll_id INTEGER REFERENCES stock_rolls(id),
    movement_type TEXT NOT NULL CHECK (movement_type IN
        ('opening','purchase','production','customer_return','supplier_return',
         'wastage','transfer','adjustment_in','adjustment_out')),
    qty_milli INTEGER NOT NULL CHECK (qty_milli <> 0),   -- signed
    value_paisa INTEGER NOT NULL DEFAULT 0,               -- signed, factory-owned only
    ref_type TEXT, ref_id TEXT,                           -- e.g. 'job','PO'
    reason TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_mov_mat ON stock_movements(material_id, created_at);
CREATE TRIGGER mov_no_update BEFORE UPDATE ON stock_movements
BEGIN SELECT RAISE(ABORT, 'stock_movements is append-only; post a reversing movement'); END;
CREATE TRIGGER mov_no_delete BEFORE DELETE ON stock_movements
BEGIN SELECT RAISE(ABORT, 'stock_movements is append-only; post a reversing movement'); END;

CREATE TABLE machines (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    kind TEXT NOT NULL CHECK (kind IN ('heating','printing','other')),
    -- electricity allocation configuration (NULL = not configured -> cost unknown)
    hourly_rate_paisa INTEGER CHECK (hourly_rate_paisa IS NULL OR hourly_rate_paisa >= 0),
    rated_kw_milli INTEGER CHECK (rated_kw_milli IS NULL OR rated_kw_milli >= 0),
    is_active INTEGER NOT NULL DEFAULT 1
);
INSERT INTO machines(name, kind) VALUES ('Heating Machine','heating'), ('Printing Machine','printing');

CREATE TABLE machine_meter_readings (
    id INTEGER PRIMARY KEY,
    machine_id INTEGER NOT NULL REFERENCES machines(id),
    read_at TEXT NOT NULL,
    kwh_milli INTEGER NOT NULL CHECK (kwh_milli >= 0),   -- cumulative meter reading
    recorded_by INTEGER REFERENCES users(id),
    UNIQUE (machine_id, read_at)
);
CREATE TABLE electricity_bills (
    id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL DEFAULT 'FESCO',
    period_start TEXT NOT NULL, period_end TEXT NOT NULL,
    units_kwh_milli INTEGER NOT NULL CHECK (units_kwh_milli > 0),
    total_amount_paisa INTEGER NOT NULL CHECK (total_amount_paisa >= 0),  -- incl. taxes/fixed charges as billed
    reference TEXT,
    created_at TEXT NOT NULL
);

INSERT INTO app_settings(key,value) VALUES
 ('currency','PKR'), ('fiscal_year_start_month','7'),
 ('costing_method','weighted_average'), ('allow_negative_stock','0'),
 ('timezone','Asia/Karachi'), ('electricity_provider','FESCO');
