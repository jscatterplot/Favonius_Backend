"""Integration tests for control loop.

Reference: Development plan Step 5.2, PRD.md#11-3-integration-tests
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg

from src.core.controller import DepotController
from src.core.controller_manager import ControllerManager
from src.core.controller_config import ControllerConfig
from src.core.models import DepotConfig, OptimizationResult
from src.core.state.assembler import StateAssembler


@pytest.fixture
def mock_db_pool():
    """Mock database connection pool."""
    pool = AsyncMock(spec=asyncpg.Pool)
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    return pool, conn


@pytest.fixture
def sample_depot_config():
    """Sample depot configuration."""
    return DepotConfig(
        vehicle_capacities={'bus_1': 324.0, 'bus_2': 324.0},
        charger_power=80.0,
        charger_efficiency=0.95,
        n_chargers=10,
        battery_capacity=500.0,
        battery_power=100.0,
        max_site_power=800.0,
    )


@pytest.fixture
def sample_optimization_result():
    """Sample optimization result."""
    return OptimizationResult(
        run_id=uuid4(),
        schedule={
            'bus_1': {
                'charging_power': [0, 0, 80, 80] * 24,
                'soc': [0.3, 0.3, 0.35, 0.40] * 24,
            },
            'bus_2': {
                'charging_power': [80, 80, 0, 0] * 24,
                'soc': [0.5, 0.55, 0.55, 0.55] * 24,
            },
        },
        battery_dispatch=[0.0] * 96,
        grid_power=[100.0] * 96,
        peak_demand=450.0,
        objective_value=1234.56,
        solve_time=12.3,
        status='completed',
    )


@pytest.fixture
def controller_config():
    """Controller configuration for testing."""
    return ControllerConfig(
        optimization_horizon_hours=24,
        hourly_optimization_start=7,
        hourly_optimization_end=23,
        optimization_timeout=30.0,
        trigger_cooldown_minutes=1,  # Short cooldown for tests
        max_optimization_failures=3,
        dispatch_retry_attempts=2,
        shutdown_timeout_seconds=5.0,
    )


class TestDepotController:
    """Test DepotController functionality."""

    @pytest.mark.asyncio
    async def test_controller_initialization(
        self, mock_db_pool, sample_depot_config, controller_config
    ):
        """Test controller initialization."""
        pool, _ = mock_db_pool

        controller = DepotController(
            pool=pool,
            depot_id=str(uuid4()),
            config=sample_depot_config,
            controller_config=controller_config,
        )

        assert controller.depot_id
        assert controller.config == sample_depot_config
        assert controller.controller_config == controller_config
        assert not controller._running

    @pytest.mark.asyncio
    async def test_run_optimization_success(
        self, mock_db_pool, sample_depot_config, sample_optimization_result,
        controller_config
    ):
        """Test successful optimization run."""
        pool, conn = mock_db_pool

        controller = DepotController(
            pool=pool,
            depot_id=str(uuid4()),
            config=sample_depot_config,
            controller_config=controller_config,
        )

        # Mock state assembly
        with patch.object(
            controller.assembler, 'get_current_state', new_callable=AsyncMock
        ) as mock_state:
            from src.core.models import DepotState
            mock_state.return_value = DepotState(
                vehicle_socs={'bus_1': 0.45, 'bus_2': 0.82},
                battery_soc=0.55,
                prices=[0.10, 0.15, 0.12] * 32,
                demand_charge_rate=20.0,
                current_month_peak=380.0,
                vehicle_availability={'bus_1': [True] * 96, 'bus_2': [True] * 96},
                energy_requirements={'bus_1': 200.0, 'bus_2': 150.0},
                departure_times={'bus_1': 48, 'bus_2': 60},
                building_power=[50.0] * 96,
            )

            # Mock optimization
            with patch('src.core.controller.optimize') as mock_optimize:
                mock_optimize.return_value = sample_optimization_result

                # Mock database storage
                conn.execute = AsyncMock()

                result = await controller.run_optimization("test_trigger")

                assert result == sample_optimization_result
                assert controller.last_result == result
                assert controller.last_schedule == result.schedule
                assert controller._optimization_failures == 0

    @pytest.mark.asyncio
    async def test_run_optimization_with_retry(
        self, mock_db_pool, sample_depot_config, sample_optimization_result,
        controller_config
    ):
        """Test optimization with retry on failure."""
        pool, conn = mock_db_pool

        controller = DepotController(
            pool=pool,
            depot_id=str(uuid4()),
            config=sample_depot_config,
            controller_config=controller_config,
        )

        # Mock state assembly
        with patch.object(
            controller.assembler, 'get_current_state', new_callable=AsyncMock
        ) as mock_state:
            from src.core.models import DepotState
            mock_state.return_value = DepotState(
                vehicle_socs={'bus_1': 0.45},
                battery_soc=0.55,
                prices=[0.10] * 96,
                demand_charge_rate=20.0,
                current_month_peak=380.0,
                vehicle_availability={'bus_1': [True] * 96},
                energy_requirements={'bus_1': 200.0},
                departure_times={'bus_1': 48},
                building_power=[50.0] * 96,
            )

            # Mock optimization - fail first time, succeed second
            with patch('src.core.controller.optimize') as mock_optimize:
                mock_optimize.side_effect = [
                    Exception("Timeout"),
                    sample_optimization_result,
                ]

                conn.execute = AsyncMock()

                result = await controller.run_optimization("test_trigger")

                assert result == sample_optimization_result
                assert mock_optimize.call_count == 2

    @pytest.mark.asyncio
    async def test_circuit_breaker(
        self, mock_db_pool, sample_depot_config, controller_config
    ):
        """Test circuit breaker opens after max failures."""
        pool, conn = mock_db_pool

        controller = DepotController(
            pool=pool,
            depot_id=str(uuid4()),
            config=sample_depot_config,
            controller_config=controller_config,
        )

        # Mock state assembly
        with patch.object(
            controller.assembler, 'get_current_state', new_callable=AsyncMock
        ) as mock_state:
            from src.core.models import DepotState
            mock_state.return_value = DepotState(
                vehicle_socs={'bus_1': 0.45},
                battery_soc=0.55,
                prices=[0.10] * 96,
                demand_charge_rate=20.0,
                current_month_peak=380.0,
                vehicle_availability={'bus_1': [True] * 96},
                energy_requirements={'bus_1': 200.0},
                departure_times={'bus_1': 48},
                building_power=[50.0] * 96,
            )

            # Mock optimization to always fail
            with patch('src.core.controller.optimize') as mock_optimize:
                mock_optimize.side_effect = Exception("Optimization failed")

                # Trigger multiple failures
                for _ in range(controller_config.max_optimization_failures):
                    try:
                        await controller.run_optimization("test_trigger")
                    except Exception:
                        pass

                # Circuit breaker should be open
                assert controller._circuit_breaker_open
                assert controller._optimization_failures >= controller_config.max_optimization_failures

    @pytest.mark.asyncio
    async def test_trigger_cooldown(
        self, mock_db_pool, sample_depot_config, controller_config
    ):
        """Test trigger cooldown prevents rapid re-optimization."""
        pool, _ = mock_db_pool

        controller = DepotController(
            pool=pool,
            depot_id=str(uuid4()),
            config=sample_depot_config,
            controller_config=controller_config,
        )

        # Mock optimization to succeed
        with patch.object(
            controller.assembler, 'get_current_state', new_callable=AsyncMock
        ) as mock_state:
            from src.core.models import DepotState
            mock_state.return_value = DepotState(
                vehicle_socs={'bus_1': 0.45},
                battery_soc=0.55,
                prices=[0.10] * 96,
                demand_charge_rate=20.0,
                current_month_peak=380.0,
                vehicle_availability={'bus_1': [True] * 96},
                energy_requirements={'bus_1': 200.0},
                departure_times={'bus_1': 48},
                building_power=[50.0] * 96,
            )

            with patch('src.core.controller.optimize') as mock_optimize:
                from src.core.models import OptimizationResult
                mock_optimize.return_value = OptimizationResult(
                    run_id=uuid4(),
                    schedule={'bus_1': {'charging_power': [80] * 96, 'soc': [0.5] * 96}},
                    battery_dispatch=[0.0] * 96,
                    grid_power=[100.0] * 96,
                    peak_demand=450.0,
                    objective_value=1234.56,
                    solve_time=12.3,
                    status='completed',
                )

                # First trigger should work
                await controller._handle_trigger("test_trigger_1")
                assert mock_optimize.call_count == 1

                # Second trigger immediately should be ignored (cooldown)
                await controller._handle_trigger("test_trigger_2")
                # Should still be 1 call due to cooldown
                assert mock_optimize.call_count == 1

    @pytest.mark.asyncio
    async def test_graceful_shutdown(
        self, mock_db_pool, sample_depot_config, controller_config
    ):
        """Test graceful shutdown waits for operations."""
        pool, _ = mock_db_pool

        controller = DepotController(
            pool=pool,
            depot_id=str(uuid4()),
            config=sample_depot_config,
            controller_config=controller_config,
        )

        # Start controller
        controller._running = True

        # Stop gracefully
        await controller.stop_async(timeout=5.0)

        assert not controller._running


class TestControllerManager:
    """Test ControllerManager functionality."""

    @pytest.mark.asyncio
    async def test_manager_initialization(self, mock_db_pool, controller_config):
        """Test manager initialization."""
        pool, _ = mock_db_pool

        manager = ControllerManager(
            pool=pool,
            controller_config=controller_config,
        )

        assert manager.pool == pool
        assert manager.controller_config == controller_config
        assert len(manager.controllers) == 0

    @pytest.mark.asyncio
    async def test_add_controller(
        self, mock_db_pool, sample_depot_config, controller_config
    ):
        """Test adding a controller."""
        pool, conn = mock_db_pool

        depot_id = str(uuid4())

        # Mock database query for depot config
        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (sample_depot_config, {})

            manager = ControllerManager(
                pool=pool,
                controller_config=controller_config,
            )

            controller = await manager.add_controller(depot_id)

            assert depot_id in manager.controllers
            assert manager.controllers[depot_id] == controller

    @pytest.mark.asyncio
    async def test_get_or_create_controller(
        self, mock_db_pool, sample_depot_config, controller_config
    ):
        """Test get or create controller."""
        pool, _ = mock_db_pool

        depot_id = str(uuid4())

        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (sample_depot_config, {})

            manager = ControllerManager(
                pool=pool,
                controller_config=controller_config,
            )

            # First call creates
            controller1 = await manager.get_or_create_controller(depot_id)
            assert depot_id in manager.controllers

            # Second call gets existing
            controller2 = await manager.get_or_create_controller(depot_id)
            assert controller1 == controller2

    @pytest.mark.asyncio
    async def test_start_all_controllers(
        self, mock_db_pool, sample_depot_config, controller_config
    ):
        """Test starting all controllers."""
        pool, conn = mock_db_pool

        # Mock depot query
        depot_id1 = str(uuid4())
        depot_id2 = str(uuid4())

        async def mock_fetch(query, *args):
            if 'SELECT depot_id' in query:
                return [
                    {'depot_id': depot_id1},
                    {'depot_id': depot_id2},
                ]
            return []

        conn.fetch = AsyncMock(side_effect=mock_fetch)

        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (sample_depot_config, {})

            manager = ControllerManager(
                pool=pool,
                controller_config=controller_config,
            )

            await manager.start_all_controllers()

            # Should have created controllers for both depots
            assert len(manager.controllers) == 2
            assert depot_id1 in manager.controllers
            assert depot_id2 in manager.controllers

    @pytest.mark.asyncio
    async def test_stop_all_controllers(
        self, mock_db_pool, sample_depot_config, controller_config
    ):
        """Test stopping all controllers."""
        pool, conn = mock_db_pool

        depot_id = str(uuid4())

        # Mock depot query for start_all_controllers
        async def mock_fetch(query, *args):
            if 'SELECT depot_id' in query:
                return [{'depot_id': depot_id}]
            return []

        conn.fetch = AsyncMock(side_effect=mock_fetch)

        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (sample_depot_config, {})

            manager = ControllerManager(
                pool=pool,
                controller_config=controller_config,
            )

            # Start controllers (this sets _running = True)
            await manager.start_all_controllers()
            assert len(manager.controllers) >= 1

            # Stop all
            await manager.stop_all_controllers()

            assert len(manager.controllers) == 0

    @pytest.mark.asyncio
    async def test_health_check(
        self, mock_db_pool, sample_depot_config, controller_config
    ):
        """Test health check."""
        pool, _ = mock_db_pool

        depot_id = str(uuid4())

        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (sample_depot_config, {})

            manager = ControllerManager(
                pool=pool,
                controller_config=controller_config,
            )

            await manager.add_controller(depot_id)

            health = await manager.health_check()

            assert depot_id in health
            assert 'status' in health[depot_id]
            assert 'running' in health[depot_id]


class TestControlLoopIntegration:
    """Integration tests for full control loop."""

    @pytest.mark.asyncio
    async def test_control_loop_startup_shutdown(
        self, mock_db_pool, sample_depot_config, controller_config
    ):
        """Test control loop startup and shutdown."""
        pool, conn = mock_db_pool

        depot_id = str(uuid4())

        # Mock depot query
        async def mock_fetch(query, *args):
            if 'SELECT depot_id' in query:
                return [{'depot_id': depot_id}]
            return []

        conn.fetch = AsyncMock(side_effect=mock_fetch)

        with patch.object(
            StateAssembler, 'load_depot_config', new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (sample_depot_config, {})

            manager = ControllerManager(
                pool=pool,
                controller_config=controller_config,
            )

            # Start all controllers
            await manager.start_all_controllers()

            # Check manager state
            assert len(manager.controllers) >= 1
            assert manager._running is True

            # Stop all controllers
            await manager.stop_all_controllers()

            assert len(manager.controllers) == 0
            assert manager._running is False

