"""Unit tests for V2X controller module."""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone

from src.websocket_handler.v2x_controller import (
    V2XController, V2XOperationMode, V2XControllerConfig, V2XSetpoint
)


class TestV2XControllerConfig:
    """Test V2XControllerConfig class."""
    
    def test_default_config(self):
        """Test default configuration values."""
        config = V2XControllerConfig()
        
        assert config.enabled is True
        assert config.supported_modes is not None
        assert len(config.supported_modes) == 8  # All operation modes
        assert config.frequency_deadband == 0.05
        assert config.update_interval is not None
        assert config.power_limits is not None
        assert config.voltage_limits is not None
    
    def test_custom_config(self):
        """Test custom configuration values."""
        config = V2XControllerConfig(
            enabled=False,
            frequency_deadband=0.1,
            supported_modes=["ChargingOnly", "CentralSetpoint"]
        )
        
        assert config.enabled is False
        assert config.frequency_deadband == 0.1
        assert config.supported_modes == ["ChargingOnly", "CentralSetpoint"]


class TestV2XSetpoint:
    """Test V2XSetpoint class."""
    
    def test_setpoint_creation(self):
        """Test V2XSetpoint creation."""
        timestamp = datetime.now(timezone.utc).isoformat()
        setpoint = V2XSetpoint(
            station_id="TEST_STATION_001",
            evse_id=1,
            power_kw=5.0,
            mode=V2XOperationMode.CHARGING_ONLY,
            timestamp=timestamp,
            duration_seconds=300,
            ramp_rate_kw_per_s=1.0
        )
        
        assert setpoint.station_id == "TEST_STATION_001"
        assert setpoint.evse_id == 1
        assert setpoint.power_kw == 5.0
        assert setpoint.mode == V2XOperationMode.CHARGING_ONLY
        assert setpoint.timestamp == timestamp
        assert setpoint.duration_seconds == 300
        assert setpoint.ramp_rate_kw_per_s == 1.0


