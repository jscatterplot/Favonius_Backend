-- Migration 045: Telemetry cleanup
--
-- Three goals:
--   1. Add energy_kwh column to the wide telemetry table so the write path
--      can land Energy.Active.Import.Register directly without the EAV fan-out.
--   2. Backfill energy_kwh from telemetry_samples for historical rows, then
--      drop telemetry_samples. All production reads (get_session_energy_kwh
--      fallback path, backfill_terra_meter_start.py) have been rewired to
--      telemetry.energy_kwh in the same PR.
--   3. NULL out poisoned soc values (fabricated 0.0 only). The ABB Terra AC
--      chargers deployed at HRX do not send SoC measurands; the `soc or 0.0`
--      bug wrote 0.0 when soc was absent. Real SoC from the SoC measurand is
--      preserved (any non-zero fraction, and 0.0 when samples prove SoC).

-- Step 1: add energy_kwh column
ALTER TABLE telemetry ADD COLUMN IF NOT EXISTS energy_kwh DOUBLE PRECISION;

-- Step 2: backfill energy_kwh into existing telemetry rows from telemetry_samples
DO $$
BEGIN
    IF to_regclass('public.telemetry_samples') IS NOT NULL THEN
        UPDATE telemetry t
        SET energy_kwh = s.energy_kwh
        FROM (
            SELECT
                time,
                station_id,
                connector_id,
                MAX(
                    CASE WHEN measurand = 'Energy.Active.Import.Register'
                         THEN CASE WHEN LOWER(COALESCE(unit, '')) IN ('kwh', 'kw·h')
                                   THEN value
                                   ELSE value / 1000.0
                              END
                    END
                ) AS energy_kwh
            FROM telemetry_samples
            GROUP BY time, station_id, connector_id
        ) s
        WHERE t.time        = s.time
          AND t.station_id  = s.station_id
          AND t.connector_id = s.connector_id
          AND s.energy_kwh IS NOT NULL
          AND t.energy_kwh IS NULL;

        -- Step 3: insert orphan telemetry_samples rows that have no matching
        -- telemetry row (gaps before migration 035 introduced the wide table).
        -- Include transaction_id so session-scoped reads (get_session_energy_kwh,
        -- backfill_terra_meter_start) can locate these rows by transaction_id.
        INSERT INTO telemetry (time, station_id, connector_id, transaction_id, energy_kwh)
        SELECT
            time,
            station_id,
            connector_id,
            MAX(s.transaction_id) AS transaction_id,
            MAX(
                CASE WHEN measurand = 'Energy.Active.Import.Register'
                     THEN CASE WHEN LOWER(COALESCE(unit, '')) IN ('kwh', 'kw·h')
                               THEN value
                               ELSE value / 1000.0
                          END
                END
            ) AS energy_kwh
        FROM telemetry_samples s
        WHERE NOT EXISTS (
            SELECT 1 FROM telemetry t
            WHERE t.time         = s.time
              AND t.station_id   = s.station_id
              AND t.connector_id = s.connector_id
        )
        GROUP BY time, station_id, connector_id
        HAVING MAX(
            CASE WHEN measurand = 'Energy.Active.Import.Register' THEN value END
        ) IS NOT NULL
        ON CONFLICT (time, station_id, connector_id) DO NOTHING;

        -- Step 4: NULL poisoned soc before dropping samples (soc or 0.0 wrote 0.0
        -- when the SoC measurand was absent; keep rows backed by a SoC sample).
        UPDATE telemetry t
        SET soc = NULL
        WHERE t.soc = 0.0
          AND NOT EXISTS (
            SELECT 1
            FROM telemetry_samples s
            WHERE s.time = t.time
              AND s.station_id = t.station_id
              AND s.connector_id = t.connector_id
              AND s.measurand = 'SoC'
          );

        -- Step 5: drop the EAV table — data is now in telemetry.energy_kwh
        DROP TABLE telemetry_samples;
    END IF;
END
$$;
