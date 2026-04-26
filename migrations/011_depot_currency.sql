-- Add ISO 4217 currency code to depots for frontend display of pricing.
-- Defaults to EUR for existing rows (TOKS Vilnius and European depots).
-- Update individual depots to USD, GBP, etc. as needed.

ALTER TABLE depots
    ADD COLUMN IF NOT EXISTS currency VARCHAR(10) NOT NULL DEFAULT 'EUR';

COMMENT ON COLUMN depots.currency IS 'ISO 4217 currency code used for pricing display (e.g. EUR, USD, GBP)';
