"""OCPP 2.1 WebSocket server implementation."""

import asyncio
import http
import logging
import ssl
import uuid
from collections import defaultdict
from typing import Dict, Optional, Tuple

import uvloop
import websockets
from websockets import WebSocketServerProtocol

# V2X removed - out of scope for MVP per PRD Section 1.2
from .cache_manager import CacheManager
from .certificate_manager import CertificateManager
from .charging_profile_manager import ChargingProfileManager
from .config import Config
from .connection_manager import ConnectionManager
from .der_control_manager import DERControlManager
from .external_control_manager import ExternalControlManager
from .message_handler import MessageHandler
from .monitoring import get_logger, setup_monitoring
from .ocpp_handler import EnhancedOCPPChargePoint
from .priority_charging_manager import PriorityChargingManager
from .timescale_client import TimescaleClient

# VDV 463 integration
try:
    from adapters.vdv463.handler import VDV463Handler
    from adapters.vdv463.messages import ValidationMode

    VDV463_AVAILABLE = True
except ImportError:
    VDV463_AVAILABLE = False
    VDV463Handler = None
    ValidationMode = None


# Prometheus metrics - imported from monitoring module
from .monitoring import ERRORS_TOTAL
from .monitoring import WEBSOCKET_CONNECTIONS as CONNECTIONS_TOTAL


