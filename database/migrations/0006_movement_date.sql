-- Business date of a stock movement (the date the stock really came in / went out), separate from the time it was typed in.
-- Old rows keep NULL; readers use COALESCE(moved_on, substr(created_at,1,10)). stock_movements is append-only, so no back-fill.
ALTER TABLE stock_movements ADD COLUMN moved_on TEXT;
