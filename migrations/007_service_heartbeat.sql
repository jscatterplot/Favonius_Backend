-- Service heartbeat table for inter-service health coordination.
-- The main API optimizer writes a timestamp here every 30 seconds.
-- The websocket_handler reads it to decide whether to activate the
-- heuristic fallback optimizer (Option B of the dual-optimizer design).

CREATE TABLE IF NOT EXISTS service_heartbeat (
    service     TEXT        PRIMARY KEY,
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Pre-seed the optimizer row so the first read always finds a row,
-- avoiding a false "main API is down" on cold start.
INSERT INTO service_heartbeat (service, last_seen)
VALUES ('optimizer', NOW())
ON CONFLICT (service) DO NOTHING;
