"""OCPP 2.1 WebSocket server implementation."""

import asyncio
import json
import logging
import ssl
import time
import uuid
from collections import defaultdict
from typing import Dict, Optional, Set
import uvloop
import websockets
from websockets.server import WebSocketServerProtocol
from prometheus_client import Counter, Histogram, Gauge

from .config import Config
from .connection_manager import ConnectionManager
from .message_handler import MessageHandler
from .ocpp_handler import EnhancedOCPPChargePoint
from .monitoring import setup_monitoring, get_logger
from .timescale_client import TimescaleClient


# Prometheus metrics
CONNECTIONS_TOTAL = Gauge("websocket_connections_active_total", "Total active WebSocket connections")
MESSAGES_RECEIVED = Counter("websocket_messages_received_total", "Total messages received", ["message_type"])
MESSAGES_SENT = Counter("websocket_messages_sent_total", "Total messages sent", ["message_type"])
MESSAGE_PROCESSING_TIME = Histogram("websocket_message_processing_seconds", "Message processing time", ["message_type"])
ERRORS_TOTAL = Counter("websocket_errors_total", "Total WebSocket errors", ["error_type"])


class OCPPWebSocketServer:
    """High-performance OCPP 2.1 WebSocket server."""
    
    def __init__(self, config: Config, timescale_client: TimescaleClient, optimization_engine=None):
        """Initialize the WebSocket server."""
        self.config = config
        self.logger = get_logger(__name__)
        self.timescale_client = timescale_client
        self.optimization_engine = optimization_engine
        
        # Core components
        self.connection_manager: Optional[ConnectionManager] = None
        self.message_handler: Optional[MessageHandler] = None
        
        # Server state
        self.server: Optional[websockets.WebSocketServer] = None
        self.running = False
        
        # Connection tracking
        self.connections: Dict[str, WebSocketServerProtocol] = {}
        self.charge_points: Dict[str, EnhancedOCPPChargePoint] = {}  # station_id -> charge_point
        self.station_connections: Dict[str, str] = {}  # station_id -> connection_id
        self.message_queues: Dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        
        # Rate limiting
        self.rate_limits: Dict[str, list] = defaultdict(list)
        
        # Background task tracking
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._rate_limit_task: Optional[asyncio.Task] = None
    
    async def start(self) -> None:
        """Start the WebSocket server and initialize components."""
        self.logger.info("Starting OCPP WebSocket server...")
        
        try:
            # Initialize components
            await self._initialize_components()
            
            # Wire optimization engine to connection manager
            if self.optimization_engine and self.connection_manager:
                self.optimization_engine.set_connection_manager(self.connection_manager)
            
            # Setup SSL context if TLS is configured
            ssl_context = self._setup_ssl_context()
            
            # Start WebSocket server
            self.server = await websockets.serve(
                self._handle_connection,
                self.config.websocket.host,
                self.config.websocket.port,
                ssl=ssl_context,
                subprotocols=["ocpp2.1"],
                max_size=self.config.websocket.max_message_size,
                ping_interval=self.config.websocket.heartbeat_interval,
                ping_timeout=10,
                compression=None,  # Disable compression for performance
            )
            
            self.running = True
            self.logger.info(
                f"WebSocket server started on {self.config.websocket.host}:{self.config.websocket.port}"
            )
            
            # Start background tasks
            self._heartbeat_task = asyncio.create_task(self._heartbeat_monitor())
            self._rate_limit_task = asyncio.create_task(self._rate_limit_cleanup())
            
            # Keep server running
            await self.server.wait_closed()
            
        except Exception as e:
            self.logger.error(f"Failed to start WebSocket server: {e}")
            ERRORS_TOTAL.labels(error_type="startup_error").inc()
            raise
    
    async def stop(self) -> None:
        """Stop the WebSocket server gracefully."""
        self.logger.info("Stopping WebSocket server...")
        
        self.running = False
        
        # Cancel background tasks
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        if self._rate_limit_task:
            self._rate_limit_task.cancel()
            try:
                await self._rate_limit_task
            except asyncio.CancelledError:
                pass
        
        # Close all connections
        close_tasks = []
        for conn_id, websocket in self.connections.items():
            self.logger.info(f"Closing connection {conn_id}")
            close_tasks.append(self._close_connection_gracefully(websocket))
        
        if close_tasks:
            await asyncio.gather(*close_tasks, return_exceptions=True)
        
        # Close server
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        
        self.logger.info("WebSocket server stopped")
    
    async def _initialize_components(self) -> None:
        """Initialize core components."""
        # Initialize connection manager
        self.connection_manager = ConnectionManager(
            config=self.config
        )
        
        # Initialize message handler
        self.message_handler = MessageHandler(
            connection_manager=self.connection_manager,
            config=self.config,
            timescale_client=self.timescale_client
        )
    
    def set_optimization_engine(self, optimization_engine) -> None:
        """Set optimization engine and wire it to connection manager."""
        if optimization_engine and self.connection_manager:
            optimization_engine.set_connection_manager(self.connection_manager)
            self.logger.info("Optimization engine wired to connection manager")
    
    def _setup_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Setup SSL context for TLS connections."""
        if not self.config.tls.cert_path or not self.config.tls.key_path:
            return None
        
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.config.tls.cert_path, self.config.tls.key_path)
        
        if self.config.tls.ca_path and self.config.tls.verify_client:
            context.load_verify_locations(self.config.tls.ca_path)
            context.verify_mode = ssl.CERT_REQUIRED
        
        # Security settings
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.set_ciphers('ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:!aNULL:!MD5:!DSS')
        
        return context
    
    async def _handle_connection(self, websocket: WebSocketServerProtocol, path: str) -> None:
        """Handle new WebSocket connection."""
        connection_id = str(uuid.uuid4())
        client_ip = websocket.remote_address[0] if websocket.remote_address else "unknown"
        
        # Check connection limits
        if len(self.connections) >= self.config.websocket.max_connections:
            self.logger.warning(f"Connection limit exceeded, rejecting {client_ip}")
            await websocket.close(1008, "Server overloaded")
            ERRORS_TOTAL.labels(error_type="connection_limit_exceeded").inc()
            return
        
        # Validate OCPP subprotocol
        if websocket.subprotocol != "ocpp2.1":
            self.logger.warning(f"Invalid subprotocol from {client_ip}: {websocket.subprotocol}")
            await websocket.close(1002, "Invalid subprotocol")
            ERRORS_TOTAL.labels(error_type="invalid_subprotocol").inc()
            return
        
        # Extract station ID from path
        if path.strip("/"):
            station_id = path.strip("/")
        else:
            # Use full UUID to avoid collisions
            station_id = f"station_{connection_id}"
        
        # Check if station already connected and clean up old connection
        if station_id in self.station_connections:
            old_connection_id = self.station_connections[station_id]
            self.logger.warning(f"Station {station_id} reconnecting, cleaning up old connection")
            # Clean up old connection
            if old_connection_id in self.connections:
                await self._cleanup_connection(old_connection_id, self.connections[old_connection_id], station_id)
        
        # Store connection
        self.connections[connection_id] = websocket
        self.station_connections[station_id] = connection_id
        CONNECTIONS_TOTAL.set(len(self.connections))
        
        # Create enhanced OCPP charge point
        charge_point = EnhancedOCPPChargePoint(
            station_id=station_id,
            connection=websocket,
            config=self.config,
            timescale_client=self.timescale_client,
            connection_manager=self.connection_manager
        )
        self.charge_points[station_id] = charge_point
        
        # Register with connection manager
        await self.connection_manager.register_connection(
            station_id, connection_id, client_ip, websocket
        )
        
        self.logger.info(f"New connection {connection_id} from {client_ip} for station {station_id}")
        
        try:
            # Start the OCPP charge point
            await charge_point.start()
        except websockets.exceptions.ConnectionClosed:
            self.logger.info(f"Connection {connection_id} closed normally")
        except Exception as e:
            self.logger.error(f"Error handling connection {connection_id}: {e}")
            ERRORS_TOTAL.labels(error_type="connection_error").inc()
        finally:
            await self._cleanup_connection(connection_id, websocket, station_id)
    
    
    
    def _check_rate_limit(self, connection_id: str) -> bool:
        """Check if connection is within rate limits."""
        now = time.time()
        minute_ago = now - 60
        
        # Clean old entries
        self.rate_limits[connection_id] = [
            ts for ts in self.rate_limits[connection_id] if ts > minute_ago
        ]
        
        # Check limit
        if len(self.rate_limits[connection_id]) >= self.config.websocket.rate_limit_per_minute:
            return False
        
        # Add current request
        self.rate_limits[connection_id].append(now)
        return True
    
    async def _cleanup_connection(self, connection_id: str, websocket: WebSocketServerProtocol, station_id: str = None) -> None:
        """Cleanup connection resources."""
        # Remove from connections
        self.connections.pop(connection_id, None)
        CONNECTIONS_TOTAL.set(len(self.connections))
        
        # Find station ID if not provided
        if not station_id:
            for sid, cid in self.station_connections.items():
                if cid == connection_id:
                    station_id = sid
                    break
        
        if station_id:
            self.station_connections.pop(station_id, None)
            self.charge_points.pop(station_id, None)
            if self.connection_manager:
                await self.connection_manager.unregister_connection(station_id)
        
        # Clean rate limit data
        self.rate_limits.pop(connection_id, None)
        
        self.logger.info(f"Cleaned up connection {connection_id} (station: {station_id})")
    
    def get_charge_point(self, station_id: str) -> Optional[EnhancedOCPPChargePoint]:
        """Get charge point for a station."""
        return self.charge_points.get(station_id)
    
    def get_all_charge_points(self) -> Dict[str, EnhancedOCPPChargePoint]:
        """Get all charge points."""
        return self.charge_points.copy()
    
    async def send_charging_profile(self, station_id: str, evse_id: int, charging_profile: Dict) -> bool:
        """Send charging profile to a station."""
        charge_point = self.get_charge_point(station_id)
        if not charge_point:
            self.logger.warning(f"No charge point found for station {station_id}")
            return False
        
        return await charge_point.send_charging_profile(evse_id, charging_profile)
    
    async def send_der_control(self, station_id: str, der_control: Dict) -> bool:
        """Send DER control to a station."""
        charge_point = self.get_charge_point(station_id)
        if not charge_point:
            self.logger.warning(f"No charge point found for station {station_id}")
            return False
        
        return await charge_point.send_der_control(der_control)
    
    async def clear_der_control(self, station_id: str) -> bool:
        """Clear DER control for a station."""
        charge_point = self.get_charge_point(station_id)
        if not charge_point:
            self.logger.warning(f"No charge point found for station {station_id}")
            return False
        
        return await charge_point.clear_der_control()
    
    async def _close_connection_gracefully(self, websocket: WebSocketServerProtocol) -> None:
        """Close connection gracefully with proper cleanup."""
        try:
            await websocket.close(1001, "Server shutdown")
        except Exception:
            pass
    
    async def _heartbeat_monitor(self) -> None:
        """Monitor connection health and send heartbeats."""
        while self.running:
            try:
                # This would be implemented to check connection health
                # For now, we rely on websockets library ping/pong
                await asyncio.sleep(30)
            except Exception as e:
                self.logger.error(f"Heartbeat monitor error: {e}")
                await asyncio.sleep(5)
    
    async def _rate_limit_cleanup(self) -> None:
        """Cleanup old rate limit entries periodically."""
        while self.running:
            try:
                now = time.time()
                minute_ago = now - 60
                
                for connection_id in list(self.rate_limits.keys()):
                    self.rate_limits[connection_id] = [
                        ts for ts in self.rate_limits[connection_id] if ts > minute_ago
                    ]
                    
                    # Remove empty entries
                    if not self.rate_limits[connection_id]:
                        del self.rate_limits[connection_id]
                
                await asyncio.sleep(60)  # Clean every minute
                
            except Exception as e:
                self.logger.error(f"Rate limit cleanup error: {e}")
                await asyncio.sleep(30)


async def run_server(config: Config) -> None:
    """Run the WebSocket server with uvloop for better performance."""
    # Set up uvloop for better async performance
    uvloop.install()
    
    # Setup monitoring
    setup_monitoring(config.monitoring)
    
    # Create and start server
    server = OCPPWebSocketServer(config)
    
    try:
        await server.start()
    except KeyboardInterrupt:
        logging.info("Received shutdown signal")
    finally:
        await server.stop()


if __name__ == "__main__":
    import sys
    from .config import Config
    
    # Load configuration
    config = Config.from_env()
    
    # Run server
    try:
        asyncio.run(run_server(config))
    except KeyboardInterrupt:
        print("\nServer stopped by user")
        sys.exit(0)
