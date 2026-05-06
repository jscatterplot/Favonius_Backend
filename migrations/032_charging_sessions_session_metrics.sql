-- Migration 032: Backfill charging_sessions session-metric columns missing
-- from earlier migrations.
--
-- Background: the canonical charging_sessions schema lives in
-- src/websocket_handler/timescale_schema.py::TimescaleSchema._create_tables,
-- but its `CREATE TABLE IF NOT EXISTS` becomes a no-op once
-- migrations/013_recovery.sql has already created the table from
-- scripts/run_migrations.py. The handler's `_apply_compatibility_migrations`
-- only patches in a subset (cost_total, site_id, revenue_v2g, the
-- live-status fields and the import_* provenance columns), leaving
-- energy_delivered_kwh and several other session-metric columns missing
-- on any DB initialised purely via the migration runner.
--
-- Symptom on TigerCloud favonius-timeseries (DB initialised by the
-- migration runner, never touched by the WS handler bootstrap path):
--
--   asyncpg.exceptions.UndefinedColumnError:
--     column "energy_delivered_kwh" of relation "charging_sessions"
--     does not exist
--
-- This migration brings the migration-controlled schema in line with the
-- WS handler's CREATE TABLE so migrations/ remains the single source of
-- truth. All statements are additive and idempotent (ADD COLUMN IF NOT
-- EXISTS), so re-running on environments that already have the columns
-- (e.g. dev DBs that booted the WS handler first) is a no-op.

ALTER TABLE charging_sessions
    ADD COLUMN IF NOT EXISTS energy_delivered_kwh    DECIMAL(10,3),
    ADD COLUMN IF NOT EXISTS energy_received_kwh     DECIMAL(10,3),
    ADD COLUMN IF NOT EXISTS cost_total              DECIMAL(10,2),
    ADD COLUMN IF NOT EXISTS revenue_v2g             DECIMAL(10,2),
    ADD COLUMN IF NOT EXISTS start_soc_percent       DECIMAL(5,2),
    ADD COLUMN IF NOT EXISTS end_soc_percent         DECIMAL(5,2),
    ADD COLUMN IF NOT EXISTS max_charge_power_kw     DECIMAL(8,2),
    ADD COLUMN IF NOT EXISTS max_discharge_power_kw  DECIMAL(8,2),
    ADD COLUMN IF NOT EXISTS operation_mode          VARCHAR(50),
    ADD COLUMN IF NOT EXISTS fleet_operator_id       UUID,
    ADD COLUMN IF NOT EXISTS sync_status             VARCHAR(20) DEFAULT 'pending';

COMMENT ON COLUMN charging_sessions.energy_delivered_kwh IS
    'Total energy delivered to the vehicle for this session, in kWh. Source: OCPP StopTransaction meter delta for live rows; "Usage Kw" cell from the XLSX export for imported rows.';
COMMENT ON COLUMN charging_sessions.cost_total IS
    'Customer-facing cost or revenue for the session, in the depot currency. Source: tariff x energy_delivered_kwh for live rows; "Revenue" cell from the XLSX export for imported rows.';
