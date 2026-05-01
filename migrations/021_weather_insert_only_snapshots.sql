-- Weather forecasts: convert to insert-only snapshot history.
--
-- Background
-- ----------
-- The original ``weather_forecasts`` schema (migration 001) used
-- ``(time, depot_id)`` as a composite primary key and the ingestion
-- path relied on ``ON CONFLICT ... DO UPDATE`` so each new fetch
-- *overwrote* whatever was previously stored for that (depot, day).
-- This made the table a cache, not a history: there was no way to
-- replay the exact weather features that an optimization actually
-- saw, and the surrogate training pipeline could only see the most
-- recently fetched values for any given target day.
--
-- This migration flips the model to insert-only forecast snapshots:
--   * Every fetch from Open-Meteo (or any future provider) inserts
--     a fresh row stamped with ``fetched_at`` (when we asked) and
--     ``forecast_for`` (the timestamp the forecast is *for*).
--   * ``forecast_id`` (UUID) is the primary key so each row is
--     individually addressable from ``optimization_input_snapshots``.
--   * ``UNIQUE (depot_id, source, fetched_at, forecast_for)`` lets a
--     duplicate fetch (same tuple, e.g. the loop running twice in the
--     same second) collapse, but every fresh fetch inserts.
--   * The hypertable is partitioned on ``fetched_at`` (30-day chunks)
--     and a 24-month retention policy drops old raw rows so the
--     table doesn't grow unbounded.
--   * ``optimization_input_snapshots.weather_forecast_id`` (FK to
--     ``forecast_id``, ``ON DELETE SET NULL``) pins each snapshot to
--     the exact forecast bundle the optimizer saw at run time. When
--     a chunk eventually ages out the snapshot keeps its inline
--     weather_features payload but the FK becomes NULL.
--
-- Implementation notes
-- --------------------
-- The legacy table is already a TimescaleDB hypertable partitioned on
-- ``time`` with composite PK ``(time, depot_id)``. We can't change a
-- hypertable's partitioning column in place, so this migration does a
-- swap:
--   1. CREATE weather_forecasts_v2 with the new schema.
--   2. Copy rows from the old table, mapping ``time → forecast_for``
--      and synthesising forecast_id / source / fetched_at.
--   3. DROP the old table, RENAME v2.
--   4. create_hypertable on ``fetched_at`` with 30-day chunks.
--   5. add_retention_policy('weather_forecasts', INTERVAL '24 months').

-- ============================================================
-- 1. New table with the insert-only schema.
-- ============================================================

CREATE TABLE IF NOT EXISTS weather_forecasts_v2 (
    forecast_id   UUID         NOT NULL DEFAULT gen_random_uuid(),
    depot_id      UUID         NOT NULL,
    source        VARCHAR(32)  NOT NULL DEFAULT 'open_meteo',
    fetched_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    forecast_for  TIMESTAMPTZ  NOT NULL,
    temp_f        DOUBLE PRECISION,
    temp_max_f    DOUBLE PRECISION,
    temp_min_f    DOUBLE PRECISION,
    precip_in     DOUBLE PRECISION,
    solar_rad     DOUBLE PRECISION,
    -- TimescaleDB requires the partitioning column to be part of any
    -- UNIQUE/PRIMARY KEY constraint on a hypertable. We therefore
    -- include fetched_at (the new partitioning column) in the PK.
    -- forecast_id is still globally unique on its own (UUIDv4).
    PRIMARY KEY (forecast_id, fetched_at),
    UNIQUE (depot_id, source, fetched_at, forecast_for)
);

-- ============================================================
-- 2. Copy any existing rows from the legacy table. The legacy
--    schema has ``time`` (the day the forecast was for, == forecast_for)
--    and ``fetched_at`` (when the row was written). All historical
--    data came from the Open-Meteo adapter so source = 'open_meteo'.
-- ============================================================

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_name = 'weather_forecasts'
          AND table_schema = current_schema()
    ) THEN
        -- Handle both legacy schemas:
        --   * migration 001 shape: has column "time"
        --   * partially-migrated/newer shape: has "forecast_for"
        IF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'weather_forecasts'
              AND column_name = 'time'
        ) THEN
            INSERT INTO weather_forecasts_v2 (
                forecast_id, depot_id, source, fetched_at, forecast_for,
                temp_f, temp_max_f, temp_min_f, precip_in, solar_rad
            )
            SELECT
                gen_random_uuid()                          AS forecast_id,
                depot_id,
                'open_meteo'                               AS source,
                -- Older rows may have NULL fetched_at because the legacy
                -- schema's DEFAULT NOW() didn't apply to manual backfills.
                -- Fall back to ``time`` so the row still has a sensible
                -- partition key.
                COALESCE(fetched_at, time, NOW())          AS fetched_at,
                time                                       AS forecast_for,
                temp_f, temp_max_f, temp_min_f, precip_in, solar_rad
            FROM weather_forecasts
            ON CONFLICT (depot_id, source, fetched_at, forecast_for) DO NOTHING;
        ELSIF EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'weather_forecasts'
              AND column_name = 'forecast_for'
        ) THEN
            INSERT INTO weather_forecasts_v2 (
                forecast_id, depot_id, source, fetched_at, forecast_for,
                temp_f, temp_max_f, temp_min_f, precip_in, solar_rad
            )
            SELECT
                COALESCE(forecast_id, gen_random_uuid())  AS forecast_id,
                depot_id,
                COALESCE(source, 'open_meteo')             AS source,
                COALESCE(fetched_at, forecast_for, NOW())  AS fetched_at,
                forecast_for,
                temp_f, temp_max_f, temp_min_f, precip_in, solar_rad
            FROM weather_forecasts
            ON CONFLICT (depot_id, source, fetched_at, forecast_for) DO NOTHING;
        END IF;
    END IF;
