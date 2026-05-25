-- VDV 463 Transit Operations Integration
-- Per favonius_development_plan_v3.md Phase 5 Step 5.3 and PRD Section 9.6
-- Requires: 001_initial_schema.sql (TimescaleDB extension)

CREATE TABLE IF NOT EXISTS vdv463_charging_requests (
    id UUID DEFAULT gen_random_uuid(),
    depot_id UUID NOT NULL,
    charging_request_id TEXT NOT NULL,
    presystem_id TEXT NOT NULL,
    vehicle_id UUID NOT NULL,
    charging_point_id UUID,
    priority INTEGER DEFAULT 1,
    charging_instruction TEXT DEFAULT 'Normal',  -- 'Normal', 'Changed', 'Terminate'
    expected_arrival TIMESTAMPTZ,
    expected_soc_at_arrival REAL,
    min_target_soc REAL NOT NULL,
    max_target_soc REAL NOT NULL,
    requested_departure TIMESTAMPTZ,
    preconditioning_type TEXT,              -- 'manual', 'automatic', or NULL
    preconditioning_start TIMESTAMPTZ,      -- For manual: hvacPreconditioningStartTime
    ambient_temperature REAL,               -- For automatic
    requested_start_time TIMESTAMPTZ,       -- For automatic
    requested_finish_time TIMESTAMPTZ,      -- For automatic
    hvac_aux_power INTEGER,                  -- W (manual: hvacAuxiliaryConsumerPower)
    system_aux_power INTEGER,               -- W (manual: systemAuxiliaryConsumerPower)
    message_id TEXT NOT NULL DEFAULT '',
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT DEFAULT 'active' CHECK (status IN ('active', 'completed', 'terminated')),
    validation_status TEXT,                 -- NULL | 'ok' | 'warning' | 'error'
    -- TimescaleDB requires that all UNIQUE constraints and PRIMARY KEYs on a
    -- hypertable include the partitioning column ('received_at' here).
    -- Use a composite primary key and unique constraint that both include
    -- 'received_at' to satisfy this requirement.
    PRIMARY KEY (id, received_at),
    UNIQUE(depot_id, charging_request_id, presystem_id, received_at)
);

CREATE INDEX IF NOT EXISTS idx_vdv463_charging_requests_depot_received
    ON vdv463_charging_requests (depot_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_vdv463_charging_requests_status
    ON vdv463_charging_requests (depot_id, status) WHERE status = 'active';

SELECT create_hypertable('vdv463_charging_requests', 'received_at', if_not_exists => TRUE);

-- VDV 463 connection log
CREATE TABLE IF NOT EXISTS vdv463_connections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id UUID NOT NULL,
    presystem_id TEXT NOT NULL,
    system_type TEXT NOT NULL,              -- 'BMS' or 'ITCS'
    connected_at TIMESTAMPTZ DEFAULT NOW(),
    disconnected_at TIMESTAMPTZ,
    disconnect_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_vdv463_connections_depot
    ON vdv463_connections (depot_id, connected_at DESC);

-- VDV 463 errors (operator diagnostics)
CREATE TABLE IF NOT EXISTS vdv463_errors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    depot_id UUID NOT NULL,
    presystem_id TEXT NOT NULL,
    charging_request_id TEXT,
    error_code TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_vdv463_errors_depot_created
    ON vdv463_errors (depot_id, created_at DESC);

-- Last VDV 463 update timestamp per depot (for trigger monitor)
CREATE TABLE IF NOT EXISTS vdv463_depot_updates (
    depot_id UUID PRIMARY KEY,
    last_update_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
