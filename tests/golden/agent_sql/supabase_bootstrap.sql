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
-- So this file reconstructs the Supabase-owned base tables. Migration 040 is
-- then applied VERBATIM on top — it remains the single source of truth for the
-- agent_views surface the golden suite gates. If a column the functions read
-- is renamed or retyped in 040, this base must track it, and the
-- function-creation step (check_function_bodies) fails loudly if it drifts.
--
-- SCHEMA PROVENANCE
-- -----------------
-- The column names AND types below were verified against the live
-- `favonius-pilot` Supabase project (ref hmxdpuzqkotmorheyexv) via the Supabase
-- MCP on 2026-05-24. Every column the agent_views.* functions read is present
-- in production with the type used here:
--   sites:             id, organization_id, name, timezone, currency, max_grid_kw,
--                      address (jsonb), latitude, longitude, tariff_config
--   vehicles:          id, site_id, vin, license_plate, battery_capacity_kwh,
--                      max_charge_rate_kw, v2g_capable, status
--   charging_stations: id, site_id, station_id, max_power_kw, connector_type,
--                      vendor, display_name
--   drivers:           id, site_id, display_name, external_driver_id, email, status
--   schedules:         id, vehicle_id, driver_id, route_id, departure_time,
--                      return_time, actual_return_time, energy_kwh, required_soc
-- This is the MINIMAL subset the functions read. Production carries many more
-- columns (most NOT NULL); they are deliberately omitted because no agent_views
-- function reads them. Nullability is also relaxed vs production so a scenario's
-- minimal fixtures don't have to populate unread columns — the column TYPES,
-- which drive the function bodies' casts/coercions, are what mirror production.
--
-- Apply order for the static test DB:
--   1. psql -f tests/golden/agent_sql/supabase_bootstrap.sql
--   2. psql -f migrations/supabase/040_agent_views_static.sql

-- Organizations — only `id` is referenced (sites.organization_id FK).
CREATE TABLE IF NOT EXISTS public.organizations (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name                   varchar NOT NULL DEFAULT 'Eval org',
    created_at             timestamptz NOT NULL DEFAULT now(),
    agent_sql_mode_enabled boolean NOT NULL DEFAULT TRUE
);

-- Sites — the depot configuration record (agent_views.depots).
-- `address` is jsonb in production; agent_views.depots reads it as `address::text`.
-- A default keeps it out of the minimal fixtures.
--
-- The trailing block (utility_id .. building_load_source) is NOT read by any
-- agent_views.* function — it exists because the HTTP /agent/turn path resolves
-- the caller's scope through `src.db.queries.get_depots_for_organization`
-- (via `build_auth_context`), whose SELECT lists these columns. The S2 golden
-- gate pre-builds AuthContext and never touches them, but the S3 real-DB
-- integration test and the AT-18 SQL-mode e2e drive the real endpoint, so the
-- base table has to carry them or `build_auth_context` raises UndefinedColumn.
-- Types/defaults mirror the production sites table (and the AT-18 inline DDL).
CREATE TABLE IF NOT EXISTS public.sites (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id  uuid REFERENCES public.organizations(id),
    name             varchar NOT NULL,
    timezone         varchar DEFAULT 'Europe/Vilnius',
    currency         varchar NOT NULL DEFAULT 'EUR',
    max_grid_kw      double precision,
    address          jsonb NOT NULL DEFAULT '{}'::jsonb,
    latitude         double precision,
    longitude        double precision,
    tariff_config    jsonb,
    utility_id                     varchar,
    demand_charge_rate_kw          double precision DEFAULT 20.0,
    demand_charge_billing_period   varchar NOT NULL DEFAULT 'monthly',
    billing_metadata               jsonb NOT NULL DEFAULT '{}'::jsonb,
    building_load_source           jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- Vehicles (agent_views.vehicles).
CREATE TABLE IF NOT EXISTS public.vehicles (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id               uuid REFERENCES public.sites(id),
    vin                   varchar,
    license_plate         varchar,
    battery_capacity_kwh  numeric,
    max_charge_rate_kw    numeric,
    v2g_capable           boolean DEFAULT false,
    status                varchar DEFAULT 'active'
);

-- Charging stations (agent_views.chargers).
CREATE TABLE IF NOT EXISTS public.charging_stations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id         uuid REFERENCES public.sites(id),
    station_id      varchar,
    max_power_kw    numeric,
    connector_type  varchar DEFAULT 'CCS',
    vendor          varchar,
    display_name    varchar
);

-- Drivers (agent_views.drivers).
CREATE TABLE IF NOT EXISTS public.drivers (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id             uuid REFERENCES public.sites(id),
    display_name        varchar,
    external_driver_id  varchar,
    email               varchar,
    status              varchar DEFAULT 'active'
);

-- Schedules (agent_views.schedules_recent; joined to vehicles for site_id).
CREATE TABLE IF NOT EXISTS public.schedules (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id          uuid REFERENCES public.vehicles(id),
    driver_id           uuid REFERENCES public.drivers(id),
    route_id            varchar,
    departure_time      timestamptz,
    return_time         timestamptz,
    actual_return_time  timestamptz,
    energy_kwh          double precision,
    required_soc        double precision
);