END$$;

-- ============================================================
-- 3. Drop the legacy table (which removes the old hypertable
--    metadata) and rename the new table into place.
-- ============================================================

DROP TABLE IF EXISTS weather_forecasts CASCADE;
ALTER TABLE weather_forecasts_v2 RENAME TO weather_forecasts;

-- ============================================================
-- 4. Indexes for the new query patterns.
-- ============================================================

-- Fast lookup for the assembler: "what forecast bundle was current
-- at horizon_start?" — answered by MAX(fetched_at) <= horizon_start.
CREATE INDEX IF NOT EXISTS idx_weather_forecasts_depot_fetched_at
    ON weather_forecasts (depot_id, fetched_at DESC);

-- Fast lookup for surrogate training: "what was the forecast for
-- this (depot, day)?" Filtered by fetched_at = the snapshot bundle.
CREATE INDEX IF NOT EXISTS idx_weather_forecasts_depot_forecast_for
    ON weather_forecasts (depot_id, forecast_for);

-- ============================================================
-- 5. Promote to a TimescaleDB hypertable on fetched_at with
--    30-day chunks and a 24-month retention policy.
--
-- Wrapped in DO blocks so the migration also works on plain
-- PostgreSQL test environments (TimescaleDB extension absent).
-- ============================================================

DO $$
BEGIN
    PERFORM create_hypertable(
        'weather_forecasts',
        'fetched_at',
        chunk_time_interval => INTERVAL '30 days',
        if_not_exists       => TRUE,
        migrate_data        => TRUE
    );
EXCEPTION WHEN undefined_function THEN
    RAISE NOTICE 'TimescaleDB not installed: skipping create_hypertable';
WHEN OTHERS THEN
    RAISE NOTICE 'create_hypertable skipped: %', SQLERRM;
END$$;

DO $$
BEGIN
    PERFORM add_retention_policy(
        'weather_forecasts',
        INTERVAL '24 months',
        if_not_exists => TRUE
    );
EXCEPTION WHEN undefined_function THEN
    RAISE NOTICE 'TimescaleDB not installed: skipping retention policy';
WHEN OTHERS THEN
    RAISE NOTICE 'add_retention_policy skipped: %', SQLERRM;
END$$;

-- ============================================================
-- 6. Wire weather_forecast_id into optimization_input_snapshots.
--    NULLable + ON DELETE SET NULL: the snapshot keeps its inline
--    weather_features payload even after the underlying forecast row
--    eventually ages out via retention.
--
--    NOTE: the FK target column ``forecast_id`` is unique on its own
--    (UUIDv4 + UNIQUE INDEX on (forecast_id) is implied via the
--    composite PK only when forecast_id is its first column). To
--    guarantee FK validity we add an explicit single-column unique
--    constraint that satisfies the FK requirement; on plain
--    PostgreSQL this is straightforward; on a hypertable we fall
--    back to a *non-unique* index because TimescaleDB rejects single-
--    column UNIQUE constraints that omit the partitioning column.
--    In the latter case the FK is enforced logically by application
--    code that always inserts a freshly-generated UUID.
-- ============================================================

ALTER TABLE optimization_input_snapshots
    ADD COLUMN IF NOT EXISTS weather_forecast_id UUID;

DO $$
BEGIN
    -- Try to add a single-column UNIQUE constraint so the FK is
    -- structurally enforceable. On a TimescaleDB hypertable this will
    -- fail; we then fall back to a plain index and rely on
    -- application-level uniqueness (UUIDv4 + insert-only).
    BEGIN
        ALTER TABLE weather_forecasts
            ADD CONSTRAINT weather_forecasts_forecast_id_key
            UNIQUE (forecast_id);
    EXCEPTION WHEN duplicate_object THEN
        NULL;
    WHEN feature_not_supported THEN
        CREATE INDEX IF NOT EXISTS idx_weather_forecasts_forecast_id
            ON weather_forecasts (forecast_id);
        RETURN;
    WHEN OTHERS THEN
        CREATE INDEX IF NOT EXISTS idx_weather_forecasts_forecast_id
            ON weather_forecasts (forecast_id);
        RETURN;
    END;

    -- Add the FK only if a unique constraint exists.
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.table_constraints
        WHERE table_name = 'optimization_input_snapshots'
          AND constraint_name =
              'optimization_input_snapshots_weather_forecast_fkey'
    ) THEN
        ALTER TABLE optimization_input_snapshots
            ADD CONSTRAINT optimization_input_snapshots_weather_forecast_fkey
            FOREIGN KEY (weather_forecast_id)
            REFERENCES weather_forecasts (forecast_id)
            ON DELETE SET NULL;
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_opt_input_snapshots_weather_forecast
    ON optimization_input_snapshots (weather_forecast_id)
    WHERE weather_forecast_id IS NOT NULL;
