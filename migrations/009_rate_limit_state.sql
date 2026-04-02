-- Migration 009: Rate Limit State (PostgreSQL-backed persistence)
-- Required by: NIS2 Article 21 (distributed rate limiting across instances)
--
-- Stores aggregate rate limit counts per sliding window for cross-instance
-- coordination and cold-start hydration. This is a plain PostgreSQL table
-- (NOT a TimescaleDB hypertable) because the data is transient — the
-- application handles cleanup of expired windows.
--
-- Both Railway instances of the Main API write to this table via periodic
-- background sync (every 15 seconds). The WebSocket Handler uses its own
-- in-memory per-station rate limiting and does not write here.

CREATE TABLE IF NOT EXISTS rate_limit_state (
    bucket_type   TEXT NOT NULL,          -- 'api', 'optimize', 'handoff'
    bucket_key    TEXT NOT NULL,          -- client_id or sorted depot pair key
    window_start  TIMESTAMPTZ NOT NULL,   -- floored to window boundary
    request_count INT NOT NULL DEFAULT 0,
    last_updated  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (bucket_type, bucket_key, window_start)
);

-- Index for periodic cleanup of expired windows
CREATE INDEX IF NOT EXISTS idx_rate_limit_state_cleanup
    ON rate_limit_state (last_updated);
