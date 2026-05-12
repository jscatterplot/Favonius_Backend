-- Migration 036: persist raw meter register values on charging_sessions.
--
-- Background: OCPP 1.6 StartTransaction carries meterStart (Wh) and
-- StopTransaction carries meterStop (Wh). The legacy adapter was computing
-- the delta in Python from an in-memory map keyed by transaction_id, which
-- silently dropped the value whenever the WS handler restarted between
-- Start and Stop (a common scenario covered by migration 013 recovery).
--
-- Storing both registers on the row makes the close-session UPDATE
-- compute energy_delivered_kwh from the stored meterStart, so the delta
-- survives a handler restart. Keeping meter_stop_wh too lets billing
-- reconciliation prove the delta against the raw register pair without
-- replaying telemetry.
--
-- Idempotent (ADD COLUMN IF NOT EXISTS); safe to re-run.

ALTER TABLE charging_sessions
    ADD COLUMN IF NOT EXISTS meter_start_wh BIGINT,
    ADD COLUMN IF NOT EXISTS meter_stop_wh  BIGINT;

COMMENT ON COLUMN charging_sessions.meter_start_wh IS
    'Raw Wh register reading from OCPP 1.6 StartTransaction.meterStart. Persisted so the StopTransaction handler can recompute energy_delivered_kwh after a handler restart.';

COMMENT ON COLUMN charging_sessions.meter_stop_wh IS
    'Raw Wh register reading from OCPP 1.6 StopTransaction.meterStop. Stored alongside meter_start_wh so the delta is auditable independently of energy_delivered_kwh.';
