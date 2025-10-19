"""V2G integration tests for DER controls and bidirectional charging."""

import pytest
import asyncio
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from src.websocket_handler.der_control_manager import (
    DERControlManager, DERControlType, DERControlEnumType, DERCurve, DERCurvePoint
)
from src.websocket_handler.timescale_client import TimescaleClient
from src.websocket_handler.config import Config, TimescaleConfig


@pytest.fixture
async def mock_timescale_client():
    """Mock TimescaleDB client for testing."""
    client = AsyncMock(spec=TimescaleClient)
    client.execute_query = AsyncMock()
    client.fetch_one = AsyncMock()
    client.fetch_all = AsyncMock()
    return client


@pytest.fixture
async def der_control_manager(mock_timescale_client):
    """Create DER control manager with mocked dependencies."""
    return DERControlManager(mock_timescale_client)


@pytest.fixture
def sample_der_control_data():
    """Sample DER control data for testing."""
    return {
        "controlId": 1,
        "isDefault": False,
        "controlType": "FixedPFInject",
        "priority": 10,
        "startTime": "2024-01-01T00:00:00Z",
        "duration": 3600,
        "isSuperseded": False
    }


@pytest.fixture
def sample_freq_droop_control():
    """Sample frequency droop control data."""
    return {
        "controlId": 2,
        "isDefault": False,
        "controlType": "FreqDroop",
        "priority": 5,
        "overFreq": 50.2,
        "underFreq": 49.8,
        "overDroop": 0.05,
        "underDroop": 0.05,
        "responseTime": 5
    }


@pytest.fixture
def sample_curve_control():
    """Sample curve-based control data."""
    return {
        "controlId": 3,
        "isDefault": False,
        "controlType": "VoltVar",
        "priority": 8,
        "curve": {
            "curveType": "VoltVar",
            "curvePoints": [
                {"x": 0.95, "y": 0.2},
                {"x": 1.0, "y": 0.0},
                {"x": 1.05, "y": -0.2}
            ],
            "curveUnitX": "p.u.",
            "curveUnitY": "p.u."
        }
    }


