-- Asset register: machines and equipment the factory owns, typed in as plain records with their price.
-- These records are NOT part of the books: they never touch cash, profit, expenses or any report figure. They only list what the factory owns.
-- A wrong record is voided (kept, marked, with a reason), never edited or deleted.
CREATE TABLE asset_records (
    id INTEGER PRIMARY KEY,
    acquired_on TEXT NOT NULL,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    price_paisa INTEGER NOT NULL CHECK (price_paisa > 0),
    notes TEXT,
    voided_at TEXT,
    voided_by INTEGER REFERENCES users(id),
    void_reason TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TRIGGER asset_no_delete BEFORE DELETE ON asset_records
BEGIN SELECT RAISE(ABORT, 'asset records are never deleted; void them instead'); END;
CREATE TRIGGER asset_only_void BEFORE UPDATE ON asset_records
WHEN OLD.voided_at IS NOT NULL OR NEW.acquired_on <> OLD.acquired_on OR NEW.name <> OLD.name OR NEW.price_paisa <> OLD.price_paisa
     OR COALESCE(NEW.notes,'') <> COALESCE(OLD.notes,'') OR NEW.voided_at IS NULL
BEGIN SELECT RAISE(ABORT, 'an asset record can only be voided, not changed'); END;
