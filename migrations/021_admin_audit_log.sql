-- Migration 021: Admin audit_log table
--
-- Purpose: structured audit trail for admin / cross-org / credential events
-- distinct from the security_audit_log hypertable (NKSC / NIS2 compliance log).
--
-- This table records *application-level* admin actions (e.g. cross-org reads
-- by favonius_admin, credential rotations, depot setup edits). It is queried
-- by tenant + actor and by target (depot/charger), so it lives in static
-- (Supabase) DB rather than the time-series hypertable.
--
-- Idempotent: every statement uses IF NOT EXISTS or equivalent.

CREATE TABLE IF NOT EXISTS audit_log (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_user_id     UUID,
    actor_role        VARCHAR(64),
    organization_id   UUID,
    depot_id          UUID,
    action            VARCHAR(64) NOT NULL,
    target_type       VARCHAR(64),
    target_id         VARCHAR(255),
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_audit_log_occurred_at
    ON audit_log (occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_log_actor
    ON audit_log (actor_user_id, occurred_at DESC)
    WHERE actor_user_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_log_action
    ON audit_log (action, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_audit_log_organization
    ON audit_log (organization_id, occurred_at DESC)
    WHERE organization_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_log_depot
    ON audit_log (depot_id, occurred_at DESC)
    WHERE depot_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_audit_log_target
    ON audit_log (target_type, target_id, occurred_at DESC)
    WHERE target_type IS NOT NULL;

COMMENT ON TABLE audit_log IS
    'Application-level admin audit trail. Covers cross-org reads, credential rotations, '
    'and other privileged actions. Distinct from security_audit_log (NKSC hypertable).';

COMMENT ON COLUMN audit_log.action IS
    'Dotted action name, e.g. admin.read, charger.credentials.rotated';

-- Add columns to station_credentials so we can expose configured/created_at/last_rotated_at
-- without ever returning the password_hash.
ALTER TABLE station_credentials
    ADD COLUMN IF NOT EXISTS last_rotated_at TIMESTAMPTZ;
