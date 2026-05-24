-- Static (Supabase) base schema for the agent-SQL golden harness.
--
-- WHY THIS FILE EXISTS
-- --------------------
-- The static half of the SQL agent runs against the curated
-- `agent_views.*` table-functions defined in
-- `migrations/supabase/040_agent_views_static.sql`. Those functions read
-- from the Supabase reference tables `sites`, `vehicles`,
-- `charging_stations`, `drivers`, and `schedules`.
--
-- Those reference tables are OWNED BY THE SUPABASE PROJECT (the frontend),
-- not by this backend repo. `migrations/supabase/006_align_static_schema_to_supabase.sql`
-- only ALTERs them ("ADD COLUMN IF NOT EXISTS …") — it assumes Supabase has
-- already created `public.sites` et al. There is therefore no way to stand
-- up the static schema on a vanilla Postgres by replaying the migrations:
-- the base tables would be missing (`relation "public.sites" does not exist`).
--
-- So this file reconstructs the Supabase-owned base tables with exactly the
-- columns the agent_views.* functions read, in the Supabase naming
-- convention (`id` PK, `site_id` FKs). Migration 040 is then applied
-- VERBATIM on top — it remains the single source of truth for the
-- agent_views surface the golden suite gates. If a column the functions
-- read is renamed or retyped in 040, this base must track it, and the
-- function-creation step (check_function_bodies) fails loudly if it drifts.
--
-- Apply order for the static test DB:
--   1. psql -f tests/golden/agent_sql/supabase_bootstrap.sql
--   2. psql -f migrations/supabase/040_agent_views_static.sql

-- Organizations — only `id` is referenced (sites.organization_id FK).
CREATE TABLE IF NOT EXISTS public.organizations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL DEFAULT 'Eval org',
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Sites — the depot configuration record (agent_views.depots).
CREATE TABLE IF NOT EXISTS public.sites (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id  uuid REFERENCES public.organizations(id),
    name             text NOT NULL,
    timezone         text DEFAULT 'Europe/Vilnius',
    currency         text DEFAULT 'EUR',
    max_grid_kw      double precision,
    address          text,
    latitude         double precision,
    longitude        double precision,
    tariff_config    jsonb
);

-- Vehicles (agent_views.vehicles).
CREATE TABLE IF NOT EXISTS public.vehicles (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id               uuid REFERENCES public.sites(id),
    vin                   text,
    license_plate         text,
    battery_capacity_kwh  double precision,
    max_charge_rate_kw    double precision,
    v2g_capable           boolean DEFAULT false,
    status                text DEFAULT 'active'
);

-- Charging stations (agent_views.chargers).
CREATE TABLE IF NOT EXISTS public.charging_stations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id         uuid REFERENCES public.sites(id),
    station_id      text,
    max_power_kw    double precision,
    connector_type  text DEFAULT 'CCS',
    vendor          text,
    display_name    text
);

-- Drivers (agent_views.drivers).
CREATE TABLE IF NOT EXISTS public.drivers (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id             uuid REFERENCES public.sites(id),
    display_name        text,
    external_driver_id  text,
    email               text,
    status              text DEFAULT 'active'
);

-- Schedules (agent_views.schedules_recent; joined to vehicles for site_id).
CREATE TABLE IF NOT EXISTS public.schedules (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id          uuid REFERENCES public.vehicles(id),
    driver_id           uuid REFERENCES public.drivers(id),
    route_id            text,
    departure_time      timestamptz,
    return_time         timestamptz,
    actual_return_time  timestamptz,
    energy_kwh          double precision,
    required_soc        double precision
);
