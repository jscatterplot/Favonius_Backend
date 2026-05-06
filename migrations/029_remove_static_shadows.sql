-- Migration 029: Remove static shadow tables from TimescaleDB.
--
-- Background: Static reference data (sites, vehicles, charging_stations,
-- organizations) lives in Supabase (`pools.static`). Migrations 001 and 015
-- created local shadow copies of these tables in TimescaleDB (`pools.ts`);
-- they were never populated in production, causing silent no-ops in the
-- connector_status trigger and FK violations on background writes.
--
-- Migration 028 already dropped the 4 FK constraints that were actively
-- breaking inserts. This migration finishes the cleanup:
--
--   1. Add organization_id / depot_id context columns to connector_status so
--      the alerts trigger can resolve tenant context WITHOUT the shadow tables.
--
--   2. Replace fn_alerts_on_connector_status to read org/depot directly from
--      the new connector_status columns instead of JOINing chargers+depots.
--
--   3. Drop all static shadow tables (CASCADE handles FK constraint removal on
--      operational tables; the tables themselves are kept, now with unvalidated
--      UUID reference columns — same posture established by migration 028 for
--      notification_alerts and optimization_input_snapshots).
--
-- After this migration:
--   - connector_status rows written with organization_id+depot_id set will
--     fire alerts as before.
--   - Legacy inserts (NULL context) bail silently — identical to current
--     production behaviour where the shadow join returns no rows.
--   - Shadow tables are gone; the assembler reads all static data from Supabase.

-- ---------------------------------------------------------------------------
-- 1. Add tenant-context columns to connector_status
-- ---------------------------------------------------------------------------

ALTER TABLE connector_status
    ADD COLUMN IF NOT EXISTS organization_id UUID,
    ADD COLUMN IF NOT EXISTS depot_id        UUID;

COMMENT ON COLUMN connector_status.organization_id IS
    'Resolved from Supabase at insert time by the OCPP handler. NULL for '
    'legacy inserts that predate this migration; trigger bails silently.';

COMMENT ON COLUMN connector_status.depot_id IS
    'Supabase sites.id for the depot that owns this charger.';

-- ---------------------------------------------------------------------------
-- 2. Replace the trigger function — read context from the row, no JOINs
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION fn_alerts_on_connector_status() RETURNS trigger AS $$
DECLARE
    org_id    UUID;
    dep_id    UUID;
    sev       TEXT;
    alert_id  UUID;
    dk        TEXT;
BEGIN
    dk := format('charger_fault:%s:%s', NEW.station_id, NEW.connector_id);

    -- Recovery path: any non-fault status resolves an existing active alert.
    IF NEW.status NOT IN ('Faulted', 'Unavailable') THEN
        UPDATE notification_alerts
           SET status = 'resolved',
               resolved_at = NOW(),
               updated_at = NOW()
         WHERE dedup_key = dk
           AND status != 'resolved';
        RETURN NEW;
    END IF;

    -- Tenant context comes from the row itself; bail silently when absent
    -- (legacy inserts or connectors not yet onboarded).
    org_id := NEW.organization_id;
    dep_id := NEW.depot_id;

    IF org_id IS NULL THEN
        RETURN NEW;
    END IF;

    sev := CASE NEW.status WHEN 'Faulted' THEN 'critical' ELSE 'warning' END;

    INSERT INTO notification_alerts (
        organization_id, depot_id, alert_type, severity, title, detail, dedup_key
    ) VALUES (
        org_id,
        dep_id,
        'charger_fault',
        sev,
        format('Charger %s connector %s: %s', NEW.station_id, NEW.connector_id, NEW.status),
        jsonb_build_object(
            'station_id',   NEW.station_id,
            'connector_id', NEW.connector_id,
            'status',       NEW.status,
            'error_code',   NEW.error_code
        ),
        dk
    )
    ON CONFLICT (organization_id, dedup_key) WHERE status != 'resolved'
    DO UPDATE SET
        last_occurrence_at = NOW(),
        occurrence_count   = notification_alerts.occurrence_count + 1,
        severity           = EXCLUDED.severity,
        title              = EXCLUDED.title,
        detail             = EXCLUDED.detail,
        updated_at         = NOW()
    RETURNING id INTO alert_id;

    PERFORM pg_notify(
        'notification_alerts_new',
        jsonb_build_object(
            'alert_id',        alert_id,
            'organization_id', org_id,
            'depot_id',        dep_id,
            'alert_type',      'charger_fault',
            'severity',        sev,
            'dedup_key',       dk
        )::text
    );

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger already exists from migration 022/023; CREATE OR REPLACE FUNCTION
-- above handles the function. Re-create the trigger to pick up new definition.
DROP TRIGGER IF EXISTS trg_alerts_on_connector_status ON connector_status;

CREATE TRIGGER trg_alerts_on_connector_status
    AFTER INSERT ON connector_status
    FOR EACH ROW
    EXECUTE FUNCTION fn_alerts_on_connector_status();

-- ---------------------------------------------------------------------------
-- 3. Drop static shadow tables (CASCADE removes referencing FK constraints;
--    operational tables are kept with unvalidated UUID columns).
-- ---------------------------------------------------------------------------

-- Leaf tables first to minimise CASCADE side-effects.
DROP TABLE IF EXISTS charger_vehicle_access  CASCADE;
DROP TABLE IF EXISTS user_organizations      CASCADE;

-- schedules is the TimescaleDB shadow of Supabase schedules; operational
-- schedule data lives in Supabase and is queried via pools.static.
DROP TABLE IF EXISTS schedules               CASCADE;

-- Second-level tables that reference depots.
DROP TABLE IF EXISTS battery_storage         CASCADE;
DROP TABLE IF EXISTS chargers                CASCADE;
DROP TABLE IF EXISTS vehicles                CASCADE;

-- Root shadow tables.
DROP TABLE IF EXISTS depots                  CASCADE;
DROP TABLE IF EXISTS organizations           CASCADE;
