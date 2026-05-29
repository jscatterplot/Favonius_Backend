-- Migration 047: widen agent_views.alerts with the human title + structured cause.
--
-- The depot chat agent's SQL mode could say *that* an alert fired but not *why*:
-- the V1 agent_views.alerts (migrations/042_agent_views_ts.sql) exposed only
-- alert_id / depot_id / created_at / severity_level / status / alert_type. The
-- cause already lives on notification_alerts (migration 022):
--   * `title`  TEXT  — the human one-liner the fault trigger composes, e.g.
--                      "Charger CP-07 connector 1: Faulted".
--   * `detail` JSONB — structured context, e.g.
--                      {"station_id": "...", "connector_id": 1, "error_code": "GroundFailure"}.
--
-- This migration re-creates agent_views.alerts (CREATE OR REPLACE) adding both
-- columns so the LLM can explain the cause. We add a NEW migration file rather
-- than editing the shipped 042 — every migration re-runs on each deploy and
-- the runner applies files in sorted order, so 047 lands after 042 and the
-- last definition wins (see CLAUDE.md "Migrations").
--
-- Idempotent. Safe to re-run. Guarded on notification_alerts existing
-- (migration 022) exactly like the 042 definition it supersedes.

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
                alert_type       text,
                title            text,
                detail           jsonb
            )
            LANGUAGE sql STABLE SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS 'SELECT id, depot_id, created_at, severity_level, status, alert_type,
                       title, detail
                FROM public.notification_alerts
                WHERE depot_id = ANY(p_depot_ids)'
        $body$;
        EXECUTE 'REVOKE ALL    ON FUNCTION agent_views.alerts(uuid[]) FROM PUBLIC';
        EXECUTE 'GRANT  EXECUTE ON FUNCTION agent_views.alerts(uuid[]) TO agent_reader_ts';
    END IF;
END
$outer$;
