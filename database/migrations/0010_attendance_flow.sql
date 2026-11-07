-- Attendance IN/OUT buttons: a shift nobody pressed OUT for is closed automatically after 10 hours and flagged.
ALTER TABLE attendance ADD COLUMN auto_out INTEGER NOT NULL DEFAULT 0 CHECK (auto_out IN (0,1));

-- A payslip that is wrong (and not yet paid) is revised by reversing its journal entry and posting a new one; every revision is kept.
CREATE TABLE payslip_revisions (
    id INTEGER PRIMARY KEY,
    payslip_id INTEGER NOT NULL REFERENCES payslips(id),
    old_values TEXT NOT NULL, new_values TEXT NOT NULL,
    reason TEXT NOT NULL,
    old_entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
    reversal_entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
    new_entry_id INTEGER NOT NULL REFERENCES journal_entries(id),
    revised_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TRIGGER pr_no_update BEFORE UPDATE ON payslip_revisions
BEGIN SELECT RAISE(ABORT, 'payslip revisions are append-only'); END;
CREATE TRIGGER pr_no_delete BEFORE DELETE ON payslip_revisions
BEGIN SELECT RAISE(ABORT, 'payslip revisions are append-only'); END;

-- The owner asked for estimated salary deductions from the Absent / Leave buttons: absence deduction is now ON by default
-- (salary / days in the month per absent day). Leave stays paid unless the owner switches it. Auto-accrual of the previous month is ON.
UPDATE app_settings SET value='calendar_days' WHERE key='absence_deduction_basis' AND value='off';
INSERT OR IGNORE INTO app_settings(key,value) VALUES ('leave_deduction','paid'), ('auto_accrue_payroll','1');
