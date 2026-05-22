-- Migration 042: Charger-side session log imports + reconciliation.
--
-- Lets operators pull a charger's own session log (OCPP 1.6 GetDiagnostics,
-- OCPP 2.0.1 GetLog, or a manual upload) and compare it against what the
-- backend recorded in charging_sessions / telemetry. Mirrors the
-- diagnostic-source enum pattern from migration 040 (session_cost_provenance):
-- every reconciliation row carries an explicit `source` so callers can tell
-- "reconciled" apart from "no log entries" apart from "parse failed".
--
-- Wire-up:
--   1. POST /admin/depots/{id}/chargers/{cid}/sessions/{sid}/fetch_logs
--      → dispatch_get_diagnostics() enqueues charging_command_queue row
--        with command_type='get_diagnostics' and inserts a charger_log_imports
--        row in status='requested'.
--   2. WS handler's ChargingCommandQueueConsumer drains, calls
--      FleetChargePoint.get_diagnostics(location=<signed URL>), the charger
--      uploads to POST /internal/charger_logs/upload?token=<HMAC>.
--   3. Upload endpoint stores raw_payload, flips status to 'received',
--      schedules parse + reconcile as a post-commit asyncio task.
--   4. Per-vendor parser yields rows into charger_session_log_entries.
--   5. reconcile_session_log writes one session_log_reconciliations row.
--
-- All three tables live on TimescaleDB alongside charging_sessions and
-- telemetry — the same pool (db_pools.ts) the cost calculator uses.
--
-- Idempotent (safe to re-run).

-- ---------------------------------------------------------------------------
-- 1. Allow 'get_diagnostics' and 'get_log' command_type values in the queue.
-- ---------------------------------------------------------------------------
ALTER TABLE charging_command_queue
    DROP CONSTRAINT IF EXISTS charging_command_queue_command_type_chk;

ALTER TABLE charging_command_queue
    ADD CONSTRAINT charging_command_queue_command_type_chk
    CHECK (command_type IN (
        'set_charging_profile',
        'remote_reset',
        'remote_start_transaction',
        'get_diagnostics',
        'get_log'
    ));

