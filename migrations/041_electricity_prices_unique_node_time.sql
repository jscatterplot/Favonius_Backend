-- Migration 041: dedup electricity_prices on (time, node_id, market_type)
-- and add a unique index so concurrent read-through cache misses can't
-- create duplicate price rows.
--
-- Background: ``src/db/queries.py::fetch_or_pull_prices_by_zone`` is the
-- read-through cache used by per-session billing and the optimizer.
-- Earlier draft did SELECT-then-INSERT with no atomicity guarantee, so
-- two concurrent cache-miss callers could both observe a given hour as
-- absent and both insert it. Without a uniqueness constraint, the
-- duplicates persisted and the helper's ``ORDER BY time`` (no tiebreak)
-- gave non-deterministic "last seen" prices on subsequent reads.
--
-- TimescaleDB requires a unique index on a hypertable to include the
-- partition column. ``electricity_prices`` is partitioned on ``time``
-- and the new constraint includes it as the first column, so the
-- constraint is permitted on the hypertable.

-- 1. Dedup pre-existing rows. Keep the earliest ctid per
--    (time, node_id, market_type) — picking the same row deterministically
--    every run.
DELETE FROM electricity_prices a
USING electricity_prices b
WHERE a.ctid > b.ctid
  AND a.time = b.time
  AND a.node_id = b.node_id
  AND a.market_type = b.market_type;

-- 2. Add the unique index. ``IF NOT EXISTS`` keeps the migration
--    idempotent across reruns. Subsequent INSERTs in
--    fetch_or_pull_prices_by_zone use ``ON CONFLICT
--    (time, node_id, market_type) DO NOTHING`` to win-or-skip without
--    races.
CREATE UNIQUE INDEX IF NOT EXISTS uq_electricity_prices_node_time_market
    ON electricity_prices (time, node_id, market_type);

COMMENT ON INDEX uq_electricity_prices_node_time_market IS
    'Uniqueness on (time, node_id, market_type). Used as the conflict target by fetch_or_pull_prices_by_zone''s read-through INSERT so concurrent callers can''t race-insert duplicates.';
