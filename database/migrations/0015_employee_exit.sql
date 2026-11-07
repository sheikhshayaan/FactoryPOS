-- An employee who resigns or is removed stays on file (their attendance, payslips and advances must remain readable); they are just no longer active.
ALTER TABLE employees ADD COLUMN left_on TEXT;
ALTER TABLE employees ADD COLUMN exit_kind TEXT;
ALTER TABLE employees ADD COLUMN exit_reason TEXT;
