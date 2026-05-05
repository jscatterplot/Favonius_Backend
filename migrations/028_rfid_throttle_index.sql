-- Partial functional index for the RFID-throttle hot path.
--
-- `RFIDAuthorizationService._is_throttled` issues this query on every Authorize:
--
--   SELECT COUNT(*) FROM security_events
--    WHERE station_id = $1
--      AND event_type = 'rfid_authorization_invalid'
--      AND timestamp >= $2
--      AND COALESCE((additional_info ->> 'id_tag'), '') = $3
--
-- Without a covering index the query degrades to a sequential scan filtered by
-- the existing single-column indexes (station_id / event_type / timestamp) and
-- a JSONB extraction the planner can't push into any of them. The partial
-- index narrows to the one event_type that matters and indexes the JSONB
-- extraction directly so the throttle check stays sub-millisecond at scale.

CREATE INDEX IF NOT EXISTS idx_security_events_rfid_invalid
    ON security_events (
        station_id,
        (additional_info ->> 'id_tag'),
        timestamp DESC
    )
    WHERE event_type = 'rfid_authorization_invalid';
