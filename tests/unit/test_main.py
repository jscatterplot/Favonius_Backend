"""Unit tests for main application module."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.websocket_handler.config import Config
from src.websocket_handler.main import Application, main, run_application


class TestApplication:
    """Test the Application class."""

    @pytest.fixture
    def mock_config(self):
        """Mock configuration."""
        config = Mock(spec=Config)
        config.environment = "test"
        config.debug = False
        config.strict_startup_validation = True
        config.monitoring = Mock()
        config.monitoring.health_check_port = 8081
        config.monitoring.metrics_port = 9090
        config.monitoring.api_port = 8082
        config.monitoring.enable_telemetry = True
        config.websocket = Mock()
        config.websocket.port = 9000
        config.supabase = Mock()
        config.timescale = Mock()
        config.optimization = Mock()
        config.optimization.enabled = True
        config.price_feeder = Mock()
        config.price_feeder.enabled = True
        config.main_api = Mock()
        config.notifications = Mock()
        config.notifications.enabled = False
        return config

    @pytest.mark.timeout(10)
    def test_application_initialization(self, mock_config):
        """Test application initialization."""
        app = Application(mock_config)

        assert app.config == mock_config
        assert app.running is False
        assert app.start_time > 0
        assert app.websocket_server is None
        assert app.health_server is None
        assert app.supabase_client is None
        assert app.auth_manager is None
        assert app.api_server is None
        assert app.timescale_client is None
        assert app.analytics_service is None
        assert app.price_feeder is None
        assert app.optimization_engine is None
        assert app.connection_monitor is None

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_initialize_supabase_components(self, mock_config):
        """Test Supabase components initialization."""
        app = Application(mock_config)

        # Mock Supabase components
        mock_supabase_client = AsyncMock()
        mock_auth_manager = Mock()
        with (
            patch("src.websocket_handler.main.SupabaseClient", return_value=mock_supabase_client),
            patch("src.websocket_handler.main.AuthManager", return_value=mock_auth_manager),
        ):

            await app._initialize_supabase_components()

            assert app.supabase_client == mock_supabase_client
            assert app.auth_manager == mock_auth_manager
            mock_supabase_client.connect.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_initialize_timescale_components(self, mock_config):
        """Test TimescaleDB components initialization."""
        app = Application(mock_config)

        # Mock TimescaleDB components
        mock_timescale_client = AsyncMock()
        mock_analytics_service = AsyncMock()
        mock_optimization_engine = AsyncMock()
        mock_price_feeder = AsyncMock()

        with (
            patch("src.websocket_handler.main.TimescaleClient", return_value=mock_timescale_client),
            patch(
                "src.websocket_handler.main.AnalyticsService", return_value=mock_analytics_service
            ),
            patch(
                "src.websocket_handler.main.OptimizationEngine",
                return_value=mock_optimization_engine,
            ),
            patch("src.websocket_handler.main.PriceFeederService", return_value=mock_price_feeder),
            patch("src.websocket_handler.main.create_timescale_schema_from_config", AsyncMock()),
        ):

            await app._initialize_timescale_components()

            assert app.timescale_client == mock_timescale_client
            assert app.analytics_service == mock_analytics_service
            assert app.optimization_engine == mock_optimization_engine
            assert app.price_feeder == mock_price_feeder
            mock_timescale_client.connect.assert_called_once()
            mock_analytics_service.initialize.assert_called_once()
            mock_optimization_engine.start.assert_called_once()
            mock_price_feeder.start.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_initialize_with_retry_retries_when_non_strict(self, mock_config):
        """Test retry behavior for component initialization when strict mode is disabled."""
        app = Application(mock_config)
        app.config.strict_startup_validation = False

        attempts = {"count": 0}

        async def flaky_initializer():
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise Exception("temporary error")

        with patch("src.websocket_handler.main.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await app._initialize_with_retry("Supabase", flaky_initializer)

        assert attempts["count"] == 3
        assert mock_sleep.await_count == 2

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_initialize_with_retry_fails_fast_when_strict(self, mock_config):
        """Test fail-fast behavior for component initialization in strict mode."""
        app = Application(mock_config)
        app.config.strict_startup_validation = True

        async def broken_initializer():
            raise Exception("permanent error")

        with pytest.raises(Exception, match="permanent error"):
            await app._initialize_with_retry("TimescaleDB", broken_initializer)

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_start_application_success(self, mock_config):
        """Test successful application startup."""
        app = Application(mock_config)

        # Mock all components
        mock_websocket_server = AsyncMock()
        mock_health_server = AsyncMock()
        mock_api_server = AsyncMock()
        mock_connection_monitor = AsyncMock()

        with (
            patch.object(app, "_initialize_supabase_components", AsyncMock()),
            patch.object(app, "_initialize_timescale_components", AsyncMock()),
            patch("src.websocket_handler.main.setup_monitoring"),
            patch(
                "src.websocket_handler.main.OCPPWebSocketServer", return_value=mock_websocket_server
            ),
            patch("src.websocket_handler.main.HealthCheckServer", return_value=mock_health_server),
            patch("src.websocket_handler.main.APIServer", return_value=mock_api_server),
            patch(
                "src.websocket_handler.main.ConnectionMonitor", return_value=mock_connection_monitor
            ),
        ):

            # Mock the websocket server start to complete immediately
            mock_websocket_server.start = AsyncMock(
                side_effect=asyncio.CancelledError("Test completion")
            )

            with pytest.raises(asyncio.CancelledError):
                await app.start()

            assert app.running is True
            assert app.websocket_server == mock_websocket_server
            assert app.health_server == mock_health_server
            assert app.api_server == mock_api_server
            assert app.connection_monitor == mock_connection_monitor

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_stop_application(self, mock_config):
        """Test application shutdown."""
        app = Application(mock_config)

        # Mock components
        mock_websocket_server = AsyncMock()
        mock_health_server = AsyncMock()
        mock_api_server = AsyncMock()
        mock_connection_monitor = AsyncMock()
        mock_timescale_client = AsyncMock()
        mock_supabase_client = AsyncMock()
        reconciler_task = asyncio.create_task(asyncio.sleep(3600))

        app.websocket_server = mock_websocket_server
        app.health_server = mock_health_server
        app.api_server = mock_api_server
        app.connection_monitor = mock_connection_monitor
        app.timescale_client = mock_timescale_client
        app.supabase_client = mock_supabase_client
        app._active_tx_reconciler_task = reconciler_task

        await app.stop()

        assert app.running is False
        mock_connection_monitor.stop_monitoring.assert_called_once()
        mock_api_server.stop.assert_called_once()
        mock_websocket_server.stop.assert_called_once()
        mock_health_server.stop.assert_called_once()
        mock_timescale_client.disconnect.assert_called_once()
        mock_supabase_client.disconnect.assert_called_once()
        assert reconciler_task.cancelled()

    @pytest.mark.timeout(10)
    def test_setup_signal_handlers(self, mock_config):
        """Test signal handler setup."""
        app = Application(mock_config)

        with patch("signal.signal") as mock_signal:
            app.setup_signal_handlers()

            # Should register SIGINT, SIGTERM, and optionally SIGHUP
            assert mock_signal.call_count >= 2

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_start_with_websocket_server_error(self, mock_config):
        """Test application startup with WebSocket server errors."""
        app = Application(mock_config)

        # Mock components
        mock_websocket_server = AsyncMock()
        mock_health_server = AsyncMock()
        mock_api_server = AsyncMock()
        mock_connection_monitor = AsyncMock()

        with (
            patch.object(app, "_initialize_supabase_components", AsyncMock()),
            patch.object(app, "_initialize_timescale_components", AsyncMock()),
            patch("src.websocket_handler.main.setup_monitoring"),
            patch(
                "src.websocket_handler.main.OCPPWebSocketServer", return_value=mock_websocket_server
            ),
            patch("src.websocket_handler.main.HealthCheckServer", return_value=mock_health_server),
            patch("src.websocket_handler.main.APIServer", return_value=mock_api_server),
            patch(
                "src.websocket_handler.main.ConnectionMonitor", return_value=mock_connection_monitor
            ),
        ):

            # Mock websocket server to raise exception then complete
            call_count = 0

            async def mock_start():
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise Exception("Test error")
                else:
                    raise asyncio.CancelledError("Test completion")

            mock_websocket_server.start = mock_start

            with pytest.raises(asyncio.CancelledError):
                await app.start()

            assert app.running is True


    @pytest.mark.timeout(10)
    def test_check_port_conflicts_no_conflict(self, mock_config):
        """Test port conflict detection with no conflicts."""
        app = Application(mock_config)
        # All ports are different — should not raise
        app._check_port_conflicts()

    @pytest.mark.timeout(10)
    def test_check_port_conflicts_ws_vs_api(self, mock_config):
        """Test port conflict between WebSocket and API server."""
        app = Application(mock_config)
        mock_config.websocket.port = 8080
        mock_config.monitoring.api_port = 8080

        with pytest.raises(RuntimeError, match="Port conflict"):
            app._check_port_conflicts()

    @pytest.mark.timeout(10)
    def test_check_port_conflicts_metrics_vs_api(self, mock_config):
        """Test port conflict between Prometheus metrics and API server."""
        app = Application(mock_config)
        mock_config.monitoring.metrics_port = 8082
        mock_config.monitoring.api_port = 8082

        with pytest.raises(RuntimeError, match="Port conflict"):
            app._check_port_conflicts()

    @pytest.mark.timeout(10)
    def test_check_port_conflicts_ws_vs_metrics(self, mock_config):
        """Test port conflict between WebSocket and Prometheus metrics."""
        app = Application(mock_config)
        mock_config.websocket.port = 9090
        mock_config.monitoring.metrics_port = 9090

        with pytest.raises(RuntimeError, match="Port conflict"):
            app._check_port_conflicts()

    @pytest.mark.timeout(10)
    def test_check_port_conflicts_skips_metrics_when_disabled(self, mock_config):
        """Test that metrics port is excluded from conflict check when telemetry is disabled."""
        app = Application(mock_config)
        mock_config.monitoring.enable_telemetry = False
        # Set metrics port same as API — no conflict because metrics is disabled
        mock_config.monitoring.metrics_port = 8082
        mock_config.monitoring.api_port = 8082

        app._check_port_conflicts()  # Should not raise


class TestRunApplication:
    """Test the run_application function."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_run_application_success(self):
        """Test successful application run."""
        mock_config = Mock(spec=Config)
        mock_app = Mock()
        mock_app.start = AsyncMock(side_effect=asyncio.CancelledError("Test completion"))
        mock_app.stop = AsyncMock()

        with (
            patch("src.websocket_handler.main.Application", return_value=mock_app),
            patch("src.websocket_handler.main.uvloop.install"),
            patch.object(mock_app, "setup_signal_handlers"),
        ):

            with pytest.raises(asyncio.CancelledError):
                await run_application(mock_config)

            mock_app.start.assert_called_once()
            mock_app.stop.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_run_application_keyboard_interrupt(self):
        """Test application run with keyboard interrupt."""
        mock_config = Mock(spec=Config)
        mock_app = Mock()
        mock_app.start = AsyncMock(side_effect=KeyboardInterrupt())
        mock_app.stop = AsyncMock()

        with (
            patch("src.websocket_handler.main.Application", return_value=mock_app),
            patch("src.websocket_handler.main.uvloop.install"),
            patch.object(mock_app, "setup_signal_handlers"),
        ):

            await run_application(mock_config)

            mock_app.start.assert_called_once()
            mock_app.stop.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_run_application_exception(self):
        """Test application run with exception."""
        mock_config = Mock(spec=Config)
        mock_app = Mock()
        mock_app.start = AsyncMock(side_effect=Exception("Test error"))
        mock_app.stop = AsyncMock()

        with (
            patch("src.websocket_handler.main.Application", return_value=mock_app),
            patch("src.websocket_handler.main.uvloop.install"),
            patch.object(mock_app, "setup_signal_handlers"),
        ):

            with pytest.raises(Exception, match="Test error"):
                await run_application(mock_config)

            mock_app.start.assert_called_once()
            mock_app.stop.assert_called_once()


