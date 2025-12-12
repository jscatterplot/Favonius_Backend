"""Unit tests for OCPP dispatch functionality.

Reference: Development plan Step 3.1, PRD.md#9-1-ocpp-integration
"""

import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg

from src.adapters.ocpp.dispatch import dispatch_charging_profiles, _store_charging_command
from src.core.models import OptimizationResult


# ============ Fixtures ============

@pytest.fixture
def mock_db_pool():
    """Mock database connection pool."""
    pool = MagicMock(spec=asyncpg.Pool)
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    return pool, conn


@pytest.fixture
def mock_ocpp_server():
    """Mock OCPP server."""
    server = MagicMock()
    return server


@pytest.fixture
def sample_optimization_result():
    """Sample optimization result."""
    return OptimizationResult(
        run_id=uuid4(),
        schedule={
            'bus_1': {
                'charging_power': [80.0, 80.0, 60.0, 0.0] * 24,
                'soc': [0.3, 0.35, 0.40, 0.45] * 24,
            },
            'bus_2': {
                'charging_power': [70.0, 70.0, 0.0, 0.0] * 24,
                'soc': [0.5, 0.55, 0.60, 0.65] * 24,
            },
        },
        battery_dispatch=[10.0, -5.0, 0.0, 5.0] * 24,
        grid_power=[200.0, 150.0, 100.0, 50.0] * 24,
        peak_demand=200.0,
        objective_value=1000.0,
        solve_time=5.0,
        status='completed',
    )


@pytest.fixture
def vehicle_to_charger_map():
    """Sample vehicle to charger mapping."""
    return {
        'bus_1': ('charger_001', 1),
        'bus_2': ('charger_002', 1),
    }


@pytest.fixture
def mock_charge_point():
    """Mock charge point."""
    cp = MagicMock()
    cp.set_charging_profile = AsyncMock(return_value=True)
    return cp


# ============ Dispatch Tests ============

