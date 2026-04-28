-- Static-schema support for fleet identity management.
--
-- Identity records are depot-scoped. API authorization must resolve ownership
-- through depots.organization_id rather than trusting client-supplied org ids.

ALTER TABLE vehicles
    ADD COLUMN IF NOT EXISTS display_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS vin VARCHAR(64),
    ADD COLUMN IF NOT EXISTS license_plate VARCHAR(64),
    ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'active',
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'vehicles_status_valid'
    ) THEN
        ALTER TABLE vehicles
            ADD CONSTRAINT vehicles_status_valid
            CHECK (status IN ('active', 'inactive', 'retired'));
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS vehicles_id_tag_unique_idx
    ON vehicles (id_tag)
    WHERE id_tag IS NOT NULL;

CREATE TABLE IF NOT EXISTS drivers (
    driver_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id           UUID NOT NULL REFERENCES depots(depot_id) ON DELETE CASCADE,
    external_driver_id VARCHAR(100),
    display_name       VARCHAR(255) NOT NULL,
    email              VARCHAR(255),
    phone              VARCHAR(64),
    status             VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT drivers_status_valid CHECK (status IN ('active', 'inactive'))
);

CREATE UNIQUE INDEX IF NOT EXISTS drivers_depot_external_driver_id_unique_idx
    ON drivers (depot_id, external_driver_id)
    WHERE external_driver_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS rfid_cards (
    card_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id   UUID NOT NULL REFERENCES depots(depot_id) ON DELETE CASCADE,
    id_tag     VARCHAR(100) NOT NULL,
    label      VARCHAR(255),
    status     VARCHAR(32) NOT NULL DEFAULT 'active',
    notes      TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT rfid_cards_status_valid CHECK (status IN ('active', 'inactive', 'lost', 'stolen'))
);

CREATE UNIQUE INDEX IF NOT EXISTS rfid_cards_id_tag_unique_idx
    ON rfid_cards (id_tag);

CREATE TABLE IF NOT EXISTS rfid_card_vehicle_assignments (
    card_id    UUID NOT NULL REFERENCES rfid_cards(card_id) ON DELETE CASCADE,
    vehicle_id UUID NOT NULL REFERENCES vehicles(vehicle_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (card_id, vehicle_id)
);

CREATE TABLE IF NOT EXISTS rfid_card_driver_assignments (
    card_id    UUID NOT NULL REFERENCES rfid_cards(card_id) ON DELETE CASCADE,
    driver_id  UUID NOT NULL REFERENCES drivers(driver_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (card_id, driver_id)
);

CREATE INDEX IF NOT EXISTS idx_drivers_depot ON drivers (depot_id);
CREATE INDEX IF NOT EXISTS idx_rfid_cards_depot ON rfid_cards (depot_id);
CREATE INDEX IF NOT EXISTS idx_rfid_card_vehicle_assignments_vehicle
    ON rfid_card_vehicle_assignments (vehicle_id);
CREATE INDEX IF NOT EXISTS idx_rfid_card_driver_assignments_driver
    ON rfid_card_driver_assignments (driver_id);
