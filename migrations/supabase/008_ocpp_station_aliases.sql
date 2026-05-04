-- OCPP station aliases for vendor-specific charger identities.

CREATE TABLE IF NOT EXISTS public.ocpp_station_aliases (
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
    ON public.ocpp_station_aliases (canonical_station_id)
    WHERE active = TRUE;

COMMENT ON TABLE public.ocpp_station_aliases IS
    'Maps vendor-provided OCPP identities to canonical backend station ids.';
