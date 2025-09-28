# TimescaleDB Configuration for V2G Time-Series Data

## Database Architecture
TimescaleDB stores all historical time-series data for analytics, billing, and optimization model training. The system must handle high-frequency telemetry ingestion while supporting complex analytical queries.

## Core Schema Design

### Primary Tables

#### charging_sessions
Main table for tracking charging/discharging sessions:
```sql
CREATE TABLE charging_sessions (
    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER NOT NULL,
    connector_id INTEGER NOT NULL,
    vehicle_id VARCHAR(255),
    id_token VARCHAR(255),
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ,
    start_soc_percent DECIMAL(5,2),
    end_soc_percent DECIMAL(5,2),
    energy_delivered_kwh DECIMAL(10,3),
    energy_received_kwh DECIMAL(10,3),
    max_charge_power_kw DECIMAL(8,2),
    max_discharge_power_kw DECIMAL(8,2),
    operation_mode VARCHAR(50),
    fleet_operator_id UUID,
    site_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_sessions_station_time ON charging_sessions(station_id, start_time DESC);
CREATE INDEX idx_sessions_fleet ON charging_sessions(fleet_operator_id, start_time DESC);
```

#### telemetry_data
High-frequency measurements hypertable:
```sql
CREATE TABLE telemetry_data (
    time TIMESTAMPTZ NOT NULL,
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER NOT NULL,
    connector_id INTEGER NOT NULL,
    session_id UUID,
    power_kw DECIMAL(8,2),
    energy_kwh DECIMAL(10,3),
    voltage_v DECIMAL(6,2),
    current_a DECIMAL(8,2),
    frequency_hz DECIMAL(5,2),
    soc_percent DECIMAL(5,2),
    temperature_c DECIMAL(5,2),
    grid_frequency_mhz INTEGER,
    reactive_power_kvar DECIMAL(8,2),
    power_factor DECIMAL(3,2)
);

SELECT create_hypertable('telemetry_data', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX idx_telemetry_station ON telemetry_data(station_id, time DESC);
```

#### optimization_decisions
Store optimization algorithm outputs:
```sql
CREATE TABLE optimization_decisions (
    decision_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    time TIMESTAMPTZ NOT NULL,
    optimization_window_start TIMESTAMPTZ NOT NULL,
    optimization_window_end TIMESTAMPTZ NOT NULL,
    fleet_operator_id UUID,
    site_id UUID,
    algorithm_version VARCHAR(50),
    objective_function VARCHAR(100),
    objective_value DECIMAL(12,2),
    computation_time_ms INTEGER,
    constraints_satisfied BOOLEAN,
    decision_payload JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

SELECT create_hypertable('optimization_decisions', 'time', chunk_time_interval => INTERVAL '1 day');
```

#### charging_schedules
V2G schedules sent to chargers:
```sql
CREATE TABLE charging_schedules (
    schedule_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    station_id VARCHAR(255) NOT NULL,
    evse_id INTEGER NOT NULL,
    decision_id UUID REFERENCES optimization_decisions(decision_id),
    profile_id VARCHAR(255) NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    schedule_periods JSONB NOT NULL,
    priority INTEGER DEFAULT 0,
    stacking_level INTEGER DEFAULT 0,
    purpose VARCHAR(50),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    executed_at TIMESTAMPTZ,
    execution_status VARCHAR(50)
);

CREATE INDEX idx_schedules_station ON charging_schedules(station_id, start_time DESC);
```

#### electricity_prices
Market price data for optimization:
```sql
CREATE TABLE electricity_prices (
    time TIMESTAMPTZ NOT NULL,
    node_id VARCHAR(100) NOT NULL,
    market_type VARCHAR(50) NOT NULL, -- 'DAM', 'RTM', 'FMM'
    lmp_price_mwh DECIMAL(10,2),
    energy_component_mwh DECIMAL(10,2),
    congestion_component_mwh DECIMAL(10,2),
    loss_component_mwh DECIMAL(10,2),
    ghg_adder_mwh DECIMAL(8,2),
    price_confidence DECIMAL(3,2),
    forecast_horizon_minutes INTEGER
);

SELECT create_hypertable('electricity_prices', 'time', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX idx_prices_node ON electricity_prices(node_id, time DESC);
```

#### grid_signals
External grid control signals:
```sql
CREATE TABLE grid_signals (
    time TIMESTAMPTZ NOT NULL,
    signal_type VARCHAR(50) NOT NULL, -- 'frequency', 'demand_response', 'regulation'
    signal_value DECIMAL(10,4),
    target_response_kw DECIMAL(10,2),
    response_deadline TIMESTAMPTZ,
    compensation_rate_kwh DECIMAL(8,2),
    grid_operator VARCHAR(100),
    region VARCHAR(100)
);

SELECT create_hypertable('grid_signals', 'time', chunk_time_interval => INTERVAL '1 day');
```

### Continuous Aggregates

