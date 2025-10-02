# TimescaleDB Integration for EV Charging Platform

This document describes the TimescaleDB integration implementation using Tiger Cloud (formerly TimescaleDB Cloud) for high-performance time-series data storage and analytics.

## Overview

The TimescaleDB integration provides:
- **High-Frequency Telemetry Storage**: 30-second sampling with hypertables
- **Real-Time Data Ingestion**: Kafka-based streaming from WebSocket handlers
- **Advanced Analytics**: Continuous aggregates and materialized views
- **Cost Optimization**: Compression and retention policies
- **Performance Monitoring**: Query optimization and indexing

## Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   WebSocket     │    │   Kafka         │    │   TimescaleDB    │
│   Handler       │───►│   Streams       │───►│   (Tiger Cloud)  │
│   (OCPP 2.1)    │    │                 │    │                 │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Redis Cache   │    │   Telemetry     │    │   Analytics     │
│   (Real-time)   │    │   Ingestion     │    │   Service       │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

## Tiger Cloud Configuration

### Connection Details
Using your Tiger Cloud credentials from `Tiger Cloud Credentials.env`:

```bash
# Tiger Cloud Connection
TIMESCALE_SERVICE_URL=postgres://tsdbadmin:lyqgv8a0j1bt1zaa@avws3fxn3w.rspy6d4hg0.tsdb.cloud.timescale.com:32634/tsdb?sslmode=require
PGHOST=avws3fxn3w.rspy6d4hg0.tsdb.cloud.timescale.com
PGPORT=32634
PGDATABASE=tsdb
PGUSER=tsdbadmin
PGPASSWORD=lyqgv8a0j1bt1zaa
PGSSLMODE=require
```

### Performance Settings
```bash
# Connection Pool Settings
TIMESCALE_MAX_CONNECTIONS=100
TIMESCALE_POOL_SIZE=20
TIMESCALE_STATEMENT_TIMEOUT=30
TIMESCALE_IDLE_TIMEOUT=600

# Hypertable Configuration
TIMESCALE_CHUNK_INTERVAL=1 day
TIMESCALE_COMPRESSION_AFTER=7 days
TIMESCALE_RETENTION_PERIOD=2 years
```

## Database Schema

### Core Hypertables

#### telemetry_data
High-frequency measurements (30-second intervals):
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
```

#### charging_sessions
Session-level data with metadata:
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
    sync_status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
```

#### optimization_decisions
Optimization algorithm outputs:
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
    sync_status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

SELECT create_hypertable('optimization_decisions', 'time', chunk_time_interval => INTERVAL '1 day');
```

#### electricity_prices
Market price data for optimization:
```sql
CREATE TABLE electricity_prices (
    time TIMESTAMPTZ NOT NULL,
    node_id VARCHAR(100) NOT NULL,
    market_type VARCHAR(50) NOT NULL,
    lmp_price_mwh DECIMAL(10,2),
    energy_component_mwh DECIMAL(10,2),
    congestion_component_mwh DECIMAL(10,2),
    loss_component_mwh DECIMAL(10,2),
    ghg_adder_mwh DECIMAL(8,2),
    price_confidence DECIMAL(3,2),
    forecast_horizon_minutes INTEGER
);

SELECT create_hypertable('electricity_prices', 'time', chunk_time_interval => INTERVAL '1 day');
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

### Compression & Retention Policies

#### Compression Configuration
```sql
-- Enable compression on telemetry data
ALTER TABLE telemetry_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'station_id',
    timescaledb.compress_orderby = 'time DESC'
);

-- Add compression policy (compress after 7 days)
SELECT add_compression_policy('telemetry_data', INTERVAL '7 days');
```

#### Retention Policies
```sql
-- Retain telemetry data for 2 years
SELECT add_retention_policy('telemetry_data', INTERVAL '2 years');

-- Retain grid signals for 1 year
SELECT add_retention_policy('grid_signals', INTERVAL '1 year');

-- Keep optimization decisions indefinitely for model training
-- Keep charging sessions indefinitely for billing
```

## Data Ingestion Pipeline

