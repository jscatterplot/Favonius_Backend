-- Audit trail for optimization pre-flight readiness checks.
--
-- Each row captures a single readiness evaluation that the UI (or operator
-- tooling) requested with `?persist=true`. Polling reads do NOT create rows.
-- The status / missing_inputs / degraded_reasons / assumptions / building_load_source
-- columns mirror the public ReadinessResponse contract one-to-one.

CREATE TABLE IF NOT EXISTS optimization_input_snapshots (
    snapshot_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id             UUID NOT NULL REFERENCES depots(depot_id) ON DELETE CASCADE,
    captured_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    horizon_hours        INTEGER NOT NULL,
    status               VARCHAR(32) NOT NULL,
    missing_inputs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    degraded_reasons     JSONB NOT NULL DEFAULT '[]'::jsonb,
    assumptions          JSONB NOT NULL DEFAULT '{}'::jsonb,
    building_load_source VARCHAR(32) NOT NULL,
    CONSTRAINT optimization_input_snapshots_status_valid
        CHECK (status IN ('ready', 'degraded', 'not_ready')),
    CONSTRAINT optimization_input_snapshots_building_load_source_valid
        CHECK (building_load_source IN ('meter', 'forecast_fallback', 'absent')),
    CONSTRAINT optimization_input_snapshots_horizon_valid
        CHECK (horizon_hours BETWEEN 1 AND 48)
);

CREATE INDEX IF NOT EXISTS optimization_input_snapshots_depot_captured_idx
    ON optimization_input_snapshots (depot_id, captured_at DESC);
