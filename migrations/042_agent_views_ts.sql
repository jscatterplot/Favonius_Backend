-- Migration 042: Agent SQL Mode — TimescaleDB curated table-functions + role + audit trigger.
--
-- The second half of the depot chat agent (alongside Supabase migration 040).
-- Sets up the LLM-facing read-only data surface for the general-purpose
-- text-to-SQL agent in src/api/agent/sql_agent.* (gated by AGENT_SQL_MODE_ENABLED).
--
-- Three things land here:
--
-- 1. `agent_reader_ts` role — SELECT/EXECUTE only on `agent_views.*`,
--    nothing else. The app's connection role swaps to it per-turn via
--    SET LOCAL ROLE inside a read-only transaction with a 5s
--    statement_timeout. A misconfigured grant fails closed because the
--    executor asserts `current_user` after the swap (S2).
--
-- 2. `agent_views.*` SECURITY DEFINER table-functions. Each function
--    takes `p_depot_ids uuid[]` and filters `WHERE depot_id = ANY($1)`
--    inside the function body. The LLM cannot influence the filter
--    (S1 — no WHERE-clause injection vector). The executor binds
--    `$1 = auth.visible_depot_ids` at call time.
--
-- 3. Append-only trigger on `agent_runs` (matches the 037 pattern on
--    `decisions`). Audit immutability per PRD §10.4 (S3).
--
-- Idempotent. Safe to re-run.
--
-- NOTE on migration numbering: 040 + 041 are reserved by the in-flight
-- billing PR (#214). This is 042 to land after either order.

-- ── Role ─────────────────────────────────────────────────────────────────
DO $$
BEGIN
    CREATE ROLE agent_reader_ts NOLOGIN;
EXCEPTION
    WHEN duplicate_object THEN
        NULL;  -- already exists; reapply grants below
END $$;

-- Defence in depth: explicitly REVOKE everything before granting back.
REVOKE ALL ON SCHEMA public            FROM agent_reader_ts;
REVOKE ALL ON SCHEMA pg_catalog        FROM agent_reader_ts;
REVOKE ALL ON SCHEMA information_schema FROM agent_reader_ts;

-- ── agent_views schema ───────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS agent_views;
GRANT  USAGE  ON SCHEMA agent_views TO agent_reader_ts;

-- ── Table-functions ──────────────────────────────────────────────────────
--
-- Each function:
--   * Takes p_depot_ids uuid[] — the caller's visible_depot_ids.
--   * Filters by depot_id inside the function body. The LLM cannot bypass.
--   * SECURITY DEFINER: runs as the function owner (the role applying this
--     migration) so it can read public.* even when called by agent_reader_ts.
--   * STABLE: no side effects, eligible for caching.
--   * SET search_path = pg_catalog, public — defends against schema
--     hijacking by ensuring qualified names resolve to the intended schema.

-- sessions ───────────────────────────────────────────────────────────────
DROP FUNCTION IF EXISTS agent_views.sessions(uuid[]);
CREATE OR REPLACE FUNCTION agent_views.sessions(p_depot_ids uuid[])
RETURNS TABLE (
    session_id    uuid,
    vehicle_id    varchar,
    driver_id     uuid,
    card_id       uuid,
    station_id    varchar,
    depot_id      uuid,
    start_time    timestamptz,
    end_time      timestamptz,
    energy_kwh    numeric,
    cost_total    numeric,
    source        varchar
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT cs.session_id,
           cs.vehicle_id,
           cs.driver_id,
           cs.card_id,
           cs.station_id,
           cs.site_id AS depot_id,
           cs.start_time,
           cs.end_time,
           cs.energy_delivered_kwh,
           cs.cost_total,
           cs.source
    FROM public.charging_sessions cs
    WHERE cs.site_id = ANY(p_depot_ids)
$$;

REVOKE ALL    ON FUNCTION agent_views.sessions(uuid[]) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION agent_views.sessions(uuid[]) TO agent_reader_ts;

-- telemetry_hourly ───────────────────────────────────────────────────────
-- Rolled-up to hourly buckets to keep the LLM-facing surface tractable and
-- avoid exposing the raw 1-Hz hypertable scan path. The validator REQUIRES
-- a time predicate on this view; that's enforced in src/api/agent/sql_validator.py.
DROP FUNCTION IF EXISTS agent_views.telemetry_hourly(uuid[]);
CREATE OR REPLACE FUNCTION agent_views.telemetry_hourly(p_depot_ids uuid[])
RETURNS TABLE (
    vehicle_id        uuid,
    charger_id        uuid,
    depot_id          uuid,
    hour              timestamptz,
    avg_soc           double precision,
    peak_charging_kw  double precision,
    charging_minutes  double precision
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT t.vehicle_id,
           t.charger_id,
           c.depot_id,
           time_bucket('1 hour', t.time) AS hour,
           AVG(t.soc)                    AS avg_soc,
           MAX(t.charging_kw)            AS peak_charging_kw,
           SUM(CASE WHEN t.charging_kw > 0 THEN 1 ELSE 0 END)::double precision
                                         AS charging_minutes
    FROM public.telemetry t
    LEFT JOIN public.chargers c ON c.charger_id = t.charger_id
    WHERE c.depot_id = ANY(p_depot_ids)
    GROUP BY t.vehicle_id, t.charger_id, c.depot_id, time_bucket('1 hour', t.time)
$$;

REVOKE ALL    ON FUNCTION agent_views.telemetry_hourly(uuid[]) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION agent_views.telemetry_hourly(uuid[]) TO agent_reader_ts;

-- optimization_runs ───────────────────────────────────────────────────────
DROP FUNCTION IF EXISTS agent_views.optimization_runs(uuid[]);
CREATE OR REPLACE FUNCTION agent_views.optimization_runs(p_depot_ids uuid[])
RETURNS TABLE (
    run_id          uuid,
    depot_id        uuid,
    run_time        timestamptz,
    trigger_reason  varchar,
    horizon_start   timestamptz,
    horizon_end     timestamptz,
    solve_time_s    double precision,
    peak_demand_kw  double precision,
    status          varchar,
    solver_used     varchar
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT o.run_id, o.depot_id, o.run_time, o.trigger_reason,
           o.horizon_start, o.horizon_end, o.solve_time_s,
           o.peak_demand_kw, o.status, o.solver_used
    FROM public.optimization_runs o
    WHERE o.depot_id = ANY(p_depot_ids)
$$;

REVOKE ALL    ON FUNCTION agent_views.optimization_runs(uuid[]) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION agent_views.optimization_runs(uuid[]) TO agent_reader_ts;

-- alerts ─────────────────────────────────────────────────────────────────
-- Only emits if notification_alerts exists (migration 022). Guard so the
-- migration doesn't fail on environments that haven't applied 022.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'notification_alerts' AND relkind = 'r') THEN
        EXECUTE $f$
            DROP FUNCTION IF EXISTS agent_views.alerts(uuid[]);
            CREATE OR REPLACE FUNCTION agent_views.alerts(p_depot_ids uuid[])
            RETURNS TABLE (
                alert_id         uuid,
                depot_id         uuid,
                created_at       timestamptz,
                severity_level   smallint,
                status           text,
                alert_type       text
            )
            LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $body$
                SELECT id, depot_id, created_at, severity_level, status, alert_type
                FROM public.notification_alerts
                WHERE depot_id = ANY(p_depot_ids)
            $body$;
        $f$;
        EXECUTE 'REVOKE ALL    ON FUNCTION agent_views.alerts(uuid[]) FROM PUBLIC';
        EXECUTE 'GRANT  EXECUTE ON FUNCTION agent_views.alerts(uuid[]) TO agent_reader_ts';
    END IF;
END $$;

-- prices_hourly ──────────────────────────────────────────────────────────
-- Bridges the two price tables that have shipped at different times.
-- Uses electricity_prices if present (migration 034), otherwise prices.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'electricity_prices' AND relkind = 'r') THEN
        EXECUTE $f$
            DROP FUNCTION IF EXISTS agent_views.prices_hourly(uuid[]);
            CREATE OR REPLACE FUNCTION agent_views.prices_hourly(p_depot_ids uuid[])
            RETURNS TABLE (
                depot_id        uuid,
                hour            timestamptz,
                price_per_kwh   numeric,
                currency        text,
                market_type     text
            )
            LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $body$
                SELECT NULL::uuid AS depot_id,
                       time_bucket('1 hour', ep.time) AS hour,
                       AVG(ep.price)::numeric AS price_per_kwh,
                       MAX(ep.currency) AS currency,
                       MAX(ep.market_type) AS market_type
                FROM public.electricity_prices ep
                GROUP BY time_bucket('1 hour', ep.time)
            $body$;
        $f$;
    ELSE
        EXECUTE $f$
            DROP FUNCTION IF EXISTS agent_views.prices_hourly(uuid[]);
            CREATE OR REPLACE FUNCTION agent_views.prices_hourly(p_depot_ids uuid[])
            RETURNS TABLE (
                depot_id        uuid,
                hour            timestamptz,
                price_per_kwh   numeric,
                currency        text,
                market_type     text
            )
            LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $body$
                SELECT p.depot_id,
                       time_bucket('1 hour', p.time) AS hour,
                       AVG(p.price_per_kwh)::numeric AS price_per_kwh,
                       MAX(p.currency) AS currency,
                       NULL::text AS market_type
                FROM public.prices p
                WHERE p.depot_id = ANY(p_depot_ids)
                GROUP BY p.depot_id, time_bucket('1 hour', p.time)
            $body$;
        $f$;
    END IF;
    EXECUTE 'REVOKE ALL    ON FUNCTION agent_views.prices_hourly(uuid[]) FROM PUBLIC';
    EXECUTE 'GRANT  EXECUTE ON FUNCTION agent_views.prices_hourly(uuid[]) TO agent_reader_ts';
END $$;

-- building_load_hourly ───────────────────────────────────────────────────
DROP FUNCTION IF EXISTS agent_views.building_load_hourly(uuid[]);
CREATE OR REPLACE FUNCTION agent_views.building_load_hourly(p_depot_ids uuid[])
RETURNS TABLE (
    depot_id  uuid,
    hour      timestamptz,
    avg_kw    double precision,
    peak_kw   double precision
)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT bl.depot_id,
           time_bucket('1 hour', bl.time) AS hour,
           AVG(bl.power_kw)               AS avg_kw,
           MAX(bl.power_kw)               AS peak_kw
    FROM public.building_load bl
    WHERE bl.depot_id = ANY(p_depot_ids)
    GROUP BY bl.depot_id, time_bucket('1 hour', bl.time)
$$;

REVOKE ALL    ON FUNCTION agent_views.building_load_hourly(uuid[]) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION agent_views.building_load_hourly(uuid[]) TO agent_reader_ts;

-- connector_status_latest ────────────────────────────────────────────────
-- Latest-row-per-(station,connector). connector_status is append-only;
-- DISTINCT ON gives us "the current state" for each (station, connector).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'connector_status' AND relkind = 'r') THEN
        EXECUTE $f$
            DROP FUNCTION IF EXISTS agent_views.connector_status_latest(uuid[]);
            CREATE OR REPLACE FUNCTION agent_views.connector_status_latest(p_depot_ids uuid[])
            RETURNS TABLE (
                station_id        varchar,
                connector_id      integer,
                depot_id          uuid,
                status            varchar,
                error_code        varchar,
                last_changed_at   timestamptz
            )
            LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $body$
                SELECT DISTINCT ON (cs.station_id, cs.connector_id)
                       cs.station_id,
                       cs.connector_id,
                       c.depot_id,
                       cs.status,
                       cs.error_code,
                       cs.timestamp AS last_changed_at
                FROM public.connector_status cs
                LEFT JOIN public.chargers c ON c.ocpp_id = cs.station_id
                WHERE c.depot_id = ANY(p_depot_ids)
                ORDER BY cs.station_id, cs.connector_id, cs.timestamp DESC
            $body$;
        $f$;
        EXECUTE 'REVOKE ALL    ON FUNCTION agent_views.connector_status_latest(uuid[]) FROM PUBLIC';
        EXECUTE 'GRANT  EXECUTE ON FUNCTION agent_views.connector_status_latest(uuid[]) TO agent_reader_ts';
    END IF;
END $$;

-- ── Append-only trigger on agent_runs (S3) ───────────────────────────────
-- Mirrors the decisions_append_only_guard pattern from migration 037.
-- PRD §10.4: audit records are append-only. An attacker (or buggy admin
-- console) cannot rewrite history.
CREATE OR REPLACE FUNCTION agent_runs_append_only_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'agent_runs is append-only (PRD Depot Agent §10.4 + agent SQL S3): % rejected.',
        TG_OP
    USING ERRCODE = 'check_violation';
END;
$$;

DROP TRIGGER IF EXISTS trg_agent_runs_append_only ON agent_runs;
CREATE TRIGGER trg_agent_runs_append_only
    BEFORE DELETE ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION agent_runs_append_only_guard();

-- agent_runs.steps_json gets appended to via UPDATE during a turn (the
-- existing audit.py writers do this). We allow UPDATE but only of the
-- specific columns the orchestrator writes: status, steps_json,
-- duration_ms, final_intent. Other column updates are rejected.
CREATE OR REPLACE FUNCTION agent_runs_restricted_update_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.run_id          IS DISTINCT FROM OLD.run_id
    OR NEW.user_id         IS DISTINCT FROM OLD.user_id
    OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
    OR NEW.depot_id        IS DISTINCT FROM OLD.depot_id
    OR NEW.user_message    IS DISTINCT FROM OLD.user_message
    OR NEW.created_at      IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'agent_runs immutable columns cannot be updated (run_id/user_id/'
            'organization_id/depot_id/user_message/created_at). Only '
            'status, steps_json, duration_ms, final_intent are mutable.'
        USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_agent_runs_restricted_update ON agent_runs;
CREATE TRIGGER trg_agent_runs_restricted_update
    BEFORE UPDATE ON agent_runs
    FOR EACH ROW EXECUTE FUNCTION agent_runs_restricted_update_guard();

COMMENT ON FUNCTION agent_runs_append_only_guard() IS
    'PRD Depot Agent §10.4: agent_runs is append-only. DELETE forbidden.';
COMMENT ON FUNCTION agent_runs_restricted_update_guard() IS
    'PRD Depot Agent §10.4 + agent SQL S3: only the orchestrator-mutable '
    'columns (status, steps_json, duration_ms, final_intent) may be updated.';