### Kafka Consumer Configuration
- **Topic**: `charger.events`
- **Consumer Group**: `websocket-handler-telemetry`
- **Batch Size**: 1000 records
- **Commit Interval**: 5 seconds
- **Compression**: snappy

### Message Types
```json
{
  "type": "telemetry",
  "timestamp": "2024-01-15T10:30:00Z",
  "station_id": "STATION_001",
  "evse_id": 1,
  "connector_id": 1,
  "session_id": "uuid-here",
  "power_kw": 7.5,
  "energy_kwh": 0.125,
  "voltage_v": 240.0,
  "current_a": 31.25,
  "frequency_hz": 60.0,
  "soc_percent": 45.2,
  "temperature_c": 25.0,
  "grid_frequency_mhz": 60000,
  "reactive_power_kvar": 0.5,
  "power_factor": 0.99
}
```

### Batch Processing
- **Buffer Size**: 1000 records per station
- **Timeout**: 5 seconds
- **Bulk Insert**: Using COPY for performance
- **Error Handling**: Retry with exponential backoff

## Analytics Capabilities

### Energy Analytics
- **Peak Power Analysis**: 15-minute demand peaks
- **Energy Efficiency**: Power factor and conversion efficiency
- **V2G Performance**: Discharge energy and revenue
- **Hourly Aggregates**: Pre-computed statistics

### Cost Analytics
- **Electricity Prices**: Real-time and forecasted LMP
- **Optimization Savings**: Algorithm performance metrics
- **Demand Charge Analysis**: Peak shaving effectiveness
- **Revenue Tracking**: V2G and grid services income

### Performance Analytics
- **Uptime Metrics**: Station availability and reliability
- **Efficiency Metrics**: Power factor and conversion rates
- **Reliability Metrics**: Session success rates
- **Overall Score**: Weighted performance indicator

## API Integration

### TimescaleDB Client Methods
```python
# Telemetry operations
await timescale_client.insert_telemetry_batch(telemetry_data)
await timescale_client.get_telemetry_data(station_id, start_time, end_time)

# Session operations
session_id = await timescale_client.create_charging_session(session_data)
await timescale_client.update_charging_session(session_id, updates)

# Analytics operations
analytics = await timescale_client.get_energy_usage_summary(fleet_id, start_time, end_time)
hourly_data = await timescale_client.get_hourly_energy_aggregates(station_id, start_time, end_time)
```

### Analytics Service Methods
```python
# Comprehensive analytics
energy_analytics = await analytics_service.get_energy_usage_analytics(fleet_id, start_time, end_time)
cost_analytics = await analytics_service.get_cost_analytics(fleet_id, start_time, end_time)
performance_analytics = await analytics_service.get_performance_analytics(fleet_id, start_time, end_time)

# Custom queries
results = await analytics_service.get_custom_analytics(query, params)

# Data export
export_data = await analytics_service.export_analytics_data(fleet_id, start_time, end_time, format='json')
```

## Performance Optimization

### Indexing Strategy
- **Time-based indexes**: BRIN indexes on time columns
- **Station indexes**: B-tree indexes on station_id
- **Composite indexes**: Multi-column indexes for common queries
- **Partial indexes**: Filtered indexes for specific conditions

### Query Optimization
- **Time Buckets**: Use time_bucket for aggregations
- **Continuous Aggregates**: Pre-computed materialized views
- **Compression**: Automatic compression for older data
- **Partitioning**: Space partitioning by station_id

### Connection Management
- **Connection Pooling**: AsyncPG pool with 100 max connections
- **Statement Timeout**: 30-second query timeout
- **Idle Timeout**: 10-minute idle connection timeout
- **Health Monitoring**: Connection health checks

## Monitoring & Observability

### Key Metrics
- **Ingestion Rate**: Messages per second processed
- **Batch Performance**: Batch size and processing time
- **Query Performance**: Average query execution time
- **Compression Ratio**: Storage savings from compression
- **Chunk Distribution**: Hypertable chunk statistics

### Health Checks
```python
# TimescaleDB health check
health = await timescale_client.health_check()
# Returns: status, asyncpg, sqlalchemy, timescaledb, pool_size

# Telemetry ingestion health
health = await telemetry_ingestion_service.health_check()
# Returns: status, timescale, kafka, metrics
```

