"""Enhanced Redis client implementing all patterns from Redis Memory Store instructions."""

import asyncio
import json
import time
import zlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from dataclasses import dataclass, asdict
import redis.asyncio as redis
from redis.asyncio import ConnectionPool
import msgpack

from .config import RedisConfig
from .monitoring import get_logger, PerformanceTimer, metrics_collector


@dataclass
class ChargerState:
    """Charger state data structure."""
    station_id: str
    status: str  # Idle, Charging, Discharging, SuspendedEVSE, SuspendedEV
    power_kw: float
    soc_percent: Optional[float] = None
    max_charge_power_kw: Optional[float] = None
    max_discharge_power_kw: Optional[float] = None
    operation_mode: Optional[str] = None
    vehicle_connected: bool = False
    session_id: Optional[str] = None
    updated_at: Optional[str] = None
    evse_id: int = 1
    connector_id: int = 1


@dataclass
class TelemetryData:
    """Telemetry data structure."""
    timestamp: str
    station_id: str
    evse_id: int
    connector_id: int
    power_kw: Optional[float] = None
    energy_kwh: Optional[float] = None
    voltage_v: Optional[float] = None
    current_a: Optional[float] = None
    frequency_hz: Optional[float] = None
    soc_percent: Optional[float] = None
    temperature_c: Optional[float] = None
    grid_frequency_mhz: Optional[int] = None
    reactive_power_kvar: Optional[float] = None
    power_factor: Optional[float] = None
    session_id: Optional[str] = None


@dataclass
class OptimizationDecision:
    """Optimization decision data structure."""
    decision_id: str
    computed_at: str
    optimization_window_start: str
    optimization_window_end: str
    fleet_operator_id: str
    objective_value: float
    computation_time_ms: int
    constraints_satisfied: bool
    decision_payload: Dict[str, Any]


