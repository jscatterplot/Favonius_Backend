-- Fleet identity support for vehicles, drivers, and RFID cards.
--
-- Static identity data is scoped by depot_id; organization ownership is
-- resolved through depots.organization_id. charging_sessions keeps nullable
-- identity snapshots so OCPP StartTransaction can preserve what was known.
--
-- Guards: vehicles and depots are Supabase shadow tables dropped by migration
-- 029; skip column/index work gracefully when absent (same pattern as 006/011).

DO $$
BEGIN
    IF to_regclass('public.vehicles') IS NULL THEN
        RAISE NOTICE 'Skipping 018 vehicle columns/indexes: table public.vehicles does not exist';
    ELSE
        ALTER TABLE vehicles
            ADD COLUMN IF NOT EXISTS display_name VARCHAR(255),
            ADD COLUMN IF NOT EXISTS vin VARCHAR(64),
            ADD COLUMN IF NOT EXISTS license_plate VARCHAR(64),
            ADD COLUMN IF NOT EXISTS status VARCHAR(32) NOT NULL DEFAULT 'active',
            ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'vehicles_status_valid'
        ) THEN
            ALTER TABLE vehicles
                ADD CONSTRAINT vehicles_status_valid
                CHECK (status IN ('active', 'inactive', 'retired'));
        END IF;

        CREATE UNIQUE INDEX IF NOT EXISTS vehicles_id_tag_unique_idx
            ON vehicles (id_tag)
            WHERE id_tag IS NOT NULL;
    END IF;
END $$;

-- drivers, rfid_cards, and rfid_card_driver_assignments reference depots.
-- Guard: skip table creation when depots doesn't exist (shadow table dropped
-- by migration 029; recreated by 001 on next boot once 001 is idempotent).
DO $$
BEGIN
    IF to_regclass('public.depots') IS NULL THEN
        RAISE NOTICE 'Skipping drivers/rfid_cards tables: table public.depots does not exist';
        RETURN;
    END IF;

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

    CREATE TABLE IF NOT EXISTS rfid_card_driver_assignments (
        card_id    UUID NOT NULL REFERENCES rfid_cards(card_id) ON DELETE CASCADE,
        driver_id  UUID NOT NULL REFERENCES drivers(driver_id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (card_id, driver_id)
    );

    CREATE INDEX IF NOT EXISTS idx_drivers_depot ON drivers (depot_id);
    CREATE INDEX IF NOT EXISTS idx_rfid_cards_depot ON rfid_cards (depot_id);
    CREATE INDEX IF NOT EXISTS idx_rfid_card_driver_assignments_driver
        ON rfid_card_driver_assignments (driver_id);
END $$;

-- rfid_card_vehicle_assignments references both rfid_cards (above) and
-- vehicles (shadow table). Skip when either is absent.
DO $$
BEGIN
    IF to_regclass('public.rfid_cards') IS NULL OR to_regclass('public.vehicles') IS NULL THEN
        RAISE NOTICE 'Skipping rfid_card_vehicle_assignments: rfid_cards or vehicles does not exist';
        RETURN;
    END IF;

    CREATE TABLE IF NOT EXISTS rfid_card_vehicle_assignments (
        card_id    UUID NOT NULL REFERENCES rfid_cards(card_id) ON DELETE CASCADE,
        vehicle_id UUID NOT NULL REFERENCES vehicles(vehicle_id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (card_id, vehicle_id)
    );

    CREATE INDEX IF NOT EXISTS idx_rfid_card_vehicle_assignments_vehicle
        ON rfid_card_vehicle_assignments (vehicle_id);
END $$;

ALTER TABLE charging_sessions
    ADD COLUMN IF NOT EXISTS driver_id UUID,
    ADD COLUMN IF NOT EXISTS card_id UUID;
