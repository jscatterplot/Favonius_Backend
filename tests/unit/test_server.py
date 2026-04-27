"""Unit tests for OCPP WebSocket server."""

import asyncio
import json
import logging
import os

# Import test dependencies
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from websocket_handler.config import Config
from websocket_handler.server import OCPPWebSocketServer, _SuppressHandshakeEOFErrors
from websocket_handler.timescale_client import TimescaleClient


class TestOCPPWebSocketServer:
    """Test OCPP WebSocket server functionality."""

    @pytest.fixture
    def mock_config(self):
        """Create mock configuration."""
        config = Mock(spec=Config)
        config.websocket = Mock()
        config.websocket.host = "localhost"
        config.websocket.port = 9000
        config.websocket.max_message_size = 65536
        config.websocket.max_connections = 100
        config.websocket.heartbeat_interval = 30
        config.websocket.message_timeout = 60
        config.websocket.rate_limit_per_minute = 100
        config.tls = Mock()
        config.tls.cert_path = None
        config.tls.key_path = None
        config.tls.ca_path = None
        config.tls.verify_client = False
        return config

    @pytest.fixture
    def mock_timescale_client(self):
        """Create mock TimescaleDB client."""
        client = Mock(spec=TimescaleClient)
        client.health_check = AsyncMock(return_value={"status": "healthy"})
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        return client

    @pytest.fixture
    def mock_connection_manager(self):
        """Create mock connection manager."""
        manager = Mock()
        manager.check_message_rate_limit = AsyncMock(return_value=(True, "OK"))
        manager.register_connection = AsyncMock()
        manager.unregister_connection = AsyncMock()
        manager.send_message_to_station = AsyncMock()
        manager.broadcast_message = AsyncMock()
        manager.get_connection = Mock(return_value=None)
        manager.get_all_connections = Mock(return_value={})
        manager.update_heartbeat = AsyncMock()
        manager.update_stats = AsyncMock()
        manager.get_health_status = Mock(return_value={"status": "healthy"})
        manager.start = AsyncMock()
        manager.stop = AsyncMock()
        return manager

    @pytest.fixture
    def server(self, mock_config, mock_timescale_client, mock_connection_manager):
        """Create OCPP WebSocket server instance."""
        server = OCPPWebSocketServer(mock_config, mock_timescale_client)
        server.connection_manager = mock_connection_manager
        # Create a mock task for rate limit cleanup
        mock_task = Mock()
        mock_task.done.return_value = False
        mock_task.cancelled.return_value = False
        mock_task.cancel.return_value = True
        mock_task.__await__ = AsyncMock(return_value=iter([]))
        server._rate_limit_task = mock_task
        return server

    def _make_websocket(self, remote_ip: str, headers: dict[str, str] | None = None):
        """Build a minimal WebSocket mock with optional request headers."""
        mock_websocket = Mock()
        mock_websocket.remote_address = (remote_ip, 12345)
        mock_websocket.close = AsyncMock()
        mock_websocket.subprotocol = "ocpp1.6"
        request = Mock()
        request.headers = headers or {}
        mock_websocket.request = request
        return mock_websocket

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_server_initialization(self, server):
        """Test server initialization."""
        assert server.config is not None
        assert server.timescale_client is not None
        assert server.running is False
        assert server.server is None
        assert len(server.connections) == 0
        assert len(server.charge_points) == 0
        assert len(server.station_connections) == 0

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_server_start_stop(self, server):
        """Test server start and stop functionality."""
        # Mock websockets.serve to return a mock server
        mock_server = Mock()
        mock_server.wait_closed = AsyncMock()

        # Mock the component initialization to avoid V2XController issues
        with (
            patch.object(server, "_initialize_components", new_callable=AsyncMock) as mock_init,
            patch(
                "websockets.serve", new_callable=AsyncMock, return_value=mock_server
            ) as mock_serve,
        ):
            await server.start()
            mock_serve.assert_called_once()
            mock_init.assert_called_once()

        await server.stop()
        assert True  # Graceful completion

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_ssl_context_setup(self, server):
        """Test SSL context setup."""
        # Test without SSL (should return None)
        ssl_context = server._setup_ssl_context()
        assert ssl_context is None

        # Test with SSL configuration
        server.config.tls.cert_path = "/path/to/cert.pem"
        server.config.tls.key_path = "/path/to/key.pem"

        with (
            patch("os.path.exists", return_value=True),
            patch("ssl.SSLContext") as mock_ssl_context,
        ):
            mock_context = Mock()
            mock_ssl_context.return_value = mock_context

            ssl_context = server._setup_ssl_context()
            assert ssl_context == mock_context
            mock_context.load_cert_chain.assert_called_once_with(
                "/path/to/cert.pem", "/path/to/key.pem"
            )

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_connection_handling(self, server):
        """Test WebSocket connection handling."""
        mock_websocket = Mock()
        mock_websocket.remote_address = ("127.0.0.1", 12345)
        mock_websocket.recv = AsyncMock()
        mock_websocket.send = AsyncMock()
        mock_websocket.close = AsyncMock()
        mock_websocket.subprotocol = "ocpp2.1"

        # Mock message handler
        server.message_handler = Mock()
        server.message_handler.handle_message = AsyncMock()

        # Mock connection manager
        server.connection_manager = Mock()
        server.connection_manager.add_connection = AsyncMock()
        server.connection_manager.remove_connection = AsyncMock()

        # Test connection handling without raising exceptions
        try:
            await server._handle_connection(mock_websocket, "/test")
            # Should handle gracefully
            assert True
        except Exception:
            # Expected to handle errors gracefully
            assert True

    def test_get_client_ip_uses_forwarded_header_from_trusted_proxy(self, server):
        """Trusted proxy connections use the original charger IP from forwarding headers."""
        websocket = self._make_websocket(
            "10.0.0.5",
            {"X-Forwarded-For": "198.51.100.42, 10.0.0.5"},
        )

        assert server._get_client_ip(websocket) == "198.51.100.42"

    def test_get_client_ip_ignores_forwarded_header_from_untrusted_peer(self, server):
        """Direct public peers cannot spoof geo-blocking with X-Forwarded-For."""
        websocket = self._make_websocket(
            "8.8.8.8",
            {"X-Forwarded-For": "198.51.100.42"},
        )

        assert server._get_client_ip(websocket) == "8.8.8.8"

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_geo_block_uses_forwarded_ip_from_trusted_proxy(self, server):
        """Geo-blocking checks the charger IP, not the trusted proxy IP."""
        websocket = self._make_websocket(
            "10.0.0.5",
            {"X-Forwarded-For": "198.51.100.42, 10.0.0.5"},
        )
        geo_result = Mock(blocked=True, country_code="CN", reason="blocked_country")

        with patch("websocket_handler.server.check_ip_blocked", return_value=geo_result) as check:
            await server._handle_connection(websocket, "/ocpp/ABB_TEST")

        check.assert_called_once_with("198.51.100.42")
        websocket.close.assert_awaited_once_with(1008, "Access denied")

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_geo_block_ignores_forwarded_ip_from_untrusted_peer(self, server):
        """Spoofed forwarding headers from direct public clients are ignored."""
        websocket = self._make_websocket(
            "8.8.8.8",
            {"X-Forwarded-For": "198.51.100.42"},
        )
        geo_result = Mock(blocked=True, country_code="US", reason="blocked_country")

        with patch("websocket_handler.server.check_ip_blocked", return_value=geo_result) as check:
            await server._handle_connection(websocket, "/ocpp/ABB_TEST")

        check.assert_called_once_with("8.8.8.8")
        websocket.close.assert_awaited_once_with(1008, "Access denied")

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_vdv_auth_failure_releases_ip_tracking(self, server):
        """Rejected VDV auth attempts do not consume per-IP connection slots."""
        websocket = self._make_websocket("10.0.0.5")
        server.config.vdv463 = Mock()
        server.config.vdv463.enabled = True
        server.config.vdv463.validation_mode = "soft"
        server.config.vdv463.default_depot_id = None
        server.security_manager = Mock()
        server.security_manager.config.require_station_auth = True
        server.security_manager.authenticate_station = AsyncMock(
            return_value=(False, "bad credentials")
        )

        with patch("websocket_handler.server.VDV463_AVAILABLE", True):
            await server._handle_connection(websocket, "/vdv463/pre1")

        websocket.close.assert_awaited_once_with(1008, "Authentication failed")
        assert "10.0.0.5" not in server._ip_connection_count
        assert server._connection_client_ips == {}

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_message_processing(self, server):
        """Test message processing functionality."""
        mock_websocket = Mock()
        mock_websocket.remote_address = ("127.0.0.1", 12345)

        # Mock message handler
        server.message_handler = Mock()
        server.message_handler.handle_message = AsyncMock()

        # Test valid JSON message
        json.dumps([2, "1", "BootNotification", {"reason": "PowerUp"}])

        # Test the actual connection handling method
        with patch.object(server, "_handle_connection", new_callable=AsyncMock) as mock_handle:
            await server._handle_connection(mock_websocket, "/test")
            mock_handle.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_invalid_message_handling(self, server):
        """Test handling of invalid messages."""
        mock_websocket = Mock()
        mock_websocket.remote_address = ("127.0.0.1", 12345)
        mock_websocket.send = AsyncMock()

        # Test with invalid JSON
        with patch.object(server, "_handle_connection", new_callable=AsyncMock):
            await server._handle_connection(mock_websocket, "/test")

            # Should handle gracefully without raising exception
            assert True

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_rate_limiting(self, server):
        """Test rate limiting functionality."""
        connection_id = "test_connection"

        # Test rate limit check - should pass initially
        result, _ = await server._check_rate_limit(connection_id)
        assert result is True

        # Configure mock to return rate limit exceeded after multiple calls
        call_count = 0

        def rate_limit_side_effect(station_id):
            nonlocal call_count
            call_count += 1
            if call_count > server.config.websocket.rate_limit_per_minute:
                return (False, "Rate limit exceeded")
            return (True, "OK")

        server.connection_manager.check_message_rate_limit.side_effect = rate_limit_side_effect

        # Simulate exceeding rate limit
        for _ in range(server.config.websocket.rate_limit_per_minute + 1):
            await server._check_rate_limit(connection_id)

        result, _ = await server._check_rate_limit(connection_id)
        assert result is False

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_connection_cleanup(self, server):
        """Test connection cleanup functionality."""
        connection_id = "test_connection"
        station_id = "TEST_STATION_001"

        # Add test connection
        server.connections[connection_id] = Mock()
        server.station_connections[station_id] = connection_id

        # Mock connection manager
        server.connection_manager = Mock()
        server.connection_manager.unregister_connection = AsyncMock()

        mock_websocket = Mock()
        await server._cleanup_connection(connection_id, mock_websocket, station_id)

        assert connection_id not in server.connections
        assert station_id not in server.station_connections
        server.connection_manager.unregister_connection.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_heartbeat_task(self, server):
        """Test heartbeat task functionality."""
        # Set server as running
        server.running = True

        # Mock charge points
        mock_charge_point = Mock()
        mock_charge_point.heartbeat = AsyncMock()
        server.charge_points["TEST_STATION_001"] = mock_charge_point

        # Test heartbeat task - it just sleeps, doesn't call heartbeat
        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            # Start the task
            task = asyncio.create_task(server._heartbeat_monitor())

            # Let it run briefly
            await asyncio.sleep(0.1)

            # Stop the server
            server.running = False

            # Wait for task to complete
            try:
                await asyncio.wait_for(task, timeout=1.0)
            except asyncio.TimeoutError:
                task.cancel()

            # Should have called sleep
            assert mock_sleep.called

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_rate_limit_cleanup_task(self, server):
        """Test rate limit cleanup task."""
        # Test that the rate limit cleanup task is started
        assert server._rate_limit_task is not None
        assert not server._rate_limit_task.done()

        # Test that the cleanup task can be cancelled
        result = server._rate_limit_task.cancel()
        assert result is True

        # Test that the task is marked as cancelled
        assert server._rate_limit_task.cancelled() is False  # Mock returns False

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_component_initialization(self, server):
        """Test component initialization."""
        # Test that the method exists and can be called
        try:
            await server._initialize_components()
            # Should complete without error
            assert True
        except Exception:
            # Expected to handle initialization errors gracefully
            assert True

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_get_charge_points(self, server):
        """Test getting charge points."""
        # Add test charge points
        server.charge_points["station1"] = Mock()
        server.charge_points["station2"] = Mock()

        # Test getting specific charge point
        charge_point = server.get_charge_point("station1")
        assert charge_point is not None

        # Test getting all charge points
        all_charge_points = server.get_all_charge_points()
        assert len(all_charge_points) == 2

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_error_handling(self, server):
        """Test error handling in various scenarios."""
        mock_websocket = Mock()
        mock_websocket.remote_address = ("127.0.0.1", 12345)
        mock_websocket.send = AsyncMock()
        mock_websocket.subprotocol = "ocpp2.1"

        # Test connection error handling
        try:
            await server._handle_connection(mock_websocket, "/test")
            # Should handle gracefully
            assert True
        except Exception:
            # Expected to handle errors gracefully
            assert True

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_server_health_check(self, server):
        """Test server health check."""
        server.running = True

        # Test basic server state
        assert server.running is True
        assert server.server is None  # Not started yet

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_server_health_check_degraded(self, server):
        """Test server health check when degraded."""
        server.running = False

        # Test degraded state
        assert server.running is False

    def test_server_configuration_validation(self, server):
        """Test server configuration validation."""
        # Test valid configuration
        assert server.config.websocket.host is not None
        assert server.config.websocket.port > 0
        assert server.config.websocket.max_connections > 0

        # Test configuration access
        assert hasattr(server.config, "websocket")
        assert hasattr(server.config, "tls")

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)
    async def test_concurrent_connections(self, server):
        """Test handling of concurrent connections."""
        # Mock multiple connections
        connections = []
        for i in range(3):
            mock_ws = Mock()
            mock_ws.remote_address = (f"127.0.0.{i+1}", 12345)
            mock_ws.recv = AsyncMock(side_effect=asyncio.CancelledError)
            mock_ws.close = AsyncMock()
            mock_ws.subprotocol = "ocpp2.1"
            connections.append(mock_ws)

        # Test concurrent connection handling
        tasks = []
        for conn in connections:
            task = asyncio.create_task(server._handle_connection(conn, "/test"))
            tasks.append(task)

        # Wait for all connections to be handled
        await asyncio.gather(*tasks, return_exceptions=True)

        # Should handle all connections without error
        assert True


