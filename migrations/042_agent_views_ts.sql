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

-- Grant runtime login-role(s) membership in agent_reader_ts so the
-- executor's `SET LOCAL ROLE agent_reader_ts` succeeds regardless of
-- whether DATABASE_URL points at a direct login user or a pooler user.
-- We attempt both current_user and session_user (deduplicated), then warn
-- if grants are blocked by privileges. Idempotent.
DO $grant$
DECLARE
    grant_role text;
BEGIN
    FOR grant_role IN
        SELECT DISTINCT rolname
        FROM (VALUES (current_user), (session_user)) AS r(rolname)
        WHERE rolname IS NOT NULL
    LOOP
        BEGIN
            EXECUTE format('GRANT agent_reader_ts TO %I', grant_role);
        EXCEPTION
            WHEN insufficient_privilege THEN
                RAISE WARNING
                    'Could not GRANT agent_reader_ts TO %: '
                    'manual grant required for SQL agent mode to function.',
                    grant_role;
        END;
    END LOOP;
END
$grant$;

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
    session_id          uuid,
    vehicle_id          varchar,
    driver_id           uuid,
    card_id             uuid,
    station_id          varchar,
    depot_id            uuid,
    start_time          timestamptz,
    end_time            timestamptz,
    energy_kwh          numeric,
    cost_total          numeric,
    cost_total_source   text,
    source              varchar
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
           cs.cost_total_source::text,
           cs.source
    FROM public.charging_sessions cs
    WHERE cs.site_id = ANY(p_depot_ids)
$$;

REVOKE ALL    ON FUNCTION agent_views.sessions(uuid[]) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION agent_views.sessions(uuid[]) TO agent_reader_ts;

-- telemetry_hourly: intentionally NOT exposed in V1.
--
-- `telemetry` is depot-blind at the table level (charger_id was a FK to
-- `public.chargers` which migration 029 dropped from TimescaleDB; the
-- canonical charger roster lives in Supabase now). Without a clean
-- depot-keyed source on the TS side we cannot enforce tenant scoping
-- with a static SECURITY DEFINER function. Re-introducing this view
-- requires either (a) a depot_id column on telemetry, (b) a snapshot
-- cache of station_id → depot_id in TS, or (c) cross-DB read via
-- postgres_fdw. None are done; tracked as follow-up. Until then the
-- agent answers peak-power / SoC questions via the per-session
-- aggregates already exposed in `agent_views.sessions`.

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
DO $outer$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname = 'notification_alerts' AND n.nspname = 'public' AND c.relkind = 'r'
    ) THEN
        EXECUTE 'DROP FUNCTION IF EXISTS agent_views.alerts(uuid[])';
        EXECUTE $body$
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
            AS 'SELECT id, depot_id, created_at, severity_level, status, alert_type
                FROM public.notification_alerts
                WHERE depot_id = ANY(p_depot_ids)'
        $body$;
        EXECUTE 'REVOKE ALL    ON FUNCTION agent_views.alerts(uuid[]) FROM PUBLIC';
        EXECUTE 'GRANT  EXECUTE ON FUNCTION agent_views.alerts(uuid[]) TO agent_reader_ts';
    END IF;
END
$outer$;

-- prices_hourly ──────────────────────────────────────────────────────────
-- ENTSO-E's electricity_prices is keyed by bidding zone (node_id), not
-- depot — joining caller's depot_ids to those zones requires reading
-- sites.tariff_config from Supabase, which the TS-side function cannot
-- do (no cross-DB joins). To avoid a tenant leak (the original draft
-- of this function returned the entire electricity_prices feed to any
-- caller), V1 only exposes the legacy per-depot `prices` table; rows
-- are NULL on the current ENTSO-E-only deployment, and that is the
-- honest answer. Zone-aware pricing for the agent is tracked as a
-- follow-up alongside the cross-DB strategy.
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
AS $$
    SELECT p.depot_id,
           time_bucket('1 hour'::interval, p.time) AS hour,
           AVG(p.price_per_kwh)::numeric AS price_per_kwh,
           MAX(p.currency) AS currency,
           NULL::text AS market_type
    FROM public.prices p
    WHERE p.depot_id = ANY(p_depot_ids)
    GROUP BY p.depot_id, time_bucket('1 hour'::interval, p.time)
$$;

REVOKE ALL    ON FUNCTION agent_views.prices_hourly(uuid[]) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION agent_views.prices_hourly(uuid[]) TO agent_reader_ts;

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
-- Migration 029 added `depot_id` directly to connector_status so we no
-- longer need to JOIN against the chargers shadow table (which 029
-- dropped). Legacy rows where depot_id is NULL are excluded — they have
-- no tenant context to attribute to.
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
AS $$
    SELECT DISTINCT ON (cs.station_id, cs.connector_id)
           cs.station_id,
           cs.connector_id,
           cs.depot_id,
           cs.status,
           cs.error_code,
           cs.timestamp AS last_changed_at
    FROM public.connector_status cs
    WHERE cs.depot_id = ANY(p_depot_ids)
    ORDER BY cs.station_id, cs.connector_id, cs.timestamp DESC
$$;

REVOKE ALL    ON FUNCTION agent_views.connector_status_latest(uuid[]) FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION agent_views.connector_status_latest(uuid[]) TO agent_reader_ts;

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
-- specific columns the orchestrator writes (status, steps_json,
-- duration_ms, final_intent), AND we enforce true append-only semantics
-- on `steps_json` itself: the new JSONB array MUST contain the old
-- array as a prefix. Pruning or rewriting prior step elements is
-- rejected — that's the forensic-integrity contract called out by
-- review (P1 codex, agent SQL S3). status/duration_ms/final_intent are
-- left mutable because the application's error path may legitimately
-- need to overwrite a terminal status if a post-close step fails.
CREATE OR REPLACE FUNCTION agent_runs_restricted_update_guard()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    old_len int;
    i int;
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

    -- steps_json: append-only. The new array must be at least as long
    -- as the old array AND every old element must remain at the same
    -- index. NULL old defaults to empty so first-write goes through.
    IF NEW.steps_json IS DISTINCT FROM OLD.steps_json THEN
        IF NEW.steps_json IS NULL
        OR jsonb_typeof(NEW.steps_json) IS DISTINCT FROM 'array'
        OR jsonb_typeof(COALESCE(OLD.steps_json, '[]'::jsonb)) IS DISTINCT FROM 'array'
        THEN
            RAISE EXCEPTION
                'agent_runs.steps_json must be a JSONB array (got %).',
                jsonb_typeof(NEW.steps_json)
            USING ERRCODE = 'check_violation';
        END IF;
        old_len := jsonb_array_length(COALESCE(OLD.steps_json, '[]'::jsonb));
        IF jsonb_array_length(NEW.steps_json) < old_len THEN
            RAISE EXCEPTION
                'agent_runs.steps_json is append-only: cannot shrink from % '
                'to % elements.',
                old_len, jsonb_array_length(NEW.steps_json)
            USING ERRCODE = 'check_violation';
        END IF;
        FOR i IN 0 .. old_len - 1 LOOP
            IF NEW.steps_json -> i IS DISTINCT FROM OLD.steps_json -> i THEN
                RAISE EXCEPTION
                    'agent_runs.steps_json is append-only: existing step at '
                    'index % cannot be modified.',
                    i
                USING ERRCODE = 'check_violation';
            END IF;
        END LOOP;
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
