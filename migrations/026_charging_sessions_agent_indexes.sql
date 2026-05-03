-- Required for v0 of the natural-language depot agent.
-- charging_sessions is a regular table (NOT a TimescaleDB hypertable), so
-- there is no chunk pruning on start_time. Without these indexes the agent's
-- consumption_by_user WHERE-clause (driver_id OR card_id, plus a time range)
-- becomes a sequential scan once a customer has more than a few weeks of data.
--
-- First-deploy form: plain CREATE INDEX IF NOT EXISTS. The migration runner
-- wraps each file in an implicit transaction, and CONCURRENTLY cannot run
-- inside one. For environments under traffic, the follow-up cleanup is to
-- swap to CREATE INDEX CONCURRENTLY in a separate per-statement migration.
CREATE INDEX IF NOT EXISTS idx_sessions_driver_time
    ON charging_sessions (driver_id, start_time DESC)
    WHERE driver_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_sessions_card_time
    ON charging_sessions (card_id, start_time DESC)
    WHERE card_id IS NOT NULL;
