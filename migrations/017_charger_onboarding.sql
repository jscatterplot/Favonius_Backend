-- Charger onboarding metadata, idempotency replay, and auth failure events.
-- Guard: chargers is a Supabase shadow table dropped by migration 029; skip
-- gracefully when absent (same pattern as migrations 006 and 011).

DO $$
BEGIN
    IF to_regclass('public.chargers') IS NULL THEN
        RAISE NOTICE 'Skipping 017 charger columns/indexes: table public.chargers does not exist';
        RETURN;
    END IF;

    ALTER TABLE chargers
        ADD COLUMN IF NOT EXISTS display_name VARCHAR(255),
        ADD COLUMN IF NOT EXISTS vendor VARCHAR(128),
        ADD COLUMN IF NOT EXISTS model VARCHAR(128),
        ADD COLUMN IF NOT EXISTS serial_number VARCHAR(128),
        ADD COLUMN IF NOT EXISTS firmware VARCHAR(128),
        ADD COLUMN IF NOT EXISTS connector_count INTEGER NOT NULL DEFAULT 1,
        ADD COLUMN IF NOT EXISTS connector_ids JSONB NOT NULL DEFAULT '[1]'::jsonb,
        ADD COLUMN IF NOT EXISTS network_notes TEXT,
        ADD COLUMN IF NOT EXISTS auth_required BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'chargers_connector_count_positive'
    ) THEN
        ALTER TABLE chargers
            ADD CONSTRAINT chargers_connector_count_positive CHECK (connector_count >= 1);
    END IF;

    CREATE INDEX IF NOT EXISTS idx_chargers_ocpp_id ON chargers (ocpp_id);
END $$;

CREATE TABLE IF NOT EXISTS charger_onboarding_idempotency (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id    UUID NOT NULL REFERENCES organizations (organization_id) ON DELETE CASCADE,
    user_id            UUID NOT NULL,
    endpoint           TEXT NOT NULL,
    idempotency_key    TEXT NOT NULL,
    request_hash       TEXT NOT NULL,
    response_json      JSONB,
    status_code        INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at         TIMESTAMPTZ NOT NULL,
    UNIQUE (organization_id, endpoint, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_charger_onboarding_idempotency_expiry
    ON charger_onboarding_idempotency (expires_at);

COMMENT ON COLUMN charger_onboarding_idempotency.response_json IS
    'Temporary replay payload for charger onboarding. May include one-time plaintext credential until expires_at.';

CREATE TABLE IF NOT EXISTS security_events (
    id              SERIAL PRIMARY KEY,
    station_id      VARCHAR(255) NOT NULL,
    event_type      VARCHAR(100) NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL,
    tech_info       TEXT,
    additional_info JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_security_events_station_id ON security_events (station_id);
CREATE INDEX IF NOT EXISTS idx_security_events_type ON security_events (event_type);
CREATE INDEX IF NOT EXISTS idx_security_events_timestamp ON security_events (timestamp);