class TestDERControlManager:
    """Test DER Control Manager functionality."""
    
    @pytest.mark.asyncio
    async def test_set_der_control_basic(self, der_control_manager, sample_der_control_data):
        """Test setting a basic DER control."""
        # Mock database operations
        der_control_manager._get_station_der_capabilities = AsyncMock(return_value={
            "modesSupported": ["FixedPFInject", "VoltVar", "WattVar"]
        })
        der_control_manager._get_active_der_controls = AsyncMock(return_value=[])
        der_control_manager._store_der_control = AsyncMock()
        der_control_manager._update_control_cache = AsyncMock()
        
        result = await der_control_manager.set_der_control("station_001", sample_der_control_data)
        
        assert result["status"] == "Accepted"
        assert "Success" in result["statusInfo"]["reasonCode"]
        der_control_manager._store_der_control.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_set_der_control_freq_droop(self, der_control_manager, sample_freq_droop_control):
        """Test setting a frequency droop control."""
        # Mock database operations
        der_control_manager._get_station_der_capabilities = AsyncMock(return_value={
            "modesSupported": ["FreqDroop", "VoltVar", "WattVar"]
        })
        der_control_manager._get_active_der_controls = AsyncMock(return_value=[])
        der_control_manager._store_der_control = AsyncMock()
        der_control_manager._update_control_cache = AsyncMock()
        
        result = await der_control_manager.set_der_control("station_001", sample_freq_droop_control)
        
        assert result["status"] == "Accepted"
        assert "Success" in result["statusInfo"]["reasonCode"]
    
    @pytest.mark.asyncio
    async def test_set_der_control_curve_based(self, der_control_manager, sample_curve_control):
        """Test setting a curve-based control."""
        # Mock database operations
        der_control_manager._get_station_der_capabilities = AsyncMock(return_value={
            "modesSupported": ["VoltVar", "WattVar", "FixedVar"]
        })
        der_control_manager._get_active_der_controls = AsyncMock(return_value=[])
        der_control_manager._store_der_control = AsyncMock()
        der_control_manager._update_control_cache = AsyncMock()
        
        result = await der_control_manager.set_der_control("station_001", sample_curve_control)
        
        assert result["status"] == "Accepted"
        assert "Success" in result["statusInfo"]["reasonCode"]
    
    @pytest.mark.asyncio
    async def test_set_der_control_unsupported_type(self, der_control_manager, sample_der_control_data):
        """Test setting an unsupported control type."""
        # Mock database operations
        der_control_manager._get_station_der_capabilities = AsyncMock(return_value={
            "modesSupported": ["VoltVar", "WattVar"]  # FixedPFInject not supported
        })
        
        result = await der_control_manager.set_der_control("station_001", sample_der_control_data)
        
        assert result["status"] == "Rejected"
        assert "NotSupported" in result["statusInfo"]["reasonCode"]
    
    @pytest.mark.asyncio
    async def test_set_der_control_priority_superseding(self, der_control_manager, sample_der_control_data):
        """Test control priority and superseding logic."""
        # Create existing control with lower priority
        existing_control = DERControlType(
            control_id=1,
            control_type=DERControlEnumType.FIXED_PF_INJECT,
            priority=5,
            is_superseded=False
        )
        
        # Mock database operations
        der_control_manager._get_station_der_capabilities = AsyncMock(return_value={
            "modesSupported": ["FixedPFInject", "VoltVar", "WattVar"]
        })
        der_control_manager._get_active_der_controls = AsyncMock(return_value=[existing_control])
        der_control_manager._update_der_control = AsyncMock()
        der_control_manager._store_der_control = AsyncMock()
        der_control_manager._update_control_cache = AsyncMock()
        
        # Set new control with higher priority
        sample_der_control_data["priority"] = 10
        result = await der_control_manager.set_der_control("station_001", sample_der_control_data)
        
        assert result["status"] == "Accepted"
        # Verify existing control was marked as superseded
        der_control_manager._update_der_control.assert_called_once()
        updated_control = der_control_manager._update_der_control.call_args[0][1]
        assert updated_control.is_superseded is True
    
    @pytest.mark.asyncio
    async def test_get_der_control_specific(self, der_control_manager):
        """Test getting a specific DER control."""
        # Create mock control
        mock_control = DERControlType(
            control_id=1,
            control_type=DERControlEnumType.FIXED_PF_INJECT,
            priority=10
        )
        
        # Mock database operations
        der_control_manager._get_der_control_by_id = AsyncMock(return_value=mock_control)
        
        result = await der_control_manager.get_der_control("station_001", control_id=1)
        
        assert result["status"] == "Accepted"
        assert "controlId" in result["der_control"]
        assert result["der_control"]["controlId"] == 1
    
    @pytest.mark.asyncio
    async def test_get_der_control_all(self, der_control_manager):
        """Test getting all active DER controls."""
        # Create mock controls
        mock_controls = [
            DERControlType(control_id=1, control_type=DERControlEnumType.FIXED_PF_INJECT, priority=10),
            DERControlType(control_id=2, control_type=DERControlEnumType.VOLT_VAR, priority=5)
        ]
        
        # Mock database operations
        der_control_manager._get_active_der_controls = AsyncMock(return_value=mock_controls)
        
        result = await der_control_manager.get_der_control("station_001")
        
        assert result["status"] == "Accepted"
        assert len(result["der_control"]) == 2
        assert all("controlId" in control for control in result["der_control"])
    
    @pytest.mark.asyncio
    async def test_report_der_control(self, der_control_manager, sample_der_control_data):
        """Test reporting DER control status."""
        # Mock database operations
        der_control_manager._store_reported_der_controls = AsyncMock()
        
        result = await der_control_manager.report_der_control("station_001", [sample_der_control_data])
        
        assert result["status"] == "Accepted"
        assert "Success" in result["statusInfo"]["reasonCode"]
        der_control_manager._store_reported_der_controls.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_clear_der_control_specific(self, der_control_manager):
        """Test clearing a specific DER control."""
        # Mock database operations
        der_control_manager._clear_der_control_by_id = AsyncMock(return_value=True)
        
        result = await der_control_manager.clear_der_control("station_001", control_id=1)
        
        assert result["status"] == "Accepted"
        assert "Success" in result["statusInfo"]["reasonCode"]
        der_control_manager._clear_der_control_by_id.assert_called_once_with("station_001", 1)
    
    @pytest.mark.asyncio
    async def test_clear_der_control_all(self, der_control_manager):
        """Test clearing all DER controls."""
        # Mock database operations
        der_control_manager._clear_all_der_controls = AsyncMock(return_value=3)
        
        result = await der_control_manager.clear_der_control("station_001")
        
        assert result["status"] == "Accepted"
        assert "Success" in result["statusInfo"]["reasonCode"]
        assert "3 DER controls" in result["statusInfo"]["additionalInfo"]
        der_control_manager._clear_all_der_controls.assert_called_once_with("station_001")
    
    @pytest.mark.asyncio
    async def test_notify_der_alarm(self, der_control_manager):
        """Test DER alarm notification."""
        # Mock database operations
        der_control_manager._store_der_alarm_event = AsyncMock()
        
        result = await der_control_manager.notify_der_alarm(
            "station_001", "FreqDroop", True, "GridFault", "2024-01-01T12:00:00Z"
        )
        
        assert result["status"] == "Accepted"
        assert "Success" in result["statusInfo"]["reasonCode"]
        der_control_manager._store_der_alarm_event.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_notify_der_start_stop(self, der_control_manager):
        """Test DER start/stop notification."""
        # Mock database operations
        der_control_manager._store_der_start_stop_event = AsyncMock()
        der_control_manager._activate_der_control = AsyncMock()
        
        result = await der_control_manager.notify_der_start_stop(
            "station_001", 1, True, None, "2024-01-01T12:00:00Z"
        )
        
        assert result["status"] == "Accepted"
        assert "Success" in result["statusInfo"]["reasonCode"]
        der_control_manager._store_der_start_stop_event.assert_called_once()
        der_control_manager._activate_der_control.assert_called_once_with("station_001", 1)
    
    @pytest.mark.asyncio
    async def test_parse_der_control_with_curve(self, der_control_manager, sample_curve_control):
        """Test parsing DER control with curve data."""
        control = der_control_manager._parse_der_control(sample_curve_control)
        
        assert control.control_id == 3
        assert control.control_type == DERControlEnumType.VOLT_VAR
        assert control.curve is not None
        assert control.curve.curve_type.value == "VoltVar"
        assert len(control.curve.points) == 3
        assert control.curve.points[0].x == 0.95
        assert control.curve.points[0].y == 0.2
    
    @pytest.mark.asyncio
    async def test_validate_der_control_freq_droop_missing_params(self, der_control_manager):
        """Test validation of frequency droop control with missing parameters."""
        # Mock database operations
        der_control_manager._get_station_der_capabilities = AsyncMock(return_value={
            "modesSupported": ["FreqDroop"]
        })
        
        # Create control with missing required parameters
        control_data = {
            "controlId": 1,
            "controlType": "FreqDroop",
            "priority": 5
            # Missing overFreq and underFreq
        }
        
        control = der_control_manager._parse_der_control(control_data)
        result = await der_control_manager._validate_der_control("station_001", control)
        
        assert result["valid"] is False
        assert "PropertyConstraintViolation" in result["reason_code"]
        assert "overFreq and underFreq" in result["message"]