-- ---------------------------------------------------------------------------
-- 2. charger_log_imports — one row per fetch attempt.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS charger_log_imports (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- OCPP charge-point id (matches charging_command_queue.charge_point_id
    -- and charging_sessions.station_id). TEXT, not FK — chargers may be
    -- decommissioned in Supabase while their historical log imports remain.
    station_id          TEXT        NOT NULL,
    -- charging_stations.id (Supabase UUID) at the moment the import was
    -- requested. NULL when the station hasn't been onboarded yet.
    charger_id          UUID,
    connector_id        INTEGER,
    -- The session this import is being compared against. NULL for
    -- depot-wide pulls (a future use case; today every dispatch path
    -- supplies a session_id).
    session_id          UUID,
    vendor              TEXT,
    -- Where the import came from. Mirrors the provenance pattern from
    -- migration 040: explicit string, not a smalltype.
    source              TEXT        NOT NULL
        CHECK (source IN ('get_diagnostics', 'get_log', 'manual_upload')),
    status              TEXT        NOT NULL DEFAULT 'requested'
        CHECK (status IN (
            'requested',   -- queued, not yet picked up by WS consumer
            'uploading',   -- charger ack'd, upload pending
            'received',    -- raw_payload populated
            'parsed',      -- entries extracted
            'reconciled',  -- session_log_reconciliations row written
            'failed',      -- terminal: see error_message
            'expired'      -- terminal: charger never uploaded in time
        )),
    requested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    received_at         TIMESTAMPTZ,
    parsed_at           TIMESTAMPTZ,
    reconciled_at       TIMESTAMPTZ,
    -- We never store the upload-URL token plaintext. Compare
    -- ``sha256(token)`` against this hash on the upload endpoint to
    -- defeat replay across imports.
    upload_token_hash   TEXT,
    file_name           TEXT,
    file_size_bytes     BIGINT,
    content_sha256      TEXT,
    raw_payload         BYTEA,
    error_message       TEXT,
    -- Stop duplicate "fetch logs for this session via GetDiagnostics" calls
    -- from creating two rows when the operator double-clicks the button.
    -- (session_id, source) is enough: manual_upload + get_diagnostics may
    -- coexist intentionally.
    idempotency_key     TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_charger_log_imports_idempotency
    ON charger_log_imports (idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_charger_log_imports_session
    ON charger_log_imports (session_id, requested_at DESC)
    WHERE session_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_charger_log_imports_station
    ON charger_log_imports (station_id, requested_at DESC);

CREATE INDEX IF NOT EXISTS idx_charger_log_imports_status
    ON charger_log_imports (status, requested_at)
    WHERE status IN ('requested', 'uploading', 'received');

COMMENT ON TABLE charger_log_imports IS
    'Charger-side session log imports. One row per GetDiagnostics/GetLog/manual fetch. raw_payload may be nulled by retention policy after CHARGER_LOG_RETENTION_DAYS; metadata + downstream normalized entries are kept.';

-- ---------------------------------------------------------------------------
-- 3. charger_session_log_entries — normalized per-vendor parse output.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS charger_session_log_entries (
    time            TIMESTAMPTZ NOT NULL,
    station_id      TEXT        NOT NULL,
    charger_id      UUID,
    connector_id    INTEGER,
    transaction_id  BIGINT,
    log_import_id   UUID        NOT NULL REFERENCES charger_log_imports(id) ON DELETE CASCADE,
    soc             DOUBLE PRECISION,
    charging_kw     DOUBLE PRECISION,
    energy_kwh      DOUBLE PRECISION,
    raw_fields      JSONB
);

-- Hypertable for the parsed entries — same partition shape as `telemetry`
-- so the reconciler's JOINs are symmetric.
SELECT create_hypertable(
    'charger_session_log_entries',
    'time',
    if_not_exists => TRUE,
    chunk_time_interval => INTERVAL '7 days'
);

CREATE INDEX IF NOT EXISTS idx_charger_session_log_entries_session
    ON charger_session_log_entries (station_id, transaction_id, time DESC);

CREATE INDEX IF NOT EXISTS idx_charger_session_log_entries_import
    ON charger_session_log_entries (log_import_id, time);

COMMENT ON TABLE charger_session_log_entries IS
    'Normalized entries extracted from charger-side session logs by per-vendor parsers. Shape mirrors telemetry so reconciliation queries can compare apples-to-apples. Indexed on (station_id, transaction_id, time) to match the telemetry join shape.';

-- ---------------------------------------------------------------------------
-- 4. session_log_reconciliations — one row per reconciler run.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_log_reconciliations (
    id                      UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              UUID        NOT NULL,
    log_import_id           UUID        NOT NULL REFERENCES charger_log_imports(id) ON DELETE CASCADE,
    computed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Energy from our side (telemetry-integrated) and the charger side
    -- (sum of entry deltas). Either may be NULL when the corresponding
    -- data is missing — the source enum disambiguates.
    our_energy_kwh          DOUBLE PRECISION,
    charger_energy_kwh      DOUBLE PRECISION,
    energy_delta_pct        DOUBLE PRECISION,
    our_duration_s          BIGINT,
    charger_duration_s      BIGINT,
    our_start_time          TIMESTAMPTZ,
    charger_start_time      TIMESTAMPTZ,
    our_end_time            TIMESTAMPTZ,
    charger_end_time        TIMESTAMPTZ,
    -- Mirrors charging_sessions.cost_total_source (migration 040): the
    -- single column readers and tests gate on.
    source                  TEXT        NOT NULL
        CHECK (source IN (
            'reconciled',      -- both sides present, deltas computed
            'partial',         -- one side missing fields; report what we have
            'no_log_entries',  -- charger upload had no per-session rows
            'no_session',      -- session_id resolves to nothing
            'parse_failed'     -- parser raised or returned nothing useful
        )),
    notes                   JSONB
);

-- One reconciliation row per (session, import) pair. Re-running the
-- reconciler updates in place rather than appending.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_session_log_reconciliations_session_import
    ON session_log_reconciliations (session_id, log_import_id);

CREATE INDEX IF NOT EXISTS idx_session_log_reconciliations_session
    ON session_log_reconciliations (session_id, computed_at DESC);

COMMENT ON TABLE session_log_reconciliations IS
    'Per-session, per-import reconciliation deltas between our recorded session and the charger-side log. source enum mirrors charging_sessions.cost_total_source so the same diagnostic style applies.';
