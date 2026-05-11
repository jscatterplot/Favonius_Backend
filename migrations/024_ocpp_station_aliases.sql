-- OCPP station aliases for vendor-specific charger identities.
--
-- Some chargers use a hardware serial as the WebSocket path or Basic Auth
-- username even when operators configure a friendlier backend OCPP id. Keep
-- aliases explicit so auth remains bound to a known canonical station.

CREATE TABLE IF NOT EXISTS ocpp_station_aliases (
    alias_station_id     VARCHAR(255) PRIMARY KEY,
    canonical_station_id VARCHAR(255) NOT NULL,
    active               BOOLEAN NOT NULL DEFAULT TRUE,
    source               VARCHAR(50) NOT NULL DEFAULT 'manual',
    notes                TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (alias_station_id <> canonical_station_id)
);

CREATE INDEX IF NOT EXISTS idx_ocpp_station_aliases_canonical
    ON ocpp_station_aliases (canonical_station_id)
    WHERE active = TRUE;

COMMENT ON TABLE ocpp_station_aliases IS
    'Maps vendor-provided OCPP identities to canonical backend station ids.';
