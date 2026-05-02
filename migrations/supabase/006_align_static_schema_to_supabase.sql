-- Align the static-DB tables that already exist in Supabase with the columns
-- the backend needs. Idempotent: only adds columns/indexes that are missing.
--
-- Supabase owns the authoritative naming for these tables (sites, vehicles,
-- charging_stations). The backend code now references them with that naming
-- and aliases the PK column to its internal `<entity>_id` field at the SQL
-- boundary (e.g. `SELECT id AS depot_id FROM sites`).
--
-- Background: see the validation report attached to the
-- claude/validate-db-schema-0ZZwt branch.

-- ============ ORGANIZATIONS ============
-- Already aligned: organizations(id, name, ...). No DDL changes needed; the
-- backend now uses `id` as the canonical PK and aliases it to `organization_id`
-- in SELECT lists when convenient.

-- ============ SITES (formerly "depots" in the backend) ============
-- The backend uses `sites` rows as the per-depot configuration record.
ALTER TABLE public.sites
    ADD COLUMN IF NOT EXISTS max_grid_kw                    DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS demand_charge_rate_kw          DOUBLE PRECISION DEFAULT 20.0,
    ADD COLUMN IF NOT EXISTS demand_charge_billing_period   VARCHAR(32) NOT NULL DEFAULT 'monthly',
    ADD COLUMN IF NOT EXISTS timezone                       VARCHAR(50) DEFAULT 'America/Los_Angeles',
    ADD COLUMN IF NOT EXISTS utility_id                     VARCHAR(100),
    ADD COLUMN IF NOT EXISTS billing_metadata               JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS building_load_source           JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS building_load_assumption_kw    DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS currency                       VARCHAR(10) NOT NULL DEFAULT 'EUR',
    ADD COLUMN IF NOT EXISTS access_mode                    VARCHAR(32) NOT NULL DEFAULT 'open',
    ADD COLUMN IF NOT EXISTS charger_vehicle_access_default BOOLEAN NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS tariff_config                  JSONB,
    ADD COLUMN IF NOT EXISTS latitude                       DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS longitude                      DOUBLE PRECISION;

CREATE INDEX IF NOT EXISTS idx_sites_organization_id ON public.sites (organization_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sites_access_mode_valid'
    ) THEN
        ALTER TABLE public.sites
            ADD CONSTRAINT sites_access_mode_valid
            CHECK (access_mode IN ('open', 'restricted'));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'sites_demand_charge_billing_period_valid'
    ) THEN
        ALTER TABLE public.sites
            ADD CONSTRAINT sites_demand_charge_billing_period_valid
            CHECK (demand_charge_billing_period IN ('monthly', 'daily', 'annual'));
    END IF;
END $$;

-- ============ VEHICLES ============
-- Add the operational fields the backend tracks (id_tag for OCPP, site_id for
-- depot scoping, external_id/vehicle_type for fleet identity).
ALTER TABLE public.vehicles
    ADD COLUMN IF NOT EXISTS site_id      UUID REFERENCES public.sites(id),
    ADD COLUMN IF NOT EXISTS external_id  VARCHAR(100),
    ADD COLUMN IF NOT EXISTS vehicle_type VARCHAR(50),
    ADD COLUMN IF NOT EXISTS id_tag       VARCHAR(100),
    ADD COLUMN IF NOT EXISTS display_name VARCHAR(255);

CREATE UNIQUE INDEX IF NOT EXISTS vehicles_external_id_unique_idx
    ON public.vehicles (external_id) WHERE external_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS vehicles_id_tag_unique_idx
    ON public.vehicles (id_tag) WHERE id_tag IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_vehicles_site_id ON public.vehicles (site_id);
CREATE INDEX IF NOT EXISTS idx_vehicles_organization_id ON public.vehicles (organization_id);

-- ============ CHARGING_STATIONS (formerly "chargers" in the backend) ============
ALTER TABLE public.charging_stations
    ADD COLUMN IF NOT EXISTS efficiency      DOUBLE PRECISION DEFAULT 0.95,
    ADD COLUMN IF NOT EXISTS display_name    VARCHAR(255),
    ADD COLUMN IF NOT EXISTS vendor          VARCHAR(128),
    ADD COLUMN IF NOT EXISTS network_notes   TEXT,
    ADD COLUMN IF NOT EXISTS auth_required   BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS connector_count INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS connector_ids   JSONB NOT NULL DEFAULT '[1]'::jsonb,
    ADD COLUMN IF NOT EXISTS updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'charging_stations_connector_count_positive'
    ) THEN
        ALTER TABLE public.charging_stations
            ADD CONSTRAINT charging_stations_connector_count_positive
            CHECK (connector_count >= 1);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_charging_stations_station_id ON public.charging_stations (station_id);
CREATE INDEX IF NOT EXISTS idx_charging_stations_site_id ON public.charging_stations (site_id);