class _SuppressHandshakeEOFErrors(logging.Filter):
    """Suppress websockets ERROR logs caused by TCP probes that close with zero bytes.

    Load balancers and infrastructure health checkers often open a TCP connection
    and close it immediately without sending any data. The websockets library logs
    these at ERROR level under ``websockets.server``, but they are expected and
    harmless. This filter drops only records whose exception cause chain includes
    ``EOFError: stream ends after 0 bytes``; all other errors pass through.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        """Return False to suppress zero-byte TCP probe errors, True otherwise."""
        if record.levelno != logging.ERROR or not record.exc_info:
            return True
        exc = record.exc_info[1]
        while exc is not None:
            if isinstance(exc, EOFError) and "stream ends after 0 bytes" in str(exc):
                return False
            exc = getattr(exc, "__cause__", None)
        return True


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

        # Cache manager for performance optimization
        self.cache_manager: Optional[CacheManager] = None

        # V2G managers
        self.charging_profile_manager: Optional[ChargingProfileManager] = None
        self.der_control_manager: Optional[DERControlManager] = None
        self.priority_charging_manager: Optional[PriorityChargingManager] = None
        self.external_control_manager: Optional[ExternalControlManager] = None
        self.certificate_manager: Optional[CertificateManager] = None
        # V2X removed - out of scope for MVP

        # Server state
        self.server: Optional[websockets.WebSocketServer] = None
        self.running = False
        self._shutting_down = False  # Flag for graceful shutdown

        # Connection tracking
        self.connections: Dict[str, WebSocketServerProtocol] = {}
        self.charge_points: Dict[str, EnhancedOCPPChargePoint] = {}  # station_id -> charge_point
        self.station_connections: Dict[str, str] = {}  # station_id -> connection_id
        # Bounded message queues to prevent memory exhaustion (maxsize=1000 per station)
        self.message_queues: Dict[str, asyncio.Queue] = defaultdict(
            lambda: asyncio.Queue(maxsize=1000)
        )

        # Rate limiting - use connection manager's rate limiter
        # self.rate_limits: Dict[str, list] = defaultdict(list)  # Removed - using RateLimiter class

        # Background task tracking
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._rate_limit_task: Optional[asyncio.Task] = None

        # Suppress noisy ERROR logs from TCP health probes (zero-byte connections).
        # Applied once per server instance; harmless if added multiple times.
        logging.getLogger("websockets.server").addFilter(_SuppressHandshakeEOFErrors())

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
                subprotocols=["ocpp1.6", "ocpp2.1"],
                max_size=self.config.websocket.max_message_size,
                ping_interval=self.config.websocket.heartbeat_interval,
                ping_timeout=10,
                compression=None,  # Disable compression for performance
                process_request=self._process_request,
            )

            self.running = True
            self.logger.info(
                f"WebSocket server started on {self.config.websocket.host}:{self.config.websocket.port}"
            )

            # Signal the health check that the port is now bound so it stops
            # reporting spurious failures during the startup grace period.
            try:
                from .health_checks import notify_websocket_ready
                notify_websocket_ready()
            except Exception:
                pass

            # Start background tasks
            self._heartbeat_task = asyncio.create_task(self._heartbeat_monitor())
            self._rate_limit_task = asyncio.create_task(self._rate_limit_cleanup())

            # Keep server running
            await self.server.wait_closed()

        except Exception as e:
            self.logger.error(f"Failed to start WebSocket server: {e}")
            ERRORS_TOTAL.labels(error_type="startup_error", station_id="unknown").inc()
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
        """Initialize core components and V2G managers."""
        # Initialize cache manager for performance optimization
        self.cache_manager = CacheManager(max_size=5000, ttl_seconds=300)

        # Initialize connection manager
        self.connection_manager = ConnectionManager(config=self.config)

        # Initialize message handler
        self.message_handler = MessageHandler(
            connection_manager=self.connection_manager,
            config=self.config,
            timescale_client=self.timescale_client,
        )

        # Initialize V2G managers with cache manager
        self.charging_profile_manager = ChargingProfileManager(
            self.timescale_client, self.cache_manager
        )
        self.der_control_manager = DERControlManager(self.timescale_client)
        self.priority_charging_manager = PriorityChargingManager(self.timescale_client)
        self.external_control_manager = ExternalControlManager(self.timescale_client)
        self.certificate_manager = CertificateManager(self.timescale_client, self.cache_manager)
        # V2X controller removed - out of scope for MVP per PRD Section 1.2

        self.logger.info("All managers initialized successfully")

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
        context.set_ciphers("ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:!aNULL:!MD5:!DSS")

        return context

    async def _process_request(self, connection, request):
        """Respond to plain HTTP health-check GETs before the WebSocket handshake.

        Infrastructure probes (Railway, load balancers) sometimes send a plain
        HTTP GET to the WebSocket port instead of a WebSocket upgrade request.
        Returning a 200 OK here satisfies the probe and prevents a confusing
        handshake-failure error in the logs.

        Requires websockets >= 13 (``connection.respond()`` API).
        """
        if request.path.rstrip("/") in ("", "/health", "/healthz", "/ready"):
            return connection.respond(http.HTTPStatus.OK, "OK\n")
        return None

    async def _handle_connection(
        self, websocket: WebSocketServerProtocol, path: Optional[str] = None
    ) -> None:
        """Handle new WebSocket connection with path-based protocol routing.

        Websockets 12+ calls the handler with a single argument (the connection).
        Path is taken from connection.request.path when not passed.
        """
        if path is None:
            req = getattr(websocket, "request", None)
            path = getattr(req, "path", "") if req is not None else ""
        connection_id = str(uuid.uuid4())
        client_ip = websocket.remote_address[0] if websocket.remote_address else "unknown"

        # Check connection limits
        if len(self.connections) >= self.config.websocket.max_connections:
            self.logger.warning(f"Connection limit exceeded, rejecting {client_ip}")
            await websocket.close(1008, "Server overloaded")
            ERRORS_TOTAL.labels(error_type="connection_limit_exceeded", station_id="unknown").inc()
            return

        # Parse path for protocol routing
        path_parts = [p for p in path.strip("/").split("/") if p]
        protocol = path_parts[0] if path_parts else None

        # Route VDV 463 connections
        if protocol == "vdv463" and VDV463_AVAILABLE and self.config.vdv463.enabled:
            presystem_id = path_parts[1] if len(path_parts) > 1 else f"presystem_{connection_id}"

            # VDV 463 doesn't require OCPP subprotocol
            validation_mode = (
                ValidationMode.HARD
                if self.config.vdv463.validation_mode == "hard"
                else ValidationMode.SOFT
            )

            db_pool = self.timescale_client.pg_pool if self.timescale_client else None
            handler = VDV463Handler(
                presystem_id=presystem_id,
                websocket=websocket,
                connection_manager=self.connection_manager,
                config=self.config,
                depot_id=self.config.vdv463.default_depot_id,
                validation_mode=validation_mode,
                db_pool=db_pool,
            )

            self.logger.info(
                f"VDV 463 connection {connection_id} from {client_ip} for presystem {presystem_id}"
            )

            try:
                await handler.run()
            except websockets.exceptions.ConnectionClosed:
                self.logger.info(f"VDV 463 connection {connection_id} closed normally")
            except Exception as e:
                self.logger.error(f"Error handling VDV 463 connection {connection_id}: {e}")
                ERRORS_TOTAL.labels(error_type="connection_error", station_id="unknown").inc()
            return

        # Default to OCPP handling (legacy or /ocpp/{charge_point_id} paths)
        # Accept both OCPP 1.6 and OCPP 2.x connections.  The server is listed
        # as supporting "ocpp1.6" in the handshake so rejecting it here is
        # contradictory and causes chargers to wait 30 s for a response that
        # never arrives before the close frame lands.
        if websocket.subprotocol not in ("ocpp1.6", "ocpp2.0.1", "ocpp2.1"):
            self.logger.warning(f"Invalid subprotocol from {client_ip}: {websocket.subprotocol}")
            await websocket.close(1002, "Invalid subprotocol")
            ERRORS_TOTAL.labels(error_type="invalid_subprotocol", station_id="unknown").inc()
            return

        # Extract station ID from path
        if protocol == "ocpp" and len(path_parts) > 1:
            station_id = path_parts[1]
        elif path.strip("/"):
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
                await self._cleanup_connection(
                    old_connection_id, self.connections[old_connection_id], station_id
                )

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
            connection_manager=self.connection_manager,
            charging_profile_manager=self.charging_profile_manager,
            der_control_manager=self.der_control_manager,
            priority_charging_manager=self.priority_charging_manager,
            external_control_manager=self.external_control_manager,
            certificate_manager=self.certificate_manager,
        )
        self.charge_points[station_id] = charge_point

        # Register with connection manager
        await self.connection_manager.register_connection(
            station_id, connection_id, client_ip, websocket
        )

        self.logger.info(
            f"New OCPP connection {connection_id} from {client_ip} for station {station_id}"
        )

        try:
            # Start the OCPP charge point
            await charge_point.start()
        except websockets.exceptions.ConnectionClosed:
            self.logger.info(f"Connection {connection_id} closed normally")
        except Exception as e:
            self.logger.error(f"Error handling connection {connection_id}: {e}")
            ERRORS_TOTAL.labels(error_type="connection_error", station_id="unknown").inc()
        finally:
            await self._cleanup_connection(connection_id, websocket, station_id)

    async def _check_rate_limit(self, station_id: str) -> Tuple[bool, str]:
        """Check if station is within rate limits using connection manager's rate limiter."""
        return await self.connection_manager.check_message_rate_limit(station_id)

    async def _cleanup_connection(
        self, connection_id: str, websocket: WebSocketServerProtocol, station_id: str = None
    ) -> None:
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

        # Clean rate limit data - handled by RateLimiter class cleanup
        # self.rate_limits.pop(connection_id, None)  # Removed - using RateLimiter class

        self.logger.info(f"Cleaned up connection {connection_id} (station: {station_id})")

    def get_charge_point(self, station_id: str) -> Optional[EnhancedOCPPChargePoint]:
        """Get charge point for a station."""
        return self.charge_points.get(station_id)

    def get_all_charge_points(self) -> Dict[str, EnhancedOCPPChargePoint]:
        """Get all charge points."""
        return self.charge_points.copy()

    async def send_charging_profile(
        self, station_id: str, evse_id: int, charging_profile: Dict
    ) -> bool:
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
                # Rate limiter handles its own cleanup
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
