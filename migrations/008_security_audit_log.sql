-- Migration 008: Security Audit Log
-- Required by: Lithuanian Article 73-3 (NKSC audit), NIS2 Article 21
--
-- Creates a tamper-evident security audit log table for recording
-- authentication events, geo-blocking decisions, rate limit violations,
-- and other security-relevant events.
--
-- Both the Main API and WebSocket Handler services write to this table.
-- Minimum retention: 90 days (NKSC audit methodology requirement).

-- Create sequence for tamper-evident sequential numbering
CREATE SEQUENCE IF NOT EXISTS security_audit_seq;

-- Create the security audit log table
CREATE TABLE IF NOT EXISTS security_audit_log (
    id              UUID DEFAULT gen_random_uuid() NOT NULL,
    seq_number      BIGINT NOT NULL DEFAULT nextval('security_audit_seq'),
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    event_type      TEXT NOT NULL,
    source_ip       TEXT,
    user_id         TEXT,
    station_id      TEXT,
    resource        TEXT,
    country_code    TEXT,
    details         JSONB DEFAULT '{}'::jsonb,
    service         TEXT NOT NULL DEFAULT 'unknown',
    PRIMARY KEY (id, timestamp)
);

-- Convert to TimescaleDB hypertable for efficient time-series queries
-- chunk_time_interval = 1 day for audit logs
SELECT create_hypertable(
    'security_audit_log',
    'timestamp',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);

-- Index for querying by event type within a time range (auditor's primary query pattern)
CREATE INDEX IF NOT EXISTS idx_security_audit_event_type_ts
    ON security_audit_log (event_type, timestamp DESC);

-- Index for querying by source IP (incident investigation)
CREATE INDEX IF NOT EXISTS idx_security_audit_source_ip_ts
    ON security_audit_log (source_ip, timestamp DESC)
    WHERE source_ip IS NOT NULL;

-- Index for querying by station ID (OCPP security events)
CREATE INDEX IF NOT EXISTS idx_security_audit_station_ts
    ON security_audit_log (station_id, timestamp DESC)
    WHERE station_id IS NOT NULL;

-- Index for sequential number (tamper-evidence verification)
CREATE INDEX IF NOT EXISTS idx_security_audit_seq
    ON security_audit_log (seq_number);

-- Set retention policy: 90 days minimum per NKSC audit requirements
-- Older data is automatically dropped by TimescaleDB
SELECT add_retention_policy(
    'security_audit_log',
    INTERVAL '90 days',
    if_not_exists => TRUE
);

-- Valid event types (documented, not enforced as enum for flexibility):
-- AUTH_SUCCESS       - Successful authentication
-- AUTH_FAILURE       - Failed authentication attempt
-- GEO_BLOCK         - Request blocked by geo-blocking
-- RATE_LIMIT        - Rate limit exceeded
-- ACCESS_DENIED     - Authorization failure (authenticated but not permitted)
-- CONFIG_CHANGE     - Security-relevant configuration change
-- CONNECTION_REJECT - WebSocket connection rejected (limit, subprotocol, etc.)
-- STATION_LOCKOUT   - Charging station locked out after repeated failures
-- INCIDENT_DETECT   - Automated incident detection trigger
