-- Migration 034: Create the legacy electricity_prices hypertable.
--
-- Background: the WS handler's price feeder writes to a table named
-- ``electricity_prices`` that historically lived only in
-- ``src/websocket_handler/timescale_schema.py::_create_tables`` (created at
-- handler startup). On migration-only deployments — TigerCloud, fresh CI DBs,
-- the new architecture — that bootstrap path never runs and the price feeder
-- crashed every interval with ``relation "electricity_prices" does not exist``.
--
-- The schema-only-elsewhere story coincided with us pivoting from CAISO
-- (US ISO with multi-component LMP) to ENTSO-E (EU day-ahead, single price).
-- The CAISO-specific columns are kept nullable so historical CSV imports
-- still load, but ENTSO-E writes only populate ``lmp_price_mwh`` /
-- ``energy_component_mwh``.
--
-- All statements are idempotent: ``CREATE TABLE IF NOT EXISTS`` and
-- ``create_hypertable(if_not_exists => TRUE)``. Safe to re-run.

CREATE TABLE IF NOT EXISTS electricity_prices (
    time                       TIMESTAMPTZ      NOT NULL,
    node_id                    TEXT             NOT NULL,
    market_type                TEXT             NOT NULL,
    lmp_price_mwh              DOUBLE PRECISION,
    energy_component_mwh       DOUBLE PRECISION,
    congestion_component_mwh   DOUBLE PRECISION,
    loss_component_mwh         DOUBLE PRECISION,
    ghg_adder_mwh              DOUBLE PRECISION,
    price_confidence           DOUBLE PRECISION,
    forecast_horizon_minutes   INTEGER
);

SELECT create_hypertable('electricity_prices', 'time', if_not_exists => TRUE);

-- Primary lookup: latest prices for a set of nodes within a time window.
CREATE INDEX IF NOT EXISTS idx_electricity_prices_node_time
    ON electricity_prices (node_id, time DESC);

-- Distinct points-per-(time,node,market) — the feeder upserts via
-- ``copy_records_to_table`` so duplicates only occur if the same window is
-- fetched twice; keep the index narrow rather than enforcing uniqueness.
CREATE INDEX IF NOT EXISTS idx_electricity_prices_market_time
    ON electricity_prices (market_type, time DESC);

COMMENT ON TABLE electricity_prices IS
    'Day-ahead and real-time electricity prices ingested by the WS handler price feeder.';
COMMENT ON COLUMN electricity_prices.node_id IS
    'ISO node id (legacy CAISO) or ENTSO-E EIC bidding zone code (e.g. 10YLT-1001A0008Q).';
COMMENT ON COLUMN electricity_prices.market_type IS
    'Source identifier: ''DAM''/''RTM'' for legacy CAISO data; ''ENTSOE_DAM'' for ENTSO-E day-ahead.';
