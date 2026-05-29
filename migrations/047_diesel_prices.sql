-- Migration 047: diesel_prices hypertable (wholesale diesel price feed).
--
-- Source for the diesel side of the EV-vs-diesel "price per kilometre"
-- comparison report (ev_vs_diesel_tco). A background poller
-- (src/adapters/diesel_prices/poller.py) fetches per-country wholesale diesel
-- prices from a configurable free source (EU Weekly Oil Bulletin by default)
-- and upserts them here; src/db/queries.py::fetch_or_pull_diesel_price reads
-- the latest row for a depot's country when generating the report.
--
-- Design notes:
--   * Keyed (time, source, region) — mirrors electricity_prices' (time,
--     node_id, market_type) shape so concurrent ingestion (poller + the
--     read-through cache) can't race-insert duplicates.
--   * region = ISO 3166-1 alpha-2 country code (e.g. 'LT', 'DE'), resolved from
--     each depot's timezone. No FK: depots live in Supabase, this is TimescaleDB.
--   * price_eur_per_l is the EX-TAX (wholesale) figure — the apples-to-apples
--     number for a fleet operator. price_incl_tax_eur_per_l keeps the consumer
--     price for reference when the source provides both.
--
-- Idempotent (safe to re-run): IF NOT EXISTS everywhere; create_hypertable /
-- retention wrapped in DO/EXCEPTION so it also works on plain PostgreSQL CI.

-- ---------------------------------------------------------------------------
-- 1. Table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS diesel_prices (
    time                     TIMESTAMPTZ NOT NULL,
    region                   TEXT        NOT NULL,  -- ISO 3166-1 alpha-2
    source                   TEXT        NOT NULL,  -- 'eu_oil_bulletin' | 'fuel_prices_eu' | 'tankerkonig'
    price_eur_per_l          DOUBLE PRECISION
        CHECK (price_eur_per_l IS NULL OR price_eur_per_l >= 0),  -- ex-tax (wholesale)
    price_incl_tax_eur_per_l DOUBLE PRECISION
        CHECK (price_incl_tax_eur_per_l IS NULL OR price_incl_tax_eur_per_l >= 0),
    PRIMARY KEY (time, source, region)
);

-- ---------------------------------------------------------------------------
-- 2. Promote to a TimescaleDB hypertable on `time` (no-op on plain Postgres).
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    PERFORM create_hypertable(
        'diesel_prices',
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
-- 3. Index supporting "latest price for a region/source at or before T".
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_diesel_prices_region_source_time
    ON diesel_prices (region, source, time DESC);

-- ---------------------------------------------------------------------------
-- 4. Retention: diesel prices are low-volume (per country, weekly), but keep a
--    generous 3-year window for historical comparisons, then drop. Wrapped so
--    it no-ops without TimescaleDB.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    PERFORM add_retention_policy(
        'diesel_prices',
        INTERVAL '1095 days',
        if_not_exists => TRUE
    );
EXCEPTION WHEN undefined_function THEN
    RAISE NOTICE 'TimescaleDB not installed: skipping retention policy';
WHEN OTHERS THEN
    RAISE NOTICE 'add_retention_policy skipped: %', SQLERRM;
END$$;
