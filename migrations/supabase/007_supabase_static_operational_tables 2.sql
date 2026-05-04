-- Adds the operational tables the backend needs but Supabase did not have.
-- All references use Supabase's canonical naming (sites, charging_stations,
-- vehicles, organizations) with `id` as the PK column.

-- ============ CHARGER ↔ VEHICLE ACCESS MATRIX ============
-- Per PRD Section 2.3: not every vehicle physically reaches every charger.
CREATE TABLE IF NOT EXISTS public.charger_vehicle_access (
    charging_station_id UUID NOT NULL REFERENCES public.charging_stations(id) ON DELETE CASCADE,
    vehicle_id          UUID NOT NULL REFERENCES public.vehicles(id) ON DELETE CASCADE,
    is_accessible       BOOLEAN DEFAULT TRUE,
    notes               VARCHAR(255),
    PRIMARY KEY (charging_station_id, vehicle_id)
);

CREATE INDEX IF NOT EXISTS idx_charger_vehicle_access_vehicle
    ON public.charger_vehicle_access (vehicle_id);

-- ============ STATIONARY BATTERY STORAGE ============
-- Per PRD Section 8.1 Constraint 11.
CREATE TABLE IF NOT EXISTS public.battery_storage (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id      UUID NOT NULL REFERENCES public.sites(id) ON DELETE CASCADE,
    capacity_kwh DOUBLE PRECISION NOT NULL CHECK (capacity_kwh > 0),
    max_power_kw DOUBLE PRECISION NOT NULL CHECK (max_power_kw > 0),
    efficiency   DOUBLE PRECISION DEFAULT 0.92 CHECK (efficiency > 0 AND efficiency <= 1),
    soc_min      DOUBLE PRECISION DEFAULT 0.2 CHECK (soc_min >= 0 AND soc_min < 1),
    soc_max      DOUBLE PRECISION DEFAULT 0.8 CHECK (soc_max > 0 AND soc_max <= 1),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT battery_soc_range CHECK (soc_min < soc_max)
);

CREATE INDEX IF NOT EXISTS idx_battery_storage_site_id ON public.battery_storage (site_id);

-- ============ FLEET SCHEDULES (per-day route data) ============
-- Distinct from public.vehicle_schedules (recurring patterns). Backend uses
-- per-day rows with concrete timestamps for optimization input.
CREATE TABLE IF NOT EXISTS public.schedules (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id         UUID NOT NULL REFERENCES public.vehicles(id) ON DELETE CASCADE,
    route_id           VARCHAR(100),
    departure_time     TIMESTAMPTZ NOT NULL,
    return_time        TIMESTAMPTZ NOT NULL,
    actual_return_time TIMESTAMPTZ,
    energy_kwh         DOUBLE PRECISION,
    required_soc       DOUBLE PRECISION DEFAULT 1.0,
    dest_site_id       UUID REFERENCES public.sites(id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_schedules_vehicle_depart
    ON public.schedules (vehicle_id, departure_time);

-- ============ DRIVERS ============
CREATE TABLE IF NOT EXISTS public.drivers (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id            UUID NOT NULL REFERENCES public.sites(id) ON DELETE CASCADE,
    external_driver_id VARCHAR(100),
    display_name       VARCHAR(255) NOT NULL,
    email              VARCHAR(255),
    phone              VARCHAR(64),
    status             VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT drivers_status_valid CHECK (status IN ('active', 'inactive'))
);

CREATE UNIQUE INDEX IF NOT EXISTS drivers_site_external_driver_id_unique_idx
    ON public.drivers (site_id, external_driver_id)
    WHERE external_driver_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_drivers_site ON public.drivers (site_id);

-- ============ RFID CARDS ============
CREATE TABLE IF NOT EXISTS public.rfid_cards (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    site_id    UUID NOT NULL REFERENCES public.sites(id) ON DELETE CASCADE,
    id_tag     VARCHAR(100) NOT NULL,
    label      VARCHAR(255),
    status     VARCHAR(32) NOT NULL DEFAULT 'active',
    notes      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT rfid_cards_status_valid CHECK (status IN ('active', 'inactive', 'lost', 'stolen'))
);

CREATE UNIQUE INDEX IF NOT EXISTS rfid_cards_id_tag_unique_idx
    ON public.rfid_cards (id_tag);

CREATE INDEX IF NOT EXISTS idx_rfid_cards_site ON public.rfid_cards (site_id);

CREATE TABLE IF NOT EXISTS public.rfid_card_vehicle_assignments (
    card_id    UUID NOT NULL REFERENCES public.rfid_cards(id) ON DELETE CASCADE,
    vehicle_id UUID NOT NULL REFERENCES public.vehicles(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (card_id, vehicle_id)
);

CREATE INDEX IF NOT EXISTS idx_rfid_card_vehicle_assignments_vehicle
    ON public.rfid_card_vehicle_assignments (vehicle_id);

CREATE TABLE IF NOT EXISTS public.rfid_card_driver_assignments (
    card_id    UUID NOT NULL REFERENCES public.rfid_cards(id) ON DELETE CASCADE,
    driver_id  UUID NOT NULL REFERENCES public.drivers(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (card_id, driver_id)
);

CREATE INDEX IF NOT EXISTS idx_rfid_card_driver_assignments_driver
    ON public.rfid_card_driver_assignments (driver_id);

-- ============ STATION CREDENTIALS ============
-- Per-charger Basic Auth credential store. password_hash is bcrypt; plaintext
-- is shown to operators exactly once at rotation time.
CREATE TABLE IF NOT EXISTS public.station_credentials (
    id              SERIAL PRIMARY KEY,
    station_id      VARCHAR(255) NOT NULL,
    username        VARCHAR(255) NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_rotated_at TIMESTAMPTZ,
    last_used       TIMESTAMPTZ,
    UNIQUE (station_id, username)
);

CREATE INDEX IF NOT EXISTS station_credentials_station_id_idx
    ON public.station_credentials (station_id);
CREATE INDEX IF NOT EXISTS station_credentials_active_idx
    ON public.station_credentials (active) WHERE active = TRUE;

-- ============ CHARGER ONBOARDING IDEMPOTENCY ============
CREATE TABLE IF NOT EXISTS public.charger_onboarding_idempotency (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id    UUID NOT NULL REFERENCES public.organizations(id) ON DELETE CASCADE,
    user_id            UUID NOT NULL,
    endpoint           TEXT NOT NULL,
    idempotency_key    TEXT NOT NULL,
    request_hash       TEXT NOT NULL,
    response_json      JSONB,
    status_code        INTEGER,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at         TIMESTAMPTZ NOT NULL,
    UNIQUE (organization_id, endpoint, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_charger_onboarding_idempotency_expiry
    ON public.charger_onboarding_idempotency (expires_at);

COMMENT ON COLUMN public.charger_onboarding_idempotency.response_json IS
    'Temporary replay payload for charger onboarding. May include one-time plaintext credential until expires_at.';

-- ============ SECURITY EVENTS ============
CREATE TABLE IF NOT EXISTS public.security_events (
    id              SERIAL PRIMARY KEY,
    station_id      VARCHAR(255) NOT NULL,
    event_type      VARCHAR(100) NOT NULL,
    timestamp       TIMESTAMPTZ NOT NULL,
    tech_info       TEXT,
    additional_info JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_security_events_station_id ON public.security_events (station_id);
CREATE INDEX IF NOT EXISTS idx_security_events_type ON public.security_events (event_type);
CREATE INDEX IF NOT EXISTS idx_security_events_timestamp ON public.security_events (timestamp);
