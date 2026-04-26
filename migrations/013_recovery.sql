-- Migration 013: Cross-restart recovery for the legacy OCPP WebSocket handler.
--
-- Adds the storage required so a handler restart, charger reboot, or transient
-- disconnect does not lose the in-flight session state held only in process
-- memory today (see src/websocket_handler/transaction_manager.py:111 and
-- src/websocket_handler/ocpp16_adapter.py).
--
--   1. charging_command_queue — durable buffer for SetChargingProfile commands
--      issued while a charger is offline. Replayed on next BootNotification.
--   2. charging_sessions.last_seen_at + transaction_id — needed so the boot
--      reload in OCPP16Session can repopulate FleetChargePoint.transactions
--      and StopTransaction does not orphan rows.
--
-- Idempotent: every statement uses IF NOT EXISTS or equivalent.
-- Reference: PRD Section 5 (recovery), 8.4 (resilience), 9.1 (OCPP).

-- ---------------------------------------------------------------------------
-- 1. charging_sessions: ensure table + recovery columns exist
-- ---------------------------------------------------------------------------
-- The canonical schema lives in src/websocket_handler/timescale_schema.py
-- (created at handler startup). The CREATE below is a defensive backstop so
-- migration-only environments (tests, fresh DBs) have what we need; it
-- intentionally includes only the columns the legacy handler reads/writes.

CREATE TABLE IF NOT EXISTS charging_sessions (
    session_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    station_id    VARCHAR(255) NOT NULL,
    evse_id       INTEGER NOT NULL,
    connector_id  INTEGER NOT NULL,
    vehicle_id    VARCHAR(255),
    id_token      VARCHAR(255),
    start_time    TIMESTAMPTZ NOT NULL,
    end_time      TIMESTAMPTZ,
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- transaction_id: OCPP 1.6 integer transactionId (sourced from the
-- ocpp_transaction_id sequence in migration 012). Nullable for legacy 2.0.1
-- rows which use string IDs in transaction_events.
ALTER TABLE charging_sessions
    ADD COLUMN IF NOT EXISTS transaction_id BIGINT;

-- last_seen_at: stamped by the connection-close hook in server.py so we can
-- distinguish a stale-but-open session from a live one after a handler crash.
ALTER TABLE charging_sessions
    ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ;

-- Fast lookup of open sessions for a given station (boot reload path).
CREATE INDEX IF NOT EXISTS charging_sessions_open_idx
    ON charging_sessions (station_id)
    WHERE end_time IS NULL;

-- Used by transaction_id → row lookups during StopTransaction.
CREATE INDEX IF NOT EXISTS charging_sessions_txid_idx
    ON charging_sessions (transaction_id)
    WHERE transaction_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. charging_command_queue — offline command buffer
-- ---------------------------------------------------------------------------
-- Distinct from charging_commands (per-optimization-run audit, FK to
-- chargers.charger_id UUID). This table is keyed by the OCPP charge-point
-- id (TEXT) so the queue is usable without a chargers row, and tracks the
-- command's delivery lifecycle independently of the optimization run.

CREATE TABLE IF NOT EXISTS charging_command_queue (
    queue_id         BIGSERIAL PRIMARY KEY,
    charge_point_id  TEXT NOT NULL,
    connector_id     INTEGER NOT NULL,
    command_type     TEXT NOT NULL,                              -- 'set_charging_profile'
    payload          JSONB NOT NULL,                             -- full OCPP profile dict
    status           TEXT NOT NULL DEFAULT 'pending',            -- pending|acked|failed|expired
    enqueued_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at          TIMESTAMPTZ,
    acked_at         TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ NOT NULL,
    last_error       TEXT,
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT charging_command_queue_status_chk
        CHECK (status IN ('pending', 'acked', 'failed', 'expired'))
);

-- Replay path: pending commands for a given cp_id, oldest first.
CREATE INDEX IF NOT EXISTS charging_command_queue_replay_idx
    ON charging_command_queue (charge_point_id, status, enqueued_at);
