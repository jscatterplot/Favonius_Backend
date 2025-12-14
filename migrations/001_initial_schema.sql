-- Favonius Energy EV Fleet Depot Optimization Platform
-- Initial Database Schema
--
-- Reference: PRD_v2.md Section 6.1 (Database Schema)
--
-- This schema is applied automatically when TimescaleDB container starts.
-- PostgreSQL 16 + TimescaleDB required.

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============ REFERENCE DATA ============

-- Depots: Physical locations with charging infrastructure
-- Per PRD Section 3.1.1: Each depot runs its own optimization independently
CREATE TABLE depots (
    depot_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            VARCHAR(255) NOT NULL,
    latitude        DOUBLE PRECISION NOT NULL,
    longitude       DOUBLE PRECISION NOT NULL,
    timezone        VARCHAR(50) DEFAULT 'America/Los_Angeles',
    utility_id      VARCHAR(100),  -- Utility provider identifier (e.g., 'PG&E', 'SCE')
    max_grid_kw     DOUBLE PRECISION NOT NULL,  -- max_site_power per PRD Section 8.1
    demand_charge_rate_kw  DOUBLE PRECISION DEFAULT 20.0,  -- $/kW per month
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Vehicles: Fleet vehicles with battery specs
-- Per PRD Section 6.1: max_charge_kw can be updated by OCPP MeterValues
CREATE TABLE vehicles (
    vehicle_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    external_id     VARCHAR(100) UNIQUE NOT NULL,  -- customer's vehicle ID (e.g., 'bus_101')
    vehicle_type    VARCHAR(50) NOT NULL,  -- 'bus_large', 'bus_small', 'van'
    battery_kwh     DOUBLE PRECISION NOT NULL CHECK (battery_kwh > 0),
    max_charge_kw   DOUBLE PRECISION NOT NULL CHECK (max_charge_kw > 0),  -- Default from config, updated by OCPP
    id_tag          VARCHAR(100),  -- OCPP idTag used in Authorize messages to map sessions to vehicles
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Chargers: EVSE/charging stations
-- Per PRD Section 3.2: CCS only for MVP
CREATE TABLE chargers (
    charger_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    ocpp_id         VARCHAR(100) UNIQUE NOT NULL,  -- OCPP charge point identifier
    rated_kw        DOUBLE PRECISION NOT NULL,
    efficiency      DOUBLE PRECISION DEFAULT 0.95,
    connector_type  VARCHAR(50) DEFAULT 'CCS',  -- MVP: CCS only
    status          VARCHAR(20) DEFAULT 'Available',  -- 'Available', 'Occupied', 'Faulted', 'Unavailable'
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Physical accessibility: which vehicles can use which chargers
-- Per PRD Section 2.3: Not all chargers physically reachable by all vehicles
CREATE TABLE charger_vehicle_access (
    charger_id      UUID NOT NULL REFERENCES chargers(charger_id),
    vehicle_id      UUID NOT NULL REFERENCES vehicles(vehicle_id),
    is_accessible   BOOLEAN DEFAULT TRUE,
    notes           VARCHAR(255),  -- e.g., "bay 3 blocked by pillar"
    PRIMARY KEY (charger_id, vehicle_id)
);

-- Battery storage: Stationary battery systems
-- Per PRD Section 8.1 Constraint 11: Battery dynamics with SoC limits
CREATE TABLE battery_storage (
    battery_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    capacity_kwh    DOUBLE PRECISION NOT NULL CHECK (capacity_kwh > 0),
    max_power_kw    DOUBLE PRECISION NOT NULL CHECK (max_power_kw > 0),
    efficiency      DOUBLE PRECISION DEFAULT 0.92 CHECK (efficiency > 0 AND efficiency <= 1),
    soc_min         DOUBLE PRECISION DEFAULT 0.2 CHECK (soc_min >= 0 AND soc_min < 1),
    soc_max         DOUBLE PRECISION DEFAULT 0.8 CHECK (soc_max > 0 AND soc_max <= 1),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT battery_soc_range CHECK (soc_min < soc_max)
);

-- ============ TIME-SERIES DATA ============

-- Telemetry: Vehicle state data from OCPP MeterValues
-- Per PRD Section 5.3: Vehicle SoC max age 15 minutes
CREATE TABLE telemetry (
    time            TIMESTAMPTZ NOT NULL,
    vehicle_id      UUID NOT NULL,
    charger_id      UUID REFERENCES chargers(charger_id),  -- Which charger reported this telemetry
    soc             DOUBLE PRECISION CHECK (soc >= 0 AND soc <= 1),  -- 0.0 to 1.0
    location_lat    DOUBLE PRECISION CHECK (location_lat >= -90 AND location_lat <= 90),
    location_lon    DOUBLE PRECISION CHECK (location_lon >= -180 AND location_lon <= 180),
    is_plugged      BOOLEAN,
    charging_kw     DOUBLE PRECISION CHECK (charging_kw >= 0),
    odometer_km     DOUBLE PRECISION CHECK (odometer_km >= 0),
    max_charge_kw   DOUBLE PRECISION CHECK (max_charge_kw > 0)  -- From OCPP MeterValues per PRD Section 8.4
);
SELECT create_hypertable('telemetry', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_telemetry_vehicle ON telemetry (vehicle_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_charger ON telemetry (charger_id, time DESC);

-- Prices: Energy and demand pricing data
-- Per PRD Section 5.3: Prices max age 24 hours
CREATE TABLE prices (
    time            TIMESTAMPTZ NOT NULL,
    depot_id        UUID NOT NULL,
    energy_kwh      DOUBLE PRECISION NOT NULL,  -- $/kWh
    demand_kw       DOUBLE PRECISION,           -- $/kW (if different by period)
    source          VARCHAR(50),                -- 'caiso_dam', 'utility_tou'
    PRIMARY KEY (time, depot_id)
);
SELECT create_hypertable('prices', 'time', if_not_exists => TRUE);

-- Weather forecasts: Weather data for surrogate model
-- Per PRD Section 5.3: Weather forecast max age 6 hours
CREATE TABLE weather_forecasts (
    time            TIMESTAMPTZ NOT NULL,
    depot_id        UUID NOT NULL,
    temp_f          DOUBLE PRECISION,
    temp_max_f      DOUBLE PRECISION,
    temp_min_f      DOUBLE PRECISION,
    precip_in       DOUBLE PRECISION,
    solar_rad       DOUBLE PRECISION,
    fetched_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (time, depot_id)
);
SELECT create_hypertable('weather_forecasts', 'time', if_not_exists => TRUE);

-- Building load: Non-EV building power consumption
-- Per PRD Section 3.2: Building load required for accurate grid power calculation
CREATE TABLE building_load (
    time            TIMESTAMPTZ NOT NULL,
    depot_id        UUID NOT NULL,
    power_kw        DOUBLE PRECISION NOT NULL,  -- Building load (kW)
    source          VARCHAR(50) NOT NULL,       -- 'meter', 'api', 'forecast'
    PRIMARY KEY (time, depot_id)
);
SELECT create_hypertable('building_load', 'time', if_not_exists => TRUE);

-- ============ OPERATIONAL DATA ============

-- Schedules: Vehicle route schedules
-- Per PRD Section 2.3: Routes/schedules are input, not decision variables
CREATE TABLE schedules (
    schedule_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vehicle_id      UUID NOT NULL REFERENCES vehicles(vehicle_id),
    route_id        VARCHAR(100),
    departure_time  TIMESTAMPTZ NOT NULL,
    return_time     TIMESTAMPTZ NOT NULL,
    actual_return_time TIMESTAMPTZ,  -- Updated when vehicle actually returns
    energy_kwh      DOUBLE PRECISION,  -- Estimated energy consumption (kWh) from surrogate model
    required_soc    DOUBLE PRECISION DEFAULT 1.0,
    dest_depot_id   UUID REFERENCES depots(depot_id),  -- if different (inter-depot)
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_schedules_vehicle_depart ON schedules (vehicle_id, departure_time);

-- Optimization runs: Records of optimization executions
-- Per PRD Section 8.5.1: Status includes 'optimal', 'feasible', 'degraded', 'infeasible'
CREATE TABLE optimization_runs (
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    run_time        TIMESTAMPTZ DEFAULT NOW(),
    trigger_reason  VARCHAR(50) NOT NULL,  -- 'scheduled', 'price_spike', 'soc_deviation', 'return_time_deviation', 'interdepot_handoff'
    horizon_start   TIMESTAMPTZ NOT NULL,
    horizon_end     TIMESTAMPTZ NOT NULL,
    solve_time_s    DOUBLE PRECISION,
    objective_value DOUBLE PRECISION,
    peak_demand_kw  DOUBLE PRECISION,
    status          VARCHAR(20) DEFAULT 'completed',  -- 'optimal', 'feasible', 'degraded', 'infeasible', 'timeout'
    solver_used     VARCHAR(20) DEFAULT 'gurobi',  -- 'gurobi' or 'highs' per PRD Section 8.2
    schedule_json   JSONB NOT NULL  -- Full optimization schedule
);
CREATE INDEX IF NOT EXISTS idx_opt_runs_depot ON optimization_runs (depot_id, run_time DESC);

-- Charging commands: OCPP SetChargingProfile commands sent to chargers
CREATE TABLE charging_commands (
    command_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID REFERENCES optimization_runs(run_id),
    charger_id      UUID NOT NULL REFERENCES chargers(charger_id),
    vehicle_id      UUID REFERENCES vehicles(vehicle_id),
    issued_at       TIMESTAMPTZ DEFAULT NOW(),
    profile_json    JSONB NOT NULL,  -- SetChargingProfile payload
    status          VARCHAR(20) DEFAULT 'pending',  -- 'pending', 'accepted', 'rejected'
    response_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_commands_run ON charging_commands (run_id);

-- Inter-depot messages: Vehicle handoff coordination
-- Per PRD Section 5.4: Inter-depot handoff flow
CREATE TABLE interdepot_messages (
    message_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    origin_depot_id UUID NOT NULL REFERENCES depots(depot_id),
    dest_depot_id   UUID NOT NULL REFERENCES depots(depot_id),
    vehicle_id      UUID NOT NULL REFERENCES vehicles(vehicle_id),
    departure_time  TIMESTAMPTZ NOT NULL,
    expected_soc    DOUBLE PRECISION NOT NULL CHECK (expected_soc >= 0 AND expected_soc <= 1),
    arrival_time    TIMESTAMPTZ NOT NULL,
    battery_kwh     DOUBLE PRECISION NOT NULL CHECK (battery_kwh > 0),
    max_charge_kw   DOUBLE PRECISION NOT NULL CHECK (max_charge_kw > 0),
    status          VARCHAR(20) DEFAULT 'pending' CHECK (status IN ('pending', 'acknowledged', 'arrived')),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ,
    arrived_at      TIMESTAMPTZ,
    CONSTRAINT valid_depot_pair CHECK (origin_depot_id != dest_depot_id),
    CONSTRAINT valid_timing CHECK (arrival_time > departure_time)
);
CREATE INDEX IF NOT EXISTS idx_interdepot_dest_status ON interdepot_messages (dest_depot_id, status);

-- Trigger log: Records of re-optimization triggers
-- Per PRD Section 5.1: Event-driven and periodic triggers
CREATE TABLE trigger_log (
    trigger_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL REFERENCES depots(depot_id),
    trigger_type    VARCHAR(50) NOT NULL,  -- 'soc_deviation', 'price_change', 'return_time_deviation', 'interdepot_handoff', 'scheduled'
    trigger_time    TIMESTAMPTZ DEFAULT NOW(),
    details         JSONB,  -- e.g., {vehicle_id, expected_soc, actual_soc, deviation}
    run_id          UUID REFERENCES optimization_runs(run_id)  -- Resulting optimization run
);
CREATE INDEX IF NOT EXISTS idx_trigger_depot ON trigger_log (depot_id, trigger_time DESC);

-- ============ SEED DATA FOR DEVELOPMENT ============

-- Insert development depot
INSERT INTO depots (depot_id, name, latitude, longitude, timezone, utility_id, max_grid_kw, demand_charge_rate_kw)
VALUES
    ('550e8400-e29b-41d4-a716-446655440001', 'Development Depot A', 37.7749, -122.4194, 'America/Los_Angeles', 'PG&E', 1000.0, 20.0),
    ('550e8400-e29b-41d4-a716-446655440002', 'Development Depot B', 37.3382, -121.8863, 'America/Los_Angeles', 'PG&E', 800.0, 20.0)
ON CONFLICT (depot_id) DO NOTHING;

-- Insert development vehicles (20 vehicles for Depot A)
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
        WHEN i <= 10 THEN 324.0  -- bus_large
        WHEN i <= 15 THEN 180.0  -- bus_small
        ELSE 100.0              -- van
    END,
    CASE
        WHEN i <= 10 THEN 150.0  -- bus_large
        WHEN i <= 15 THEN 100.0  -- bus_small
        ELSE 50.0               -- van
    END
FROM generate_series(1, 20) AS i
ON CONFLICT (external_id) DO NOTHING;

-- Insert development chargers (10 chargers for Depot A)
INSERT INTO chargers (charger_id, depot_id, ocpp_id, rated_kw, efficiency, connector_type, status)
SELECT
    gen_random_uuid(),
    '550e8400-e29b-41d4-a716-446655440001'::uuid,
    'charger_' || LPAD(i::text, 2, '0'),
    CASE
        WHEN i <= 3 THEN 50.0   -- 3 chargers @ 50kW
        WHEN i <= 8 THEN 80.0   -- 5 chargers @ 80kW
        ELSE 150.0              -- 2 chargers @ 150kW
    END,
    0.95,
    'CCS',
    'Available'
FROM generate_series(1, 10) AS i
ON CONFLICT (ocpp_id) DO NOTHING;

-- Insert development battery storage (1 for Depot A)
INSERT INTO battery_storage (battery_id, depot_id, capacity_kwh, max_power_kw, efficiency, soc_min, soc_max)
VALUES
    (gen_random_uuid(), '550e8400-e29b-41d4-a716-446655440001', 500.0, 100.0, 0.92, 0.2, 0.8)
ON CONFLICT DO NOTHING;

-- ============ PERMISSIONS ============

-- Grant permissions to favonius user (created by docker-compose)
-- Note: This assumes the database was created with user 'favonius'
DO $$
BEGIN
    -- Grant on tables
    EXECUTE 'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO favonius';
    EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO favonius';
EXCEPTION
    WHEN undefined_object THEN
        -- User doesn't exist yet, will be created by PostgreSQL
        NULL;
END $$;

-- Enable compression on hypertables after data starts accumulating
-- Per PRD Section 5.2: TimescaleDB compression for long-term storage
-- Note: Run this after tables have data (e.g., after 7 days)
-- SELECT add_compression_policy('telemetry', INTERVAL '7 days');
-- SELECT add_compression_policy('prices', INTERVAL '7 days');
-- SELECT add_compression_policy('weather_forecasts', INTERVAL '7 days');
-- SELECT add_compression_policy('building_load', INTERVAL '7 days');
