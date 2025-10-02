"""TimescaleDB client for time-series data operations."""

import asyncio
import json
from typing import Dict, Any, List, Optional, Union
from datetime import datetime, timezone, timedelta
import asyncpg
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool

from .config import TimescaleConfig
from .monitoring import get_logger


class TimescaleClient:
    """TimescaleDB client for time-series operations."""
    
    def __init__(self, config: TimescaleConfig):
        """Initialize TimescaleDB client."""
        self.config = config
        self.logger = get_logger(__name__)
        
        # Connection pools
        self.pg_pool: Optional[asyncpg.Pool] = None
        self.sqlalchemy_engine = None
        
        # Connection state
        self.connected = False
        
    async def connect(self) -> None:
        """Establish connections to TimescaleDB."""
        try:
            # Create asyncpg connection pool
            self.pg_pool = await asyncpg.create_pool(
                host=self.config.host,
                port=self.config.port,
                database=self.config.database,
                user=self.config.user,
                password=self.config.password,
                ssl=self.config.sslmode,
                min_size=1,
                max_size=self.config.max_connections,
                command_timeout=self.config.statement_timeout,
                server_settings={
                    'statement_timeout': f'{self.config.statement_timeout}s',
                    'idle_in_transaction_session_timeout': f'{self.config.idle_timeout}s'
                }
            )
            
            # Create SQLAlchemy engine for pandas operations
            self.sqlalchemy_engine = create_engine(
                self.config.service_url,
                poolclass=QueuePool,
                pool_size=self.config.pool_size,
                max_overflow=self.config.max_connections - self.config.pool_size,
                pool_timeout=30,
                pool_recycle=3600,
                echo=False
            )
            
            # Test connections
            await self._test_connections()
            
            self.connected = True
            self.logger.info("TimescaleDB client connected successfully")
            
        except Exception as e:
            self.logger.error(f"Failed to connect to TimescaleDB: {e}")
            raise
    
    async def disconnect(self) -> None:
        """Close all connections."""
        if self.pg_pool:
            await self.pg_pool.close()
        
        if self.sqlalchemy_engine:
            self.sqlalchemy_engine.dispose()
        
        self.connected = False
        self.logger.info("TimescaleDB client disconnected")
    
    async def _test_connections(self) -> None:
        """Test all connections."""
        # Test asyncpg connection
        async with self.pg_pool.acquire() as conn:
            result = await conn.fetchval('SELECT 1')
            if result != 1:
                raise Exception("AsyncPG connection test failed")
        
        # Test SQLAlchemy connection
        with self.sqlalchemy_engine.connect() as conn:
            result = conn.execute(text('SELECT 1')).scalar()
            if result != 1:
                raise Exception("SQLAlchemy connection test failed")
    
    # Telemetry Data Operations
    async def insert_telemetry_batch(self, telemetry_data: List[Dict[str, Any]]) -> None:
        """Insert batch of telemetry data."""
        if not telemetry_data:
            return
        
        try:
            async with self.pg_pool.acquire() as conn:
                # Prepare data for bulk insert
                values = []
                for data in telemetry_data:
                    values.append((
                        data['time'],
                        data['station_id'],
                        data['evse_id'],
                        data['connector_id'],
                        data.get('session_id'),
                        data.get('power_kw'),
                        data.get('energy_kwh'),
                        data.get('voltage_v'),
                        data.get('current_a'),
                        data.get('frequency_hz'),
                        data.get('soc_percent'),
                        data.get('temperature_c'),
                        data.get('grid_frequency_mhz'),
                        data.get('reactive_power_kvar'),
                        data.get('power_factor')
                    ))
                
                # Bulk insert using COPY
                await conn.copy_records_to_table(
                    'telemetry_data',
                    records=values,
                    columns=[
                        'time', 'station_id', 'evse_id', 'connector_id', 'session_id',
                        'power_kw', 'energy_kwh', 'voltage_v', 'current_a', 'frequency_hz',
                        'soc_percent', 'temperature_c', 'grid_frequency_mhz',
                        'reactive_power_kvar', 'power_factor'
                    ]
                )
                
                self.logger.debug(f"Inserted {len(telemetry_data)} telemetry records")
                
        except Exception as e:
            self.logger.error(f"Failed to insert telemetry batch: {e}")
            raise
    
    async def get_telemetry_data(self, station_id: str, start_time: datetime, 
                               end_time: datetime, limit: int = 1000) -> List[Dict[str, Any]]:
        """Get telemetry data for a station."""
        try:
            async with self.pg_pool.acquire() as conn:
                query = """
                    SELECT time, station_id, evse_id, connector_id, session_id,
                           power_kw, energy_kwh, voltage_v, current_a, frequency_hz,
                           soc_percent, temperature_c, grid_frequency_mhz,
                           reactive_power_kvar, power_factor
                    FROM telemetry_data
                    WHERE station_id = $1 
                        AND time >= $2 
                        AND time <= $3
                    ORDER BY time DESC
                    LIMIT $4
                """
                
                rows = await conn.fetch(query, station_id, start_time, end_time, limit)
                return [dict(row) for row in rows]
                
        except Exception as e:
            self.logger.error(f"Failed to get telemetry data: {e}")
            raise
    
    # Charging Sessions Operations
    async def create_charging_session(self, session_data: Dict[str, Any]) -> str:
        """Create a new charging session."""
        try:
            async with self.pg_pool.acquire() as conn:
                session_id = await conn.fetchval("""
                    INSERT INTO charging_sessions (
                        station_id, evse_id, connector_id, vehicle_id, id_token,
                        start_time, start_soc_percent, operation_mode,
                        fleet_operator_id, site_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    RETURNING session_id
                """, 
                session_data['station_id'],
                session_data['evse_id'],
                session_data['connector_id'],
                session_data.get('vehicle_id'),
                session_data.get('id_token'),
                session_data['start_time'],
                session_data.get('start_soc_percent'),
                session_data.get('operation_mode'),
                session_data.get('fleet_operator_id'),
                session_data.get('site_id')
                )
                
                return str(session_id)
                
        except Exception as e:
            self.logger.error(f"Failed to create charging session: {e}")
            raise
    
    async def update_charging_session(self, session_id: str, updates: Dict[str, Any]) -> None:
        """Update charging session."""
        try:
            async with self.pg_pool.acquire() as conn:
                # Build dynamic update query
                set_clauses = []
                values = []
                param_count = 1
                
                for key, value in updates.items():
                    if key in ['end_time', 'end_soc_percent', 'energy_delivered_kwh', 
                              'energy_received_kwh', 'max_charge_power_kw', 'max_discharge_power_kw']:
                        set_clauses.append(f"{key} = ${param_count}")
                        values.append(value)
                        param_count += 1
                
                if set_clauses:
                    set_clauses.append("updated_at = NOW()")
                    values.append(session_id)
                    
                    query = f"""
                        UPDATE charging_sessions 
                        SET {', '.join(set_clauses)}
                        WHERE session_id = ${param_count}
                    """
                    
                    await conn.execute(query, *values)
                    
        except Exception as e:
            self.logger.error(f"Failed to update charging session: {e}")
            raise
    
    async def get_charging_sessions(self, fleet_operator_id: str, start_time: datetime,
                                  end_time: datetime, limit: int = 100) -> List[Dict[str, Any]]:
        """Get charging sessions for a fleet operator."""
        try:
            async with self.pg_pool.acquire() as conn:
                query = """
                    SELECT session_id, station_id, evse_id, connector_id, vehicle_id,
                           start_time, end_time, start_soc_percent, end_soc_percent,
                           energy_delivered_kwh, energy_received_kwh, operation_mode,
                           fleet_operator_id, site_id, created_at, updated_at
                    FROM charging_sessions
                    WHERE fleet_operator_id = $1 
                        AND start_time >= $2 
                        AND start_time <= $3
                    ORDER BY start_time DESC
                    LIMIT $4
                """
                
                rows = await conn.fetch(query, fleet_operator_id, start_time, end_time, limit)
                return [dict(row) for row in rows]
                
        except Exception as e:
            self.logger.error(f"Failed to get charging sessions: {e}")
            raise
    
    # Optimization Decisions Operations
    async def store_optimization_decision(self, decision_data: Dict[str, Any]) -> str:
        """Store optimization decision."""
        try:
            async with self.pg_pool.acquire() as conn:
                decision_id = await conn.fetchval("""
                    INSERT INTO optimization_decisions (
                        time, optimization_window_start, optimization_window_end,
                        fleet_operator_id, site_id, algorithm_version,
                        objective_function, objective_value, computation_time_ms,
                        constraints_satisfied, decision_payload
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    RETURNING decision_id
                """,
                decision_data['time'],
                decision_data['optimization_window_start'],
                decision_data['optimization_window_end'],
                decision_data.get('fleet_operator_id'),
                decision_data.get('site_id'),
                decision_data.get('algorithm_version'),
                decision_data.get('objective_function'),
                decision_data.get('objective_value'),
                decision_data.get('computation_time_ms'),
                decision_data.get('constraints_satisfied'),
                json.dumps(decision_data.get('decision_payload', {}))
                )
                
                return str(decision_id)
                
        except Exception as e:
            self.logger.error(f"Failed to store optimization decision: {e}")
            raise
    
    async def get_optimization_decisions(self, fleet_operator_id: str, start_time: datetime,
                                       end_time: datetime) -> List[Dict[str, Any]]:
        """Get optimization decisions."""
        try:
            async with self.pg_pool.acquire() as conn:
                query = """
                    SELECT decision_id, time, optimization_window_start, optimization_window_end,
                           algorithm_version, objective_function, objective_value,
                           computation_time_ms, constraints_satisfied, decision_payload
                    FROM optimization_decisions
                    WHERE fleet_operator_id = $1 
                        AND time >= $2 
                        AND time <= $3
                    ORDER BY time DESC
                """
                
                rows = await conn.fetch(query, fleet_operator_id, start_time, end_time)
                return [dict(row) for row in rows]
                
        except Exception as e:
            self.logger.error(f"Failed to get optimization decisions: {e}")
            raise
    
    # Charging Schedules Operations
    async def store_charging_schedule(self, schedule_data: Dict[str, Any]) -> str:
        """Store charging schedule."""
        try:
            async with self.pg_pool.acquire() as conn:
                schedule_id = await conn.fetchval("""
                    INSERT INTO charging_schedules (
                        station_id, evse_id, decision_id, profile_id,
                        start_time, end_time, schedule_periods,
                        priority, stacking_level, purpose
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    RETURNING schedule_id
                """,
                schedule_data['station_id'],
                schedule_data['evse_id'],
                schedule_data.get('decision_id'),
                schedule_data['profile_id'],
                schedule_data['start_time'],
                schedule_data['end_time'],
                json.dumps(schedule_data['schedule_periods']),
                schedule_data.get('priority', 0),
                schedule_data.get('stacking_level', 0),
                schedule_data.get('purpose')
                )
                
                return str(schedule_id)
                
        except Exception as e:
            self.logger.error(f"Failed to store charging schedule: {e}")
            raise
    
    async def update_schedule_execution(self, schedule_id: str, executed_at: datetime, 
                                      execution_status: str) -> None:
        """Update schedule execution status."""
        try:
            async with self.pg_pool.acquire() as conn:
                await conn.execute("""
                    UPDATE charging_schedules 
                    SET executed_at = $1, execution_status = $2
                    WHERE schedule_id = $3
                """, executed_at, execution_status, schedule_id)
                
        except Exception as e:
            self.logger.error(f"Failed to update schedule execution: {e}")
            raise
    
    # Electricity Prices Operations
    async def store_electricity_prices(self, price_data: List[Dict[str, Any]]) -> None:
        """Store electricity price data."""
        if not price_data:
            return
        
        try:
            async with self.pg_pool.acquire() as conn:
                values = []
                for data in price_data:
                    values.append((
                        data['time'],
                        data['node_id'],
                        data['market_type'],
                        data.get('lmp_price_mwh'),
                        data.get('energy_component_mwh'),
                        data.get('congestion_component_mwh'),
                        data.get('loss_component_mwh'),
                        data.get('ghg_adder_mwh'),
                        data.get('price_confidence'),
                        data.get('forecast_horizon_minutes')
                    ))
                
                await conn.copy_records_to_table(
                    'electricity_prices',
                    records=values,
                    columns=[
                        'time', 'node_id', 'market_type', 'lmp_price_mwh',
                        'energy_component_mwh', 'congestion_component_mwh',
                        'loss_component_mwh', 'ghg_adder_mwh', 'price_confidence',
                        'forecast_horizon_minutes'
                    ]
                )
                
                self.logger.debug(f"Stored {len(price_data)} electricity price records")
                
        except Exception as e:
            self.logger.error(f"Failed to store electricity prices: {e}")
            raise
    
    async def get_electricity_prices(self, node_id: str, start_time: datetime,
                                   end_time: datetime, market_type: str = None) -> List[Dict[str, Any]]:
        """Get electricity prices."""
        try:
            async with self.pg_pool.acquire() as conn:
                query = """
                    SELECT time, node_id, market_type, lmp_price_mwh,
                           energy_component_mwh, congestion_component_mwh,
                           loss_component_mwh, ghg_adder_mwh, price_confidence,
                           forecast_horizon_minutes
                    FROM electricity_prices
                    WHERE node_id = $1 
                        AND time >= $2 
                        AND time <= $3
                """
                params = [node_id, start_time, end_time]
                
                if market_type:
                    query += " AND market_type = $4"
                    params.append(market_type)
                
                query += " ORDER BY time DESC"
                
                rows = await conn.fetch(query, *params)
                return [dict(row) for row in rows]
                
        except Exception as e:
            self.logger.error(f"Failed to get electricity prices: {e}")
            raise
    
    # Analytics Operations
    async def get_hourly_energy_aggregates(self, station_id: str, start_time: datetime,
                                         end_time: datetime) -> pd.DataFrame:
        """Get hourly energy aggregates using pandas."""
        try:
            query = """
                SELECT hour, station_id, avg_power_kw, max_power_kw, min_power_kw,
                       energy_charged_kwh, energy_discharged_kwh, avg_soc, sample_count
                FROM hourly_energy_aggregates
                WHERE station_id = %s 
                    AND hour >= %s 
                    AND hour <= %s
                ORDER BY hour DESC
            """
            
            df = pd.read_sql(
                query,
                self.sqlalchemy_engine,
                params=[station_id, start_time, end_time]
            )
            
            return df
            
        except Exception as e:
            self.logger.error(f"Failed to get hourly energy aggregates: {e}")
            raise
    
    async def get_daily_fleet_metrics(self, fleet_operator_id: str, start_time: datetime,
                                    end_time: datetime) -> pd.DataFrame:
        """Get daily fleet metrics."""
        try:
            query = """
                SELECT day, fleet_operator_id, active_stations, total_sessions,
                       total_energy_charged_kwh, total_energy_discharged_kwh,
                       avg_session_duration_hours
                FROM daily_fleet_metrics
                WHERE fleet_operator_id = %s 
                    AND day >= %s 
                    AND day <= %s
                ORDER BY day DESC
            """
            
            df = pd.read_sql(
                query,
                self.sqlalchemy_engine,
                params=[fleet_operator_id, start_time, end_time]
            )
            
            return df
            
        except Exception as e:
            self.logger.error(f"Failed to get daily fleet metrics: {e}")
            raise
    
    async def get_energy_usage_summary(self, fleet_operator_id: str, start_time: datetime,
                                     end_time: datetime) -> Dict[str, Any]:
        """Get energy usage summary."""
        try:
            async with self.pg_pool.acquire() as conn:
                query = """
                    SELECT 
                        COUNT(DISTINCT session_id) as total_sessions,
                        SUM(energy_delivered_kwh) as total_energy_charged,
                        SUM(energy_received_kwh) as total_energy_discharged,
                        AVG(EXTRACT(EPOCH FROM (end_time - start_time))/3600) as avg_session_duration_hours,
                        COUNT(DISTINCT station_id) as active_stations
                    FROM charging_sessions
                    WHERE fleet_operator_id = $1 
                        AND start_time >= $2 
                        AND start_time <= $3
                        AND end_time IS NOT NULL
                """
                
                result = await conn.fetchrow(query, fleet_operator_id, start_time, end_time)
                return dict(result) if result else {}
                
        except Exception as e:
            self.logger.error(f"Failed to get energy usage summary: {e}")
            raise
    
    # Health Check
    async def health_check(self) -> Dict[str, Any]:
        """Check TimescaleDB connection health."""
        try:
            if not self.connected:
                return {"status": "disconnected", "error": "Not connected"}
            
            # Test asyncpg connection
            pg_status = "healthy"
            try:
                async with self.pg_pool.acquire() as conn:
                    await conn.fetchval('SELECT 1')
            except Exception as e:
                pg_status = f"unhealthy: {str(e)}"
            
            # Test SQLAlchemy connection
            sqlalchemy_status = "healthy"
            try:
                with self.sqlalchemy_engine.connect() as conn:
                    conn.execute(text('SELECT 1'))
            except Exception as e:
                sqlalchemy_status = f"unhealthy: {str(e)}"
            
            # Check TimescaleDB extension
            timescale_status = "healthy"
            try:
                async with self.pg_pool.acquire() as conn:
                    result = await conn.fetchval("SELECT extname FROM pg_extension WHERE extname = 'timescaledb'")
                    if not result:
                        timescale_status = "timescaledb extension not found"
            except Exception as e:
                timescale_status = f"unhealthy: {str(e)}"
            
            return {
                "status": "healthy" if all(s == "healthy" for s in [pg_status, sqlalchemy_status, timescale_status]) else "degraded",
                "asyncpg": pg_status,
                "sqlalchemy": sqlalchemy_status,
                "timescaledb": timescale_status,
                "pool_size": self.pg_pool.get_size() if self.pg_pool else 0,
                "max_connections": self.config.max_connections
            }
            
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}
    
    # Utility Methods
    async def execute_query(self, query: str, *args) -> List[Dict[str, Any]]:
        """Execute a custom query."""
        try:
            async with self.pg_pool.acquire() as conn:
                rows = await conn.fetch(query, *args)
                return [dict(row) for row in rows]
        except Exception as e:
            self.logger.error(f"Failed to execute query: {e}")
            raise
    
    async def execute_command(self, command: str, *args) -> str:
        """Execute a command (INSERT, UPDATE, DELETE)."""
        try:
            async with self.pg_pool.acquire() as conn:
                result = await conn.execute(command, *args)
                return result
        except Exception as e:
            self.logger.error(f"Failed to execute command: {e}")
            raise
