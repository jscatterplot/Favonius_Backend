"""Unit tests for monitoring and observability."""

import asyncio
import os

# Import monitoring
import sys
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from websocket_handler.config import MonitoringConfig
from websocket_handler.monitoring import (
    HealthChecker,
    MetricsCollector,
    PerformanceTimer,
    async_timer,
    get_logger,
    setup_health_checks,
    setup_logging,
    setup_monitoring,
    timer,
)


class TestMonitoringSetup:
    """Test monitoring setup functionality."""

    @patch("websocket_handler.monitoring.start_http_server")
    def test_setup_monitoring_enabled(self, mock_start_server):
        """Test monitoring setup when enabled."""
        config = MonitoringConfig(enable_telemetry=True, metrics_port=8080, log_level="INFO")

        setup_monitoring(config)

        mock_start_server.assert_called_once_with(8080)

    @patch("websocket_handler.monitoring.start_http_server")
    def test_setup_monitoring_disabled(self, mock_start_server):
        """Test monitoring setup when disabled."""
        config = MonitoringConfig(enable_telemetry=False, metrics_port=8080, log_level="INFO")

        setup_monitoring(config)

        mock_start_server.assert_not_called()

    @patch("websocket_handler.monitoring.structlog.configure")
    def test_setup_logging_info_level(self, mock_configure):
        """Test logging setup with INFO level."""
        config = MonitoringConfig(log_level="INFO")

        setup_logging(config)

        mock_configure.assert_called_once()

    @patch("websocket_handler.monitoring.structlog.configure")
    def test_setup_logging_debug_level(self, mock_configure):
        """Test logging setup with DEBUG level."""
        config = MonitoringConfig(log_level="DEBUG")

        setup_logging(config)

        mock_configure.assert_called_once()

    def test_get_logger(self):
        """Test getting logger instance."""
        logger = get_logger("test_module")

        assert logger is not None
        assert hasattr(logger, "info")
        assert hasattr(logger, "error")
        assert hasattr(logger, "warning")
        assert hasattr(logger, "debug")


class TestMetricsCollector:
    """Test MetricsCollector functionality."""

    @pytest.fixture
    def metrics_collector(self):
        """Create MetricsCollector instance."""
        return MetricsCollector()

    def test_metrics_collector_initialization(self, metrics_collector):
        """Test metrics collector initialization."""
        assert metrics_collector._connection_count == 0
        assert metrics_collector._message_counts == {}
        assert metrics_collector._error_counts == {}

    @patch("websocket_handler.monitoring.WEBSOCKET_CONNECTIONS")
    def test_record_connection_connect(self, mock_gauge, metrics_collector):
        """Test recording connection event."""
        metrics_collector.record_connection(connected=True)

        assert metrics_collector._connection_count == 1
        mock_gauge.inc.assert_called_once()

    @patch("websocket_handler.monitoring.WEBSOCKET_CONNECTIONS")
    def test_record_connection_disconnect(self, mock_gauge, metrics_collector):
        """Test recording disconnection event."""
        metrics_collector._connection_count = 5
        metrics_collector.record_connection(connected=False)

        assert metrics_collector._connection_count == 4
        mock_gauge.dec.assert_called_once()

    @patch("websocket_handler.monitoring.MESSAGES_RECEIVED_TOTAL")
    def test_record_message_received(self, mock_counter, metrics_collector):
        """Test recording received message."""
        metrics_collector.record_message_received("STATION_001", "BootNotification")

        mock_counter.labels.assert_called_once_with(
            station_id="STATION_001", message_type="BootNotification"
        )
        mock_counter.labels.return_value.inc.assert_called_once()

    @patch("websocket_handler.monitoring.MESSAGES_SENT_TOTAL")
    def test_record_message_sent(self, mock_counter, metrics_collector):
        """Test recording sent message."""
        metrics_collector.record_message_sent("STATION_001", "BootNotificationResponse")

        mock_counter.labels.assert_called_once_with(
            station_id="STATION_001", message_type="BootNotificationResponse"
        )
        mock_counter.labels.return_value.inc.assert_called_once()

    def test_record_error(self, metrics_collector):
        """Test recording error event."""
        metrics_collector.record_error("ConnectionError", "STATION_001")

        assert "ConnectionError:STATION_001" in metrics_collector._error_counts
        assert metrics_collector._error_counts["ConnectionError:STATION_001"] == 1

    @patch("websocket_handler.monitoring.REDIS_OPERATIONS_TOTAL")
    @patch("websocket_handler.monitoring.REDIS_OPERATION_DURATION")
    def test_record_redis_operation_success(self, mock_duration, mock_total, metrics_collector):
        """Test recording successful Redis operation."""
        metrics_collector.record_redis_operation("GET", 0.1, success=True)

        mock_total.labels.assert_called_once_with(operation="GET", status="success")
        mock_total.labels.return_value.inc.assert_called_once()
        mock_duration.labels.assert_called_once_with(operation="GET")
        mock_duration.labels.return_value.observe.assert_called_once_with(0.1)

    @patch("websocket_handler.monitoring.REDIS_OPERATIONS_TOTAL")
    @patch("websocket_handler.monitoring.REDIS_OPERATION_DURATION")
    def test_record_redis_operation_error(self, mock_duration, mock_total, metrics_collector):
        """Test recording failed Redis operation."""
        metrics_collector.record_redis_operation("SET", 0.2, success=False)

        mock_total.labels.assert_called_once_with(operation="SET", status="error")
        mock_total.labels.return_value.inc.assert_called_once()
        mock_duration.labels.assert_called_once_with(operation="SET")
        mock_duration.labels.return_value.observe.assert_called_once_with(0.2)

    def test_get_connection_count(self, metrics_collector):
        """Test getting connection count."""
        metrics_collector._connection_count = 5
        assert metrics_collector.get_connection_count() == 5

    def test_get_summary_stats(self, metrics_collector):
        """Test getting summary statistics."""
        metrics_collector._connection_count = 3
        metrics_collector._message_counts = {"key1": 1, "key2": 2}
        metrics_collector._error_counts = {"error1": 1}

        stats = metrics_collector.get_summary_stats()

        assert stats["connections"] == 3
        assert stats["messages"] == 2
        assert stats["errors"] == 1
        assert "timestamp" in stats
        assert isinstance(stats["timestamp"], float)


