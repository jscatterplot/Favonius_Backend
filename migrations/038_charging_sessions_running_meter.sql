-- Migration 038: Running meter register + stale-session index on charging_sessions.
--
-- Background: migration 036 added meter_start_wh / meter_stop_wh so a handler
-- restart between Start and Stop can still compute the energy delta on close.
-- But the close path is the ONLY writer of meter_stop_wh — and OCPP 1.6 chargers
-- can drop StopTransaction entirely (EVDisconnected without a final stop,
-- network loss before stop, charger crash) or send meterStop=0 / NULL. In
-- production this leaves charging_sessions rows with end_time IS NULL forever
-- and energy_delivered_kwh = NULL, blocking accounting.
--
-- This migration adds two columns that the MeterValues ingest path will
-- update on every sample carrying Energy.Active.Import.Register:
--
--   * last_meter_wh        — most recent cumulative register reading (Wh)
--   * last_meter_seen_at   — when that reading was recorded
--
-- Combined with the StopTransaction fallback (parse transactionData),
-- these become the source of truth for an orphan recovery job that
-- closes stale rows using last_meter_wh as meter_stop_wh.
--
-- The partial index on (last_seen_at) WHERE end_time IS NULL supports
-- the recovery job scan. WebSocket close already stamps last_seen_at
-- (mark_sessions_seen), so the index has near-zero write overhead.

ALTER TABLE charging_sessions
    ADD COLUMN IF NOT EXISTS last_meter_wh      BIGINT,
    ADD COLUMN IF NOT EXISTS last_meter_seen_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS stop_reason        VARCHAR(64);

COMMENT ON COLUMN charging_sessions.last_meter_wh IS
    'Most recent Energy.Active.Import.Register reading seen in MeterValues (Wh). Monotonic — only advances. Used as meter_stop_wh fallback when StopTransaction is missing or carries meterStop=0/NULL.';
COMMENT ON COLUMN charging_sessions.last_meter_seen_at IS
    'Timestamp of the last_meter_wh write. Distinct from last_seen_at (which is stamped on WebSocket close). NULL until the first MeterValues with a register reading.';
COMMENT ON COLUMN charging_sessions.stop_reason IS
    'Why the session ended. ''EVDisconnected'', ''Local'', ''Remote'' etc. when set by OCPP StopTransaction.reason; ''orphaned_recovered'' when the orphan-recovery job closes a session whose StopTransaction never arrived. NULL on legacy and currently-open rows.';

-- Partial index for the orphan recovery job. The job filters
-- end_time IS NULL AND last_seen_at < NOW() - INTERVAL '...'; this index
-- makes the scan touch only open rows ordered by staleness.
CREATE INDEX IF NOT EXISTS charging_sessions_stale_idx
    ON charging_sessions (last_seen_at)
    WHERE end_time IS NULL;