class TestMainFunction:
    """Test the main function."""

    @pytest.mark.timeout(10)
    def test_main_function_success(self):
        """Test successful main function execution."""
        mock_logger = Mock()
        with (
            patch("src.websocket_handler.main.Config.from_env") as mock_from_env,
            patch("src.websocket_handler.main.asyncio.run") as mock_run,
            patch("src.websocket_handler.main.setup_logging"),
            patch("src.websocket_handler.main.get_logger", return_value=mock_logger),
        ):

            mock_config = Mock()
            mock_from_env.return_value = mock_config

            main()

            mock_from_env.assert_called_once()
            mock_run.assert_called_once()
            mock_logger.info.assert_called()

    @pytest.mark.timeout(10)
    def test_main_function_keyboard_interrupt(self):
        """Test main function with keyboard interrupt."""
        mock_logger = Mock()
        with (
            patch("src.websocket_handler.main.Config.from_env") as mock_from_env,
            patch("src.websocket_handler.main.asyncio.run", side_effect=KeyboardInterrupt()),
            patch("src.websocket_handler.main.setup_logging"),
            patch("src.websocket_handler.main.get_logger", return_value=mock_logger),
            patch("sys.exit") as mock_exit,
        ):

            mock_config = Mock()
            mock_from_env.return_value = mock_config

            main()

            mock_exit.assert_called_once_with(0)

    @pytest.mark.timeout(10)
    def test_main_function_exception(self):
        """Test main function with exception."""
        mock_logger = Mock()
        with (
            patch("src.websocket_handler.main.Config.from_env") as mock_from_env,
            patch("src.websocket_handler.main.asyncio.run", side_effect=Exception("Test error")),
            patch("src.websocket_handler.main.setup_logging"),
            patch("src.websocket_handler.main.get_logger", return_value=mock_logger),
            patch("sys.exit") as mock_exit,
        ):

            mock_config = Mock()
            mock_from_env.return_value = mock_config

            main()

            mock_exit.assert_called_once_with(1)
            mock_logger.error.assert_called()
