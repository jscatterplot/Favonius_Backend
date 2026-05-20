-- Supabase migration 040: Agent SQL Mode — Static-side curated table-functions + role.
--
-- The static (Supabase) half of the depot chat agent's read-only data surface.
-- Pairs with TimescaleDB migration 042. See that migration for the security
-- architecture (S1: parameterized table-functions; S2: role-swap assertion;
-- S3: append-only audit on agent_runs in the TS DB).
--
-- This migration lands:
--
-- 1. `agent_reader_static` role — SELECT/EXECUTE only on agent_views.*
--    on the Supabase instance.
--
-- 2. `agent_views.*` SECURITY DEFINER table-functions for the static
--    reference tables (sites, vehicles, drivers, charging_stations,
--    schedules). Same parameter contract as the TS side: every function
--    takes p_depot_ids uuid[] and filters by depot inside the body.
--
-- Idempotent.

DO $$
BEGIN
    CREATE ROLE agent_reader_static NOLOGIN;
EXCEPTION
    WHEN duplicate_object THEN
        NULL;
END $$;

-- Runtime role membership: app login role must be able to SET ROLE.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'favonius') THEN
        GRANT agent_reader_static TO favonius;
    END IF;
END $$;

REVOKE ALL ON SCHEMA public            FROM agent_reader_static;
REVOKE ALL ON SCHEMA pg_catalog        FROM agent_reader_static;
REVOKE ALL ON SCHEMA information_schema FROM agent_reader_static;

CREATE SCHEMA IF NOT EXISTS agent_views;
GRANT  USAGE  ON SCHEMA agent_views TO agent_reader_static;

-- depots ─────────────────────────────────────────────────────────────────
-- The Supabase table is `sites`; the backend vocabulary is "depot".
-- The function returns the depot-vocabulary view so the LLM sees consistent
-- terminology with the rest of the system prompt.
DROP FUNCTION IF EXISTS agent_views.depots(uuid[]);
CREATE OR REPLACE FUNCTION agent_views.depots(p_depot_ids uuid[])
RETURNS TABLE (
    depot_id        uuid,
    name            text,
    timezone        text,
    currency        text,
    max_grid_kw     double precision,
    address         text,
    latitude        double precision,
    longitude       double precision
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT s.id            AS depot_id,
           s.name::text,
           s.timezone::text,
           s.currency::text,
           s.max_grid_kw,
           s.address::text,
           s.latitude,
           s.longitude
    FROM public.sites s
    WHERE s.id = ANY(p_depot_ids)
$$;

REVOKE ALL    ON FUNCTION agent_views.depots(uuid[]) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION agent_views.depots(uuid[]) TO agent_reader_static;

-- vehicles ───────────────────────────────────────────────────────────────
DROP FUNCTION IF EXISTS agent_views.vehicles(uuid[]);
CREATE OR REPLACE FUNCTION agent_views.vehicles(p_depot_ids uuid[])
RETURNS TABLE (
    vehicle_id              uuid,
    depot_id                uuid,
    vin                     text,
    license_plate           text,
    battery_capacity_kwh    double precision,
    max_charge_rate_kw      double precision,
    v2g_capable             boolean,
    status                  text
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT v.id                        AS vehicle_id,
           v.site_id                   AS depot_id,
           v.vin::text,
           v.license_plate::text,
           v.battery_capacity_kwh,
           v.max_charge_rate_kw,
           v.v2g_capable,
           v.status::text
    FROM public.vehicles v
    WHERE v.site_id = ANY(p_depot_ids)
$$;

REVOKE ALL    ON FUNCTION agent_views.vehicles(uuid[]) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION agent_views.vehicles(uuid[]) TO agent_reader_static;

-- chargers ───────────────────────────────────────────────────────────────
-- Supabase table is `charging_stations`. The function returns the charger
-- vocabulary (matching the backend) including the OCPP station id.
DROP FUNCTION IF EXISTS agent_views.chargers(uuid[]);
CREATE OR REPLACE FUNCTION agent_views.chargers(p_depot_ids uuid[])
RETURNS TABLE (
    charger_id      uuid,
    depot_id        uuid,
    ocpp_id         text,
    rated_kw        double precision,
    connector_type  text,
    vendor          text,
    display_name    text
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT cs.id                AS charger_id,
           cs.site_id           AS depot_id,
           cs.station_id::text  AS ocpp_id,
           cs.max_power_kw      AS rated_kw,
           cs.connector_type::text,
           cs.vendor::text,
           cs.display_name::text
    FROM public.charging_stations cs
    WHERE cs.site_id = ANY(p_depot_ids)
$$;

REVOKE ALL    ON FUNCTION agent_views.chargers(uuid[]) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION agent_views.chargers(uuid[]) TO agent_reader_static;

-- drivers ────────────────────────────────────────────────────────────────
DROP FUNCTION IF EXISTS agent_views.drivers(uuid[]);
CREATE OR REPLACE FUNCTION agent_views.drivers(p_depot_ids uuid[])
RETURNS TABLE (
    driver_id           uuid,
    depot_id            uuid,
    display_name        text,
    external_driver_id  text,
    email               text,
    status              text
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT d.id                     AS driver_id,
           d.site_id                AS depot_id,
           d.display_name::text,
           d.external_driver_id::text,
           d.email::text,
           d.status::text
    FROM public.drivers d
    WHERE d.site_id = ANY(p_depot_ids)
$$;

REVOKE ALL    ON FUNCTION agent_views.drivers(uuid[]) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION agent_views.drivers(uuid[]) TO agent_reader_static;

-- schedules_recent ───────────────────────────────────────────────────────
-- Last 14 days + next 14 days of schedules. Bounded surface to keep query
-- cost predictable when the LLM omits a time predicate.
DROP FUNCTION IF EXISTS agent_views.schedules_recent(uuid[]);
CREATE OR REPLACE FUNCTION agent_views.schedules_recent(p_depot_ids uuid[])
RETURNS TABLE (
    schedule_id          uuid,
    depot_id             uuid,
    vehicle_id           uuid,
    driver_id            uuid,
    route_id             text,
    departure_time       timestamptz,
    return_time          timestamptz,
    actual_return_time   timestamptz,
    energy_kwh           double precision,
    required_soc         double precision
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT sc.id                           AS schedule_id,
           v.site_id                       AS depot_id,
           sc.vehicle_id,
           sc.driver_id,
           sc.route_id::text,
           sc.departure_time,
           sc.return_time,
           sc.actual_return_time,
           sc.energy_kwh,
           sc.required_soc
    FROM public.schedules sc
    JOIN public.vehicles v ON v.id = sc.vehicle_id
    WHERE v.site_id = ANY(p_depot_ids)
      AND sc.departure_time BETWEEN NOW() - INTERVAL '14 days'
                                 AND NOW() + INTERVAL '14 days'
$$;

REVOKE ALL    ON FUNCTION agent_views.schedules_recent(uuid[]) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION agent_views.schedules_recent(uuid[]) TO agent_reader_static;
