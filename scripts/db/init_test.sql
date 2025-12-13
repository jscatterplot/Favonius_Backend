-- Test Database Initialization Script
-- Reference: PRD_v2.md#6-data-models

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Depots table
CREATE TABLE IF NOT EXISTS depots (
    depot_id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    location VARCHAR(255),
    n_chargers INTEGER NOT NULL DEFAULT 10,
    charger_power DECIMAL(10,2) NOT NULL DEFAULT 80.0,
    max_site_power DECIMAL(10,2) NOT NULL DEFAULT 1200.0,
    battery_capacity DECIMAL(10,2) DEFAULT 500.0,
    battery_power DECIMAL(10,2) DEFAULT 100.0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Vehicles table
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id VARCHAR(50) PRIMARY KEY,
    depot_id UUID REFERENCES depots(depot_id),
    battery_capacity DECIMAL(10,2) NOT NULL DEFAULT 324.0,
    efficiency DECIMAL(5,3) NOT NULL DEFAULT 0.95,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Chargers table
CREATE TABLE IF NOT EXISTS chargers (
    charger_id VARCHAR(50) PRIMARY KEY,
    depot_id UUID REFERENCES depots(depot_id),
    connector_id INTEGER NOT NULL DEFAULT 1,
    max_power DECIMAL(10,2) NOT NULL DEFAULT 80.0,
    status VARCHAR(20) DEFAULT 'Available',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Vehicle-Charger mapping
CREATE TABLE IF NOT EXISTS vehicle_charger_mapping (
    vehicle_id VARCHAR(50) REFERENCES vehicles(vehicle_id),
    charger_id VARCHAR(50) REFERENCES chargers(charger_id),
    connector_id INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (vehicle_id, charger_id)
);

-- Telemetry hypertable
CREATE TABLE IF NOT EXISTS telemetry (
    time TIMESTAMPTZ NOT NULL,
    vehicle_id VARCHAR(50) NOT NULL,
    soc DECIMAL(5,4),
    charging_power DECIMAL(10,2),
    odometer DECIMAL(12,2),
    location_lat DECIMAL(10,6),
    location_lon DECIMAL(10,6),
    status VARCHAR(20)
);

SELECT create_hypertable('telemetry', 'time', if_not_exists => TRUE);

-- Prices hypertable
CREATE TABLE IF NOT EXISTS prices (
    time TIMESTAMPTZ NOT NULL,
    node_id VARCHAR(50) NOT NULL,
    price DECIMAL(10,4) NOT NULL,
    price_type VARCHAR(20) DEFAULT 'LMP'
);

SELECT create_hypertable('prices', 'time', if_not_exists => TRUE);

-- Optimization runs table
CREATE TABLE IF NOT EXISTS optimization_runs (
    run_id UUID PRIMARY KEY,
    depot_id UUID REFERENCES depots(depot_id),
    trigger_reason VARCHAR(50),
    status VARCHAR(20) NOT NULL DEFAULT 'running',
    objective_value DECIMAL(15,2),
    peak_demand DECIMAL(10,2),
    solve_time DECIMAL(10,3),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

-- Charging commands table
CREATE TABLE IF NOT EXISTS charging_commands (
    command_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID REFERENCES optimization_runs(run_id),
    vehicle_id VARCHAR(50),
    charger_id VARCHAR(50),
    connector_id INTEGER DEFAULT 1,
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ,
    target_power DECIMAL(10,2),
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Inter-depot messages table
CREATE TABLE IF NOT EXISTS interdepot_messages (
    message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    origin_depot_id UUID,
    destination_depot_id UUID,
    vehicle_id VARCHAR(50),
    message_type VARCHAR(50) NOT NULL,
    payload JSONB,
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    acknowledged_at TIMESTAMPTZ
);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_telemetry_vehicle ON telemetry(vehicle_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_prices_node ON prices(node_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_opt_runs_depot ON optimization_runs(depot_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_commands_run ON charging_commands(run_id);
CREATE INDEX IF NOT EXISTS idx_interdepot_dest ON interdepot_messages(destination_depot_id, status);

-- Insert test depot
INSERT INTO depots (depot_id, name, location, n_chargers, charger_power, max_site_power)
VALUES 
    ('00000000-0000-0000-0000-000000000001', 'Test Depot A', 'Test Location A', 10, 80.0, 1200.0),
    ('00000000-0000-0000-0000-000000000002', 'Test Depot B', 'Test Location B', 8, 80.0, 1000.0)
ON CONFLICT (depot_id) DO NOTHING;

-- Insert test vehicles
INSERT INTO vehicles (vehicle_id, depot_id, battery_capacity)
SELECT 
    'bus_' || LPAD(i::text, 2, '0'),
    '00000000-0000-0000-0000-000000000001'::uuid,
    324.0
FROM generate_series(1, 20) AS i
ON CONFLICT (vehicle_id) DO NOTHING;

-- Insert test chargers
INSERT INTO chargers (charger_id, depot_id, connector_id, max_power)
SELECT 
    'charger_' || i,
    '00000000-0000-0000-0000-000000000001'::uuid,
    1,
    80.0
FROM generate_series(1, 10) AS i
ON CONFLICT (charger_id) DO NOTHING;

-- Grant permissions
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO favonius_test;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO favonius_test;
