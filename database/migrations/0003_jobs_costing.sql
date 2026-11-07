CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    job_no TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date TEXT NOT NULL,
    delivery_date TEXT,
    product_type TEXT NOT NULL,
    fabric_source TEXT NOT NULL CHECK (fabric_source IN ('factory','customer')),
    quantity_pieces INTEGER CHECK (quantity_pieces IS NULL OR quantity_pieces > 0),
    fabric_length_milli INTEGER CHECK (fabric_length_milli IS NULL OR fabric_length_milli > 0),
    specs TEXT, notes TEXT,
    status TEXT NOT NULL DEFAULT 'quotation'
        CHECK (status IN ('quotation','approved','in_progress','completed','finalized','cancelled')),
    pricing_method TEXT CHECK (pricing_method IN ('fixed','per_meter','cost_plus','target_margin','manual')),
    quoted_price_paisa INTEGER CHECK (quoted_price_paisa IS NULL OR quoted_price_paisa >= 0),
    final_price_paisa INTEGER CHECK (final_price_paisa IS NULL OR final_price_paisa >= 0),
    final_cost_paisa INTEGER,          -- snapshot at finalisation
    finalized_by INTEGER REFERENCES users(id),
    finalized_at TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
-- Estimates (quotation stage)
CREATE TABLE job_estimates (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL CHECK (kind IN ('material','labor','electricity','machine','other')),
    material_id INTEGER REFERENCES materials(id),
    qty_milli INTEGER NOT NULL DEFAULT 0,            -- material quantity (milli-units)
    minutes INTEGER NOT NULL DEFAULT 0,
    amount_paisa INTEGER NOT NULL DEFAULT 0 CHECK (amount_paisa >= 0),
    description TEXT
);
-- Actual cost lines; append-only (corrections are negative lines).
CREATE TABLE job_costs (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL CHECK (kind IN ('material','labor','electricity','machine','other','wastage')),
    material_id INTEGER REFERENCES materials(id),
    qty_milli INTEGER NOT NULL DEFAULT 0,
    minutes INTEGER NOT NULL DEFAULT 0,
    amount_paisa INTEGER NOT NULL,
    is_estimate INTEGER NOT NULL DEFAULT 0 CHECK (is_estimate IN (0,1)),
    basis TEXT,
    description TEXT,
    machine_id INTEGER REFERENCES machines(id),
    stock_movement_ids TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_job_costs_job ON job_costs(job_id);
CREATE TRIGGER jc_no_update BEFORE UPDATE ON job_costs
BEGIN SELECT RAISE(ABORT, 'job_costs is append-only; add a correcting line'); END;
CREATE TRIGGER jc_no_delete BEFORE DELETE ON job_costs
BEGIN SELECT RAISE(ABORT, 'job_costs is append-only; add a correcting line'); END;

CREATE TABLE machine_runs (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    machine_id INTEGER NOT NULL REFERENCES machines(id),
    started_at TEXT NOT NULL, ended_at TEXT NOT NULL,
    minutes INTEGER NOT NULL CHECK (minutes >= 0),
    downtime_minutes INTEGER NOT NULL DEFAULT 0 CHECK (downtime_minutes >= 0),
    meters_milli INTEGER CHECK (meters_milli IS NULL OR meters_milli >= 0),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_runs_job ON machine_runs(job_id);
