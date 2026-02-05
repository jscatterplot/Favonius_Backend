-- Favonius Energy: connector_status table for OCPP StatusNotification persistence
-- Enables GET /depots/{id}/alerts to return active charger faults (PRD §7.1, §10.5)
-- WebSocket Handler writes via timescale_client.insert_connector_status; Main API reads for alerts.

CREATE TABLE IF NOT EXISTS connector_status (
    station_id   VARCHAR(255) NOT NULL,   -- OCPP charge_point_id / chargers.ocpp_id
    connector_id  INTEGER NOT NULL,
    status       VARCHAR(50) NOT NULL,    -- Available, Preparing, Charging, SuspendedEV, Faulted, Unavailable, etc.
    error_code   VARCHAR(100),            -- OCPP 1.6 fault code when status = 'Faulted'
    timestamp    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_connector_status_latest
    ON connector_status (station_id, connector_id, timestamp DESC);

COMMENT ON TABLE connector_status IS 'OCPP StatusNotification history; latest per (station_id, connector_id) used for active faults in GET /depots/{id}/alerts';
