CREATE TABLE accounts (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('asset','liability','equity','revenue','expense')),
    -- expense grouping: direct_production + factory_overhead = cost of production; selling_admin = operating
    category TEXT CHECK (category IN ('direct_production','factory_overhead','selling_admin')),
    is_active INTEGER NOT NULL DEFAULT 1
);
INSERT INTO accounts(code,name,type,category) VALUES
 ('1000','Cash','asset',NULL),
 ('1100','Accounts Receivable','asset',NULL),
 ('1200','Raw Materials Inventory','asset',NULL),
 ('1500','Fixed Assets (capital expenditure)','asset',NULL),
 ('2000','Accounts Payable (Suppliers)','liability',NULL),
 ('2200','Customer Advances','liability',NULL),
 ('2300','Salaries Payable','liability',NULL),
 ('2400','Partner Distributions Payable','liability',NULL),
 ('3000','Partner Capital','equity',NULL),
 ('3100','Partner Drawings','equity',NULL),
 ('3200','Profit Distributions','equity',NULL),
 ('3500','Opening Balance Equity','equity',NULL),
 ('4000','Sales Revenue','revenue',NULL),
 ('5000','Direct Materials Consumed','expense','direct_production'),
 ('5100','Direct Labor','expense','direct_production'),
 ('5200','Electricity & Utilities','expense','factory_overhead'),
 ('5300','Production Wastage','expense','direct_production'),
 ('5400','Factory Overheads (repairs, maintenance, rent)','expense','factory_overhead'),
 ('5900','Inventory Adjustments','expense','direct_production'),
 ('6000','Selling & Administrative Expenses','expense','selling_admin'),
 ('6100','Administrative Salaries','expense','selling_admin');

CREATE TABLE journal_entries (
    id INTEGER PRIMARY KEY,
    entry_date TEXT NOT NULL CHECK (entry_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    description TEXT NOT NULL,
    source_type TEXT, source_id TEXT,
    reverses_entry_id INTEGER UNIQUE REFERENCES journal_entries(id),   -- UNIQUE: can be reversed once
    posted_by INTEGER REFERENCES users(id),
    posted_at TEXT NOT NULL
);
CREATE INDEX idx_je_date ON journal_entries(entry_date);
CREATE TABLE journal_lines (
    id INTEGER PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    debit_paisa INTEGER NOT NULL DEFAULT 0 CHECK (debit_paisa >= 0),
    credit_paisa INTEGER NOT NULL DEFAULT 0 CHECK (credit_paisa >= 0),
    customer_id INTEGER REFERENCES customers(id),
    supplier_id INTEGER REFERENCES suppliers(id),
    partner_id INTEGER,
    employee_id INTEGER,
    memo TEXT,
    CHECK ((debit_paisa > 0 AND credit_paisa = 0) OR (credit_paisa > 0 AND debit_paisa = 0))
);
CREATE INDEX idx_jl_acct ON journal_lines(account_id, entry_id);
CREATE INDEX idx_jl_entry ON journal_lines(entry_id);
CREATE TRIGGER je_no_update BEFORE UPDATE ON journal_entries
BEGIN SELECT RAISE(ABORT, 'posted journal entries are immutable; post a reversal'); END;
CREATE TRIGGER je_no_delete BEFORE DELETE ON journal_entries
BEGIN SELECT RAISE(ABORT, 'posted journal entries are immutable; post a reversal'); END;
CREATE TRIGGER jl_no_update BEFORE UPDATE ON journal_lines
BEGIN SELECT RAISE(ABORT, 'posted journal lines are immutable; post a reversal'); END;
CREATE TRIGGER jl_no_delete BEFORE DELETE ON journal_lines
BEGIN SELECT RAISE(ABORT, 'posted journal lines are immutable; post a reversal'); END;

CREATE TABLE invoices (
    id INTEGER PRIMARY KEY,
    invoice_no TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    job_id INTEGER REFERENCES jobs(id),
    invoice_date TEXT NOT NULL,
    total_paisa INTEGER NOT NULL CHECK (total_paisa > 0),
    entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
-- cash receipts and advance applications against an invoice
CREATE TABLE invoice_allocations (
    id INTEGER PRIMARY KEY,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    amount_paisa INTEGER NOT NULL CHECK (amount_paisa > 0),
    kind TEXT NOT NULL CHECK (kind IN ('payment','advance_applied')),
    method TEXT, reference TEXT, paid_on TEXT NOT NULL,
    entry_id INTEGER NOT NULL REFERENCES journal_entries(id)
);

CREATE TABLE partners (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    phone TEXT, joined_on TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
-- Historical: a new set of rows per effective date; old rows are never edited.
CREATE TABLE partner_profit_shares (
    id INTEGER PRIMARY KEY,
    partner_id INTEGER NOT NULL REFERENCES partners(id),
    effective_from TEXT NOT NULL,
    share_bps INTEGER NOT NULL CHECK (share_bps >= 0 AND share_bps <= 10000),   -- 10000 = 100%
    approved_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (partner_id, effective_from)
);
CREATE TRIGGER pps_no_update BEFORE UPDATE ON partner_profit_shares
BEGIN SELECT RAISE(ABORT, 'profit shares are historical; add a new effective-dated row'); END;
CREATE TRIGGER pps_no_delete BEFORE DELETE ON partner_profit_shares
BEGIN SELECT RAISE(ABORT, 'profit shares are historical; add a new effective-dated row'); END;

CREATE TABLE distributions (
    id INTEGER PRIMARY KEY,
    period_start TEXT NOT NULL, period_end TEXT NOT NULL,
    amount_paisa INTEGER NOT NULL CHECK (amount_paisa > 0),
    status TEXT NOT NULL CHECK (status IN ('approved')) DEFAULT 'approved',
    shares_as_of TEXT NOT NULL,
    note TEXT,
    approved_by INTEGER NOT NULL REFERENCES users(id),
    approved_at TEXT NOT NULL,
    entry_id INTEGER NOT NULL REFERENCES journal_entries(id)
);
CREATE TABLE distribution_lines (
    id INTEGER PRIMARY KEY,
    distribution_id INTEGER NOT NULL REFERENCES distributions(id),
    partner_id INTEGER NOT NULL REFERENCES partners(id),
    share_bps INTEGER NOT NULL,
    amount_paisa INTEGER NOT NULL CHECK (amount_paisa >= 0),
    paid_paisa INTEGER NOT NULL DEFAULT 0 CHECK (paid_paisa >= 0 AND paid_paisa <= amount_paisa)
);

INSERT INTO app_settings(key,value) VALUES ('allow_negative_cash','0');
