"""Connection manager for WebSocket connections and routing."""

import asyncio
import json
import time
from typing import Dict, Optional, Set
from websockets.server import WebSocketServerProtocol

from .config import Config
# Redis removed for simplification
from .monitoring import get_logger


class ConnectionManager:
    """Manages WebSocket connections and message routing."""
    
    def __init__(self, config: Config):
        """Initialize connection manager."""
        # Redis client removed for simplification
        self.config = config
        self.logger = get_logger(__name__)
        
        # Local connection storage
        self.connections: Dict[str, WebSocketServerProtocol] = {}
        self.station_connections: Dict[str, str] = {}  # station_id -> connection_id
        
        # Connection health monitoring
        self.last_heartbeats: Dict[str, float] = {}
        self.connection_stats: Dict[str, Dict] = {}
        
        # Background tasks
        self._monitoring_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._started = False
    
    def _start_monitoring(self) -> None:
        """Start background monitoring tasks."""
        self._monitoring_task = asyncio.create_task(self._monitor_connections())
        self._cleanup_task = asyncio.create_task(self._cleanup_stale_connections())
    
    async def register_connection(self, station_id: str, connection_id: str, 
                                client_ip: str, websocket: WebSocketServerProtocol) -> None:
        """Register a new WebSocket connection."""
        # Start monitoring if not already started
        if not self._started:
            self._start_monitoring()
            self._started = True
            
        try:
            # Store connection locally
            self.connections[connection_id] = websocket
            self.station_connections[station_id] = connection_id
            self.last_heartbeats[station_id] = time.time()
            
            # Initialize connection stats
            self.connection_stats[connection_id] = {
                "station_id": station_id,
                "client_ip": client_ip,
                "connected_at": time.time(),
                "messages_received": 0,
                "messages_sent": 0,
                "bytes_received": 0,
                "bytes_sent": 0,
                "last_activity": time.time(),
            }
            
            # Redis registration removed for simplification
            
            self.logger.info(f"Registered connection {connection_id} for station {station_id} from {client_ip}")
            
        except Exception as e:
            self.logger.error(f"Failed to register connection {connection_id}: {e}")
            raise
    
    async def unregister_connection(self, station_id: str, connection_id: Optional[str] = None) -> None:
        """Unregister a WebSocket connection."""
        try:
            # Find connection ID if not provided
            if not connection_id:
                connection_id = self.station_connections.get(station_id)
            
            if not connection_id:
                self.logger.warning(f"No connection found for station {station_id}")
                return
            
            # Remove from local storage
            self.connections.pop(connection_id, None)
            self.station_connections.pop(station_id, None)
            self.last_heartbeats.pop(station_id, None)
            stats = self.connection_stats.pop(connection_id, None)
            
            # Redis unregistration removed for simplification
            
            # Log connection statistics
            if stats:
                duration = time.time() - stats["connected_at"]
                self.logger.info(
                    f"Unregistered connection {connection_id} for station {station_id}. "
                    f"Duration: {duration:.1f}s, Messages: {stats['messages_received']}/{stats['messages_sent']}"
                )
            
        except Exception as e:
            self.logger.error(f"Failed to unregister connection for station {station_id}: {e}")
    
    async def get_connection(self, station_id: str) -> Optional[WebSocketServerProtocol]:
        """Get WebSocket connection for a station."""
        connection_id = self.station_connections.get(station_id)
        if connection_id:
            return self.connections.get(connection_id)
        return None
    
    async def send_message_to_station(self, station_id: str, message: Dict) -> bool:
        """Send message to a specific station."""
        connection = await self.get_connection(station_id)
        if not connection:
            self.logger.warning(f"No connection found for station {station_id}")
            return False
        
        try:
            # Serialize message
            raw_message = json.dumps(message, separators=(',', ':'))
            
            # Send message
            await connection.send(raw_message)
            
            # Update statistics
            connection_id = self.station_connections[station_id]
            if connection_id in self.connection_stats:
                self.connection_stats[connection_id]["messages_sent"] += 1
                self.connection_stats[connection_id]["bytes_sent"] += len(raw_message)
                self.connection_stats[connection_id]["last_activity"] = time.time()
            
            self.logger.debug(f"Sent message to station {station_id}: {message.get('action', 'unknown')}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to send message to station {station_id}: {e}")
            # Connection might be dead, mark for cleanup
            await self._mark_connection_for_cleanup(station_id)
            return False
    
    async def broadcast_message(self, message: Dict, station_filter: Optional[Set[str]] = None) -> int:
        """Broadcast message to multiple stations."""
        sent_count = 0
        
        # Determine target stations
        target_stations = station_filter if station_filter else set(self.station_connections.keys())
        
        # Send to each station
        send_tasks = []
        for station_id in target_stations:
            if station_id in self.station_connections:
                task = self.send_message_to_station(station_id, message)
                send_tasks.append(task)
        
        # Wait for all sends to complete
        if send_tasks:
            results = await asyncio.gather(*send_tasks, return_exceptions=True)
            sent_count = sum(1 for result in results if result is True)
        
        self.logger.info(f"Broadcast message to {sent_count}/{len(target_stations)} stations")
        return sent_count
    
    async def send_charging_profile(self, station_id: str, evse_id: int, 
                                  charging_profile: Dict) -> bool:
        """Send charging profile to station."""
        message = {
            "messageTypeId": 2,  # CALL
            "uniqueId": f"cp_{int(time.time())}",
            "action": "SetChargingProfile",
            "payload": {
                "evseId": evse_id,
                "chargingProfile": charging_profile
            }
        }
        
        success = await self.send_message_to_station(station_id, message)
        
        if success:
            # Profile tracking removed for simplification
            self.logger.info(f"Sent charging profile to {station_id}, EVSE {evse_id}")
        
        return success
    
    async def request_composite_schedule(self, station_id: str, evse_id: int, 
                                       duration: int) -> bool:
        """Request composite schedule from station."""
        message = {
            "messageTypeId": 2,  # CALL
            "uniqueId": f"gcs_{int(time.time())}",
            "action": "GetCompositeSchedule",
            "payload": {
                "evseId": evse_id,
                "duration": duration,
                "chargingRateUnit": "W"
            }
        }
        
        return await self.send_message_to_station(station_id, message)
    
    async def update_heartbeat(self, station_id: str) -> None:
        """Update last heartbeat timestamp for a station."""
        self.last_heartbeats[station_id] = time.time()
        # Redis heartbeat update removed for simplification
    
    async def record_message_received(self, station_id: str, message_size: int) -> None:
        """Record statistics for received message."""
        connection_id = self.station_connections.get(station_id)
        if connection_id and connection_id in self.connection_stats:
            self.connection_stats[connection_id]["messages_received"] += 1
            self.connection_stats[connection_id]["bytes_received"] += message_size
            self.connection_stats[connection_id]["last_activity"] = time.time()
    
    def get_connection_stats(self) -> Dict[str, Dict]:
        """Get connection statistics."""
        return self.connection_stats.copy()
    
    def get_active_stations(self) -> Set[str]:
        """Get set of active station IDs."""
        return set(self.station_connections.keys())
    
    def get_connection_count(self) -> int:
        """Get total number of active connections."""
        return len(self.connections)
    
    async def _monitor_connections(self) -> None:
        """Monitor connection health in background."""
        while True:
            try:
                now = time.time()
                stale_threshold = now - (self.config.websocket.heartbeat_interval * 3)
                stale_stations = []
                
                # Check for stale connections
                for station_id, last_heartbeat in self.last_heartbeats.items():
                    if last_heartbeat < stale_threshold:
                        stale_stations.append(station_id)
                
                # Cleanup stale connections
                for station_id in stale_stations:
                    self.logger.warning(f"Connection for station {station_id} appears stale")
                    await self._mark_connection_for_cleanup(station_id)
                
                # Redis connection status update removed for simplification
                
                await asyncio.sleep(30)  # Monitor every 30 seconds
                
            except Exception as e:
                self.logger.error(f"Error in connection monitoring: {e}")
                await asyncio.sleep(10)
    
    async def _cleanup_stale_connections(self) -> None:
        """Cleanup stale connections periodically."""
        while True:
            try:
                # Clean up connection stats for closed connections
                closed_connections = []
                for connection_id, websocket in self.connections.items():
                    if websocket.closed:
                        closed_connections.append(connection_id)
                
                for connection_id in closed_connections:
                    # Find station ID
                    station_id = None
                    for sid, cid in self.station_connections.items():
                        if cid == connection_id:
                            station_id = sid
                            break
                    
                    if station_id:
                        await self.unregister_connection(station_id, connection_id)
                
                await asyncio.sleep(60)  # Cleanup every minute
                
            except Exception as e:
                self.logger.error(f"Error in connection cleanup: {e}")
                await asyncio.sleep(30)
    
    async def _mark_connection_for_cleanup(self, station_id: str) -> None:
        """Mark a connection for cleanup due to issues."""
        connection_id = self.station_connections.get(station_id)
        if connection_id and connection_id in self.connections:
            websocket = self.connections[connection_id]
            try:
                # Try to close gracefully
                if not websocket.closed:
                    await websocket.close(1001, "Connection marked for cleanup")
            except Exception:
                pass  # Connection might already be closed
            
            # Unregister the connection
            await self.unregister_connection(station_id, connection_id)
    
    async def shutdown(self) -> None:
        """Shutdown connection manager."""
        self.logger.info("Shutting down connection manager...")
        
        # Cancel background tasks
        if self._monitoring_task:
            self._monitoring_task.cancel()
        if self._cleanup_task:
            self._cleanup_task.cancel()
        
        # Close all connections
        close_tasks = []
        for station_id in list(self.station_connections.keys()):
            connection = await self.get_connection(station_id)
            if connection and not connection.closed:
                close_tasks.append(connection.close(1001, "Server shutdown"))
        
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        
        # Clear local state
        self.connections.clear()
        self.station_connections.clear()
        self.last_heartbeats.clear()
        self.connection_stats.clear()
        
        self.logger.info("Connection manager shutdown complete")
    
    async def get_health_status(self) -> Dict:
        """Get health status of connection manager."""
        now = time.time()
        
        # Count healthy connections
        healthy_count = 0
        stale_count = 0
        stale_threshold = now - (self.config.websocket.heartbeat_interval * 3)
        
        for station_id, last_heartbeat in self.last_heartbeats.items():
            if last_heartbeat >= stale_threshold:
                healthy_count += 1
            else:
                stale_count += 1
        
        return {
            "total_connections": len(self.connections),
            "healthy_connections": healthy_count,
            "stale_connections": stale_count,
            "active_stations": len(self.station_connections),
            "monitoring_active": not (self._monitoring_task and self._monitoring_task.done()),
            "cleanup_active": not (self._cleanup_task and self._cleanup_task.done()),
        }
