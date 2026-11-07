INSERT INTO accounts(code,name,type,category) VALUES ('1300','Employee Advances','asset',NULL);

CREATE TABLE employees (
    id INTEGER PRIMARY KEY,
    emp_code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    phone TEXT, job_role TEXT,
    joined_on TEXT,
    salary_type TEXT NOT NULL DEFAULT 'monthly' CHECK (salary_type IN ('monthly')),
    default_shift TEXT NOT NULL DEFAULT 'day' CHECK (default_shift IN ('day','night')),
    cost_category TEXT NOT NULL DEFAULT 'direct' CHECK (cost_category IN ('direct','admin')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE salary_history (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    effective_from TEXT NOT NULL,
    monthly_paisa INTEGER NOT NULL CHECK (monthly_paisa >= 0),
    reason TEXT,
    set_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (employee_id, effective_from)
);
CREATE TRIGGER sh_no_update BEFORE UPDATE ON salary_history
BEGIN SELECT RAISE(ABORT, 'salary history is append-only'); END;
CREATE TRIGGER sh_no_delete BEFORE DELETE ON salary_history
BEGIN SELECT RAISE(ABORT, 'salary history is append-only'); END;

CREATE TABLE attendance (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    work_date TEXT NOT NULL,                       -- date the shift STARTED
    status TEXT NOT NULL CHECK (status IN ('present','absent','half_day','leave')),
    shift TEXT CHECK (shift IN ('day','night')),
    clock_in TEXT, clock_out TEXT,                 -- ISO datetimes (out may be next day)
    break_minutes INTEGER NOT NULL DEFAULT 0 CHECK (break_minutes >= 0),
    worked_minutes INTEGER NOT NULL DEFAULT 0 CHECK (worked_minutes >= 0),
    notes TEXT,
    recorded_by INTEGER REFERENCES users(id),
    UNIQUE (employee_id, work_date)
);
CREATE TABLE attendance_corrections (
    id INTEGER PRIMARY KEY,
    attendance_id INTEGER NOT NULL REFERENCES attendance(id),
    old_values TEXT NOT NULL, new_values TEXT NOT NULL,
    reason TEXT NOT NULL,
    approved_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE payslips (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    period TEXT NOT NULL CHECK (period GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'),
    basic_paisa INTEGER NOT NULL CHECK (basic_paisa >= 0),
    allowances_paisa INTEGER NOT NULL DEFAULT 0 CHECK (allowances_paisa >= 0),
    deductions_paisa INTEGER NOT NULL DEFAULT 0 CHECK (deductions_paisa >= 0),
    advance_recovered_paisa INTEGER NOT NULL DEFAULT 0 CHECK (advance_recovered_paisa >= 0),
    net_paisa INTEGER NOT NULL CHECK (net_paisa >= 0),
    deduction_notes TEXT,
    entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    UNIQUE (employee_id, period)
);
CREATE TABLE payslip_payments (
    id INTEGER PRIMARY KEY,
    payslip_id INTEGER NOT NULL REFERENCES payslips(id),
    amount_paisa INTEGER NOT NULL CHECK (amount_paisa > 0),
    paid_on TEXT NOT NULL,
    method TEXT, reference TEXT,
    entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
    created_by INTEGER REFERENCES users(id)
);
-- absence_deduction_basis: 'off' (default, no assumption) | 'calendar_days' (salary / days in month per absent day)
INSERT INTO app_settings(key,value) VALUES ('absence_deduction_basis','off'), ('overtime_rule','none');
