-- Migration 008: Fix sites schema gaps left by migration 006.
--
-- Three problems caught by post-migration verification:
--
-- 1. `sites.charger_vehicle_access_default` was added by mig 006 as BOOLEAN,
--    but the optimizer's StateAssembler reads it as a string with values
--    'all_to_all' / 'explicit_matrix' (per migrations/021_depot_access_mode_and_tariff.sql).
--    Fix: drop and re-add as VARCHAR(32) with the expected check constraint.
--    Safe: sites table is empty (verified via row count).
--
-- 2. `sites` is missing the tariff/cap columns the optimizer reads in
--    StateAssembler.load_depot_config() (assembler.py:1296-1305):
--      tariff_type, energy_cap_kwh, under_cap_rate_per_kwh,
--      over_cap_penalty_per_kwh, cap_billing_period
--    Without these the depot config query raises "column does not exist".
--
-- 3. `vehicles.vin` was NOT NULL in Supabase's native schema, but the
--    backend's create_vehicle_identity accepts vin as Optional[str]; the
--    INSERT would fail when no VIN is supplied (common for transit fleets
--    that key off external_id). Make vin nullable.
--
-- 4. `battery_storage` had no UNIQUE constraint on site_id, but
--    upsert_battery_storage uses ON CONFLICT (site_id) — that requires a
--    matching unique index. The "one battery row per depot" semantic the
--    code assumes maps to a UNIQUE (site_id) constraint.

BEGIN;

-- ============================================================
-- 1. Fix charger_vehicle_access_default type (boolean -> varchar)
-- ============================================================

-- Drop the wrongly-typed column. sites is empty so no data loss.
ALTER TABLE public.sites
    DROP COLUMN IF EXISTS charger_vehicle_access_default;

ALTER TABLE public.sites
    ADD COLUMN charger_vehicle_access_default VARCHAR(32) NOT NULL DEFAULT 'explicit_matrix';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'sites_charger_vehicle_access_default_check'
    ) THEN
        ALTER TABLE public.sites
            ADD CONSTRAINT sites_charger_vehicle_access_default_check
            CHECK (charger_vehicle_access_default IN ('all_to_all', 'explicit_matrix'));
    END IF;
END$$;

-- ============================================================
-- 2. Add tariff / cap columns the optimizer expects
-- ============================================================

ALTER TABLE public.sites
    ADD COLUMN IF NOT EXISTS tariff_type VARCHAR(32) NOT NULL DEFAULT 'simple_demand',
    ADD COLUMN IF NOT EXISTS energy_cap_kwh DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS under_cap_rate_per_kwh DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS over_cap_penalty_per_kwh DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cap_billing_period VARCHAR(32) DEFAULT 'monthly';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sites_tariff_type_check'
    ) THEN
        ALTER TABLE public.sites
            ADD CONSTRAINT sites_tariff_type_check
            CHECK (tariff_type IN ('simple_demand', 'energy_cap'));
    END IF;

    -- When tariff_type='energy_cap' the cap and both rates must be present.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sites_energy_cap_consistency'
    ) THEN
        ALTER TABLE public.sites
            ADD CONSTRAINT sites_energy_cap_consistency CHECK (
                tariff_type <> 'energy_cap' OR (
                    energy_cap_kwh IS NOT NULL AND energy_cap_kwh > 0
                    AND under_cap_rate_per_kwh IS NOT NULL
                    AND over_cap_penalty_per_kwh IS NOT NULL
                )
            );
    END IF;
END$$;

-- ============================================================
-- 3. Make vehicles.vin nullable
-- ============================================================

ALTER TABLE public.vehicles
    ALTER COLUMN vin DROP NOT NULL;

-- ============================================================
-- 4. UNIQUE constraint on battery_storage.site_id for upsert support
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'battery_storage_site_id_key'
    ) THEN
        ALTER TABLE public.battery_storage
            ADD CONSTRAINT battery_storage_site_id_key UNIQUE (site_id);
    END IF;
END$$;

COMMIT;
