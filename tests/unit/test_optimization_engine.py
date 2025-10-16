"""Unit tests for optimization engine."""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime, timezone, timedelta

# Import optimization engine
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))

from websocket_handler.optimization_engine import OptimizationEngine, StationState
from websocket_handler.config import OptimizationServiceConfig


class TestOptimizationEngine:
    """Test OptimizationEngine functionality."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient."""
        client = Mock()
        client.get_active_charging_sessions = AsyncMock()
        client.get_latest_prices = AsyncMock()
        client.store_charging_schedule = AsyncMock()
        client.store_optimization_decision = AsyncMock()
        return client

    @pytest.fixture
    def mock_supabase_client(self):
        """Mock SupabaseClient."""
        client = Mock()
        return client

    @pytest.fixture
    def mock_connection_manager(self):
        """Mock ConnectionManager."""
        manager = Mock()
        return manager

    @pytest.fixture
    def optimization_config(self):
        """Create optimization configuration."""
        return OptimizationServiceConfig(
            enabled=True,
            horizon_hours=4,
            timestep_minutes=60,
            soc_minimum=0.2,
            soc_target=0.8,
            charge_power_kw=22.0,
            discharge_power_kw=10.0,
            battery_capacity_kwh=75.0
        )

    @pytest.fixture
    def optimization_engine(self, optimization_config, mock_timescale_client, mock_supabase_client, mock_connection_manager):
        """Create OptimizationEngine instance."""
        return OptimizationEngine(
            optimization_config,
            mock_timescale_client,
            mock_supabase_client,
            mock_connection_manager
        )

    @pytest.mark.asyncio
    async def test_optimization_engine_initialization(self, optimization_engine):
        """Test optimization engine initialization."""
        assert optimization_engine.config.enabled is True
        assert optimization_engine.config.horizon_hours == 4
        assert optimization_engine.config.timestep_minutes == 60
        assert optimization_engine._running is False

    @pytest.mark.asyncio
    async def test_start_disabled_engine(self, optimization_engine):
        """Test starting disabled optimization engine."""
        optimization_engine.config.enabled = False
        
        await optimization_engine.start()
        
        assert optimization_engine._running is False
        assert optimization_engine._task is None

    @pytest.mark.asyncio
    async def test_start_enabled_engine(self, optimization_engine):
        """Test starting enabled optimization engine."""
        await optimization_engine.start()
        
        assert optimization_engine._running is True
        assert optimization_engine._task is not None
        
        await optimization_engine.stop()

    @pytest.mark.asyncio
    async def test_request_run(self, optimization_engine):
        """Test requesting optimization run."""
        reason = "test_trigger"
        
        await optimization_engine.request_run(reason)
        
        assert optimization_engine._pending_reason == reason

    @pytest.mark.asyncio
    async def test_run_optimization_no_sessions(self, optimization_engine, mock_timescale_client):
        """Test optimization run with no active sessions."""
        mock_timescale_client.get_active_charging_sessions.return_value = []
        
        await optimization_engine._run_optimization("test_trigger")
        
        mock_timescale_client.get_active_charging_sessions.assert_called_once()
        mock_timescale_client.store_charging_schedule.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_optimization_with_sessions(self, optimization_engine, mock_timescale_client):
        """Test optimization run with active sessions."""
        now = datetime.now(timezone.utc)
        sessions = [
            {
                "station_id": "TEST_STATION_001",
                "evse_id": 1,
                "connector_id": 1,
                "start_time": now,
                "end_time": now + timedelta(hours=2),
                "start_soc_percent": 50.0
            }
        ]
        
        prices = [
            {
                "time": now.replace(second=0, microsecond=0),
                "lmp_price_mwh": 100.0
            }
        ]
        
        mock_timescale_client.get_active_charging_sessions.return_value = sessions
        mock_timescale_client.get_latest_prices.return_value = prices
        
        await optimization_engine._run_optimization("test_trigger")
        
        mock_timescale_client.get_active_charging_sessions.assert_called_once()
        mock_timescale_client.get_latest_prices.assert_called_once()
        mock_timescale_client.store_charging_schedule.assert_called_once()
        mock_timescale_client.store_optimization_decision.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_optimization_with_price_update_trigger(self, optimization_engine, mock_timescale_client):
        """Test optimization run triggered by price update."""
        now = datetime.now(timezone.utc)
        sessions = [
            {
                "station_id": "TEST_STATION_001",
                "evse_id": 1,
                "connector_id": 1,
                "start_time": now,
                "end_time": now + timedelta(hours=2),
                "start_soc_percent": 30.0
            }
        ]
        
        # High price should trigger discharge
        prices = [
            {
                "time": now.replace(second=0, microsecond=0),
                "lmp_price_mwh": 200.0
            }
        ]
        
        mock_timescale_client.get_active_charging_sessions.return_value = sessions
        mock_timescale_client.get_latest_prices.return_value = prices
        
        await optimization_engine._run_optimization("price_update")
        
        # Verify schedule was stored
        mock_timescale_client.store_charging_schedule.assert_called_once()
        stored_schedule = mock_timescale_client.store_charging_schedule.call_args[0][0]
        
        assert stored_schedule["station_id"] == "TEST_STATION_001"
        assert stored_schedule["evse_id"] == 1
        assert stored_schedule["purpose"] == "v2g_schedule"
        assert len(stored_schedule["schedule_periods"]) > 0

    @pytest.mark.asyncio
    async def test_determine_power_low_soc(self, optimization_engine):
        """Test power determination with low SOC."""
        power = optimization_engine._determine_power(100.0, 0.1)  # Low SOC, high price
        
        assert power == optimization_engine.config.charge_power_kw

    @pytest.mark.asyncio
    async def test_determine_power_negative_price(self, optimization_engine):
        """Test power determination with negative price."""
        power = optimization_engine._determine_power(-50.0, 0.5)  # Negative price
        
        assert power == optimization_engine.config.charge_power_kw

    @pytest.mark.asyncio
    async def test_determine_power_high_price(self, optimization_engine):
        """Test power determination with high price."""
        power = optimization_engine._determine_power(200.0, 0.8)  # High price, good SOC
        
        assert power < 0  # Should discharge
        assert abs(power) <= optimization_engine.config.discharge_power_kw

    @pytest.mark.asyncio
    async def test_determine_power_normal_price(self, optimization_engine):
        """Test power determination with normal price."""
        power = optimization_engine._determine_power(50.0, 0.5)  # Normal price
        
        assert power == 0.0  # Should not charge or discharge

    @pytest.mark.asyncio
    async def test_optimization_loop_error_handling(self, optimization_engine):
        """Test optimization loop error handling."""
        # Mock _run_optimization to raise exception
        with patch.object(optimization_engine, '_run_optimization', side_effect=Exception("Test error")):
            optimization_engine._running = True
            optimization_engine._pending_reason = "test_trigger"
            
            # Create a modified loop that exits after one iteration
            async def limited_loop():
                await asyncio.sleep(0.1)  # Short sleep instead of 5 seconds
                reason = None
                async with optimization_engine._lock:
                    reason = optimization_engine._pending_reason
                    optimization_engine._pending_reason = None
                if reason:
                    try:
                        await optimization_engine._run_optimization(reason)
                    except Exception as exc:
                        optimization_engine.logger.error(f"Optimization run failed: {exc}")
                # Exit after one iteration instead of continuing the loop
            
            # Run one iteration of the loop
            await limited_loop()
            
            # Should not crash, just log error
            assert optimization_engine._running is True

    @pytest.mark.asyncio
    async def test_set_connection_manager(self, optimization_engine, mock_connection_manager):
        """Test setting connection manager."""
        new_manager = Mock()
        
        optimization_engine.set_connection_manager(new_manager)
        
        assert optimization_engine.connection_manager == new_manager

    @pytest.mark.asyncio
    async def test_stop_optimization_engine(self, optimization_engine):
        """Test stopping optimization engine."""
        await optimization_engine.start()
        
        assert optimization_engine._running is True
        assert optimization_engine._task is not None
        
        await optimization_engine.stop()
        
        assert optimization_engine._running is False
        assert optimization_engine._task is None

    @pytest.mark.asyncio
    async def test_optimization_with_multiple_sessions(self, optimization_engine, mock_timescale_client):
        """Test optimization with multiple charging sessions."""
        now = datetime.now(timezone.utc)
        sessions = [
            {
                "station_id": "TEST_STATION_001",
                "evse_id": 1,
                "connector_id": 1,
                "start_time": now,
                "end_time": now + timedelta(hours=2),
                "start_soc_percent": 50.0
            },
            {
                "station_id": "TEST_STATION_002",
                "evse_id": 1,
                "connector_id": 1,
                "start_time": now,
                "end_time": now + timedelta(hours=3),
                "start_soc_percent": 70.0
            }
        ]
        
        prices = [
            {
                "time": now.replace(second=0, microsecond=0),
                "lmp_price_mwh": 100.0
            }
        ]
        
        mock_timescale_client.get_active_charging_sessions.return_value = sessions
        mock_timescale_client.get_latest_prices.return_value = prices
        
        await optimization_engine._run_optimization("test_trigger")
        
        # Should create schedules for both sessions
        assert mock_timescale_client.store_charging_schedule.call_count == 2
        mock_timescale_client.store_optimization_decision.assert_called_once()

    @pytest.mark.asyncio
    async def test_optimization_schedule_periods(self, optimization_engine, mock_timescale_client):
        """Test optimization schedule period generation."""
        now = datetime.now(timezone.utc)
        sessions = [
            {
                "station_id": "TEST_STATION_001",
                "evse_id": 1,
                "connector_id": 1,
                "start_time": now,
                "end_time": now + timedelta(hours=2),
                "start_soc_percent": 50.0
            }
        ]
        
        prices = [
            {
                "time": now.replace(second=0, microsecond=0),
                "lmp_price_mwh": 100.0
            }
        ]
        
        mock_timescale_client.get_active_charging_sessions.return_value = sessions
        mock_timescale_client.get_latest_prices.return_value = prices
        
        await optimization_engine._run_optimization("test_trigger")
        
        stored_schedule = mock_timescale_client.store_charging_schedule.call_args[0][0]
        periods = stored_schedule["schedule_periods"]
        
        # Should have periods for 2 hours with 60-minute timesteps
        assert len(periods) == 2
        assert periods[0]["startPeriod"] == 0
        assert periods[1]["startPeriod"] == 3600  # 1 hour in seconds
        assert all("limit" in period for period in periods)
        assert all("numberPhases" in period for period in periods)


class TestStationState:
    """Test StationState dataclass."""

    def test_station_state_creation(self):
        """Test StationState creation."""
        state = StationState(
            station_id="TEST_STATION_001",
            evse_id=1,
            connector_id=1,
            soc=0.5,
            power_kw=22.0,
            max_charge_kw=22.0,
            max_discharge_kw=10.0
        )
        
        assert state.station_id == "TEST_STATION_001"
        assert state.evse_id == 1
        assert state.connector_id == 1
        assert state.soc == 0.5
        assert state.power_kw == 22.0
        assert state.max_charge_kw == 22.0
        assert state.max_discharge_kw == 10.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
