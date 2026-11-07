-- An invoice raised from a production entry remembers how it was priced (pieces x rate per piece), so the invoice PDF can show it.
ALTER TABLE invoices ADD COLUMN pieces INTEGER;
ALTER TABLE invoices ADD COLUMN rate_paisa INTEGER;
