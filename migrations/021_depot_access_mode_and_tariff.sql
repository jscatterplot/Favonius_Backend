-- Depot-level charger-access mode toggle + energy-cap tariff option.
--
-- charger_vehicle_access_default:
--   'all_to_all'      = StateAssembler synthesizes a full charger×vehicle
--                       matrix; the charger_vehicle_access table is ignored
--                       and readiness will not flag it as missing.
--   'explicit_matrix' = legacy behavior; rows in charger_vehicle_access are
--                       authoritative and required for readiness.
--
-- tariff_type:
--   'simple_demand' = legacy behavior; demand_charge_rate_kw × P_peak.
--   'energy_cap'    = piecewise rate: under_cap_rate_per_kwh up to
--                     energy_cap_kwh of cumulative billing-period kWh,
--                     over_cap_penalty_per_kwh above. Reset cadence is
--                     given by cap_billing_period.
--
-- Guard: depots is a Supabase shadow table retired from 001; skip when absent.

DO $$
BEGIN
    IF to_regclass('public.depots') IS NULL THEN
        RAISE NOTICE 'Skipping 021 depots columns/constraints: shadow table depots does not exist';
        RETURN;
    END IF;

    ALTER TABLE depots
        ADD COLUMN IF NOT EXISTS charger_vehicle_access_default VARCHAR(32) NOT NULL
            DEFAULT 'explicit_matrix',
        ADD COLUMN IF NOT EXISTS tariff_type VARCHAR(32) NOT NULL
            DEFAULT 'simple_demand',
        ADD COLUMN IF NOT EXISTS energy_cap_kwh DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS under_cap_rate_per_kwh DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS over_cap_penalty_per_kwh DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS cap_billing_period VARCHAR(32) DEFAULT 'monthly';

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'depots_charger_vehicle_access_default_check'
    ) THEN
        ALTER TABLE depots
            ADD CONSTRAINT depots_charger_vehicle_access_default_check
            CHECK (charger_vehicle_access_default IN ('all_to_all', 'explicit_matrix'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'depots_tariff_type_check'
    ) THEN
        ALTER TABLE depots
            ADD CONSTRAINT depots_tariff_type_check
            CHECK (tariff_type IN ('simple_demand', 'energy_cap'));
    END IF;

    -- When tariff_type='energy_cap' the cap and both rates must be present
    -- and the over-cap penalty must strictly exceed the under-cap rate
    -- (otherwise the optimizer has no incentive to stay under the cap).
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'depots_energy_cap_fields_check'
    ) THEN
        ALTER TABLE depots
            ADD CONSTRAINT depots_energy_cap_fields_check
            CHECK (
                tariff_type <> 'energy_cap' OR (
                    energy_cap_kwh IS NOT NULL AND energy_cap_kwh > 0
                    AND under_cap_rate_per_kwh IS NOT NULL AND under_cap_rate_per_kwh > 0
                    AND over_cap_penalty_per_kwh IS NOT NULL AND over_cap_penalty_per_kwh > 0
                    AND over_cap_penalty_per_kwh > under_cap_rate_per_kwh
                )
            );
    END IF;
END$$;
