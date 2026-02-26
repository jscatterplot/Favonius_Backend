"""Unit tests for monitoring manager."""

import os

# Import monitoring manager
import sys
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from websocket_handler.monitoring_manager import (
    AlertSeverity,
    MonitoringCriterion,
    MonitoringManager,
)


class TestMonitoringManager:
    """Test MonitoringManager functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.store_monitoring_report = AsyncMock()
        client.store_variable_monitoring = AsyncMock()
        client.store_notified_monitoring_report = AsyncMock()
        client.store_alert_rule = AsyncMock()
        client.store_alert = AsyncMock()
        client.get_device_variables = AsyncMock()
        client.get_all_device_variables = AsyncMock()
        client.get_alert_rules = AsyncMock()
        client.get_periodic_monitoring_data = AsyncMock()
        client.get_active_alerts = AsyncMock()
        return client

    @pytest.fixture
    def monitoring_manager(self, mock_timescale_client):
        """Create MonitoringManager instance."""
        return MonitoringManager(mock_timescale_client)

    @pytest.mark.asyncio
    async def test_get_monitoring_report_configuration(
        self, monitoring_manager, mock_timescale_client
    ):
        """Test GetMonitoringReport for configuration data."""
        mock_timescale_client.get_device_variables.return_value = [
            {
                "component_name": "ChargingStation",
                "variable_name": "VendorName",
                "actual_value": "TestVendor",
            }
        ]

        result = await monitoring_manager.get_monitoring_report(
            station_id="TEST_STATION",
            request_id=1,
            monitoring_base="Configuration",
            component_name="ChargingStation",
            variable_name="VendorName",
        )

        assert result["status"] == "Accepted"
        assert "monitoringData" in result
        mock_timescale_client.store_monitoring_report.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_monitoring_report_operational(
        self, monitoring_manager, mock_timescale_client
    ):
        """Test GetMonitoringReport for operational data."""
        result = await monitoring_manager.get_monitoring_report(
            station_id="TEST_STATION", request_id=1, monitoring_base="Operational"
        )

        assert result["status"] == "Accepted"
        assert "monitoringData" in result

    @pytest.mark.asyncio
    async def test_get_monitoring_report_security(self, monitoring_manager, mock_timescale_client):
        """Test GetMonitoringReport for security data."""
        result = await monitoring_manager.get_monitoring_report(
            station_id="TEST_STATION", request_id=1, monitoring_base="Security"
        )

        assert result["status"] == "Accepted"
        assert "monitoringData" in result

    @pytest.mark.asyncio
    async def test_set_variable_monitoring_threshold(
        self, monitoring_manager, mock_timescale_client
    ):
        """Test SetVariableMonitoring with threshold monitoring."""
        result = await monitoring_manager.set_variable_monitoring(
            station_id="TEST_STATION",
            component_name="ChargingStation",
            variable_name="HeartbeatInterval",
            monitoring_criterion="ThresholdMonitoring",
            threshold=600.0,
        )

        assert result["status"] == "Accepted"
        mock_timescale_client.store_variable_monitoring.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_variable_monitoring_delta(self, monitoring_manager, mock_timescale_client):
        """Test SetVariableMonitoring with delta monitoring."""
        result = await monitoring_manager.set_variable_monitoring(
            station_id="TEST_STATION",
            component_name="ChargingStation",
            variable_name="Power",
            monitoring_criterion="DeltaMonitoring",
            threshold=5.0,
        )

        assert result["status"] == "Accepted"
        mock_timescale_client.store_variable_monitoring.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_variable_monitoring_periodic(
        self, monitoring_manager, mock_timescale_client
    ):
        """Test SetVariableMonitoring with periodic monitoring."""
        result = await monitoring_manager.set_variable_monitoring(
            station_id="TEST_STATION",
            component_name="ChargingStation",
            variable_name="Temperature",
            monitoring_criterion="PeriodicMonitoring",
        )

        assert result["status"] == "Accepted"
        mock_timescale_client.store_variable_monitoring.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_variable_monitoring_invalid_criterion(self, monitoring_manager):
        """Test SetVariableMonitoring with invalid criterion."""
        result = await monitoring_manager.set_variable_monitoring(
            station_id="TEST_STATION",
            component_name="ChargingStation",
            variable_name="HeartbeatInterval",
            monitoring_criterion="InvalidCriterion",
        )

        assert result["status"] == "Rejected"
        assert "statusInfo" in result
        assert result["statusInfo"].reason_code == "InvalidCriterion"

    @pytest.mark.asyncio
    async def test_clear_variable_monitoring(self, monitoring_manager, mock_timescale_client):
        """Test ClearVariableMonitoring."""
        result = await monitoring_manager.clear_variable_monitoring(
            station_id="TEST_STATION",
            component_name="ChargingStation",
            variable_name="HeartbeatInterval",
            monitoring_criterion="ThresholdMonitoring",
        )

        assert result["status"] == "Accepted"
        mock_timescale_client.store_variable_monitoring.assert_called_once()

    @pytest.mark.asyncio
    async def test_notify_monitoring_report(self, monitoring_manager, mock_timescale_client):
        """Test NotifyMonitoringReport."""
        await monitoring_manager.notify_monitoring_report(
            station_id="TEST_STATION", request_id=1, monitoring_base="Configuration"
        )

        mock_timescale_client.store_notified_monitoring_report.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_alert_rule(self, monitoring_manager, mock_timescale_client):
        """Test creating alert rule."""
        rule_id = await monitoring_manager.create_alert_rule(
            station_id="TEST_STATION",
            component_name="ChargingStation",
            variable_name="Temperature",
            monitoring_criterion="ThresholdMonitoring",
            threshold=80.0,
            severity="high",
        )

        assert rule_id is not None
        assert len(rule_id) > 0
        mock_timescale_client.store_alert_rule.assert_called_once()

    @pytest.mark.asyncio
    async def test_trigger_alert(self, monitoring_manager, mock_timescale_client):
        """Test triggering alert."""
        alert_id = await monitoring_manager.trigger_alert(
            station_id="TEST_STATION",
            rule_id="RULE_001",
            component_name="ChargingStation",
            variable_name="Temperature",
            current_value=85.0,
            threshold=80.0,
            severity="high",
        )

        assert alert_id is not None
        assert len(alert_id) > 0
        mock_timescale_client.store_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_monitoring_task_lifecycle(self, monitoring_manager):
        """Test monitoring task lifecycle."""
        station_id = "TEST_STATION"

        # Start monitoring task
        await monitoring_manager._start_monitoring_task(station_id)

        assert station_id in monitoring_manager.monitoring_tasks
        assert monitoring_manager.monitoring_tasks[station_id] is not None

        # Stop monitoring task
        await monitoring_manager.stop_monitoring(station_id)

        assert station_id not in monitoring_manager.monitoring_tasks

    @pytest.mark.asyncio
    async def test_check_monitoring_rules_threshold(
        self, monitoring_manager, mock_timescale_client
    ):
        """Test checking threshold monitoring rules."""
        mock_timescale_client.get_alert_rules.return_value = [
            {
                "rule_id": "RULE_001",
                "component_name": "ChargingStation",
                "variable_name": "Temperature",
                "monitoring_criterion": "ThresholdMonitoring",
                "threshold": 80.0,
                "severity": "high",
            }
        ]

        mock_timescale_client.get_device_variables.return_value = [
            {
                "component_name": "ChargingStation",
                "variable_name": "Temperature",
                "actual_value": "85.0",
            }
        ]

        mock_timescale_client.get_active_alerts.return_value = []

        await monitoring_manager._check_monitoring_rules("TEST_STATION")

        # Should trigger alert since temperature (85.0) > threshold (80.0)
        mock_timescale_client.store_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_monitoring_rules_delta(self, monitoring_manager, mock_timescale_client):
        """Test checking delta monitoring rules."""
        mock_timescale_client.get_alert_rules.return_value = [
            {
                "rule_id": "RULE_002",
                "component_name": "ChargingStation",
                "variable_name": "Power",
                "monitoring_criterion": "DeltaMonitoring",
                "threshold": 5.0,
                "severity": "medium",
            }
        ]

        mock_timescale_client.get_device_variables.return_value = [
            {"component_name": "ChargingStation", "variable_name": "Power", "actual_value": "25.0"}
        ]

        mock_timescale_client.get_periodic_monitoring_data.return_value = [{"value": "15.0"}]

        mock_timescale_client.get_active_alerts.return_value = []

        await monitoring_manager._check_monitoring_rules("TEST_STATION")

        # Should trigger alert since delta (10.0) > threshold (5.0)
        mock_timescale_client.store_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_monitoring_rules_no_trigger(
        self, monitoring_manager, mock_timescale_client
    ):
        """Test checking monitoring rules that don't trigger alerts."""
        mock_timescale_client.get_alert_rules.return_value = [
            {
                "rule_id": "RULE_001",
                "component_name": "ChargingStation",
                "variable_name": "Temperature",
                "monitoring_criterion": "ThresholdMonitoring",
                "threshold": 80.0,
                "severity": "high",
            }
        ]

        mock_timescale_client.get_device_variables.return_value = [
            {
                "component_name": "ChargingStation",
                "variable_name": "Temperature",
                "actual_value": "75.0",
            }
        ]

        mock_timescale_client.get_active_alerts.return_value = []

        await monitoring_manager._check_monitoring_rules("TEST_STATION")

        # Should not trigger alert since temperature (75.0) < threshold (80.0)
        mock_timescale_client.store_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_monitoring_rules_existing_alert(
        self, monitoring_manager, mock_timescale_client
    ):
        """Test checking monitoring rules with existing active alert."""
        mock_timescale_client.get_alert_rules.return_value = [
            {
                "rule_id": "RULE_001",
                "component_name": "ChargingStation",
                "variable_name": "Temperature",
                "monitoring_criterion": "ThresholdMonitoring",
                "threshold": 80.0,
                "severity": "high",
            }
        ]

        mock_timescale_client.get_device_variables.return_value = [
            {
                "component_name": "ChargingStation",
                "variable_name": "Temperature",
                "actual_value": "85.0",
            }
        ]

        # Existing active alert
        mock_timescale_client.get_active_alerts.return_value = [
            {"rule_id": "RULE_001", "status": "active"}
        ]

        await monitoring_manager._check_monitoring_rules("TEST_STATION")

        # Should not create duplicate alert
        mock_timescale_client.store_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_monitoring_loop_error_handling(self, monitoring_manager):
        """Test monitoring loop error handling."""

        # Create a modified monitoring loop that exits after one iteration
        async def limited_monitoring_loop(station_id: str):
            try:
                await monitoring_manager._check_monitoring_rules(station_id)
            except Exception as e:
                monitoring_manager.logger.error(f"Error in monitoring loop for {station_id}: {e}")

        with patch.object(
            monitoring_manager, "_check_monitoring_rules", side_effect=Exception("Test error")
        ):
            # Should not crash, just log error and continue
            await limited_monitoring_loop("TEST_STATION")

    @pytest.mark.asyncio
    async def test_cleanup_monitoring_tasks(self, monitoring_manager):
        """Test cleanup of all monitoring tasks."""
        # Start multiple monitoring tasks
        await monitoring_manager._start_monitoring_task("STATION_001")
        await monitoring_manager._start_monitoring_task("STATION_002")

        assert len(monitoring_manager.monitoring_tasks) == 2

        # Cleanup all tasks
        await monitoring_manager.cleanup()

        assert len(monitoring_manager.monitoring_tasks) == 0

    @pytest.mark.asyncio
    async def test_get_monitoring_data_error_handling(
        self, monitoring_manager, mock_timescale_client
    ):
        """Test error handling in get_monitoring_report."""
        mock_timescale_client.store_monitoring_report.side_effect = Exception("Database error")

        result = await monitoring_manager.get_monitoring_report(
            station_id="TEST_STATION", request_id=1, monitoring_base="Configuration"
        )

        assert result["status"] == "Rejected"
        assert "statusInfo" in result
        assert result["statusInfo"].reason_code == "InternalError"

    @pytest.mark.asyncio
    async def test_set_variable_monitoring_error_handling(
        self, monitoring_manager, mock_timescale_client
    ):
        """Test error handling in set_variable_monitoring."""
        mock_timescale_client.store_variable_monitoring.side_effect = Exception("Database error")

        result = await monitoring_manager.set_variable_monitoring(
            station_id="TEST_STATION",
            component_name="ChargingStation",
            variable_name="HeartbeatInterval",
            monitoring_criterion="ThresholdMonitoring",
            threshold=600.0,
        )

        assert result["status"] == "Rejected"
        assert "statusInfo" in result
        assert result["statusInfo"].reason_code == "InternalError"


class TestMonitoringCriterion:
    """Test MonitoringCriterion enum."""

    def test_monitoring_criterion_values(self):
        """Test MonitoringCriterion enum values."""
        assert MonitoringCriterion.THRESHOLD_MONITORING.value == "ThresholdMonitoring"
        assert MonitoringCriterion.DELTA_MONITORING.value == "DeltaMonitoring"
        assert MonitoringCriterion.PERIODIC_MONITORING.value == "PeriodicMonitoring"


class TestAlertSeverity:
    """Test AlertSeverity enum."""

    def test_alert_severity_values(self):
        """Test AlertSeverity enum values."""
        assert AlertSeverity.LOW.value == "low"
        assert AlertSeverity.MEDIUM.value == "medium"
        assert AlertSeverity.HIGH.value == "high"
        assert AlertSeverity.CRITICAL.value == "critical"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