class TestPerformanceTimer:
    """Test PerformanceTimer functionality."""

    def test_performance_timer_context_manager(self):
        """Test PerformanceTimer as context manager."""
        callback_called = False
        callback_args = None

        def callback(operation, duration, success):
            nonlocal callback_called, callback_args
            callback_called = True
            callback_args = (operation, duration, success)

        with PerformanceTimer("test_operation", callback):
            time.sleep(0.01)  # Small delay

        assert callback_called is True
        assert callback_args[0] == "test_operation"
        assert callback_args[1] > 0
        assert callback_args[2] is True

    def test_performance_timer_with_exception(self):
        """Test PerformanceTimer with exception."""
        callback_called = False
        callback_args = None

        def callback(operation, duration, success):
            nonlocal callback_called, callback_args
            callback_called = True
            callback_args = (operation, duration, success)

        try:
            with PerformanceTimer("test_operation", callback):
                raise Exception("Test error")
        except Exception:
            pass

        assert callback_called is True
        assert callback_args[0] == "test_operation"
        assert callback_args[1] > 0
        assert callback_args[2] is False

    def test_performance_timer_no_callback(self):
        """Test PerformanceTimer without callback."""
        with PerformanceTimer("test_operation") as timer:
            time.sleep(0.01)

        assert timer.duration > 0

    def test_timer_decorator(self):
        """Test timer decorator."""

        @timer("decorated_function")
        def test_function():
            time.sleep(0.01)
            return "success"

        result = test_function()
        assert result == "success"

    @pytest.mark.asyncio
    async def test_async_timer_context_manager(self):
        """Test async_timer context manager."""
        async with async_timer("async_operation"):
            await asyncio.sleep(0.01)

        # Should not raise exception


