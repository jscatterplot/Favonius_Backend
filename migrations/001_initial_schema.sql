-- Favonius Energy EV Fleet Depot Optimization Platform
-- Initial Database Schema
--
-- Reference: PRD_v2.md Section 6.1 (Database Schema)
--
-- This schema is applied automatically when TimescaleDB container starts.
-- PostgreSQL 16 + TimescaleDB required.
--
-- Static reference tables (depots/sites, vehicles, charging_stations,
-- organizations, schedules, battery_storage, charger_vehicle_access) live
-- exclusively in Supabase (`pools.static`).  This file creates only the
-- TimescaleDB time-series and operational tables.  Depot/vehicle/charger
-- identity columns on operational tables are plain UUID references — no FKs
-- to static shadow copies — matching the post-028/029 production posture.

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ============ TIME-SERIES DATA ============

-- Telemetry: Vehicle state data from OCPP MeterValues
-- Per PRD Section 5.3: Vehicle SoC max age 15 minutes
CREATE TABLE IF NOT EXISTS telemetry (
    time            TIMESTAMPTZ NOT NULL,
    vehicle_id      UUID NOT NULL,
    charger_id      UUID,  -- Which charger reported this telemetry
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
CREATE TABLE IF NOT EXISTS prices (
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
CREATE TABLE IF NOT EXISTS weather_forecasts (
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
CREATE TABLE IF NOT EXISTS building_load (
    time            TIMESTAMPTZ NOT NULL,
    depot_id        UUID NOT NULL,
    power_kw        DOUBLE PRECISION NOT NULL,  -- Building load (kW)
    source          VARCHAR(50) NOT NULL,       -- 'meter', 'api', 'forecast'
    PRIMARY KEY (time, depot_id)
);
SELECT create_hypertable('building_load', 'time', if_not_exists => TRUE);

-- ============ OPERATIONAL DATA ============

-- Optimization runs: Records of optimization executions
-- Per PRD Section 8.5.1: Status includes 'optimal', 'feasible', 'degraded', 'infeasible'
CREATE TABLE IF NOT EXISTS optimization_runs (
    run_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL,
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
CREATE TABLE IF NOT EXISTS charging_commands (
    command_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id          UUID REFERENCES optimization_runs(run_id),
    charger_id      UUID NOT NULL,
    vehicle_id      UUID,
    issued_at       TIMESTAMPTZ DEFAULT NOW(),
    profile_json    JSONB NOT NULL,  -- SetChargingProfile payload
    status          VARCHAR(20) DEFAULT 'pending',  -- 'pending', 'accepted', 'rejected'
    response_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_commands_run ON charging_commands (run_id);

-- Inter-depot messages: Vehicle handoff coordination
-- Per PRD Section 5.4: Inter-depot handoff flow
CREATE TABLE IF NOT EXISTS interdepot_messages (
    message_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    origin_depot_id UUID NOT NULL,
    dest_depot_id   UUID NOT NULL,
    vehicle_id      UUID NOT NULL,
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
CREATE TABLE IF NOT EXISTS trigger_log (
    trigger_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id        UUID NOT NULL,
    trigger_type    VARCHAR(50) NOT NULL,  -- 'soc_deviation', 'price_change', 'return_time_deviation', 'interdepot_handoff', 'scheduled'
    trigger_time    TIMESTAMPTZ DEFAULT NOW(),
    details         JSONB,  -- e.g., {vehicle_id, expected_soc, actual_soc, deviation}
    run_id          UUID REFERENCES optimization_runs(run_id)  -- Resulting optimization run
);
CREATE INDEX IF NOT EXISTS idx_trigger_depot ON trigger_log (depot_id, trigger_time DESC);

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
