-- Migration 036: Persist OCPP meter readings on charging_sessions.
--
-- Background: OCPP 1.6 StartTransaction carries `meterStart` (Wh) and
-- StopTransaction carries `meterStop` (Wh). The handler historically
-- forwarded both to the main API push path but never persisted them on
-- the `charging_sessions` row. Two consequences:
--
--   1. On a handler restart between StartTransaction and StopTransaction,
--      the in-memory meter_start is lost. fetch_open_sessions (mig 013)
--      could re-attach the transaction_id but had nothing to subtract
--      from on close, so energy_delivered_kwh was never computed.
--   2. Even without restart, the close path only stamped `end_time`
--      (timescale_client.py::close_open_session) — `energy_delivered_kwh`
--      was left NULL for every live row.
--
-- This migration adds nullable Wh columns so the start path can stash
-- meter_start in the DB and the close path can write meter_stop +
-- compute energy_delivered_kwh atomically.
--
-- No backfill: rows closed before this migration keep their NULL
-- energy_delivered_kwh. Forward-only fix.

ALTER TABLE charging_sessions
    ADD COLUMN IF NOT EXISTS meter_start_wh BIGINT,
    ADD COLUMN IF NOT EXISTS meter_stop_wh  BIGINT;

COMMENT ON COLUMN charging_sessions.meter_start_wh IS
    'OCPP StartTransaction.meterStart in Wh. Persisted so a handler restart between Start and Stop can still compute the energy delta. NULL on legacy rows and imported rows.';
COMMENT ON COLUMN charging_sessions.meter_stop_wh IS
    'OCPP StopTransaction.meterStop in Wh. Persisted alongside the computed energy_delivered_kwh as a billing-grade audit trail.';