class TestHealthChecker:
    """Test HealthChecker functionality."""

    @pytest.fixture
    def health_checker(self):
        """Create HealthChecker instance."""
        return HealthChecker()

    def test_health_checker_initialization(self, health_checker):
        """Test health checker initialization."""
        assert health_checker.checks == {}

    def test_register_check(self, health_checker):
        """Test registering health check."""

        def test_check():
            return {"status": "healthy"}

        health_checker.register_check("test_check", test_check, critical=True)

        assert "test_check" in health_checker.checks
        assert health_checker.checks["test_check"]["critical"] is True
        assert health_checker.checks["test_check"]["func"] == test_check

    @pytest.mark.asyncio
    async def test_run_checks_sync_function(self, health_checker):
        """Test running health checks with sync function."""

        def test_check():
            return {"status": "healthy", "details": "All good"}

        health_checker.register_check("test_check", test_check, critical=True)

        results = await health_checker.run_checks()

        assert results["status"] == "healthy"
        assert "test_check" in results["checks"]
        assert results["checks"]["test_check"]["status"] == "healthy"
        assert "duration" in results["checks"]["test_check"]

    @pytest.mark.asyncio
    async def test_run_checks_async_function(self, health_checker):
        """Test running health checks with async function."""

        async def test_check():
            await asyncio.sleep(0.01)
            return {"status": "healthy", "details": "All good"}

        health_checker.register_check("test_check", test_check, critical=True)

        results = await health_checker.run_checks()

        assert results["status"] == "healthy"
        assert "test_check" in results["checks"]
        assert results["checks"]["test_check"]["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_run_checks_unhealthy(self, health_checker):
        """Test running health checks with unhealthy result."""

        def test_check():
            return {"status": "unhealthy", "details": "Something wrong"}

        health_checker.register_check("test_check", test_check, critical=True)

        results = await health_checker.run_checks()

        assert results["status"] == "unhealthy"
        assert results["checks"]["test_check"]["status"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_run_checks_non_critical_failure(self, health_checker):
        """Test running health checks with non-critical failure."""

        def test_check():
            return {"status": "unhealthy", "details": "Something wrong"}

        health_checker.register_check("test_check", test_check, critical=False)

        results = await health_checker.run_checks()

        assert results["status"] == "healthy"  # Non-critical failure doesn't affect overall status
        assert results["checks"]["test_check"]["status"] == "unhealthy"

    @pytest.mark.asyncio
    async def test_run_checks_exception(self, health_checker):
        """Test running health checks with exception."""

        def test_check():
            raise Exception("Check failed")

        health_checker.register_check("test_check", test_check, critical=True)

        results = await health_checker.run_checks()

        assert results["status"] == "unhealthy"
        assert results["checks"]["test_check"]["status"] == "error"
        assert "error" in results["checks"]["test_check"]

    def test_get_last_results(self, health_checker):
        """Test getting last health check results."""

        def test_check():
            return {"status": "healthy"}

        health_checker.register_check("test_check", test_check, critical=True)

        # No results yet
        results = health_checker.get_last_results()
        assert results == {}

        # Run checks to populate results
        asyncio.run(health_checker.run_checks())

        results = health_checker.get_last_results()
        assert "test_check" in results
        assert results["test_check"]["status"] == "healthy"


class TestSetupHealthChecks:
    """Test setup_health_checks functionality."""

    @patch("websocket_handler.monitoring.health_checker")
    def test_setup_health_checks_with_connection_manager(self, mock_health_checker):
        """Test setting up health checks with connection manager."""
        mock_connection_manager = Mock()
        mock_connection_manager.get_health_status = Mock(return_value={"status": "healthy"})

        setup_health_checks(connection_manager=mock_connection_manager)

        # Should register both connections check and system check
        assert mock_health_checker.register_check.call_count == 2

        # Check that connections check was registered
        calls = mock_health_checker.register_check.call_args_list
        connections_call = next((call for call in calls if call[0][0] == "connections"), None)
        assert connections_call is not None
        assert connections_call[0][1] == mock_connection_manager.get_health_status
        assert connections_call[1]["critical"] is True

    @patch("websocket_handler.monitoring.health_checker")
    def test_setup_health_checks_with_timescale_client(self, mock_health_checker):
        """Test setting up health checks with timescale client."""
        mock_timescale_client = Mock()
        mock_timescale_client.health_check = AsyncMock(return_value={"status": "healthy"})

        setup_health_checks(timescale_client=mock_timescale_client)

        # Should register both timescale check and system check
        assert mock_health_checker.register_check.call_count == 2

        # Check that timescale check was registered
        calls = mock_health_checker.register_check.call_args_list
        timescale_call = next((call for call in calls if call[0][0] == "timescale"), None)
        assert timescale_call is not None
        assert timescale_call[0][1] == mock_timescale_client.health_check
        assert timescale_call[1]["critical"] is True

    @patch("websocket_handler.monitoring.health_checker")
    def test_setup_health_checks_system_check(self, mock_health_checker):
        """Test setting up health checks with system check."""
        # Mock psutil inside the function
        with (
            patch("psutil.cpu_percent", return_value=50),
            patch("psutil.virtual_memory") as mock_memory,
            patch("psutil.disk_usage") as mock_disk,
        ):

            # Setup mock memory and disk objects
            mock_memory.return_value.percent = 60
            mock_disk.return_value.percent = 40

            setup_health_checks()

            # Should register only system check
            mock_health_checker.register_check.assert_called_once()
            call_args = mock_health_checker.register_check.call_args
            assert call_args[0][0] == "system"
            assert call_args[1]["critical"] is False

    @patch("websocket_handler.monitoring.health_checker")
    def test_setup_health_checks_with_both(self, mock_health_checker):
        """Test setting up health checks with both connection manager and timescale client."""
        mock_connection_manager = Mock()
        mock_connection_manager.get_health_status = Mock(return_value={"status": "healthy"})

        mock_timescale_client = Mock()
        mock_timescale_client.health_check = AsyncMock(return_value={"status": "healthy"})

        setup_health_checks(
            connection_manager=mock_connection_manager, timescale_client=mock_timescale_client
        )

        # Should register both checks
        assert mock_health_checker.register_check.call_count >= 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