class TestV2GIntegrationScenarios:
    """Test complete V2G integration scenarios."""
    
    @pytest.mark.asyncio
    async def test_bidirectional_charging_workflow(self, der_control_manager, mock_timescale_client):
        """Test complete bidirectional charging workflow."""
        # Mock database operations
        der_control_manager._get_station_der_capabilities = AsyncMock(return_value={
            "modesSupported": ["FixedPFInject", "VoltVar", "WattVar", "LimitMaxDischarge"]
        })
        der_control_manager._get_active_der_controls = AsyncMock(return_value=[])
        der_control_manager._store_der_control = AsyncMock()
        der_control_manager._update_control_cache = AsyncMock()
        
        # Step 1: Set discharge limit control
        discharge_control = {
            "controlId": 1,
            "controlType": "LimitMaxDischarge",
            "priority": 10,
            "pctMaxDischargePower": 0.8  # 80% of max discharge power
        }
        
        result1 = await der_control_manager.set_der_control("station_001", discharge_control)
        assert result1["status"] == "Accepted"
        
        # Step 2: Set power factor control for grid support
        pf_control = {
            "controlId": 2,
            "controlType": "FixedPFInject",
            "priority": 5,
            "startTime": "2024-01-01T12:00:00Z",
            "duration": 3600
        }
        
        result2 = await der_control_manager.set_der_control("station_001", pf_control)
        assert result2["status"] == "Accepted"
        
        # Step 3: Report control status
        reported_controls = [discharge_control, pf_control]
        result3 = await der_control_manager.report_der_control("station_001", reported_controls)
        assert result3["status"] == "Accepted"
        
        # Step 4: Notify start of controls
        result4 = await der_control_manager.notify_der_start_stop("station_001", 1, True)
        assert result4["status"] == "Accepted"
        
        result5 = await der_control_manager.notify_der_start_stop("station_001", 2, True)
        assert result5["status"] == "Accepted"
    
    @pytest.mark.asyncio
    async def test_grid_frequency_response_workflow(self, der_control_manager):
        """Test grid frequency response workflow."""
        # Mock database operations
        der_control_manager._get_station_der_capabilities = AsyncMock(return_value={
            "modesSupported": ["FreqDroop", "VoltVar", "WattVar"]
        })
        der_control_manager._get_active_der_controls = AsyncMock(return_value=[])
        der_control_manager._store_der_control = AsyncMock()
        der_control_manager._update_control_cache = AsyncMock()
        der_control_manager._store_der_alarm_event = AsyncMock()
        
        # Step 1: Set frequency droop control
        freq_control = {
            "controlId": 1,
            "controlType": "FreqDroop",
            "priority": 15,
            "overFreq": 50.2,
            "underFreq": 49.8,
            "overDroop": 0.05,
            "underDroop": 0.05,
            "responseTime": 5
        }
        
        result1 = await der_control_manager.set_der_control("station_001", freq_control)
        assert result1["status"] == "Accepted"
        
        # Step 2: Simulate frequency alarm
        result2 = await der_control_manager.notify_der_alarm(
            "station_001", "FreqDroop", False, "OverFrequency", "2024-01-01T12:00:00Z"
        )
        assert result2["status"] == "Accepted"
        
        # Step 3: Start frequency response
        result3 = await der_control_manager.notify_der_start_stop("station_001", 1, True)
        assert result3["status"] == "Accepted"
        
        # Step 4: Simulate alarm end
        result4 = await der_control_manager.notify_der_alarm(
            "station_001", "FreqDroop", True, None, "2024-01-01T12:05:00Z"
        )
        assert result4["status"] == "Accepted"
    
    @pytest.mark.asyncio
    async def test_voltage_regulation_workflow(self, der_control_manager):
        """Test voltage regulation workflow with curve-based control."""
        # Mock database operations
        der_control_manager._get_station_der_capabilities = AsyncMock(return_value={
            "modesSupported": ["VoltVar", "WattVar", "FixedVar"]
        })
        der_control_manager._get_active_der_controls = AsyncMock(return_value=[])
        der_control_manager._store_der_control = AsyncMock()
        der_control_manager._update_control_cache = AsyncMock()
        
        # Step 1: Set voltage-reactive power curve
        voltvar_control = {
            "controlId": 1,
            "controlType": "VoltVar",
            "priority": 12,
            "curve": {
                "curveType": "VoltVar",
                "curvePoints": [
                    {"x": 0.95, "y": 0.2},  # Low voltage -> inject reactive power
                    {"x": 1.0, "y": 0.0},   # Nominal voltage -> no reactive power
                    {"x": 1.05, "y": -0.2}  # High voltage -> absorb reactive power
                ],
                "curveUnitX": "p.u.",
                "curveUnitY": "p.u."
            }
        }
        
        result1 = await der_control_manager.set_der_control("station_001", voltvar_control)
        assert result1["status"] == "Accepted"
        
        # Step 2: Start voltage regulation
        result2 = await der_control_manager.notify_der_start_stop("station_001", 1, True)
        assert result2["status"] == "Accepted"
        
        # Step 3: Report control status
        result3 = await der_control_manager.report_der_control("station_001", [voltvar_control])
        assert result3["status"] == "Accepted"


@pytest.mark.asyncio
async def test_database_integration():
    """Test actual database integration with TimescaleDB."""
    # This test requires a real TimescaleDB instance
    # Skip if not available
    try:
        config = TimescaleConfig(
            host="localhost",
            port=5432,
            database="test_tsdb",
            user="test_user",
            password="test_password",
            sslmode="disable"
        )
        
        client = TimescaleClient(config)
        await client.connect()
        
        # Test basic query
        result = await client.fetch_one("SELECT 1 as test")
        assert result is not None
        assert result[0] == 1
        
        await client.close()
        
    except Exception as e:
        pytest.skip(f"TimescaleDB not available: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])