-- Add ISO 4217 currency code to depots for frontend display of pricing.
-- Defaults to EUR for existing rows (the pilot depot and other European depots).
-- Update individual depots to USD, GBP, etc. as needed.

DO $$
BEGIN
    IF to_regclass('public.depots') IS NULL THEN
        RAISE NOTICE 'Skipping 011_depot_currency: table public.depots does not exist';
        RETURN;
    END IF;

    ALTER TABLE depots
        ADD COLUMN IF NOT EXISTS currency VARCHAR(10) NOT NULL DEFAULT 'EUR';

    COMMENT ON COLUMN depots.currency IS 'ISO 4217 currency code used for pricing display (e.g. EUR, USD, GBP)';
END $$;
