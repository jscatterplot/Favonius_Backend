-- Depot setup metadata for first-depot onboarding endpoints.
-- Adds optional structured setup metadata and read-path indexes.
-- Guards below follow the same to_regclass pattern as migrations 006 and 011:
-- depots/vehicles/chargers/battery_storage are shadow tables that migration
-- 029 drops; skip gracefully when they are absent.

DO $$
BEGIN
    IF to_regclass('public.depots') IS NULL THEN
        RAISE NOTICE 'Skipping 016 depots columns: table public.depots does not exist';
        RETURN;
    END IF;
    ALTER TABLE depots
        ADD COLUMN IF NOT EXISTS demand_charge_billing_period VARCHAR(32) NOT NULL DEFAULT 'monthly',
        ADD COLUMN IF NOT EXISTS address JSONB NOT NULL DEFAULT '{}'::jsonb,
        ADD COLUMN IF NOT EXISTS billing_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        ADD COLUMN IF NOT EXISTS building_load_source JSONB NOT NULL DEFAULT '{}'::jsonb;
END $$;

DO $$
BEGIN
    IF to_regclass('public.vehicles') IS NULL THEN
        RAISE NOTICE 'Skipping idx_vehicles_depot_id: table public.vehicles does not exist';
        RETURN;
    END IF;
    CREATE INDEX IF NOT EXISTS idx_vehicles_depot_id ON vehicles (depot_id);
END $$;

DO $$
BEGIN
    IF to_regclass('public.chargers') IS NULL THEN
        RAISE NOTICE 'Skipping idx_chargers_depot_id: table public.chargers does not exist';
        RETURN;
    END IF;
    CREATE INDEX IF NOT EXISTS idx_chargers_depot_id ON chargers (depot_id);
END $$;

DO $$
BEGIN
    IF to_regclass('public.battery_storage') IS NULL THEN
        RAISE NOTICE 'Skipping battery_storage indexes: table public.battery_storage does not exist';
        RETURN;
    END IF;
    CREATE INDEX IF NOT EXISTS idx_battery_storage_depot_id ON battery_storage (depot_id);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_battery_storage_depot_id ON battery_storage (depot_id);
END $$;
