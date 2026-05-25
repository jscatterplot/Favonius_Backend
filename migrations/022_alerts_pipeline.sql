-- Migration 022: Alerts pipeline (notification_alerts, notification_deliveries,
-- notification_recipients) plus a connector_status trigger that produces
-- charger_fault alerts and PERFORM pg_notify so a LISTEN-based dispatcher
-- can react sub-second.
--
-- See docs/plans/alerts-pipeline.md for the full design and locked decisions.
--
-- Idempotent.

-- ---------------------------------------------------------------------------
-- 1. notification_alerts: depot-scoped aggregator
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notification_alerts (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id       UUID NOT NULL,
    depot_id              UUID,
    alert_type            VARCHAR(64) NOT NULL,
    severity              VARCHAR(16) NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    severity_level        SMALLINT GENERATED ALWAYS AS (
                              CASE severity
                                  WHEN 'critical' THEN 3
                                  WHEN 'warning'  THEN 2
                                  ELSE 1
                              END
                          ) STORED,
    title                 TEXT NOT NULL,
    detail                JSONB NOT NULL DEFAULT '{}'::jsonb,
    dedup_key             TEXT NOT NULL,
    status                VARCHAR(16) NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'acknowledged', 'resolved')),
    first_occurrence_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_occurrence_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_notified_at      TIMESTAMPTZ,
    last_notified_count   SMALLINT NOT NULL DEFAULT 0,
    acknowledged_at       TIMESTAMPTZ,
    acknowledged_by       UUID,
    resolved_at           TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE notification_alerts IS
    'Depot/org-scoped alert aggregator. One active row per (organization_id, dedup_key); '
    'duplicate occurrences bump last_occurrence_at via the connector_status trigger or app code.';

-- Partial unique index: only one non-resolved alert per (org, dedup_key).
-- Once resolved, a new alert with the same dedup_key can be inserted.
CREATE UNIQUE INDEX IF NOT EXISTS uq_notification_alerts_active
    ON notification_alerts (organization_id, dedup_key)
    WHERE status != 'resolved';

-- /depots/{id}/alerts ordering: severity DESC, then most-recent first.
CREATE INDEX IF NOT EXISTS idx_notification_alerts_depot_ranking
    ON notification_alerts (depot_id, status, severity_level DESC, last_occurrence_at DESC)
    WHERE depot_id IS NOT NULL;

-- Dispatcher claim query: status='active' AND (last_notified_at IS NULL OR last_notified_at < now() - interval).
CREATE INDEX IF NOT EXISTS idx_notification_alerts_dispatch
    ON notification_alerts (status, last_notified_at NULLS FIRST);

-- ---------------------------------------------------------------------------
-- 2. notification_recipients: per-org email subscription
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notification_recipients (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id       UUID NOT NULL,
    email                 VARCHAR(255) NOT NULL,
    display_name          VARCHAR(255),
    alert_types           TEXT[] NOT NULL DEFAULT ARRAY['*']::TEXT[],
    min_severity          VARCHAR(16) NOT NULL DEFAULT 'warning'
                              CHECK (min_severity IN ('info', 'warning', 'critical')),
    min_severity_level    SMALLINT GENERATED ALWAYS AS (
                              CASE min_severity
                                  WHEN 'critical' THEN 3
                                  WHEN 'warning'  THEN 2
                                  ELSE 1
                              END
                          ) STORED,
    active                BOOLEAN NOT NULL DEFAULT TRUE,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (organization_id, email)
);

COMMENT ON TABLE notification_recipients IS
    'Per-organization alert subscribers. alert_types=ARRAY[''*''] means all types; '
    'otherwise a subset of recognized alert_type values. min_severity gates which '
    'severity floor the recipient receives.';

CREATE INDEX IF NOT EXISTS idx_notification_recipients_lookup
    ON notification_recipients (organization_id, active, min_severity_level)
    INCLUDE (email, alert_types);

-- ---------------------------------------------------------------------------
-- 3. notification_deliveries: append-only ledger
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notification_deliveries (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_id              UUID NOT NULL REFERENCES notification_alerts (id) ON DELETE CASCADE,
    recipient_id          UUID NOT NULL REFERENCES notification_recipients (id) ON DELETE CASCADE,
    notified_count        SMALLINT NOT NULL,
    channel               VARCHAR(16) NOT NULL DEFAULT 'email',
    provider_message_id   VARCHAR(255),
    status                VARCHAR(16) NOT NULL DEFAULT 'sent'
                              CHECK (status IN ('sent', 'delivered', 'bounced', 'complained', 'failed')),
    status_detail         JSONB,
    sent_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    provider_updated_at   TIMESTAMPTZ,
    UNIQUE (alert_id, recipient_id, notified_count)
);

COMMENT ON TABLE notification_deliveries IS
    'Append-only delivery ledger. UNIQUE (alert_id, recipient_id, notified_count) is '
    'the idempotency anchor: a re-tick that observes the same notified_count cannot '
    'double-INSERT. Webhook handler updates status/provider_updated_at.';

CREATE INDEX IF NOT EXISTS idx_notification_deliveries_provider_msg
    ON notification_deliveries (provider_message_id)
    WHERE provider_message_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_notification_deliveries_alert
    ON notification_deliveries (alert_id, sent_at DESC);

-- ---------------------------------------------------------------------------
-- 4. Trigger on connector_status: produce charger_fault alerts
-- ---------------------------------------------------------------------------
-- Faulted/Unavailable rows UPSERT an active alert keyed by station_id+connector_id;
-- any other status resolves an existing active alert for that charger.
-- pg_notify fires on the 'notification_alerts_new' channel so a LISTEN-based
-- dispatcher can react sub-second.

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

    -- Resolve org/depot via chargers; bail silently if the station is unknown
    -- (e.g. test rows or chargers not yet onboarded).
    SELECT d.organization_id, d.depot_id
      INTO org_id, dep_id
      FROM chargers c
      JOIN depots   d ON d.depot_id = c.depot_id
     WHERE c.ocpp_id = NEW.station_id
     LIMIT 1;

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

-- ---------------------------------------------------------------------------
-- 5. Permissions (mirror migration 001 pattern)
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    EXECUTE 'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO favonius';
    EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO favonius';
EXCEPTION
    WHEN undefined_object THEN
        NULL;
END $$;
