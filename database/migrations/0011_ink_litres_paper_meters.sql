-- Ink is now counted in litres (L) instead of millilitres, and paper rolls know how many meters are in one roll.
-- 1 ml = 1 milli-litre, so an ink quantity stored as milli-ml is simply divided by 1000. The migration REFUSES to run
-- (the whole thing rolls back, nothing changes) if any ink figure has a fraction of a millilitre, so no quantity is ever rounded.

CREATE TEMP TABLE _ink_ids AS SELECT id FROM materials WHERE category = 'ink' AND base_unit = 'ml';

CREATE TEMP TABLE _ink_whole_ml (ok INTEGER CHECK (ok = 0));
INSERT INTO _ink_whole_ml SELECT
      (SELECT COUNT(*) FROM material_stock   WHERE material_id IN (SELECT id FROM _ink_ids) AND qty_milli % 1000 <> 0)
    + (SELECT COUNT(*) FROM stock_lots       WHERE material_id IN (SELECT id FROM _ink_ids) AND (qty_received_milli % 1000 <> 0 OR qty_remaining_milli % 1000 <> 0))
    + (SELECT COUNT(*) FROM stock_movements  WHERE material_id IN (SELECT id FROM _ink_ids) AND qty_milli % 1000 <> 0)
    + (SELECT COUNT(*) FROM job_costs        WHERE material_id IN (SELECT id FROM _ink_ids) AND qty_milli % 1000 <> 0)
    + (SELECT COUNT(*) FROM job_estimates    WHERE material_id IN (SELECT id FROM _ink_ids) AND qty_milli % 1000 <> 0)
    + (SELECT COUNT(*) FROM materials        WHERE id IN (SELECT id FROM _ink_ids) AND reorder_level_milli % 1000 <> 0);
DROP TABLE _ink_whole_ml;

DROP TRIGGER mov_no_update;
DROP TRIGGER mov_no_delete;
DROP TRIGGER jc_no_update;
DROP TRIGGER jc_no_delete;

UPDATE material_stock  SET qty_milli = qty_milli / 1000 WHERE material_id IN (SELECT id FROM _ink_ids);
UPDATE stock_lots      SET qty_received_milli = qty_received_milli / 1000, qty_remaining_milli = qty_remaining_milli / 1000
                       WHERE material_id IN (SELECT id FROM _ink_ids);
UPDATE stock_movements SET qty_milli = qty_milli / 1000 WHERE material_id IN (SELECT id FROM _ink_ids);
UPDATE job_costs       SET qty_milli = qty_milli / 1000 WHERE material_id IN (SELECT id FROM _ink_ids);
UPDATE job_estimates   SET qty_milli = qty_milli / 1000 WHERE material_id IN (SELECT id FROM _ink_ids);
UPDATE materials       SET reorder_level_milli = reorder_level_milli / 1000, base_unit = 'L' WHERE id IN (SELECT id FROM _ink_ids);

CREATE TRIGGER mov_no_update BEFORE UPDATE ON stock_movements
BEGIN SELECT RAISE(ABORT, 'stock_movements is append-only; post a reversing movement'); END;
CREATE TRIGGER mov_no_delete BEFORE DELETE ON stock_movements
BEGIN SELECT RAISE(ABORT, 'stock_movements is append-only; post a reversing movement'); END;
CREATE TRIGGER jc_no_update BEFORE UPDATE ON job_costs
BEGIN SELECT RAISE(ABORT, 'job_costs is append-only; add a correcting line'); END;
CREATE TRIGGER jc_no_delete BEFORE DELETE ON job_costs
BEGIN SELECT RAISE(ABORT, 'job_costs is append-only; add a correcting line'); END;

DROP TABLE _ink_ids;

-- Paper is stored in rolls; the length of one roll (milli-meters) turns rolls into meters and back. NULL for every other material.
ALTER TABLE materials ADD COLUMN roll_length_milli INTEGER CHECK (roll_length_milli IS NULL OR roll_length_milli > 0);
UPDATE materials SET roll_length_milli = 300000 WHERE category = 'paper' AND base_unit = 'roll';
