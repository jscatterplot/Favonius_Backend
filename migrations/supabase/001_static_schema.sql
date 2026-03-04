-- Favonius Energy — Supabase Static Schema
--
-- Reference: PRD_v2.md Section 6.1 (Database Schema)
--
-- This file contains only the static reference tables that live in Supabase.
-- Time-series and operational tables (telemetry, prices, optimization_runs, etc.)
-- live in TimescaleDB and are created by migrations/001_initial_schema.sql.
--
-- Run with: python scripts/run_migrations.py --target supabase
-- Requires DATABASE_URL pointing at Supabase.
--
-- NOTE: Cross-database foreign keys cannot be enforced at the DB level.
--       Application code is responsible for referential consistency between
--       the two databases (e.g. optimization_runs.depot_id must match a real
--       depots.depot_id in Supabase).

-- ============ REFERENCE DATA ============

-- Depots: Physical locations with charging infrastructure
-- Per PRD Section 3.1.1: Each depot runs its own optimization independently
CREATE TABLE IF NOT EXISTS depots (
    depot_id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                   VARCHAR(255) NOT NULL,
    latitude               DOUBLE PRECISION NOT NULL,
    longitude              DOUBLE PRECISION NOT NULL,
    timezone               VARCHAR(50) DEFAULT 'America/Los_Angeles',
    utility_id             VARCHAR(100),
    max_grid_kw            DOUBLE PRECISION NOT NULL,
    demand_charge_rate_kw  DOUBLE PRECISION DEFAULT 20.0,
    created_at             TIMESTAMPTZ DEFAULT NOW(),
    updated_at             TIMESTAMPTZ DEFAULT NOW()
);

-- Vehicles: Fleet vehicles with battery specs
-- Per PRD Section 6.1: max_charge_kw updated dynamically by OCPP MeterValues
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id      UUID NOT NULL REFERENCES depots(depot_id),
    external_id   VARCHAR(100) UNIQUE NOT NULL,
    vehicle_type  VARCHAR(50) NOT NULL,
    battery_kwh   DOUBLE PRECISION NOT NULL CHECK (battery_kwh > 0),
    max_charge_kw DOUBLE PRECISION NOT NULL CHECK (max_charge_kw > 0),
    id_tag        VARCHAR(100),
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Chargers: EVSE/charging stations
-- Per PRD Section 3.2: CCS only for MVP
CREATE TABLE IF NOT EXISTS chargers (
    charger_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id       UUID NOT NULL REFERENCES depots(depot_id),
    ocpp_id        VARCHAR(100) UNIQUE NOT NULL,
    rated_kw       DOUBLE PRECISION NOT NULL,
    efficiency     DOUBLE PRECISION DEFAULT 0.95,
    connector_type VARCHAR(50) DEFAULT 'CCS',
    status         VARCHAR(20) DEFAULT 'Available',
    created_at     TIMESTAMPTZ DEFAULT NOW()
);

-- Physical accessibility: which vehicles can use which chargers
-- Per PRD Section 2.3: Not all chargers physically reachable by all vehicles
CREATE TABLE IF NOT EXISTS charger_vehicle_access (
    charger_id    UUID NOT NULL REFERENCES chargers(charger_id),
    vehicle_id    UUID NOT NULL REFERENCES vehicles(vehicle_id),
    is_accessible BOOLEAN DEFAULT TRUE,
    notes         VARCHAR(255),
    PRIMARY KEY (charger_id, vehicle_id)
);

-- Battery storage: Stationary battery systems
-- Per PRD Section 8.1 Constraint 11: Battery dynamics with SoC limits
CREATE TABLE IF NOT EXISTS battery_storage (
    battery_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id     UUID NOT NULL REFERENCES depots(depot_id),
    capacity_kwh DOUBLE PRECISION NOT NULL CHECK (capacity_kwh > 0),
    max_power_kw DOUBLE PRECISION NOT NULL CHECK (max_power_kw > 0),
    efficiency   DOUBLE PRECISION DEFAULT 0.92 CHECK (efficiency > 0 AND efficiency <= 1),
    soc_min      DOUBLE PRECISION DEFAULT 0.2 CHECK (soc_min >= 0 AND soc_min < 1),
    soc_max      DOUBLE PRECISION DEFAULT 0.8 CHECK (soc_max > 0 AND soc_max <= 1),
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT battery_soc_range CHECK (soc_min < soc_max)
);

-- Schedules: Vehicle route schedules
-- Per PRD Section 2.3: Routes/schedules are input, not decision variables
CREATE TABLE IF NOT EXISTS schedules (
    schedule_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id         UUID NOT NULL REFERENCES vehicles(vehicle_id),
    route_id           VARCHAR(100),
    departure_time     TIMESTAMPTZ NOT NULL,
    return_time        TIMESTAMPTZ NOT NULL,
    actual_return_time TIMESTAMPTZ,
    energy_kwh         DOUBLE PRECISION,
    required_soc       DOUBLE PRECISION DEFAULT 1.0,
    dest_depot_id      UUID REFERENCES depots(depot_id),
    created_at         TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_schedules_vehicle_depart ON schedules (vehicle_id, departure_time);

-- ============ SEED DATA FOR DEVELOPMENT ============

INSERT INTO depots (depot_id, name, latitude, longitude, timezone, utility_id, max_grid_kw, demand_charge_rate_kw)
VALUES
    ('550e8400-e29b-41d4-a716-446655440001', 'Development Depot A', 37.7749, -122.4194, 'America/Los_Angeles', 'PG&E', 1000.0, 20.0),
    ('550e8400-e29b-41d4-a716-446655440002', 'Development Depot B', 37.3382, -121.8863, 'America/Los_Angeles', 'PG&E', 800.0, 20.0)
ON CONFLICT (depot_id) DO NOTHING;

INSERT INTO vehicles (vehicle_id, depot_id, external_id, vehicle_type, battery_kwh, max_charge_kw)
SELECT
    gen_random_uuid(),
    '550e8400-e29b-41d4-a716-446655440001'::uuid,
    'bus_' || LPAD(i::text, 2, '0'),
    CASE
        WHEN i <= 10 THEN 'bus_large'
        WHEN i <= 15 THEN 'bus_small'
        ELSE 'van'
    END,
    CASE
        WHEN i <= 10 THEN 324.0
        WHEN i <= 15 THEN 180.0
        ELSE 100.0
    END,
    CASE
        WHEN i <= 10 THEN 150.0
        WHEN i <= 15 THEN 100.0
        ELSE 50.0
    END
FROM generate_series(1, 20) AS i
ON CONFLICT (external_id) DO NOTHING;

INSERT INTO chargers (charger_id, depot_id, ocpp_id, rated_kw, efficiency, connector_type, status)
SELECT
    gen_random_uuid(),
    '550e8400-e29b-41d4-a716-446655440001'::uuid,
    'charger_' || LPAD(i::text, 2, '0'),
    CASE
        WHEN i <= 3 THEN 50.0
        WHEN i <= 8 THEN 80.0
        ELSE 150.0
    END,
    0.95,
    'CCS',
    'Available'
FROM generate_series(1, 10) AS i
ON CONFLICT (ocpp_id) DO NOTHING;

INSERT INTO battery_storage (battery_id, depot_id, capacity_kwh, max_power_kw, efficiency, soc_min, soc_max)
VALUES
    (gen_random_uuid(), '550e8400-e29b-41d4-a716-446655440001', 500.0, 100.0, 0.92, 0.2, 0.8)
ON CONFLICT DO NOTHING;