### Logging
- **Structured Logging**: JSON format with correlation IDs
- **Performance Logging**: Query execution times
- **Error Tracking**: Failed operations and retries
- **Audit Logging**: Data access and modifications

## Installation & Setup

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Initialize TimescaleDB Schema
```bash
python init_timescale.py
```

### 3. Start the Application
```bash
python -m src.websocket_handler.main
```

The application will start:
- **WebSocket Server**: Port 9000 (OCPP 2.1)
- **REST API Server**: Port 8080 (User-facing)
- **Telemetry Ingestion**: Kafka consumer
- **Health Check**: Port 8081
- **Metrics**: Port 8080/metrics

## Production Deployment

### Tiger Cloud Configuration
```bash
# Production environment variables
export ENVIRONMENT=production
export TIMESCALE_MAX_CONNECTIONS=100
export TIMESCALE_POOL_SIZE=50
export TIMESCALE_STATEMENT_TIMEOUT=60
```

### Docker Deployment
```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ ./src/
COPY init_timescale.py .

CMD ["python", "-m", "src.websocket_handler.main"]
```

### Kubernetes Deployment
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ev-charging-platform
spec:
  replicas: 3
  selector:
    matchLabels:
      app: ev-charging-platform
  template:
    metadata:
      labels:
        app: ev-charging-platform
    spec:
      containers:
      - name: websocket-handler
        image: ev-charging-platform:latest
        ports:
        - containerPort: 9000
        - containerPort: 8080
        env:
        - name: TIMESCALE_SERVICE_URL
          valueFrom:
            secretKeyRef:
              name: timescale-secrets
              key: service-url
        # ... other environment variables
```

## Troubleshooting

### Common Issues

#### Connection Errors
```bash
# Test Tiger Cloud connection
psql "postgres://tsdbadmin:lyqgv8a0j1bt1zaa@avws3fxn3w.rspy6d4hg0.tsdb.cloud.timescale.com:32634/tsdb?sslmode=require"

# Check TimescaleDB extension
psql -c "SELECT extname FROM pg_extension WHERE extname = 'timescaledb';"
```

#### Performance Issues
```sql
-- Check chunk distribution
SELECT chunk_name, range_start, range_end, table_bytes, index_bytes
FROM timescaledb_information.chunks
WHERE hypertable_name = 'telemetry_data'
ORDER BY range_start DESC
LIMIT 10;

-- Check compression status
SELECT chunk_name, compressed, before_compression_total_bytes, after_compression_total_bytes
FROM timescaledb_information.chunks
WHERE hypertable_name = 'telemetry_data' AND compressed = true;
```

#### Ingestion Issues
```bash
# Check Kafka consumer lag
kafka-consumer-groups.sh --bootstrap-server localhost:9092 \
  --group websocket-handler-telemetry --describe

# Check telemetry ingestion metrics
curl http://localhost:8081/health
```

### Performance Optimization

#### Query Optimization
```sql
-- Use time buckets for aggregations
SELECT time_bucket('1 hour', time) as hour, AVG(power_kw)
FROM telemetry_data
WHERE time >= NOW() - INTERVAL '24 hours'
GROUP BY hour;

-- Use continuous aggregates for dashboards
SELECT hour, avg_power_kw, energy_charged_kwh
FROM hourly_energy_aggregates
WHERE hour >= NOW() - INTERVAL '7 days';
```

#### Index Optimization
```sql
-- Create covering index for common queries
CREATE INDEX idx_telemetry_station_time_power 
ON telemetry_data(station_id, time DESC) 
INCLUDE (power_kw, soc_percent);

-- Create partial index for active sessions
CREATE INDEX idx_telemetry_active_sessions 
ON telemetry_data(time DESC) 
WHERE session_id IS NOT NULL;
```

## Support & Documentation

### Additional Resources
- [TimescaleDB Documentation](https://docs.timescale.com/)
- [Tiger Cloud Documentation](https://www.timescale.com/products)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [AsyncPG Documentation](https://magicstack.github.io/asyncpg/)

### Contact
For technical support or questions about the TimescaleDB integration, please contact the development team or create an issue in the project repository.
