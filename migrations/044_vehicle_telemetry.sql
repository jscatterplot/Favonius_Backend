-- Migration 044: vehicle_telemetry hypertable (telematics SoC/position feed).
--
-- Second source of vehicle SoC, independent of OCPP. A background poller
-- (src/adapters/navirec/poller.py) pulls readings from the fleet telematics
-- provider (Navirec) into this table; StateAssembler._get_vehicle_socs UNIONs
-- it with the charger-side `telemetry` hypertable and keeps the freshest
-- reading per vehicle. This gives the optimizer a live SoC even when a vehicle
-- is unplugged (out on a route or parked but not connected), which directly
-- supports the departure-SoC >= 99% hard constraint.
--
-- Design notes:
--   * `time` is the DEVICE's own reading timestamp (UTC), never now(), so the
--     downstream 15-min freshness check (src/security/data_freshness.py) and
--     the 24h scan bound in the merge query both judge true age. Re-ingesting
--     the same reading is a no-op via ON CONFLICT (vehicle_id, time) DO NOTHING.
--   * No FK to `vehicles`: vehicles live in Supabase (static pool); this table
--     lives in TimescaleDB. `telemetry` follows the same cross-DB convention
--     (bare vehicle_id UUID, no FK).
--   * Column names mirror `telemetry` (soc, location_lat, location_lon) so the
--     UNION in _get_vehicle_socs is symmetric.
--
-- Idempotent (safe to re-run).

-- ---------------------------------------------------------------------------
-- 1. Table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS vehicle_telemetry (
    time            TIMESTAMPTZ NOT NULL,
    vehicle_id      UUID NOT NULL,
    soc             DOUBLE PRECISION CHECK (soc >= 0 AND soc <= 1),  -- 0.0 to 1.0
    location_lat    DOUBLE PRECISION CHECK (location_lat >= -90 AND location_lat <= 90),
    location_lon    DOUBLE PRECISION CHECK (location_lon >= -180 AND location_lon <= 180),
    source          TEXT NOT NULL DEFAULT 'navirec',
    raw_fields      JSONB,
    PRIMARY KEY (vehicle_id, time)
);

-- ---------------------------------------------------------------------------
-- 2. Promote to a TimescaleDB hypertable on `time`.
--    Wrapped in a DO block so the migration also works on plain PostgreSQL
--    test environments (TimescaleDB extension absent).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    PERFORM create_hypertable(
        'vehicle_telemetry',
        'time',
        if_not_exists => TRUE,
        migrate_data  => TRUE
    );
EXCEPTION WHEN undefined_function THEN
    RAISE NOTICE 'TimescaleDB not installed: skipping create_hypertable';
WHEN OTHERS THEN
    RAISE NOTICE 'create_hypertable skipped: %', SQLERRM;
END$$;

-- ---------------------------------------------------------------------------
-- 3. Index supporting DISTINCT ON (vehicle_id) ... ORDER BY time DESC
--    (mirrors idx_telemetry_vehicle from 001_initial_schema.sql).
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_vehicle_telemetry_vehicle
    ON vehicle_telemetry (vehicle_id, time DESC);

-- ---------------------------------------------------------------------------
-- 4. Retention: drop raw telematics readings older than 90 days. This is a
--    high-frequency feed (every few minutes x vehicles x depots); unlike the
--    charger `telemetry` table we cap growth up front. Wrapped so it no-ops
--    without TimescaleDB.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    PERFORM add_retention_policy(
        'vehicle_telemetry',
        INTERVAL '90 days',
        if_not_exists => TRUE
    );
EXCEPTION WHEN undefined_function THEN
    RAISE NOTICE 'TimescaleDB not installed: skipping retention policy';
WHEN OTHERS THEN
    RAISE NOTICE 'add_retention_policy skipped: %', SQLERRM;
END$$;