class TestDispatchChargingProfiles:
    """Tests for dispatch_charging_profiles function."""

    @pytest.mark.asyncio
    async def test_dispatch_success(
        self, mock_ocpp_server, sample_optimization_result,
        vehicle_to_charger_map, mock_charge_point
    ):
        """Test successful dispatch to connected chargers."""
        mock_ocpp_server.get_charge_point.return_value = mock_charge_point
        
        results = await dispatch_charging_profiles(
            mock_ocpp_server,
            sample_optimization_result,
            vehicle_to_charger_map,
        )
        
        assert results['bus_1'] is True
        assert results['bus_2'] is True
        assert mock_charge_point.set_charging_profile.call_count == 2

    @pytest.mark.asyncio
    async def test_dispatch_to_disconnected_charger(
        self, mock_ocpp_server, sample_optimization_result, vehicle_to_charger_map
    ):
        """Test dispatch when charger is not connected."""
        mock_ocpp_server.get_charge_point.return_value = None
        
        results = await dispatch_charging_profiles(
            mock_ocpp_server,
            sample_optimization_result,
            vehicle_to_charger_map,
        )
        
        assert results['bus_1'] is False
        assert results['bus_2'] is False

    @pytest.mark.asyncio
    async def test_dispatch_partial_failure(
        self, mock_ocpp_server, sample_optimization_result, vehicle_to_charger_map
    ):
        """Test dispatch when some chargers fail."""
        # First charger succeeds, second is disconnected
        connected_cp = MagicMock()
        connected_cp.set_charging_profile = AsyncMock(return_value=True)
        
        def get_charge_point(cp_id):
            if cp_id == 'charger_001':
                return connected_cp
            return None
        
        mock_ocpp_server.get_charge_point.side_effect = get_charge_point
        
        results = await dispatch_charging_profiles(
            mock_ocpp_server,
            sample_optimization_result,
            vehicle_to_charger_map,
        )
        
        assert results['bus_1'] is True
        assert results['bus_2'] is False

    @pytest.mark.asyncio
    async def test_dispatch_charger_rejects_profile(
        self, mock_ocpp_server, sample_optimization_result,
        vehicle_to_charger_map
    ):
        """Test dispatch when charger rejects profile."""
        mock_cp = MagicMock()
        mock_cp.set_charging_profile = AsyncMock(return_value=False)
        mock_ocpp_server.get_charge_point.return_value = mock_cp
        
        results = await dispatch_charging_profiles(
            mock_ocpp_server,
            sample_optimization_result,
            vehicle_to_charger_map,
        )
        
        assert results['bus_1'] is False
        assert results['bus_2'] is False

    @pytest.mark.asyncio
    async def test_dispatch_missing_vehicle_mapping(
        self, mock_ocpp_server, mock_charge_point
    ):
        """Test dispatch when vehicle has no charger mapping."""
        mock_ocpp_server.get_charge_point.return_value = mock_charge_point
        
        result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {'charging_power': [80.0] * 96, 'soc': [0.5] * 96},
                'bus_3': {'charging_power': [60.0] * 96, 'soc': [0.4] * 96},  # No mapping
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[140.0] * 96,
            peak_demand=140.0,
            objective_value=500.0,
            solve_time=3.0,
            status='completed',
        )
        
        # Only bus_1 has mapping
        vehicle_map = {'bus_1': ('charger_001', 1)}
        
        results = await dispatch_charging_profiles(
            mock_ocpp_server,
            result,
            vehicle_map,
        )
        
        assert results['bus_1'] is True
        assert results['bus_3'] is False

    @pytest.mark.asyncio
    async def test_dispatch_empty_charging_power(
        self, mock_ocpp_server, vehicle_to_charger_map
    ):
        """Test dispatch with empty charging power list."""
        result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {'charging_power': [], 'soc': []},
            },
            battery_dispatch=[],
            grid_power=[],
            peak_demand=0.0,
            objective_value=0.0,
            solve_time=1.0,
            status='completed',
        )
        
        results = await dispatch_charging_profiles(
            mock_ocpp_server,
            result,
            vehicle_to_charger_map,
        )
        
        assert results['bus_1'] is False

    @pytest.mark.asyncio
    async def test_dispatch_all_none_charging_power(
        self, mock_ocpp_server, vehicle_to_charger_map
    ):
        """Test dispatch with all None values in charging power."""
        result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {'charging_power': [None, None, None], 'soc': [0.5, 0.5, 0.5]},
            },
            battery_dispatch=[0.0, 0.0, 0.0],
            grid_power=[0.0, 0.0, 0.0],
            peak_demand=0.0,
            objective_value=0.0,
            solve_time=1.0,
            status='completed',
        )
        
        results = await dispatch_charging_profiles(
            mock_ocpp_server,
            result,
            vehicle_to_charger_map,
        )
        
        assert results['bus_1'] is False

    @pytest.mark.asyncio
    async def test_dispatch_requires_mapping_or_pool(
        self, mock_ocpp_server, sample_optimization_result
    ):
        """Test that dispatch requires either mapping or pool+depot_id."""
        with pytest.raises(ValueError, match="Either vehicle_to_charger_map"):
            await dispatch_charging_profiles(
                mock_ocpp_server,
                sample_optimization_result,
                vehicle_to_charger_map=None,
                pool=None,
            )

    @pytest.mark.asyncio
    async def test_dispatch_builds_mapping_from_db(
        self, mock_ocpp_server, mock_db_pool, sample_optimization_result, mock_charge_point
    ):
        """Test dispatch builds vehicle mapping from database."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        
        mock_ocpp_server.get_charge_point.return_value = mock_charge_point
        
        with patch('src.adapters.ocpp.dispatch.get_vehicle_to_charger_map') as mock_get_map:
            mock_get_map.return_value = {
                'bus_1': ('charger_001', 1),
                'bus_2': ('charger_002', 1),
            }
            
            results = await dispatch_charging_profiles(
                mock_ocpp_server,
                sample_optimization_result,
                pool=pool,
                depot_id=depot_id,
            )
            
            mock_get_map.assert_called_once_with(pool, depot_id, use_cache=True)
            assert results['bus_1'] is True

    @pytest.mark.asyncio
    async def test_dispatch_handles_set_profile_exception(
        self, mock_ocpp_server, sample_optimization_result, vehicle_to_charger_map
    ):
        """Test dispatch handles exception from set_charging_profile."""
        mock_cp = MagicMock()
        mock_cp.set_charging_profile = AsyncMock(side_effect=Exception("Connection lost"))
        mock_ocpp_server.get_charge_point.return_value = mock_cp
        
        results = await dispatch_charging_profiles(
            mock_ocpp_server,
            sample_optimization_result,
            vehicle_to_charger_map,
        )
        
        assert results['bus_1'] is False
        assert results['bus_2'] is False


# ============ Profile Conversion Tests ============

class TestProfileConversion:
    """Tests for charging profile conversion edge cases."""

    @pytest.mark.asyncio
    async def test_dispatch_with_custom_delta_t(
        self, mock_ocpp_server, vehicle_to_charger_map, mock_charge_point
    ):
        """Test dispatch with custom time step."""
        mock_ocpp_server.get_charge_point.return_value = mock_charge_point
        
        result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {'charging_power': [80.0, 60.0], 'soc': [0.3, 0.5]},
            },
            battery_dispatch=[0.0, 0.0],
            grid_power=[80.0, 60.0],
            peak_demand=80.0,
            objective_value=100.0,
            solve_time=1.0,
            status='completed',
        )
        
        results = await dispatch_charging_profiles(
            mock_ocpp_server,
            result,
            vehicle_to_charger_map,
            delta_t=0.5,  # 30-minute time steps
        )
        
        assert results['bus_1'] is True

    @pytest.mark.asyncio
    async def test_dispatch_handles_conversion_error(
        self, mock_ocpp_server, vehicle_to_charger_map, mock_charge_point
    ):
        """Test dispatch handles profile conversion errors."""
        mock_ocpp_server.get_charge_point.return_value = mock_charge_point
        
        result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {'charging_power': [80.0], 'soc': [0.3]},
            },
            battery_dispatch=[0.0],
            grid_power=[80.0],
            peak_demand=80.0,
            objective_value=50.0,
            solve_time=1.0,
            status='completed',
        )
        
        with patch(
            'src.adapters.ocpp.dispatch.convert_schedule_to_ocpp_profile',
            side_effect=ValueError("Invalid schedule format")
        ):
            results = await dispatch_charging_profiles(
                mock_ocpp_server,
                result,
                vehicle_to_charger_map,
            )
            
            assert results['bus_1'] is False


# ============ Storage Tests ============

class TestStoreChargingCommand:
    """Tests for _store_charging_command function."""

    @pytest.mark.asyncio
    async def test_store_command_success(self, mock_db_pool):
        """Test successful storage of charging command."""
        pool, conn = mock_db_pool
        
        await _store_charging_command(
            pool,
            vehicle_id='bus_1',
            charge_point_id='charger_001',
            connector_id=1,
            charging_profile=[{'start_period': 0, 'limit': 80000}],
            optimization_result=OptimizationResult(
                run_id=uuid4(),
                schedule={},
                battery_dispatch=[],
                grid_power=[],
                peak_demand=0.0,
                objective_value=0.0,
                solve_time=1.0,
                status='completed',
            ),
        )
        
        conn.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_command_handles_db_error(self, mock_db_pool):
        """Test storage handles database errors gracefully."""
        pool, conn = mock_db_pool
        conn.execute.side_effect = asyncpg.PostgresError("DB error")
        
        # Should not raise
        await _store_charging_command(
            pool,
            vehicle_id='bus_1',
            charge_point_id='charger_001',
            connector_id=1,
            charging_profile=[],
            optimization_result=OptimizationResult(
                run_id=uuid4(),
                schedule={},
                battery_dispatch=[],
                grid_power=[],
                peak_demand=0.0,
                objective_value=0.0,
                solve_time=1.0,
                status='completed',
            ),
        )


# ============ Retry Logic Tests ============

class TestDispatchRetryLogic:
    """Tests for dispatch retry behavior.
    
    Note: Retry logic is implemented in DepotController._dispatch_commands,
    but we test that dispatch_charging_profiles reports failures correctly
    for higher-level retry handling.
    """

    @pytest.mark.asyncio
    async def test_dispatch_reports_all_failures(
        self, mock_ocpp_server, vehicle_to_charger_map
    ):
        """Test that all failures are reported for retry handling."""
        mock_ocpp_server.get_charge_point.return_value = None
        
        result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {'charging_power': [80.0] * 4, 'soc': [0.5] * 4},
                'bus_2': {'charging_power': [60.0] * 4, 'soc': [0.4] * 4},
            },
            battery_dispatch=[0.0] * 4,
            grid_power=[140.0] * 4,
            peak_demand=140.0,
            objective_value=200.0,
            solve_time=2.0,
            status='completed',
        )
        
        results = await dispatch_charging_profiles(
            mock_ocpp_server,
            result,
            vehicle_to_charger_map,
        )
        
        # All failures should be reported
        failed_count = sum(1 for v in results.values() if not v)
        assert failed_count == 2


# ============ Edge Cases ============

class TestDispatchEdgeCases:
    """Edge case tests for dispatch."""

    @pytest.mark.asyncio
    async def test_dispatch_empty_schedule(
        self, mock_ocpp_server, vehicle_to_charger_map
    ):
        """Test dispatch with empty schedule."""
        result = OptimizationResult(
            run_id=uuid4(),
            schedule={},
            battery_dispatch=[],
            grid_power=[],
            peak_demand=0.0,
            objective_value=0.0,
            solve_time=0.5,
            status='completed',
        )
        
        results = await dispatch_charging_profiles(
            mock_ocpp_server,
            result,
            vehicle_to_charger_map,
        )
        
        assert results == {}

    @pytest.mark.asyncio
    async def test_dispatch_stores_on_success(
        self, mock_ocpp_server, mock_db_pool, vehicle_to_charger_map, mock_charge_point
    ):
        """Test that successful dispatch stores command in database."""
        pool, conn = mock_db_pool
        mock_ocpp_server.get_charge_point.return_value = mock_charge_point
        
        result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {'charging_power': [80.0] * 4, 'soc': [0.5] * 4},
            },
            battery_dispatch=[0.0] * 4,
            grid_power=[80.0] * 4,
            peak_demand=80.0,
            objective_value=100.0,
            solve_time=1.0,
            status='completed',
        )
        
        await dispatch_charging_profiles(
            mock_ocpp_server,
            result,
            vehicle_to_charger_map,
            pool=pool,
        )
        
        # Should have stored the command
        assert conn.execute.called

    @pytest.mark.asyncio
    async def test_dispatch_with_single_vehicle(
        self, mock_ocpp_server, mock_charge_point
    ):
        """Test dispatch with single vehicle."""
        mock_ocpp_server.get_charge_point.return_value = mock_charge_point
        
        result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_solo': {'charging_power': [80.0, 60.0, 40.0], 'soc': [0.3, 0.4, 0.5]},
            },
            battery_dispatch=[0.0, 0.0, 0.0],
            grid_power=[80.0, 60.0, 40.0],
            peak_demand=80.0,
            objective_value=50.0,
            solve_time=0.8,
            status='completed',
        )
        
        vehicle_map = {'bus_solo': ('charger_solo', 1)}
        
        results = await dispatch_charging_profiles(
            mock_ocpp_server,
            result,
            vehicle_map,
        )
        
        assert results['bus_solo'] is True

