-- Depot setup metadata for first-depot onboarding endpoints.
-- Adds optional structured setup metadata and read-path indexes.

ALTER TABLE depots
    ADD COLUMN IF NOT EXISTS demand_charge_billing_period VARCHAR(32) NOT NULL DEFAULT 'monthly',
    ADD COLUMN IF NOT EXISTS address JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS billing_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS building_load_source JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_vehicles_depot_id ON vehicles (depot_id);
CREATE INDEX IF NOT EXISTS idx_chargers_depot_id ON chargers (depot_id);
CREATE INDEX IF NOT EXISTS idx_battery_storage_depot_id ON battery_storage (depot_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_battery_storage_depot_id ON battery_storage (depot_id);