#### hourly_energy_aggregates
Pre-computed hourly statistics:
```sql
CREATE MATERIALIZED VIEW hourly_energy_aggregates
WITH (timescaledb.continuous) AS
SELECT 
    time_bucket('1 hour', time) AS hour,
    station_id,
    AVG(power_kw) AS avg_power_kw,
    MAX(power_kw) AS max_power_kw,
    MIN(power_kw) AS min_power_kw,
    SUM(CASE WHEN power_kw > 0 THEN power_kw * 1/120 ELSE 0 END) AS energy_charged_kwh,
    SUM(CASE WHEN power_kw < 0 THEN ABS(power_kw) * 1/120 ELSE 0 END) AS energy_discharged_kwh,
    AVG(soc_percent) AS avg_soc,
    COUNT(*) AS sample_count
FROM telemetry_data
GROUP BY hour, station_id;

SELECT add_continuous_aggregate_policy('hourly_energy_aggregates',
    start_offset => INTERVAL '3 hours',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour');
```

#### daily_fleet_metrics
Fleet-level daily summaries:
```sql
CREATE MATERIALIZED VIEW daily_fleet_metrics
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', cs.start_time) AS day,
    cs.fleet_operator_id,
    COUNT(DISTINCT cs.station_id) AS active_stations,
    COUNT(DISTINCT cs.session_id) AS total_sessions,
    SUM(cs.energy_delivered_kwh) AS total_energy_charged_kwh,
    SUM(cs.energy_received_kwh) AS total_energy_discharged_kwh,
    AVG(EXTRACT(EPOCH FROM (cs.end_time - cs.start_time))/3600) AS avg_session_duration_hours
FROM charging_sessions cs
WHERE cs.end_time IS NOT NULL
GROUP BY day, cs.fleet_operator_id;
```

### Compression Policies
Configure automatic compression for older data:
```sql
ALTER TABLE telemetry_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'station_id',
    timescaledb.compress_orderby = 'time DESC'
);

SELECT add_compression_policy('telemetry_data', INTERVAL '7 days');
SELECT add_compression_policy('electricity_prices', INTERVAL '30 days');
```

### Retention Policies
Automatic data lifecycle management:
```sql
SELECT add_retention_policy('telemetry_data', INTERVAL '2 years');
SELECT add_retention_policy('grid_signals', INTERVAL '1 year');
-- Keep optimization decisions indefinitely for model training
```

## Performance Optimization

### Indexing Strategy
- Create BRIN indexes on time columns for range queries
- Use GIN indexes on JSONB columns for schedule searches
- Partial indexes for frequently filtered columns
- Covering indexes for common query patterns

### Partitioning Configuration
- Chunk interval: 1 day for high-frequency data
- Chunk interval: 1 week for session data
- Space partitioning by station_id for multi-tenant queries

### Query Optimization
- Use time_bucket for aggregations
- Leverage continuous aggregates for dashboards
- Implement query result caching in Redis
- Use COPY for bulk inserts

## Data Ingestion Pipeline

### Kafka Consumer Configuration
- Topic: `charger.telemetry`
- Consumer group: `timescaledb-writer`
- Batch size: 1000 records
- Commit interval: 5 seconds
- Compression: snappy

### Bulk Insert Strategy
```sql
-- Use COPY for batch inserts
COPY telemetry_data FROM STDIN WITH (FORMAT csv, HEADER false);

-- Alternative: Multi-row INSERT with ON CONFLICT
INSERT INTO telemetry_data (time, station_id, ...) 
VALUES 
    ($1, $2, ...),
    ($3, $4, ...)
ON CONFLICT (time, station_id) DO NOTHING;
```

### Connection Pool Settings
- Max connections: 100
- Pool size: 20
- Statement timeout: 30 seconds
- Idle timeout: 10 minutes

## Monitoring and Maintenance

### Key Metrics to Track
- Chunk count and size distribution
- Compression ratios and savings
- Query execution times
- Cache hit ratios
- Replication lag
- Disk usage trends

### Maintenance Tasks
- Daily: Update continuous aggregates
- Weekly: Refresh materialized views
- Monthly: Analyze tables for statistics
- Quarterly: Review and adjust retention policies

## Integration with Other Systems

### Redis Cache Patterns
- Cache recent telemetry (last 24 hours)
- Store aggregated metrics for dashboards
- Maintain optimization decision cache

### Supabase Replication
- Set up logical replication for user-facing tables
- Filter replication to exclude high-frequency telemetry
- Maintain read replicas for analytics queries

### Julia Optimization Interface
- Provide read-only connection for historical data
- Expose materialized views for model training
- Implement data export procedures for batch processing

## Backup and Recovery

### Backup Strategy
- Continuous WAL archiving to S3
- Daily base backups
- Point-in-time recovery capability
- Cross-region replication for disaster recovery

### Recovery Procedures
- RPO: 1 minute (WAL archiving)
- RTO: 30 minutes (automated recovery)
- Test recovery procedures monthly
- Maintain runbooks for common scenarios