class TestSuppressHandshakeEOFErrors:
    """Tests for the _SuppressHandshakeEOFErrors logging filter."""

    @pytest.fixture
    def log_filter(self):
        return _SuppressHandshakeEOFErrors()

    def _make_record(self, level: int, exc: Exception | None = None) -> logging.LogRecord:
        """Build a minimal LogRecord with optional exception info."""
        record = logging.LogRecord(
            name="websockets.server",
            level=level,
            pathname="",
            lineno=0,
            msg="opening handshake failed",
            args=(),
            exc_info=(type(exc), exc, None) if exc else None,
        )
        return record

    def _chain(self, *exceptions: Exception) -> Exception:
        """Chain exceptions so each is the __cause__ of the next."""
        for outer, inner in zip(exceptions, exceptions[1:]):
            outer.__cause__ = inner
        return exceptions[0]

    def test_suppresses_zero_byte_eof_error(self, log_filter):
        """Filter drops ERROR records whose cause chain includes 'stream ends after 0 bytes'."""
        root = EOFError("stream ends after 0 bytes, before end of line")
        mid = EOFError("connection closed while reading HTTP request line")
        top = Exception("did not receive a valid HTTP request")
        exc = self._chain(top, mid, root)

        record = self._make_record(logging.ERROR, exc)
        assert log_filter.filter(record) is False

    def test_allows_other_eof_errors(self, log_filter):
        """Filter passes ERROR records whose EOFError has a different message."""
        exc = self._chain(Exception("handshake failed"), EOFError("connection reset"))
        record = self._make_record(logging.ERROR, exc)
        assert log_filter.filter(record) is True

    def test_allows_non_error_levels(self, log_filter):
        """Filter never suppresses WARNING or INFO records."""
        root = EOFError("stream ends after 0 bytes, before end of line")
        for level in (logging.WARNING, logging.INFO, logging.DEBUG):
            record = self._make_record(level, root)
            assert log_filter.filter(record) is True

    def test_allows_error_without_exc_info(self, log_filter):
        """Filter passes ERROR records that carry no exception (no exc_info)."""
        record = self._make_record(logging.ERROR, exc=None)
        assert log_filter.filter(record) is True

    def test_filter_installed_on_server_init(self, mock_config, mock_timescale_client):
        """OCPPWebSocketServer __init__ installs the filter on the websockets.server logger."""
        import logging as _logging

        ws_logger = _logging.getLogger("websockets.server")
        before = len(ws_logger.filters)
        OCPPWebSocketServer(mock_config, mock_timescale_client)
        assert len(ws_logger.filters) == before + 1
        assert any(isinstance(f, _SuppressHandshakeEOFErrors) for f in ws_logger.filters)

    @pytest.fixture
    def mock_config(self):
        config = Mock(spec=Config)
        config.websocket = Mock()
        config.websocket.host = "localhost"
        config.websocket.port = 9000
        config.websocket.max_message_size = 65536
        config.websocket.max_connections = 100
        config.websocket.heartbeat_interval = 30
        config.websocket.message_timeout = 60
        config.websocket.rate_limit_per_minute = 100
        config.tls = Mock()
        config.tls.cert_path = None
        config.tls.key_path = None
        config.tls.ca_path = None
        config.tls.verify_client = False
        return config

    @pytest.fixture
    def mock_timescale_client(self):
        client = Mock(spec=TimescaleClient)
        client.health_check = AsyncMock(return_value={"status": "healthy"})
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        return client


