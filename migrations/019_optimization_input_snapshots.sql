-- Optimization input snapshots: full input bundle persisted before each
-- optimization run. Captures organization_id, depot/vehicles/chargers config,
-- charger_vehicle_access matrix, schedules, prices, telemetry, building load
-- (with explicit source), weather features, incoming vehicles, and the
-- assumptions / missing inputs that drove the readiness verdict.
--
-- Lifecycle: a row is INSERTed before the solver runs. If the run completes,
-- run_id is back-filled to link the snapshot to its optimization_runs row.
-- Snapshots survive solver crashes/timeouts so they are useful for replay
-- and post-mortem debugging.

CREATE TABLE IF NOT EXISTS optimization_input_snapshots (
    snapshot_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id             UUID NOT NULL,
    organization_id      UUID,
    run_id               UUID REFERENCES optimization_runs(run_id) ON DELETE SET NULL,
    captured_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    horizon_start        TIMESTAMPTZ NOT NULL,
    horizon_end          TIMESTAMPTZ NOT NULL,
    readiness_status     VARCHAR(20) NOT NULL
        CHECK (readiness_status IN ('ready', 'degraded', 'not_ready')),
    building_load_source VARCHAR(32) NOT NULL
        CHECK (building_load_source IN ('meter', 'forecast_fallback', 'absent')),
    missing_inputs       JSONB NOT NULL DEFAULT '[]'::jsonb,
    assumptions          JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload              JSONB NOT NULL,
    payload_schema       VARCHAR(16) NOT NULL DEFAULT 'v1'
);

CREATE INDEX IF NOT EXISTS idx_opt_input_snapshots_depot_time
    ON optimization_input_snapshots (depot_id, captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_opt_input_snapshots_run
    ON optimization_input_snapshots (run_id)
    WHERE run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_opt_input_snapshots_readiness
    ON optimization_input_snapshots (depot_id, readiness_status, captured_at DESC);
