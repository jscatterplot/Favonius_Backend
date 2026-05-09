-- Migration 035: Charger-keyed telemetry
--
-- Goal: keep telemetry writes even when vehicle_id is unknown.
-- Primary key moves from (time, vehicle_id) to (time, station_id, connector_id).
-- vehicle_id remains as optional enrichment for vehicle-centric joins.

ALTER TABLE telemetry
    ADD COLUMN IF NOT EXISTS station_id TEXT,
    ADD COLUMN IF NOT EXISTS connector_id INTEGER,
    ADD COLUMN IF NOT EXISTS transaction_id BIGINT;

-- Backfill station/connector defaults for pre-migration rows.
UPDATE telemetry
SET station_id = COALESCE(station_id, 'legacy-unknown')
WHERE station_id IS NULL;

UPDATE telemetry
SET connector_id = COALESCE(connector_id, 1)
WHERE connector_id IS NULL;

ALTER TABLE telemetry
    ALTER COLUMN station_id SET DEFAULT 'unknown',
    ALTER COLUMN connector_id SET DEFAULT 1,
    ALTER COLUMN station_id SET NOT NULL,
    ALTER COLUMN connector_id SET NOT NULL;

-- Vehicle is now optional enrichment, not the primary identity.
ALTER TABLE telemetry
    ALTER COLUMN vehicle_id DROP NOT NULL;

-- Replace old PK and indexes.
ALTER TABLE telemetry DROP CONSTRAINT IF EXISTS telemetry_pkey;
ALTER TABLE telemetry
    ADD CONSTRAINT telemetry_pkey PRIMARY KEY (time, station_id, connector_id);

DROP INDEX IF EXISTS idx_telemetry_vehicle;
CREATE INDEX IF NOT EXISTS idx_telemetry_vehicle ON telemetry (vehicle_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_station ON telemetry (station_id, connector_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_charger ON telemetry (charger_id, time DESC);

-- Backfill charger-keyed telemetry from normalized raw samples where rows are missing.
-- This preserves recent power/SoC visibility after migration without requiring vehicle mapping.
WITH sample_rollup AS (
    SELECT
        s.time,
        s.station_id,
        s.connector_id,
        s.transaction_id,
        MAX(
            CASE
                WHEN s.measurand = 'Power.Active.Import' THEN
                    CASE WHEN COALESCE(s.unit, '') = 'W' THEN s.value / 1000.0 ELSE s.value END
                WHEN s.measurand = 'Power.Active.Export' THEN
                    -1.0 * (CASE WHEN COALESCE(s.unit, '') = 'W' THEN s.value / 1000.0 ELSE s.value END)
                ELSE NULL
            END
        ) AS charging_kw,
        MAX(
            CASE
                WHEN s.measurand = 'SoC' THEN s.value / 100.0
                ELSE NULL
            END
        ) AS soc,
        MAX(
            CASE
                WHEN s.measurand = 'Max.Current.Offered' THEN
                    CASE WHEN COALESCE(s.unit, '') = 'A' THEN s.value ELSE NULL END
                ELSE NULL
            END
        ) AS max_charge_kw
    FROM telemetry_samples s
    GROUP BY s.time, s.station_id, s.connector_id, s.transaction_id
)
INSERT INTO telemetry (
    time,
    station_id,
    connector_id,
    transaction_id,
    vehicle_id,
    charger_id,
    soc,
    is_plugged,
    charging_kw,
    max_charge_kw
)
SELECT
    r.time,
    r.station_id,
    r.connector_id,
    r.transaction_id,
    NULL,
    NULL,
    r.soc,
    CASE WHEN r.charging_kw IS NULL THEN NULL ELSE (r.charging_kw > 0.1) END,
    r.charging_kw,
    r.max_charge_kw
FROM sample_rollup r
LEFT JOIN telemetry t
    ON t.time = r.time
   AND t.station_id = r.station_id
   AND t.connector_id = r.connector_id
WHERE t.time IS NULL;
