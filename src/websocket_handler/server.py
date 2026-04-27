"""OCPP 2.1 WebSocket server implementation."""

import asyncio
import base64
import binascii
import http
import ipaddress
import logging
import os
import secrets
import ssl
import uuid
from collections import defaultdict
from typing import Any, Dict, Optional, Tuple

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
from .security_manager import SecurityConfig, SecurityManager
from .supabase_client import SupabaseClient
from .timescale_client import TimescaleClient

# Geo-blocking (Article 73-3 compliance)
try:
    from src.security.geo_block import check_ip_blocked

    GEO_BLOCK_AVAILABLE = True
except ImportError:
    GEO_BLOCK_AVAILABLE = False
    check_ip_blocked = None  # type: ignore[assignment]
    logging.getLogger(__name__).warning(
        "Geo-blocking module not available — Article 73-3 controls inactive"
    )

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
from .monitoring import CONNECTED_CHARGERS_COUNT
from .monitoring import WEBSOCKET_CONNECTIONS as CONNECTIONS_TOTAL

try:
    from .ocpp16_adapter import OCPP16Session

    OCPP16_AVAILABLE = True
    logging.getLogger(__name__).info("OCPP 1.6 adapter loaded — OCPP16_AVAILABLE=True")
except ImportError as _ocpp16_import_err:
    OCPP16_AVAILABLE = False
    OCPP16Session = None  # type: ignore[assignment,misc]
    logging.getLogger(__name__).error(
        "OCPP 1.6 adapter failed to import — OCPP 1.6 chargers will be misrouted "
        "through the 2.0.1 handler and produce schema validation errors. "
        "Import error: %s",
        _ocpp16_import_err,
    )


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

    def __init__(
        self,
        config: Config,
        timescale_client: TimescaleClient,
        optimization_engine=None,
        supabase_client: Optional[SupabaseClient] = None,
    ):
        """Initialize the WebSocket server."""
        self.config = config
        self.logger = get_logger(__name__)
        self.timescale_client = timescale_client
        self.supabase_client = supabase_client
        self.optimization_engine = optimization_engine

        # Core components
        self.connection_manager: Optional[ConnectionManager] = None
        self.message_handler: Optional[MessageHandler] = None
        self.security_manager: Optional[SecurityManager] = None

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

        # Per-IP connection tracking (M8 — prevent single IP from exhausting all slots)
        self._ip_connection_count: dict[str, int] = defaultdict(int)
        self._max_connections_per_ip = int(os.getenv("MAX_CONNECTIONS_PER_IP", "10"))
        self._connection_client_ips: dict[str, str] = {}
        self._trusted_proxy_networks = self._parse_ip_networks(
            os.getenv("OCPP_TRUSTED_PROXY_RANGES", "")
        )
        self._trust_private_proxy_headers = (
            os.getenv("OCPP_TRUST_PRIVATE_PROXY_HEADERS", "true").lower() == "true"
        )

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
                ping_interval=self.config.websocket.ping_interval,
                ping_timeout=self.config.websocket.ping_timeout,
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

        # Initialize message handler — pass optimization_engine so successful
        # HTTP event pushes update the fast-path liveness timestamp.
        self.message_handler = MessageHandler(
            connection_manager=self.connection_manager,
            config=self.config,
            timescale_client=self.timescale_client,
            optimization_engine=self.optimization_engine,
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

        # Initialize security manager for station authentication (Article 73-3 / NIS2)
        # Configurable via environment: OCPP_REQUIRE_AUTH (default "true" in production)
        require_auth = os.getenv("OCPP_REQUIRE_AUTH", "true").lower() == "true"
        security_config = SecurityConfig(
            require_station_auth=require_auth,
            require_mtls=False,  # Railway terminates TLS at edge
        )
        self.security_manager = SecurityManager(self.timescale_client, security_config)
        self.logger.info(
            "Security manager initialized (require_auth=%s)", require_auth
        )

        _environment = os.getenv("ENVIRONMENT", "development")
        if _environment == "production" and not require_auth:
            raise RuntimeError(
                "OCPP_REQUIRE_AUTH must be 'true' in production. "
                "Unauthenticated OCPP connections are a critical security risk."
            )

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

    @staticmethod
    def _parse_ip_networks(ranges: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        """Parse comma-separated IP/CIDR ranges, ignoring invalid entries."""
        networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
        for raw_range in ranges.split(","):
            raw_range = raw_range.strip()
            if not raw_range:
                continue
            try:
                networks.append(ipaddress.ip_network(raw_range, strict=False))
            except ValueError:
                logging.getLogger(__name__).warning(
                    "Invalid OCPP_TRUSTED_PROXY_RANGES entry ignored: %s", raw_range
                )
        return networks

    @staticmethod
    def _normalize_forwarded_ip(raw_ip: str) -> Optional[str]:
        """Normalize one forwarded IP candidate, stripping quotes, brackets, and ports."""
        value = raw_ip.strip().strip('"')
        if not value:
            return None

        if value.startswith("["):
            host, separator, _port = value[1:].partition("]")
            value = host if separator else value
        elif value.count(":") == 1 and "." in value:
            value = value.rsplit(":", 1)[0]

        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            return None

    @classmethod
    def _extract_forwarded_ip(cls, headers: Any) -> Optional[str]:
        """Extract the original client IP from common reverse-proxy headers."""
        if not hasattr(headers, "get"):
            return None

        forwarded = headers.get("Forwarded", "")
        for proxy_hop in forwarded.split(","):
            for item in proxy_hop.split(";"):
                key, separator, value = item.strip().partition("=")
                if separator and key.lower() == "for":
                    parsed = cls._normalize_forwarded_ip(value)
                    if parsed:
                        return parsed

        x_forwarded_for = headers.get("X-Forwarded-For", "")
        for candidate in x_forwarded_for.split(","):
            parsed = cls._normalize_forwarded_ip(candidate)
            if parsed:
                return parsed

        x_real_ip = headers.get("X-Real-IP", "")
        return cls._normalize_forwarded_ip(x_real_ip) if x_real_ip else None

    @staticmethod
    def _get_peer_ip(websocket: WebSocketServerProtocol) -> str:
        """Return the direct TCP peer IP, or unknown when unavailable."""
        remote_address = getattr(websocket, "remote_address", None)
        if isinstance(remote_address, (tuple, list)) and remote_address:
            return str(remote_address[0])
        return "unknown"

    def _is_trusted_proxy_ip(self, ip_str: str) -> bool:
        """Return True when forwarded headers from this peer may be trusted."""
        try:
            ip_addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return False

        if self._trust_private_proxy_headers and (
            ip_addr.is_private or ip_addr.is_loopback or ip_addr.is_link_local
        ):
            return True

        return any(ip_addr in network for network in self._trusted_proxy_networks)

    def _get_client_ip(self, websocket: WebSocketServerProtocol) -> str:
        """Resolve the effective client IP for geo-blocking, auth logs, and limits."""
        peer_ip = self._get_peer_ip(websocket)
        if peer_ip == "unknown" or not self._is_trusted_proxy_ip(peer_ip):
            return peer_ip

        request = getattr(websocket, "request", None)
        headers = getattr(request, "headers", {}) if request is not None else {}
        forwarded_ip = self._extract_forwarded_ip(headers)
        if forwarded_ip:
            self.logger.info(
                "Using forwarded OCPP client IP %s from trusted proxy %s",
                forwarded_ip,
                peer_ip,
            )
            return forwarded_ip

        return peer_ip

    def _release_client_ip(self, client_ip: str) -> None:
        """Decrement per-IP connection accounting for a rejected or closed connection."""
        if client_ip in self._ip_connection_count:
            self._ip_connection_count[client_ip] = max(
                0, self._ip_connection_count[client_ip] - 1
            )
            if self._ip_connection_count[client_ip] == 0:
                del self._ip_connection_count[client_ip]

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
        client_ip = self._get_client_ip(websocket)

        # Geo-blocking check (Article 73-3) — must execute before any other logic
        if GEO_BLOCK_AVAILABLE and check_ip_blocked is not None:
            geo_result = check_ip_blocked(client_ip)
            if geo_result.blocked:
                self.logger.warning(
                    "Geo-blocked WebSocket connection from %s (country: %s, reason: %s)",
                    client_ip,
                    geo_result.country_code,
                    geo_result.reason,
                )
                ERRORS_TOTAL.labels(
                    error_type="geo_blocked", station_id="unknown"
                ).inc()
                await websocket.close(1008, "Access denied")
                return

        # IEC 62443 zone validation — log unexpected charger network ranges
        # In production, chargers should connect from expected network ranges.
        # This is informational logging (not blocking) to build the zone model.
        expected_ranges_str = os.getenv("OCPP_EXPECTED_IP_RANGES", "")
        if expected_ranges_str and client_ip != "unknown":
            import ipaddress as _ipaddress

            try:
                client_addr = _ipaddress.ip_address(client_ip)
                expected = [
                    _ipaddress.ip_network(r.strip(), strict=False)
                    for r in expected_ranges_str.split(",")
                    if r.strip()
                ]
                if expected and not any(client_addr in net for net in expected):
                    self.logger.warning(
                        "IEC 62443 zone alert: charger connection from unexpected "
                        "network %s (expected: %s)",
                        client_ip,
                        expected_ranges_str,
                    )
            except ValueError:
                pass  # Invalid IP or range config — don't block

        # Check connection limits
        if len(self.connections) >= self.config.websocket.max_connections:
            self.logger.warning(f"Connection limit exceeded, rejecting {client_ip}")
            await websocket.close(1008, "Server overloaded")
            ERRORS_TOTAL.labels(error_type="connection_limit_exceeded", station_id="unknown").inc()
            return

        # Per-IP connection limit
        if self._ip_connection_count[client_ip] >= self._max_connections_per_ip:
            self.logger.warning(
                "Per-IP connection limit exceeded for %s (%d/%d)",
                client_ip,
                self._ip_connection_count[client_ip],
                self._max_connections_per_ip,
            )
            await websocket.close(1008, "Too many connections from this IP")
            return

        self._ip_connection_count[client_ip] += 1
        self._connection_client_ips[connection_id] = client_ip

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

            # Security: VDV 463 connections must be authenticated (parity with OCPP)
            if self.security_manager and self.security_manager.config.require_station_auth:
                auth_data: Dict[str, Any] = {}
                request = getattr(websocket, "request", None)
                if request is not None:
                    headers = getattr(request, "headers", {})
                    if hasattr(headers, "get"):
                        auth_header = headers.get("Authorization", "")
                        if auth_header.startswith("Bearer "):
                            auth_data["bearer_token"] = auth_header[7:]
                        api_key = headers.get("X-API-Key", "")
                        if api_key:
                            auth_data["api_key"] = api_key

                auth_ok, auth_error = await self.security_manager.authenticate_station(
                    presystem_id, auth_data
                )
                if not auth_ok:
                    self.logger.warning(
                        "VDV 463 authentication failed for presystem %s from %s: %s",
                        presystem_id,
                        client_ip,
                        auth_error,
                    )
                    await websocket.close(1008, "Authentication failed")
                    self._release_client_ip(client_ip)
                    self._connection_client_ips.pop(connection_id, None)
                    return

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
            finally:
                # Decrement per-IP counter
                self._release_client_ip(client_ip)
                self._connection_client_ips.pop(connection_id, None)
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
            self._release_client_ip(client_ip)
            self._connection_client_ips.pop(connection_id, None)
            return

        # Extract station ID from path
        if protocol == "ocpp" and len(path_parts) > 1:
            station_id = path_parts[1]
        elif path.strip("/"):
            station_id = path.strip("/")
        else:
            # Use full UUID to avoid collisions
            station_id = f"station_{connection_id}"

        # Authenticate station (Article 73-3 / NIS2 compliance)
        # Uses the SecurityManager's fallback chain: cert → JWT → API key → basic auth
        if self.security_manager and self.security_manager.config.require_station_auth:
            # Extract auth data from WebSocket request headers
            auth_data: Dict[str, Any] = {}
            request = getattr(websocket, "request", None)
            if request is not None:
                headers = getattr(request, "headers", {})
                if hasattr(headers, "get"):
                    auth_header = headers.get("Authorization", "")
                    if auth_header.startswith("Bearer "):
                        auth_data["bearer_token"] = auth_header[7:]
                    elif auth_header.startswith("Basic "):
                        encoded_credentials = auth_header[6:].strip()
                        try:
                            decoded = base64.b64decode(
                                encoded_credentials.encode("ascii"),
                                validate=True,
                            ).decode("utf-8")
                            username, separator, password = decoded.partition(":")
                            if separator:
                                # OCPP 1.6 basic auth payload uses username:password.
                                auth_data["username"] = username
                                auth_data["password"] = password
                                if not secrets.compare_digest(username, station_id):
                                    self.logger.warning(
                                        "Basic auth username mismatch from %s: station=%s username=%s",
                                        client_ip,
                                        station_id,
                                        username,
                                    )
                                    auth_data.pop("username", None)
                                    auth_data.pop("password", None)
                            else:
                                self.logger.warning(
                                    "Malformed basic auth payload from %s for station %s",
                                    client_ip,
                                    station_id,
                                )
                        except (binascii.Error, UnicodeDecodeError) as exc:
                            self.logger.warning(
                                "Failed to decode basic auth payload from %s for station %s: %s",
                                client_ip,
                                station_id,
                                exc,
                            )
                    api_key = headers.get("X-API-Key", "")
                    if api_key:
                        auth_data["api_key"] = api_key
                    # Password from OCPP Basic Auth (charge_point_id as username)
                    password = headers.get("X-OCPP-Password", "")
                    if password and "password" not in auth_data:
                        # Backward compatible override path for simulator tooling.
                        auth_data["password"] = password

            auth_ok, auth_error = await self.security_manager.authenticate_station(
                station_id, auth_data
            )
            if not auth_ok:
                self.logger.warning(
                    "Authentication failed for station %s from %s: %s",
                    station_id, client_ip, auth_error,
                )
                ERRORS_TOTAL.labels(
                    error_type="auth_failed", station_id=station_id
                ).inc()
                await websocket.close(1008, "Authentication failed")
                self._release_client_ip(client_ip)
                self._connection_client_ips.pop(connection_id, None)
                return

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
        CONNECTED_CHARGERS_COUNT.set(len(self.station_connections))

        # Route to the correct OCPP library based on the negotiated subprotocol.
        # OCPP 1.6 chargers must use the v16 library; passing their messages
        # through EnhancedOCPPChargePoint (v201) causes _validate_payload to
        # dump the entire 2.0.1 JSON schema on every message, flooding logs.
        if websocket.subprotocol == "ocpp1.6" and OCPP16_AVAILABLE:
            charge_point = OCPP16Session(
                station_id=station_id,
                websocket=websocket,
                timescale_client=self.timescale_client,
                message_handler=self.message_handler,
            )
        else:
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
        client_ip = self._connection_client_ips.pop(connection_id, None)

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
            CONNECTED_CHARGERS_COUNT.set(len(self.station_connections))
            if self.connection_manager:
                await self.connection_manager.unregister_connection(station_id)
            # Persist that the charger is gone so reads (alerts, state) and
            # the boot-replay path can distinguish a stale-but-open session
            # from a live one. Both calls are best-effort; the connection is
            # already torn down.
            if self.timescale_client is not None:
                try:
                    await self.timescale_client.mark_connectors_unavailable(station_id)
                except Exception as exc:
                    self.logger.warning(
                        "mark_connectors_unavailable failed for station=%s: %s",
                        station_id,
                        exc,
                    )
                try:
                    await self.timescale_client.mark_sessions_seen(station_id)
                except Exception as exc:
                    self.logger.warning(
                        "mark_sessions_seen failed for station=%s: %s",
                        station_id,
                        exc,
                    )

        # Clean rate limit data - handled by RateLimiter class cleanup
        # self.rate_limits.pop(connection_id, None)  # Removed - using RateLimiter class

        # Decrement per-IP counter
        client_ip = client_ip or self._get_peer_ip(websocket)
        self._release_client_ip(client_ip)

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
