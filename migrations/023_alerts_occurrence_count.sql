-- 023_alerts_occurrence_count.sql
--
-- Adds occurrence_count to notification_alerts and updates the
-- fn_alerts_on_connector_status trigger to increment it on dedup conflicts.
--
-- The frontend's AlertItemSchema requires occurrence_count to render
-- "happened 17 times" copy. Until now the dedup logic only bumped
-- last_occurrence_at; the count was implicit and uncounted.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS, CREATE OR REPLACE TRIGGER FUNCTION.

ALTER TABLE notification_alerts
    ADD COLUMN IF NOT EXISTS occurrence_count BIGINT NOT NULL DEFAULT 1;

COMMENT ON COLUMN notification_alerts.occurrence_count IS
    'Number of times this dedup_key has fired since the row was inserted. '
    'Reset to 1 when a resolved row is succeeded by a fresh INSERT.';

-- Replace the connector_status trigger so existing UPSERT bumps the counter.
CREATE OR REPLACE FUNCTION fn_alerts_on_connector_status() RETURNS trigger AS $$
DECLARE
    org_id    UUID;
    dep_id    UUID;
    sev       TEXT;
    alert_id  UUID;
    dk        TEXT;
BEGIN
    dk := format('charger_fault:%s:%s', NEW.station_id, NEW.connector_id);

    IF NEW.status NOT IN ('Faulted', 'Unavailable') THEN
        UPDATE notification_alerts
           SET status = 'resolved',
               resolved_at = NOW(),
               updated_at = NOW()
         WHERE dedup_key = dk
           AND status != 'resolved';
        RETURN NEW;
    END IF;

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

DROP TRIGGER IF EXISTS trg_alerts_on_connector_status ON connector_status;

CREATE TRIGGER trg_alerts_on_connector_status
    AFTER INSERT ON connector_status
    FOR EACH ROW
    EXECUTE FUNCTION fn_alerts_on_connector_status();