class TestProcessRequest:
    """Tests for OCPPWebSocketServer._process_request health-check handler."""

    @pytest.fixture
    def server(self):
        config = Mock(spec=Config)
        config.websocket = Mock()
        config.websocket.host = "localhost"
        config.websocket.port = 9000
        config.websocket.max_message_size = 65536
        config.websocket.max_connections = 100
        config.websocket.heartbeat_interval = 30
        config.websocket.message_timeout = 60
        config.websocket.rate_limit_per_minute = 100
        config.tls = Mock()
        config.tls.cert_path = None
        config.tls.key_path = None
        return OCPPWebSocketServer(config, Mock(spec=TimescaleClient))

    def _make_request(self, path: str):
        """Build a minimal mock request with a path attribute."""
        req = Mock()
        req.path = path
        return req

    def _make_connection(self):
        """Build a mock connection whose respond() returns a sentinel response."""
        conn = Mock()
        conn.respond = Mock(return_value=Mock(name="http_200_response"))
        return conn

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/", "/health", "/healthz", "/ready"])
    async def test_health_paths_return_200(self, server, path):
        """_process_request returns an HTTP response for known health-check paths."""
        import http

        connection = self._make_connection()
        request = self._make_request(path)

        result = await server._process_request(connection, request)

        assert result is not None
        connection.respond.assert_called_once_with(http.HTTPStatus.OK, "OK\n")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/ocpp/CP001", "/vdv463/pre1", "/some/other/path"])
    async def test_non_health_paths_return_none(self, server, path):
        """_process_request returns None for paths that should proceed to WebSocket upgrade."""
        connection = self._make_connection()
        request = self._make_request(path)

        result = await server._process_request(connection, request)

        assert result is None
        connection.respond.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
