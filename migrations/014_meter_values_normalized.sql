-- Migration 014: normalized OCPP MeterValues storage
--
-- Keep legacy telemetry summary rows for optimizer compatibility while adding
-- an append-only per-measurand hypertable for pilot diagnostics and auditing.

CREATE TABLE IF NOT EXISTS telemetry_samples (
    time            TIMESTAMPTZ NOT NULL,
    station_id      TEXT NOT NULL,
    connector_id    INTEGER NOT NULL,
    transaction_id  BIGINT,
    measurand       TEXT NOT NULL,
    phase           TEXT,
    location        TEXT,
    unit            TEXT,
    context         TEXT,
    format          TEXT,
    value           DOUBLE PRECISION NOT NULL
);

SELECT create_hypertable('telemetry_samples', 'time', if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS idx_telemetry_samples_station_time
    ON telemetry_samples (station_id, time DESC);

CREATE INDEX IF NOT EXISTS idx_telemetry_samples_tx
    ON telemetry_samples (transaction_id, time DESC);