class TestV2XController:
    """Test V2XController class."""
    
    @pytest.fixture
    def mock_config(self):
        """Mock configuration."""
        config = Mock()
        config.v2x_controller = V2XControllerConfig()
        return config
    
    @pytest.fixture
    def v2x_controller(self, mock_config):
        """Create V2XController instance."""
        return V2XController(config=mock_config.v2x_controller, app_config=mock_config)
    
    @pytest.mark.timeout(10)
    def test_v2x_controller_initialization(self, mock_config):
        """Test V2XController initialization."""
        controller = V2XController(config=mock_config.v2x_controller, app_config=mock_config)
        
        assert controller.v2x_config == mock_config.v2x_controller
        assert controller.app_config == mock_config
        assert controller.active_setpoints == {}
        assert controller.mode_controllers is not None
        assert len(controller.mode_controllers) == 8  # All operation modes
        assert controller.tasks == {}
        assert controller.running is False
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_start_stop(self, v2x_controller):
        """Test starting and stopping the controller."""
        assert v2x_controller.running is False
        
        # Start controller
        await v2x_controller.start()
        assert v2x_controller.running is True
        
        # Stop controller
        await v2x_controller.stop()
        assert v2x_controller.running is False
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_set_power_setpoint(self, v2x_controller):
        """Test setting power setpoint."""
        timestamp = datetime.now(timezone.utc).isoformat()
        setpoint = V2XSetpoint(
            station_id="TEST_STATION_001",
            evse_id=1,
            power_kw=5.0,
            mode=V2XOperationMode.CHARGING_ONLY,
            timestamp=timestamp,
            duration_seconds=300
        )
        
        with patch.object(v2x_controller, '_validate_setpoint', return_value=True), \
             patch.object(v2x_controller, '_send_setpoint_to_charger', return_value=True), \
             patch.object(v2x_controller, '_store_setpoint_event', return_value=None):
            
            success = await v2x_controller.set_power_setpoint(setpoint)
            assert success is True
            assert setpoint.station_id in v2x_controller.active_setpoints
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_set_power_setpoint_invalid(self, v2x_controller):
        """Test setting invalid power setpoint."""
        timestamp = datetime.now(timezone.utc).isoformat()
        setpoint = V2XSetpoint(
            station_id="TEST_STATION_001",
            evse_id=1,
            power_kw=5.0,
            mode=V2XOperationMode.CHARGING_ONLY,
            timestamp=timestamp,
            duration_seconds=300
        )
        
        with patch.object(v2x_controller, '_validate_setpoint', return_value=False):
            success = await v2x_controller.set_power_setpoint(setpoint)
            assert success is False
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_active_setpoint(self, v2x_controller):
        """Test getting active setpoint for a station."""
        timestamp = datetime.now(timezone.utc).isoformat()
        setpoint = V2XSetpoint(
            station_id="TEST_STATION_001",
            evse_id=1,
            power_kw=5.0,
            mode=V2XOperationMode.CHARGING_ONLY,
            timestamp=timestamp,
            duration_seconds=300
        )
        
        v2x_controller.active_setpoints["TEST_STATION_001"] = setpoint
        
        active_setpoint = await v2x_controller.get_active_setpoint("TEST_STATION_001")
        assert active_setpoint == setpoint
        
        # Test non-existent station
        active_setpoint = await v2x_controller.get_active_setpoint("NON_EXISTENT")
        assert active_setpoint is None
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_clear_setpoint(self, v2x_controller):
        """Test clearing setpoint."""
        timestamp = datetime.now(timezone.utc).isoformat()
        setpoint = V2XSetpoint(
            station_id="TEST_STATION_001",
            evse_id=1,
            power_kw=5.0,
            mode=V2XOperationMode.CHARGING_ONLY,
            timestamp=timestamp,
            duration_seconds=300
        )
        
        v2x_controller.active_setpoints["TEST_STATION_001"] = setpoint
        
        with patch.object(v2x_controller, '_send_setpoint_to_charger', return_value=True):
            success = await v2x_controller.clear_setpoint("TEST_STATION_001")
            assert success is True
            assert "TEST_STATION_001" not in v2x_controller.active_setpoints
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_station_setpoint(self, v2x_controller):
        """Test getting station setpoint."""
        timestamp = datetime.now(timezone.utc).isoformat()
        setpoint = V2XSetpoint(
            station_id="TEST_STATION_001",
            evse_id=1,
            power_kw=5.0,
            mode=V2XOperationMode.CHARGING_ONLY,
            timestamp=timestamp,
            duration_seconds=300
        )
        
        v2x_controller.active_setpoints["TEST_STATION_001"] = setpoint
        
        retrieved_setpoint = await v2x_controller.get_active_setpoint("TEST_STATION_001")
        assert retrieved_setpoint == setpoint
        
        # Test non-existent station
        retrieved_setpoint = await v2x_controller.get_active_setpoint("NON_EXISTENT")
        assert retrieved_setpoint is None
    
    @pytest.mark.timeout(10)
    def test_operation_mode_enum(self):
        """Test V2XOperationMode enum values."""
        assert V2XOperationMode.CHARGING_ONLY.value == "ChargingOnly"
        assert V2XOperationMode.CENTRAL_SETPOINT.value == "CentralSetpoint"
        assert V2XOperationMode.CENTRAL_FREQUENCY.value == "CentralFrequency"
        assert V2XOperationMode.LOCAL_FREQUENCY.value == "LocalFrequency"
        assert V2XOperationMode.EXTERNAL_SETPOINT.value == "ExternalSetpoint"
        assert V2XOperationMode.EXTERNAL_LIMITS.value == "ExternalLimits"
        assert V2XOperationMode.LOCAL_LOAD_BALANCING.value == "LocalLoadBalancing"
        assert V2XOperationMode.IDLE.value == "Idle"
    
    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_controller_disabled(self, mock_config):
        """Test controller behavior when disabled."""
        mock_config.v2x_controller.enabled = False
        controller = V2XController(config=mock_config.v2x_controller, app_config=mock_config)
        
        await controller.start()
        assert controller.running is False
        assert len(controller.tasks) == 0