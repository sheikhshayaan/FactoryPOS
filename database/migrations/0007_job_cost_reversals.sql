-- A stock line given back to inventory is corrected with a NEGATIVE cost line that points at the line it reverses.
-- job_costs is append-only, so nothing is edited; the unique index lets a line be reversed only once.
ALTER TABLE job_costs ADD COLUMN reverses_id INTEGER REFERENCES job_costs(id);
CREATE UNIQUE INDEX idx_jc_reverses ON job_costs(reverses_id) WHERE reverses_id IS NOT NULL;
