"""TimescaleDB schema creation and management."""

import asyncio
import os
from typing import List, Dict, Any
import asyncpg
from datetime import datetime, timezone

from .config import TimescaleConfig
from .monitoring import get_logger


class TimescaleSchema:
    """TimescaleDB schema management."""
    
    def __init__(self, config: TimescaleConfig):
        """Initialize TimescaleDB schema manager."""
        self.config = config
        self.logger = get_logger(__name__)
    
    async def create_schema(self) -> None:
        """Create all TimescaleDB tables, hypertables, and policies."""
        try:
            # Connect to database
            conn = await asyncpg.connect(
                host=self.config.host,
                port=self.config.port,
                database=self.config.database,
                user=self.config.user,
                password=self.config.password,
                ssl=self.config.sslmode
            )
            
            try:
                # Enable TimescaleDB extension
                await self._enable_timescaledb(conn)
                
                # Create tables
                await self._create_tables(conn)
                
                # Create hypertables
                await self._create_hypertables(conn)
                
                # Create indexes
                await self._create_indexes(conn)
                
                # Create continuous aggregates
                await self._create_continuous_aggregates(conn)
                
                # Setup compression policies
                await self._setup_compression_policies(conn)
                
                # Setup retention policies
                await self._setup_retention_policies(conn)
                
                self.logger.info("TimescaleDB schema created successfully")
                
            finally:
                await conn.close()
                
        except Exception as e:
            self.logger.error(f"Failed to create TimescaleDB schema: {e}")
            raise
    
    async def _enable_timescaledb(self, conn: asyncpg.Connection) -> None:
        """Enable TimescaleDB extension."""
        try:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;")
            self.logger.info("TimescaleDB extension enabled")
        except Exception as e:
            self.logger.warning(f"TimescaleDB extension may already exist: {e}")
    
    async def _create_tables(self, conn: asyncpg.Connection) -> None:
        """Create all database tables."""
        
        # Charging sessions table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS charging_sessions (
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
        """)
        
        # Telemetry data table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry_data (
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
        """)
        
        # Optimization decisions table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS optimization_decisions (
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
        """)
        
        # Charging schedules table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS charging_schedules (
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
        """)
        
        # Electricity prices table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS electricity_prices (
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
        """)
        
        # Grid signals table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS grid_signals (
                time TIMESTAMPTZ NOT NULL,
                signal_type VARCHAR(50) NOT NULL,
                signal_value DECIMAL(10,4),
                target_response_kw DECIMAL(10,2),
                response_deadline TIMESTAMPTZ,
                compensation_rate_kwh DECIMAL(8,2),
                grid_operator VARCHAR(100),
                region VARCHAR(100)
            );
        """)
        
        # Vehicle telemetry table (for real-time vehicle state)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS vehicle_telemetry (
                time TIMESTAMPTZ NOT NULL,
                vehicle_id UUID NOT NULL,
                organization_id UUID,
                current_soc DECIMAL(5,2),
                current_power_kw DECIMAL(8,2),
                charging_status VARCHAR(50),
                location_lat DECIMAL(10,8),
                location_lon DECIMAL(11,8),
                last_seen TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        
        self.logger.info("All tables created successfully")
    
    async def _create_hypertables(self, conn: asyncpg.Connection) -> None:
        """Create TimescaleDB hypertables."""
        
        # Create hypertable for telemetry data
        await conn.execute(f"""
            SELECT create_hypertable('telemetry_data', 'time', 
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)
        
        # Create hypertable for optimization decisions
        await conn.execute(f"""
            SELECT create_hypertable('optimization_decisions', 'time', 
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)
        
        # Create hypertable for electricity prices
        await conn.execute(f"""
            SELECT create_hypertable('electricity_prices', 'time', 
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)
        
        # Create hypertable for grid signals
        await conn.execute(f"""
            SELECT create_hypertable('grid_signals', 'time', 
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)
        
        # Create hypertable for vehicle telemetry
        await conn.execute(f"""
            SELECT create_hypertable('vehicle_telemetry', 'time', 
                chunk_time_interval => INTERVAL '{self.config.chunk_time_interval}',
                if_not_exists => TRUE);
        """)
        
        self.logger.info("All hypertables created successfully")
    
    async def _create_indexes(self, conn: asyncpg.Connection) -> None:
        """Create database indexes for performance."""
        
        # Charging sessions indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_station_time 
            ON charging_sessions(station_id, start_time DESC);
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_fleet 
            ON charging_sessions(fleet_operator_id, start_time DESC);
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_sessions_sync_status 
            ON charging_sessions(sync_status, created_at);
        """)
        
        # Telemetry data indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telemetry_station 
            ON telemetry_data(station_id, time DESC);
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telemetry_session 
            ON telemetry_data(session_id, time DESC);
        """)
        
        # Optimization decisions indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_optimization_fleet 
            ON optimization_decisions(fleet_operator_id, time DESC);
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_optimization_sync_status 
            ON optimization_decisions(sync_status, created_at);
        """)
        
        # Charging schedules indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_schedules_station 
            ON charging_schedules(station_id, start_time DESC);
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_schedules_decision 
            ON charging_schedules(decision_id);
        """)
        
        # Electricity prices indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_prices_node 
            ON electricity_prices(node_id, time DESC);
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_prices_market 
            ON electricity_prices(market_type, time DESC);
        """)
        
        # Grid signals indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_grid_signals_type 
            ON grid_signals(signal_type, time DESC);
        """)
        
        # Vehicle telemetry indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vehicle_telemetry_vehicle 
            ON vehicle_telemetry(vehicle_id, time DESC);
        """)
        
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_vehicle_telemetry_org 
            ON vehicle_telemetry(organization_id, time DESC);
        """)
        
        self.logger.info("All indexes created successfully")
    
    async def _create_continuous_aggregates(self, conn: asyncpg.Connection) -> None:
        """Create continuous aggregates for performance."""
        
        # Hourly energy aggregates
        await conn.execute("""
            CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_energy_aggregates
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
        """)
        
        # Add continuous aggregate policy
        await conn.execute("""
            SELECT add_continuous_aggregate_policy('hourly_energy_aggregates',
                start_offset => INTERVAL '3 hours',
                end_offset => INTERVAL '1 hour',
                schedule_interval => INTERVAL '1 hour');
        """)
        
        # Daily fleet metrics
        await conn.execute("""
            CREATE MATERIALIZED VIEW IF NOT EXISTS daily_fleet_metrics
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
        """)
        
        # Add continuous aggregate policy for daily metrics
        await conn.execute("""
            SELECT add_continuous_aggregate_policy('daily_fleet_metrics',
                start_offset => INTERVAL '3 days',
                end_offset => INTERVAL '1 day',
                schedule_interval => INTERVAL '1 day');
        """)
        
        # Hourly optimization performance
        await conn.execute("""
            CREATE MATERIALIZED VIEW IF NOT EXISTS hourly_optimization_performance
            WITH (timescaledb.continuous) AS
            SELECT
                time_bucket('1 hour', time) AS hour,
                fleet_operator_id,
                COUNT(*) AS total_decisions,
                AVG(computation_time_ms) AS avg_computation_time_ms,
                COUNT(CASE WHEN constraints_satisfied THEN 1 END) AS successful_decisions,
                AVG(objective_value) AS avg_objective_value
            FROM optimization_decisions
            GROUP BY hour, fleet_operator_id;
        """)
        
        # Add continuous aggregate policy for optimization performance
        await conn.execute("""
            SELECT add_continuous_aggregate_policy('hourly_optimization_performance',
                start_offset => INTERVAL '3 hours',
                end_offset => INTERVAL '1 hour',
                schedule_interval => INTERVAL '1 hour');
        """)
        
        self.logger.info("All continuous aggregates created successfully")
    
    async def _setup_compression_policies(self, conn: asyncpg.Connection) -> None:
        """Setup compression policies for older data."""
        
        # Enable compression on telemetry data
        await conn.execute("""
            ALTER TABLE telemetry_data SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'station_id',
                timescaledb.compress_orderby = 'time DESC'
            );
        """)
        
        # Add compression policy
        await conn.execute(f"""
            SELECT add_compression_policy('telemetry_data', INTERVAL '{self.config.compression_after}');
        """)
        
        # Enable compression on electricity prices
        await conn.execute("""
            ALTER TABLE electricity_prices SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'node_id',
                timescaledb.compress_orderby = 'time DESC'
            );
        """)
        
        # Add compression policy for prices
        await conn.execute("""
            SELECT add_compression_policy('electricity_prices', INTERVAL '30 days');
        """)
        
        # Enable compression on grid signals
        await conn.execute("""
            ALTER TABLE grid_signals SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'signal_type',
                timescaledb.compress_orderby = 'time DESC'
            );
        """)
        
        # Add compression policy for grid signals
        await conn.execute("""
            SELECT add_compression_policy('grid_signals', INTERVAL '30 days');
        """)
        
        # Enable compression on vehicle telemetry
        await conn.execute("""
            ALTER TABLE vehicle_telemetry SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'vehicle_id',
                timescaledb.compress_orderby = 'time DESC'
            );
        """)
        
        # Add compression policy for vehicle telemetry
        await conn.execute(f"""
            SELECT add_compression_policy('vehicle_telemetry', INTERVAL '{self.config.compression_after}');
        """)
        
        self.logger.info("All compression policies created successfully")
    
    async def _setup_retention_policies(self, conn: asyncpg.Connection) -> None:
        """Setup retention policies for data lifecycle management."""
        
        # Retention policy for telemetry data
        await conn.execute(f"""
            SELECT add_retention_policy('telemetry_data', INTERVAL '{self.config.retention_period}');
        """)
        
        # Retention policy for grid signals
        await conn.execute("""
            SELECT add_retention_policy('grid_signals', INTERVAL '1 year');
        """)
        
        # Retention policy for vehicle telemetry
        await conn.execute(f"""
            SELECT add_retention_policy('vehicle_telemetry', INTERVAL '{self.config.retention_period}');
        """)
        
        # Keep optimization decisions indefinitely for model training
        # Keep charging sessions indefinitely for billing and analytics
        # Keep electricity prices indefinitely for historical analysis
        
        self.logger.info("All retention policies created successfully")
    
    async def get_schema_info(self) -> Dict[str, Any]:
        """Get information about the current schema."""
        try:
            conn = await asyncpg.connect(
                host=self.config.host,
                port=self.config.port,
                database=self.config.database,
                user=self.config.user,
                password=self.config.password,
                ssl=self.config.sslmode
            )
            
            try:
                # Get hypertables
                hypertables = await conn.fetch("""
                    SELECT hypertable_name, num_dimensions, num_chunks
                    FROM timescaledb_information.hypertables;
                """)
                
                # Get continuous aggregates
                continuous_aggregates = await conn.fetch("""
                    SELECT view_name, materialized_only, finalized
                    FROM timescaledb_information.continuous_aggregates;
                """)
                
                # Get compression policies
                compression_policies = await conn.fetch("""
                    SELECT hypertable_name, compress_after
                    FROM timescaledb_information.jobs
                    WHERE proc_name = 'policy_compression';
                """)
                
                # Get retention policies
                retention_policies = await conn.fetch("""
                    SELECT hypertable_name, drop_after
                    FROM timescaledb_information.jobs
                    WHERE proc_name = 'policy_retention';
                """)
                
                return {
                    "hypertables": [dict(row) for row in hypertables],
                    "continuous_aggregates": [dict(row) for row in continuous_aggregates],
                    "compression_policies": [dict(row) for row in compression_policies],
                    "retention_policies": [dict(row) for row in retention_policies]
                }
                
            finally:
                await conn.close()
                
        except Exception as e:
            self.logger.error(f"Failed to get schema info: {e}")
            raise


async def create_timescale_schema_from_config(config: TimescaleConfig) -> None:
    """Create TimescaleDB schema from configuration."""
    schema = TimescaleSchema(config)
    await schema.create_schema()


async def get_timescale_schema_info(config: TimescaleConfig) -> Dict[str, Any]:
    """Get TimescaleDB schema information."""
    schema = TimescaleSchema(config)
    return await schema.get_schema_info()
