"""Redis client for real-time state management and caching."""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set
import redis.asyncio as redis
from redis.asyncio import ConnectionPool

from .config import RedisConfig
from .monitoring import get_logger


class RedisClient:
    """Async Redis client for V2G state management."""
    
    def __init__(self, config: RedisConfig):
        """Initialize Redis client."""
        self.config = config
        self.logger = get_logger(__name__)
        
        # Connection objects
        self.pool: Optional[ConnectionPool] = None
        self.client: Optional[redis.Redis] = None
        self.pubsub: Optional[redis.client.PubSub] = None
        
        # Lua scripts for atomic operations
        self._scripts: Dict[str, str] = {}
        self._load_lua_scripts()
    
    async def connect(self) -> None:
        """Connect to Redis cluster."""
        try:
            # Create connection pool
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
            
            # Create Redis client
            self.client = redis.Redis(connection_pool=self.pool)
            
            # Test connection
            await self.client.ping()
            
            # Setup pub/sub
            self.pubsub = self.client.pubsub()
            
            self.logger.info("Connected to Redis successfully")
            
        except Exception as e:
            self.logger.error(f"Failed to connect to Redis: {e}")
            raise
    
    async def close(self) -> None:
        """Close Redis connections."""
        try:
            if self.pubsub:
                await self.pubsub.close()
            if self.pool:
                await self.pool.disconnect()
            self.logger.info("Redis connections closed")
        except Exception as e:
            self.logger.error(f"Error closing Redis connections: {e}")
    
    # Connection Management
    async def register_connection(self, station_id: str, connection_id: str, client_ip: str) -> None:
        """Register WebSocket connection."""
        connection_data = {
            "server_id": "ws-server-1",  # Would be actual server ID in production
            "connection_id": connection_id,
            "client_ip": client_ip,
            "connected_at": datetime.now(timezone.utc).isoformat(),
            "last_heartbeat": datetime.now(timezone.utc).isoformat(),
            "protocol_version": "ocpp2.1",
            "status": "connected"
        }
        
        # Store connection info
        await self.client.hmset(f"charger:connections:{station_id}", connection_data)
        await self.client.expire(f"charger:connections:{station_id}", 120)  # 2-minute TTL
        
        # Add to active connections set
        await self.client.sadd("connections:active", station_id)
        
        self.logger.debug(f"Registered connection for station {station_id}")
    
    async def unregister_connection(self, station_id: str) -> None:
        """Unregister WebSocket connection."""
        await self.client.delete(f"charger:connections:{station_id}")
        await self.client.srem("connections:active", station_id)
        
        # Publish disconnection event
        await self.publish_notification(f"notify:charger:{station_id}", {
            "event_type": "connection_lost",
            "station_id": station_id,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })
        
        self.logger.debug(f"Unregistered connection for station {station_id}")
    
    async def get_active_connections(self) -> Set[str]:
        """Get all active station connections."""
        return await self.client.smembers("connections:active")
    
    # Station Information
    async def update_station_info(self, station_id: str, station_info: Dict[str, Any]) -> None:
        """Update station information."""
        await self.client.hmset(f"station:info:{station_id}", station_info)
        # Station info doesn't expire
        
        self.logger.debug(f"Updated station info for {station_id}")
    
    async def get_station_info(self, station_id: str) -> Optional[Dict[str, Any]]:
        """Get station information."""
        info = await self.client.hgetall(f"station:info:{station_id}")
        return info if info else None
    
    # Real-time State Management
    async def update_charger_status(self, station_id: str, status_data: Dict[str, Any]) -> None:
        """Update charger status atomically."""
        # Use Lua script for atomic update
        script = self.client.register_script(self._scripts["update_charger_state"])
        
        timestamp = datetime.now(timezone.utc).isoformat()
        status_json = json.dumps(status_data)
        
        await script([f"charger:state:{station_id}"], [status_json, timestamp])
        
        self.logger.debug(f"Updated charger status for {station_id}")
    
    async def get_charger_status(self, station_id: str) -> Optional[Dict[str, Any]]:
        """Get current charger status."""
        state_data = await self.client.hget(f"charger:state:{station_id}", "data")
        if state_data:
            return json.loads(state_data)
        return None
    
    async def update_heartbeat(self, station_id: str, timestamp: str) -> None:
        """Update last heartbeat timestamp."""
        await self.client.hset(f"charger:connections:{station_id}", "last_heartbeat", timestamp)
        await self.client.expire(f"charger:connections:{station_id}", 120)
    
    # Telemetry Management
    async def update_telemetry(self, station_id: str, telemetry_data: Dict[str, Any]) -> None:
        """Update real-time telemetry data."""
        timestamp = int(time.time() * 1000)  # Unix timestamp in milliseconds
        
        # Store in time series (if Redis Stack available) or as regular hash
        telemetry_key = f"charger:telemetry:{station_id}"
        
        # Store latest values in hash
        await self.client.hmset(f"{telemetry_key}:latest", telemetry_data)
        await self.client.expire(f"{telemetry_key}:latest", 300)  # 5 minutes TTL
        
        # Store in sorted set for time series (last 24 hours)
        for metric, value in telemetry_data.items():
            if isinstance(value, (int, float)):
                await self.client.zadd(f"{telemetry_key}:{metric}", {str(value): timestamp})
                # Keep only last 24 hours (86400000 ms)
                cutoff = timestamp - 86400000
                await self.client.zremrangebyscore(f"{telemetry_key}:{metric}", 0, cutoff)
        
        self.logger.debug(f"Updated telemetry for {station_id}")
    
    async def get_latest_telemetry(self, station_id: str) -> Optional[Dict[str, Any]]:
        """Get latest telemetry data."""
        telemetry = await self.client.hgetall(f"charger:telemetry:{station_id}:latest")
        
        # Convert numeric strings back to numbers
        if telemetry:
            for key, value in telemetry.items():
                try:
                    if '.' in value:
                        telemetry[key] = float(value)
                    else:
                        telemetry[key] = int(value)
                except (ValueError, TypeError):
                    pass  # Keep as string
        
        return telemetry if telemetry else None
    
    async def get_telemetry_history(self, station_id: str, metric: str, 
                                   minutes: int = 60) -> List[tuple]:
        """Get telemetry history for a specific metric."""
        end_time = int(time.time() * 1000)
        start_time = end_time - (minutes * 60 * 1000)
        
        history = await self.client.zrangebyscore(
            f"charger:telemetry:{station_id}:{metric}",
            start_time, end_time, withscores=True
        )
        
        return [(float(value), int(timestamp)) for value, timestamp in history]
    
    # Transaction Management
    async def update_transaction(self, station_id: str, transaction_id: str, 
                               transaction_data: Dict[str, Any]) -> None:
        """Update transaction information."""
        # Store transaction data
        await self.client.hmset(f"transaction:{transaction_id}", transaction_data)
        await self.client.expire(f"transaction:{transaction_id}", 86400)  # 24 hours
        
        # Add to station's active transactions
        if transaction_data.get("event_type") == "Started":
            await self.client.sadd(f"station:transactions:{station_id}", transaction_id)
        elif transaction_data.get("event_type") == "Ended":
            await self.client.srem(f"station:transactions:{station_id}", transaction_id)
        
        self.logger.debug(f"Updated transaction {transaction_id} for station {station_id}")
    
    async def get_active_transactions(self, station_id: str) -> List[str]:
        """Get active transaction IDs for a station."""
        return list(await self.client.smembers(f"station:transactions:{station_id}"))
    
    # EV Charging Needs and Schedules
    async def update_ev_needs(self, station_id: str, evse_id: int, 
                            needs_data: Dict[str, Any]) -> None:
        """Update EV charging needs."""
        key = f"charger:ev_needs:{station_id}:{evse_id}"
        await self.client.hmset(key, needs_data)
        await self.client.expire(key, 3600)  # 1 hour TTL
        
        # Publish to optimization system
        await self.publish_notification("notify:optimization:ev_needs", {
            "station_id": station_id,
            "evse_id": evse_id,
            "needs": needs_data
        })
    
    async def update_ev_schedule(self, station_id: str, evse_id: int, 
                               schedule_data: Dict[str, Any]) -> None:
        """Update EV proposed charging schedule."""
        key = f"charger:ev_schedule:{station_id}:{evse_id}"
        await self.client.hmset(key, schedule_data)
        await self.client.expire(key, 3600)  # 1 hour TTL
    
    # Charging Profile Management
    async def store_sent_profile(self, station_id: str, evse_id: int, 
                               profile: Dict[str, Any]) -> None:
        """Store sent charging profile."""
        profile_data = {
            "profile": json.dumps(profile),
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "status": "sent"
        }
        
        key = f"charger:profile:{station_id}:{evse_id}"
        await self.client.hmset(key, profile_data)
        await self.client.expire(key, 7200)  # 2 hours TTL
    
    async def get_active_profile(self, station_id: str, evse_id: int) -> Optional[Dict[str, Any]]:
        """Get active charging profile."""
        profile_data = await self.client.hgetall(f"charger:profile:{station_id}:{evse_id}")
        
        if profile_data and profile_data.get("profile"):
            profile_data["profile"] = json.loads(profile_data["profile"])
            return profile_data
        return None
    
    # Fleet Management
    async def update_fleet_state(self, operator_id: str, vehicle_states: Dict[str, Any]) -> None:
        """Update fleet-level state information."""
        # Store fleet state
        await self.client.hmset(f"fleet:state:{operator_id}", vehicle_states)
        await self.client.expire(f"fleet:state:{operator_id}", 300)  # 5 minutes TTL
        
        # Update availability bitmap
        for vehicle_index, available in vehicle_states.get("availability", {}).items():
            if available:
                await self.client.setbit(f"fleet:{operator_id}:available", int(vehicle_index), 1)
            else:
                await self.client.setbit(f"fleet:{operator_id}:available", int(vehicle_index), 0)
    
    async def get_available_vehicle_count(self, operator_id: str) -> int:
        """Get count of available vehicles in fleet."""
        return await self.client.bitcount(f"fleet:{operator_id}:available")
    
    # Market Data and Pricing
    async def update_electricity_prices(self, node_id: str, prices: Dict[str, Any]) -> None:
        """Update electricity price data."""
        price_data = {
            **prices,
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        
        await self.client.hmset(f"prices:lmp:{node_id}", price_data)
        await self.client.expire(f"prices:lmp:{node_id}", 300)  # 5 minutes TTL
    
    async def get_current_prices(self, node_id: str) -> Optional[Dict[str, Any]]:
        """Get current electricity prices."""
        return await self.client.hgetall(f"prices:lmp:{node_id}")
    
    # Optimization Data
    async def store_optimization_decision(self, decision_id: str, decision_data: Dict[str, Any]) -> None:
        """Store optimization decision."""
        # Store latest decision
        await self.client.hmset("optimization:decision:latest", {
            "decision_id": decision_id,
            "computed_at": datetime.now(timezone.utc).isoformat(),
            **decision_data
        })
        
        # Add to decision history (keep last 100)
        decision_json = json.dumps(decision_data)
        await self.client.lpush("optimization:decisions", decision_json)
        await self.client.ltrim("optimization:decisions", 0, 99)
    
    async def get_latest_optimization(self) -> Optional[Dict[str, Any]]:
        """Get latest optimization decision."""
        return await self.client.hgetall("optimization:decision:latest")
    
    # Data Transfer Storage
    async def store_data_transfer(self, station_id: str, transfer_data: Dict[str, Any]) -> None:
        """Store custom data transfer."""
        key = f"charger:data_transfer:{station_id}"
        transfer_json = json.dumps(transfer_data)
        
        # Store in list (keep last 50 transfers)
        await self.client.lpush(key, transfer_json)
        await self.client.ltrim(key, 0, 49)
        await self.client.expire(key, 86400)  # 24 hours
    
    # Pub/Sub for Real-time Notifications
    async def publish_notification(self, channel: str, data: Dict[str, Any]) -> None:
        """Publish notification to Redis channel."""
        message = json.dumps(data, separators=(',', ':'))
        await self.client.publish(channel, message)
    
    async def subscribe_to_notifications(self, channels: List[str]) -> None:
        """Subscribe to notification channels."""
        if self.pubsub:
            await self.pubsub.subscribe(*channels)
    
    async def get_next_notification(self) -> Optional[Dict[str, Any]]:
        """Get next notification message."""
        if not self.pubsub:
            return None
        
        try:
            message = await asyncio.wait_for(self.pubsub.get_message(timeout=1), timeout=2)
            if message and message.get('type') == 'message':
                return {
                    'channel': message['channel'],
                    'data': json.loads(message['data'])
                }
        except (asyncio.TimeoutError, json.JSONDecodeError):
            pass
        
        return None
    
    # Health Check
    async def health_check(self) -> Dict[str, Any]:
        """Perform Redis health check."""
        try:
            # Test basic operation
            test_key = "health:test"
            await self.client.set(test_key, "ok", ex=10)
            result = await self.client.get(test_key)
            await self.client.delete(test_key)
            
            # Get memory info
            info = await self.client.info("memory")
            memory_usage = info.get("used_memory", 0)
            max_memory = info.get("maxmemory", 0)
            
            return {
                "status": "healthy" if result == "ok" else "unhealthy",
                "memory_used": memory_usage,
                "memory_max": max_memory,
                "memory_usage_percent": memory_usage / max_memory * 100 if max_memory > 0 else 0,
                "connected": True
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "error": str(e),
                "connected": False
            }
    
    def _load_lua_scripts(self) -> None:
        """Load Lua scripts for atomic operations."""
        
        # Script for atomic charger state update
        self._scripts["update_charger_state"] = """
            local station_key = KEYS[1]
            local state_json = ARGV[1]
            local timestamp = ARGV[2]
            
            -- Update state
            redis.call('HSET', station_key, 'data', state_json)
            redis.call('HSET', station_key, 'updated_at', timestamp)
            redis.call('EXPIRE', station_key, 300)
            
            -- Update heartbeat
            local connection_key = string.gsub(station_key, 'state', 'connections')
            redis.call('HSET', connection_key, 'last_heartbeat', timestamp)
            
            -- Publish notification
            local station_id = string.match(station_key, 'charger:state:(.+)')
            if station_id then
                redis.call('PUBLISH', 'notify:charger:' .. station_id, state_json)
            end
            
            return 'OK'
        """
