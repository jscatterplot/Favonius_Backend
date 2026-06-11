-- Favonius Energy: server-clock ingestion order for connector_status
--
-- The offline-charger monitor (src/websocket_handler/offline_monitor.py) must
-- identify the latest *server-observed* connector status to decide whether a
-- charger is currently disconnected. Ordering by the existing ``timestamp``
-- column is unsafe: StatusNotification rows carry the CHARGER-supplied clock,
-- so a charger whose clock is hours/days ahead would have a future-dated row
-- outrank the server-written ``(Unavailable,'ConnectionLost')`` disconnect
-- marker (always stamped with NOW()), hiding the outage from the monitor.
--
-- ``created_at`` is the server ingestion time (DEFAULT now()), so it is a
-- trustworthy ordering key independent of device clocks. The disconnect
-- duration the monitor reports still comes from the marker's ``timestamp``
-- (which the server writes as NOW() on the drop), so genuinely old markers are
-- reported with their true age immediately after deploy.
--
-- The accompanying index lets the latest-per-station lookup use an index scan
-- instead of sorting the full append-only history every sweep (the existing
-- idx_connector_status_latest puts connector_id between the two ordering
-- columns, so it cannot serve (station_id, created_at DESC)).
--
-- Idempotent; safe to re-run. NOTE: ADD COLUMN with a volatile DEFAULT now()
-- rewrites the table once (all existing rows get the migration-time value);
-- connector_status is small at pilot scale so this is cheap.

ALTER TABLE connector_status
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS idx_connector_status_ingest_latest
    ON connector_status (station_id, created_at DESC);

COMMENT ON COLUMN connector_status.created_at IS
    'Server ingestion time (DEFAULT now()). Trustworthy ordering key for '
    'offline detection — unlike the charger-supplied timestamp column, which '
    'reflects the device clock and can be skewed.';