class EnhancedRedisClient:
    """Enhanced Redis client with full V2G pattern implementation."""
    
    def __init__(self, config: RedisConfig):
        """Initialize enhanced Redis client."""
        self.config = config
        self.logger = get_logger(__name__)
        
        # Connection objects
        self.pool: Optional[ConnectionPool] = None
        self.client: Optional[redis.Redis] = None
        self.pubsub: Optional[redis.client.PubSub] = None
        
        # Cluster support
        self.cluster_client: Optional[redis.RedisCluster] = None
        self.is_cluster = False
        
        # Lua scripts
        self._lua_scripts: Dict[str, redis.client.Script] = {}
        
        # Compression settings
        self.compression_enabled = True
        self.compression_threshold = 1024  # bytes
        
        # Performance metrics
        self._operation_count = 0
        self._error_count = 0
        
    async def connect(self) -> None:
        """Connect to Redis with cluster support."""
        try:
            # Try cluster connection first
            if self._is_cluster_url():
                await self._connect_cluster()
            else:
                await self._connect_single()
            
            # Register Lua scripts
            await self._register_lua_scripts()
            
            # Setup pub/sub
            self.pubsub = self.client.pubsub()
            
            # Verify connection
            await self.client.ping()
            
            self.logger.info(f"Connected to Redis {'cluster' if self.is_cluster else 'single'}")
            
        except Exception as e:
            self.logger.error(f"Failed to connect to Redis: {e}")
            raise
    
    def _is_cluster_url(self) -> bool:
        """Check if URL indicates cluster setup."""
        return 'cluster' in self.config.url.lower() or ',' in self.config.url
    
    async def _connect_cluster(self) -> None:
        """Connect to Redis cluster."""
        startup_nodes = []
        
        if ',' in self.config.url:
            # Multiple nodes specified
            for node_url in self.config.url.split(','):
                host, port = self._parse_redis_url(node_url.strip())
                startup_nodes.append({"host": host, "port": port})
        else:
            # Single cluster URL
            host, port = self._parse_redis_url(self.config.url)
            startup_nodes.append({"host": host, "port": port})
        
        self.cluster_client = redis.RedisCluster(
            startup_nodes=startup_nodes,
            password=self.config.password,
            socket_timeout=self.config.socket_timeout,
            socket_connect_timeout=5,
            socket_keepalive=True,
            socket_keepalive_options={1: 1, 2: 3, 3: 5},
            retry_on_timeout=self.config.retry_on_timeout,
            health_check_interval=self.config.health_check_interval,
            max_connections=self.config.max_connections,
            encoding='utf-8',
            decode_responses=True,
            skip_full_coverage_check=True,
        )
        
        self.client = self.cluster_client
        self.is_cluster = True
    
    async def _connect_single(self) -> None:
        """Connect to single Redis instance."""
        self.pool = ConnectionPool.from_url(
            self.config.url,
            password=self.config.password,
            max_connections=self.config.max_connections,
            socket_timeout=self.config.socket_timeout,
            socket_connect_timeout=5,
            socket_keepalive=True,
            socket_keepalive_options={1: 1, 2: 3, 3: 5},
            retry_on_timeout=self.config.retry_on_timeout,
            health_check_interval=self.config.health_check_interval,
            encoding='utf-8',
            decode_responses=True
        )
        
        self.client = redis.Redis(connection_pool=self.pool)
        self.is_cluster = False
    
    def _parse_redis_url(self, url: str) -> Tuple[str, int]:
        """Parse Redis URL to host and port."""
        if '://' in url:
            url = url.split('://', 1)[1]
        if '@' in url:
            url = url.split('@', 1)[1]
        if ':' in url:
            host, port = url.rsplit(':', 1)
            return host, int(port)
        return url, 6379
    
    async def _register_lua_scripts(self) -> None:
        """Register all Lua scripts."""
        scripts = {
            'update_charger_state': """
                local station_key = KEYS[1]
                local connection_key = KEYS[2]
                local state_data = cjson.decode(ARGV[1])
                local timestamp = ARGV[2]
                
                -- Update charger state atomically
                for key, value in pairs(state_data) do
                    redis.call('HSET', station_key, key, tostring(value))
                end
                redis.call('HSET', station_key, 'updated_at', timestamp)
                redis.call('EXPIRE', station_key, 300)
                
                -- Update heartbeat
                redis.call('HSET', connection_key, 'last_heartbeat', timestamp)
                redis.call('EXPIRE', connection_key, 120)
                
                -- Publish state change notification
                local station_id = string.match(station_key, 'charger:state:(.+)')
                if station_id then
                    redis.call('PUBLISH', 'notify:charger:' .. station_id, ARGV[1])
                end
                
                return 'OK'
            """,
            
            'acquire_optimization_lock': """
                local lock_key = KEYS[1]
                local lock_id = ARGV[1]
                local ttl_ms = tonumber(ARGV[2])
                
                local current = redis.call('GET', lock_key)
                if current == false then
                    redis.call('SET', lock_key, lock_id, 'PX', ttl_ms)
                    return 1
                elseif current == lock_id then
                    redis.call('PEXPIRE', lock_key, ttl_ms)
                    return 1
                else
                    return 0
                end
            """,
            
            'update_fleet_availability': """
                local fleet_key = KEYS[1]
                local vehicle_states = cjson.decode(ARGV[1])
                local timestamp = ARGV[2]
                
                -- Update availability bitmap
                for vehicle_id, available in pairs(vehicle_states) do
                    local bit_value = available and 1 or 0
                    redis.call('SETBIT', fleet_key, tonumber(vehicle_id), bit_value)
                end
                
                -- Set expiration
                redis.call('EXPIRE', fleet_key, 300)
                
                -- Update metadata
                local meta_key = string.gsub(fleet_key, ':available', ':meta')
                redis.call('HSET', meta_key, 'updated_at', timestamp)
                redis.call('HSET', meta_key, 'vehicle_count', #vehicle_states)
                redis.call('EXPIRE', meta_key, 300)
                
                -- Return available count
                return redis.call('BITCOUNT', fleet_key)
            """,
            
            'store_telemetry_timeseries': """
                local base_key = KEYS[1]
                local timestamp = tonumber(ARGV[1])
                local telemetry_data = cjson.decode(ARGV[2])
                local retention_seconds = tonumber(ARGV[3]) or 86400
                
                -- Store in time series for each metric
                for metric, value in pairs(telemetry_data) do
                    if type(value) == 'number' then
                        local ts_key = base_key .. ':' .. metric
                        redis.call('ZADD', ts_key, timestamp, value)
                        
                        -- Clean old data
                        local cutoff = timestamp - (retention_seconds * 1000)
                        redis.call('ZREMRANGEBYSCORE', ts_key, 0, cutoff)
                        
                        -- Set expiration
                        redis.call('EXPIRE', ts_key, retention_seconds)
                    end
                end
                
                return 'OK'
            """,
            
            'get_fleet_charging_queue': """
                local queue_key = KEYS[1]
                local max_items = tonumber(ARGV[1]) or 10
                
                -- Get highest priority items
                local items = redis.call('ZREVRANGE', queue_key, 0, max_items - 1, 'WITHSCORES')
                
                local result = {}
                for i = 1, #items, 2 do
                    table.insert(result, {
                        vehicle_id = items[i],
                        priority = tonumber(items[i + 1])
                    })
                end
                
                return cjson.encode(result)
            """
        }
        
        for name, script in scripts.items():
            self._lua_scripts[name] = self.client.register_script(script)
    
    def _serialize_data(self, data: Any) -> bytes:
        """Serialize data with optional compression."""
        if isinstance(data, str):
            encoded = data.encode('utf-8')
        else:
            encoded = msgpack.packb(data)
        
        if self.compression_enabled and len(encoded) > self.compression_threshold:
            return zlib.compress(encoded)
        return encoded
    
    def _deserialize_data(self, data: bytes) -> Any:
        """Deserialize data with decompression."""
        try:
            # Try decompression first
            decompressed = zlib.decompress(data)
            return msgpack.unpackb(decompressed, raw=False)
        except zlib.error:
            # Not compressed, try direct msgpack
            try:
                return msgpack.unpackb(data, raw=False)
            except (msgpack.exceptions.ExtraData, ValueError):
                # Fallback to string
                return data.decode('utf-8')
    
    # ==========================================
    # Charger State Management
    # ==========================================
    
    async def update_charger_state_atomic(self, station_id: str, state: ChargerState) -> None:
        """Update charger state atomically using Lua script."""
        with PerformanceTimer("redis_update_charger_state"):
            state_key = f"charger:state:{station_id}"
            connection_key = f"charger:connections:{station_id}"
            timestamp = datetime.now(timezone.utc).isoformat()
            
            # Convert state to dict, excluding None values
            state_dict = {k: v for k, v in asdict(state).items() if v is not None}
            state_json = json.dumps(state_dict, separators=(',', ':'))
            
            await self._lua_scripts['update_charger_state'].execute(
                keys=[state_key, connection_key],
                args=[state_json, timestamp]
            )
            
            metrics_collector.record_redis_operation("update_charger_state", 0.001, True)
    
    async def get_charger_state(self, station_id: str) -> Optional[ChargerState]:
        """Get charger state."""
        with PerformanceTimer("redis_get_charger_state"):
            state_data = await self.client.hgetall(f"charger:state:{station_id}")
            
            if not state_data:
                return None
            
            # Convert back to ChargerState
            return ChargerState(
                station_id=state_data.get('station_id', station_id),
                status=state_data.get('status', 'Unknown'),
                power_kw=float(state_data.get('power_kw', 0)),
                soc_percent=float(state_data['soc_percent']) if state_data.get('soc_percent') else None,
                max_charge_power_kw=float(state_data['max_charge_power_kw']) if state_data.get('max_charge_power_kw') else None,
                max_discharge_power_kw=float(state_data['max_discharge_power_kw']) if state_data.get('max_discharge_power_kw') else None,
                operation_mode=state_data.get('operation_mode'),
                vehicle_connected=state_data.get('vehicle_connected', 'false').lower() == 'true',
                session_id=state_data.get('session_id'),
                updated_at=state_data.get('updated_at'),
                evse_id=int(state_data.get('evse_id', 1)),
                connector_id=int(state_data.get('connector_id', 1))
            )
    
    async def get_all_charger_states(self, station_ids: List[str]) -> Dict[str, ChargerState]:
        """Get multiple charger states efficiently using pipeline."""
        with PerformanceTimer("redis_get_all_charger_states"):
            pipe = self.client.pipeline()
            
            for station_id in station_ids:
                pipe.hgetall(f"charger:state:{station_id}")
            
            results = await pipe.execute()
            
            states = {}
            for i, state_data in enumerate(results):
                if state_data:
                    station_id = station_ids[i]
                    states[station_id] = ChargerState(
                        station_id=state_data.get('station_id', station_id),
                        status=state_data.get('status', 'Unknown'),
                        power_kw=float(state_data.get('power_kw', 0)),
                        soc_percent=float(state_data['soc_percent']) if state_data.get('soc_percent') else None,
                        max_charge_power_kw=float(state_data['max_charge_power_kw']) if state_data.get('max_charge_power_kw') else None,
                        max_discharge_power_kw=float(state_data['max_discharge_power_kw']) if state_data.get('max_discharge_power_kw') else None,
                        operation_mode=state_data.get('operation_mode'),
                        vehicle_connected=state_data.get('vehicle_connected', 'false').lower() == 'true',
                        session_id=state_data.get('session_id'),
                        updated_at=state_data.get('updated_at'),
                        evse_id=int(state_data.get('evse_id', 1)),
                        connector_id=int(state_data.get('connector_id', 1))
                    )
            
            return states
    
    # ==========================================
    # Telemetry Management with Time Series
    # ==========================================
    
    async def store_telemetry_timeseries(self, station_id: str, telemetry: TelemetryData) -> None:
        """Store telemetry in time series format."""
        with PerformanceTimer("redis_store_telemetry_timeseries"):
            timestamp = int(time.time() * 1000)  # Unix timestamp in milliseconds
            base_key = f"charger:telemetry:{station_id}"
            
            # Prepare telemetry data for time series storage
            telemetry_dict = {
                k: v for k, v in asdict(telemetry).items() 
                if v is not None and isinstance(v, (int, float)) and k != 'timestamp'
            }
            
            telemetry_json = json.dumps(telemetry_dict)
            
            await self._lua_scripts['store_telemetry_timeseries'].execute(
                keys=[base_key],
                args=[timestamp, telemetry_json, 86400]  # 24 hour retention
            )
            
            # Also store latest values in hash for quick access
            latest_key = f"{base_key}:latest"
            await self.client.hmset(latest_key, telemetry_dict)
            await self.client.expire(latest_key, 300)  # 5 minutes TTL
    
    async def get_telemetry_timeseries(self, station_id: str, metric: str, 
                                     start_time: Optional[int] = None,
                                     end_time: Optional[int] = None,
                                     limit: int = 1000) -> List[Tuple[int, float]]:
        """Get telemetry time series data."""
        with PerformanceTimer("redis_get_telemetry_timeseries"):
            ts_key = f"charger:telemetry:{station_id}:{metric}"
            
            if start_time is None:
                start_time = int((time.time() - 3600) * 1000)  # Last hour
            if end_time is None:
                end_time = int(time.time() * 1000)  # Now
            
            results = await self.client.zrangebyscore(
                ts_key, start_time, end_time, 
                withscores=True, start=0, num=limit
            )
            
            return [(int(score), float(value)) for value, score in results]
    
    async def get_telemetry_aggregated(self, station_id: str, metric: str,
                                     window_seconds: int = 300,
                                     start_time: Optional[int] = None,
                                     end_time: Optional[int] = None) -> List[Tuple[int, float, float, float]]:
        """Get aggregated telemetry data (timestamp, avg, min, max)."""
        raw_data = await self.get_telemetry_timeseries(station_id, metric, start_time, end_time)
        
        if not raw_data:
            return []
        
        # Group data by time windows
        aggregated = []
        window_ms = window_seconds * 1000
        
        current_window = None
        window_values = []
        
        for timestamp, value in raw_data:
            window_start = (timestamp // window_ms) * window_ms
            
            if current_window != window_start:
                # Process previous window
                if window_values:
                    avg_val = sum(window_values) / len(window_values)
                    min_val = min(window_values)
                    max_val = max(window_values)
                    aggregated.append((current_window, avg_val, min_val, max_val))
                
                # Start new window
                current_window = window_start
                window_values = [value]
            else:
                window_values.append(value)
        
        # Process final window
        if window_values:
            avg_val = sum(window_values) / len(window_values)
            min_val = min(window_values)
            max_val = max(window_values)
            aggregated.append((current_window, avg_val, min_val, max_val))
        
        return aggregated
    
    # ==========================================
    # Fleet Management with Bitmaps
    # ==========================================
    
    async def update_fleet_availability(self, operator_id: str, 
                                      vehicle_states: Dict[int, bool]) -> int:
        """Update fleet availability bitmap."""
        with PerformanceTimer("redis_update_fleet_availability"):
            fleet_key = f"fleet:{operator_id}:available"
            timestamp = datetime.now(timezone.utc).isoformat()
            
            vehicle_states_json = json.dumps(vehicle_states)
            
            available_count = await self._lua_scripts['update_fleet_availability'].execute(
                keys=[fleet_key],
                args=[vehicle_states_json, timestamp]
            )
            
            return int(available_count)
    
    async def get_fleet_availability_count(self, operator_id: str) -> int:
        """Get count of available vehicles in fleet."""
        return await self.client.bitcount(f"fleet:{operator_id}:available")
    
    async def get_fleet_availability_list(self, operator_id: str, max_vehicles: int = 1000) -> List[int]:
        """Get list of available vehicle indices."""
        available_vehicles = []
        fleet_key = f"fleet:{operator_id}:available"
        
        # Get bitmap data
        bitmap_data = await self.client.get(fleet_key)
        if not bitmap_data:
            return []
        
        # Parse bitmap to find set bits
        for i in range(max_vehicles):
            if await self.client.getbit(fleet_key, i):
                available_vehicles.append(i)
        
        return available_vehicles
    
    async def update_fleet_constraints(self, operator_id: str, constraints: Dict[str, Any]) -> None:
        """Update fleet-level constraints."""
        constraint_key = f"fleet:{operator_id}:constraints"
        
        await self.client.hmset(constraint_key, constraints)
        # Fleet constraints are persistent (no expiration)
    
    async def get_fleet_constraints(self, operator_id: str) -> Dict[str, Any]:
        """Get fleet constraints."""
        return await self.client.hgetall(f"fleet:{operator_id}:constraints")
    
    # ==========================================
    # Charging Queue Management
    # ==========================================
    
    async def add_to_charging_queue(self, operator_id: str, vehicle_id: str, 
                                  priority_score: float) -> None:
        """Add vehicle to charging priority queue."""
        queue_key = f"fleet:{operator_id}:queue"
        await self.client.zadd(queue_key, {vehicle_id: priority_score})
        await self.client.expire(queue_key, 3600)  # 1 hour expiration
    
    async def get_charging_queue(self, operator_id: str, count: int = 10) -> List[Dict[str, Any]]:
        """Get top priority vehicles from charging queue."""
        with PerformanceTimer("redis_get_charging_queue"):
            queue_key = f"fleet:{operator_id}:queue"
            
            result_json = await self._lua_scripts['get_fleet_charging_queue'].execute(
                keys=[queue_key],
                args=[count]
            )
            
            return json.loads(result_json)
    
    async def remove_from_charging_queue(self, operator_id: str, vehicle_id: str) -> bool:
        """Remove vehicle from charging queue."""
        queue_key = f"fleet:{operator_id}:queue"
        removed = await self.client.zrem(queue_key, vehicle_id)
        return removed > 0
    
    # ==========================================
    # Market Data and Pricing
    # ==========================================
    
    async def update_electricity_prices(self, node_id: str, market_type: str,
                                      price_data: Dict[str, Any]) -> None:
        """Update electricity price data."""
        price_key = f"prices:{market_type}:{node_id}"
        
        price_data_with_timestamp = {
            **price_data,
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'node_id': node_id,
            'market_type': market_type
        }
        
        await self.client.hmset(price_key, price_data_with_timestamp)
        await self.client.expire(price_key, 300)  # 5 minutes TTL
    
    async def get_current_prices(self, node_id: str, market_type: str = "lmp") -> Optional[Dict[str, Any]]:
        """Get current electricity prices."""
        price_key = f"prices:{market_type}:{node_id}"
        price_data = await self.client.hgetall(price_key)
        
        if not price_data:
            return None
        
        # Convert numeric strings back to numbers
        numeric_fields = ['lmp_price_mwh', 'energy_component_mwh', 'congestion_component_mwh', 
                         'loss_component_mwh', 'ghg_adder_mwh', 'price_confidence']
        
        for field in numeric_fields:
            if field in price_data and price_data[field]:
                try:
                    price_data[field] = float(price_data[field])
                except ValueError:
                    pass
        
        return price_data
    
    async def store_price_forecast(self, node_id: str, forecasts: List[Dict[str, Any]]) -> None:
        """Store price forecast timeline."""
        forecast_key = f"prices:forecast:{node_id}"
        
        # Clear old forecasts
        await self.client.delete(forecast_key)
        
        # Store new forecasts in sorted set
        forecast_mapping = {}
        for forecast in forecasts:
            timestamp = forecast.get('timestamp')
            if timestamp:
                if isinstance(timestamp, str):
                    timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp()
                forecast_mapping[json.dumps(forecast, separators=(',', ':'))] = timestamp
        
        if forecast_mapping:
            await self.client.zadd(forecast_key, forecast_mapping)
            await self.client.expire(forecast_key, 86400)  # 24 hours
    
    async def get_price_forecast(self, node_id: str, start_time: Optional[float] = None,
                               end_time: Optional[float] = None) -> List[Dict[str, Any]]:
        """Get price forecast data."""
        forecast_key = f"prices:forecast:{node_id}"
        
        if start_time is None:
            start_time = time.time()
        if end_time is None:
            end_time = time.time() + 86400  # Next 24 hours
        
        forecasts = await self.client.zrangebyscore(
            forecast_key, start_time, end_time, withscores=True
        )
        
        result = []
        for forecast_json, timestamp in forecasts:
            try:
                forecast_data = json.loads(forecast_json)
                forecast_data['timestamp'] = timestamp
                result.append(forecast_data)
            except json.JSONDecodeError:
                continue
        
        return result
    
    # ==========================================
    # Grid Signals with Redis Streams
    # ==========================================
    
    async def publish_grid_signal(self, signal_data: Dict[str, Any]) -> str:
        """Publish grid control signal using Redis Streams."""
        stream_key = "grid:signals:stream"
        
        # Add to stream
        message_id = await self.client.xadd(stream_key, signal_data, maxlen=10000)
        
        # Also publish to pub/sub for real-time notifications
        await self.client.publish("notify:grid:signal", json.dumps(signal_data))
        
        return message_id
    
    async def get_grid_signals(self, count: int = 100, 
                             start_id: str = "-") -> List[Dict[str, Any]]:
        """Get grid signals from stream."""
        stream_key = "grid:signals:stream"
        
        messages = await self.client.xread({stream_key: start_id}, count=count)
        
        signals = []
        if messages:
            for stream_name, stream_messages in messages:
                for message_id, fields in stream_messages:
                    signal_data = dict(fields)
                    signal_data['message_id'] = message_id
                    signal_data['stream'] = stream_name
                    signals.append(signal_data)
        
        return signals
    
    async def acknowledge_grid_signal(self, consumer_group: str, consumer_name: str,
                                    message_id: str) -> None:
        """Acknowledge processed grid signal."""
        stream_key = "grid:signals:stream"
        await self.client.xack(stream_key, consumer_group, message_id)
    
    # ==========================================
    # Optimization Engine Integration
    # ==========================================
    
    async def store_optimization_decision(self, decision: OptimizationDecision) -> None:
        """Store optimization decision."""
        # Store latest decision
        latest_key = "optimization:decision:latest"
        decision_data = asdict(decision)
        
        await self.client.hmset(latest_key, decision_data)
        
        # Add to decision history (keep last 100)
        history_key = "optimization:decisions"
        decision_json = json.dumps(decision_data, separators=(',', ':'))
        
        pipe = self.client.pipeline()
        pipe.lpush(history_key, decision_json)
        pipe.ltrim(history_key, 0, 99)
        await pipe.execute()
        
        # Publish decision to subscribers
        await self.client.publish("notify:optimization:complete", decision_json)
    
    async def get_latest_optimization_decision(self) -> Optional[OptimizationDecision]:
        """Get latest optimization decision."""
        decision_data = await self.client.hgetall("optimization:decision:latest")
        
        if not decision_data:
            return None
        
        return OptimizationDecision(
            decision_id=decision_data['decision_id'],
            computed_at=decision_data['computed_at'],
            optimization_window_start=decision_data['optimization_window_start'],
            optimization_window_end=decision_data['optimization_window_end'],
            fleet_operator_id=decision_data['fleet_operator_id'],
            objective_value=float(decision_data['objective_value']),
            computation_time_ms=int(decision_data['computation_time_ms']),
            constraints_satisfied=decision_data['constraints_satisfied'].lower() == 'true',
            decision_payload=json.loads(decision_data['decision_payload'])
        )
    
    async def acquire_optimization_lock(self, lock_id: str, ttl_ms: int = 30000) -> bool:
        """Acquire optimization lock for exclusive processing."""
        lock_key = "optimization:lock"
        
        result = await self._lua_scripts['acquire_optimization_lock'].execute(
            keys=[lock_key],
            args=[lock_id, ttl_ms]
        )
        
        return bool(result)
    
    async def release_optimization_lock(self, lock_id: str) -> None:
        """Release optimization lock."""
        lock_key = "optimization:lock"
        
        # Use Lua script to ensure we only delete our own lock
        script = """
            local lock_key = KEYS[1]
            local lock_id = ARGV[1]
            
            if redis.call('GET', lock_key) == lock_id then
                return redis.call('DEL', lock_key)
            else
                return 0
            end
        """
        
        await self.client.eval(script, 1, lock_key, lock_id)
    
    # ==========================================
    # Session Management
    # ==========================================
    
    async def create_active_session(self, session_id: str, session_data: Dict[str, Any]) -> None:
        """Create active charging session."""
        session_key = f"session:{session_id}"
        
        session_data_with_metadata = {
            **session_data,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'status': 'active'
        }
        
        await self.client.hmset(session_key, session_data_with_metadata)
        await self.client.expire(session_key, 86400)  # 24 hours
        
        # Add to active sessions index
        operator_id = session_data.get('fleet_operator_id')
        if operator_id:
            await self.client.sadd(f"sessions:active:{operator_id}", session_id)
        
        # Add to timeline
        start_timestamp = time.time()
        await self.client.zadd("sessions:timeline", {session_id: start_timestamp})
    
    async def end_active_session(self, session_id: str, end_data: Dict[str, Any]) -> None:
        """End active charging session."""
        session_key = f"session:{session_id}"
        
        # Get existing session data
        session_data = await self.client.hgetall(session_key)
        if not session_data:
            return
        
        # Update with end data
        end_data_with_metadata = {
            **end_data,
            'ended_at': datetime.now(timezone.utc).isoformat(),
            'status': 'completed'
        }
        
        await self.client.hmset(session_key, end_data_with_metadata)
        
        # Remove from active sessions
        operator_id = session_data.get('fleet_operator_id')
        if operator_id:
            await self.client.srem(f"sessions:active:{operator_id}", session_id)
    
    async def get_active_sessions(self, operator_id: str) -> List[str]:
        """Get active session IDs for operator."""
        return list(await self.client.smembers(f"sessions:active:{operator_id}"))
    
    async def get_session_data(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Get session data."""
        session_data = await self.client.hgetall(f"session:{session_id}")
        
        if not session_data:
            return None
        
        # Convert numeric strings
        numeric_fields = ['start_soc', 'end_soc', 'energy_delivered_kwh', 
                         'energy_received_kwh', 'max_charge_power_kw', 'max_discharge_power_kw']
        
        for field in numeric_fields:
            if field in session_data and session_data[field]:
                try:
                    session_data[field] = float(session_data[field])
                except ValueError:
                    pass
        
        return session_data
    
    # ==========================================
    # Pub/Sub and Event Broadcasting
    # ==========================================
    
    async def publish_notification(self, channel: str, data: Dict[str, Any]) -> int:
        """Publish notification to Redis channel."""
        message = json.dumps(data, separators=(',', ':'))
        return await self.client.publish(channel, message)
    
    async def subscribe_to_channels(self, channels: List[str]) -> None:
        """Subscribe to multiple channels."""
        if self.pubsub:
            await self.pubsub.subscribe(*channels)
    
    async def get_next_message(self, timeout: float = 1.0) -> Optional[Dict[str, Any]]:
        """Get next message from subscribed channels."""
        if not self.pubsub:
            return None
        
        try:
            message = await asyncio.wait_for(
                self.pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout),
                timeout=timeout + 0.5
            )
            
            if message and message.get('type') == 'message':
                try:
                    return {
                        'channel': message['channel'],
                        'data': json.loads(message['data']),
                        'pattern': message.get('pattern')
                    }
                except json.JSONDecodeError:
                    return {
                        'channel': message['channel'],
                        'data': message['data'],
                        'pattern': message.get('pattern')
                    }
        except asyncio.TimeoutError:
            pass
        
        return None
    
    # ==========================================
    # Performance and Monitoring
    # ==========================================
    
    async def get_performance_stats(self) -> Dict[str, Any]:
        """Get Redis performance statistics."""
        info = await self.client.info()
        
        return {
            'connected_clients': info.get('connected_clients', 0),
            'used_memory': info.get('used_memory', 0),
            'used_memory_human': info.get('used_memory_human', '0B'),
            'maxmemory': info.get('maxmemory', 0),
            'total_commands_processed': info.get('total_commands_processed', 0),
            'instantaneous_ops_per_sec': info.get('instantaneous_ops_per_sec', 0),
            'keyspace_hits': info.get('keyspace_hits', 0),
            'keyspace_misses': info.get('keyspace_misses', 0),
            'evicted_keys': info.get('evicted_keys', 0),
            'rejected_connections': info.get('rejected_connections', 0),
            'operation_count': self._operation_count,
            'error_count': self._error_count
        }
    
    async def health_check(self) -> Dict[str, Any]:
        """Comprehensive Redis health check."""
        try:
            # Test basic operation
            start_time = time.time()
            test_key = f"health:test:{int(time.time())}"
            
            await self.client.set(test_key, "ok", ex=10)
            result = await self.client.get(test_key)
            await self.client.delete(test_key)
            
            latency = (time.time() - start_time) * 1000
            
            # Get system info
            info = await self.client.info()
            memory_used = info.get('used_memory', 0)
            memory_max = info.get('maxmemory', 0)
            
            # Calculate memory usage percentage
            memory_usage_percent = (memory_used / memory_max * 100) if memory_max > 0 else 0
            
            # Check cluster health if applicable
            cluster_healthy = True
            if self.is_cluster:
                try:
                    cluster_info = await self.client.cluster_info()
                    cluster_healthy = cluster_info.get('cluster_state') == 'ok'
                except Exception:
                    cluster_healthy = False
            
            return {
                'status': 'healthy' if result == 'ok' and cluster_healthy else 'unhealthy',
                'latency_ms': round(latency, 2),
                'memory_used_bytes': memory_used,
                'memory_max_bytes': memory_max,
                'memory_usage_percent': round(memory_usage_percent, 2),
                'connected_clients': info.get('connected_clients', 0),
                'ops_per_sec': info.get('instantaneous_ops_per_sec', 0),
                'cluster_mode': self.is_cluster,
                'cluster_healthy': cluster_healthy,
                'connected': True
            }
            
        except Exception as e:
            return {
                'status': 'unhealthy',
                'error': str(e),
                'connected': False,
                'cluster_mode': self.is_cluster
            }
    
    async def close(self) -> None:
        """Close Redis connections."""
        try:
            if self.pubsub:
                await self.pubsub.close()
            
            if self.cluster_client:
                await self.cluster_client.close()
            elif self.pool:
                await self.pool.disconnect()
            
            self.logger.info("Redis connections closed")
            
        except Exception as e:
            self.logger.error(f"Error closing Redis connections: {e}")
    
    def __del__(self):
        """Cleanup on object destruction."""
        if hasattr(self, 'client') and self.client:
            try:
                asyncio.create_task(self.close())
            except Exception:
                pass
