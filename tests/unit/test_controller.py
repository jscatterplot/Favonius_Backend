"""Unit tests for DepotController.

Reference: Development plan Step 5.2, PRD.md#11-2-unit-test-requirements
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest

from src.core.controller import DepotController
from src.core.controller_config import ControllerConfig
from src.core.models import DepotConfig, DepotState, OptimizationResult
from src.db.pools import DatabasePools

# ============ Fixtures ============


@pytest.fixture
def mock_db_pool():
    """Mock database connection pool wrapped in DatabasePools."""
    pool = MagicMock(spec=asyncpg.Pool)
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    return DatabasePools(static=pool, ts=pool), conn


@pytest.fixture
def depot_config():
    """Sample depot configuration."""
    vehicle_ids = ["bus_1", "bus_2"]
    return DepotConfig(
        vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
        vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
        charger_groups={80.0: 5},
        charger_efficiency=0.95,
        charger_vehicle_access={},
        battery_capacity=500.0,
        battery_power=100.0,
        max_site_power=800.0,
    )


@pytest.fixture
def controller_config():
    """Controller configuration for testing."""
    return ControllerConfig(
        optimization_horizon_hours=24,
        hourly_optimization_start=7,
        hourly_optimization_end=23,
        optimization_timeout=30.0,
        trigger_cooldown_minutes=1,  # Short for testing
        max_optimization_failures=3,
        dispatch_retry_attempts=2,
        dispatch_retry_delay_seconds=0.1,  # Short for testing
        shutdown_timeout_seconds=5.0,
    )


@pytest.fixture
def sample_optimization_result():
    """Sample optimization result."""
    return OptimizationResult(
        run_id=uuid4(),
        schedule={
            "bus_1": {"charging_power": [80.0] * 96, "soc": [0.3] * 96},
            "bus_2": {"charging_power": [60.0] * 96, "soc": [0.5] * 96},
        },
        battery_dispatch=[0.0] * 96,
        grid_power=[140.0] * 96,
        peak_demand=200.0,
        objective_value=1000.0,
        solve_time=5.0,
        status="completed",
    )


@pytest.fixture
def sample_depot_state():
    """Sample depot state."""
    n_t = 96
    return DepotState(
        vehicle_socs={"bus_1": 0.3, "bus_2": 0.5},
        battery_soc=0.5,
        prices=[0.10] * n_t,
        demand_charge_rate=15.0,
        current_month_peak=100.0,
        vehicle_availability={
            "bus_1": [True] * n_t,
            "bus_2": [True] * n_t,
        },
        energy_requirements={"bus_1": 200.0, "bus_2": 150.0},
        departure_times={"bus_1": 48, "bus_2": 60},
        building_power=[50.0] * n_t,
    )


# ============ Initialization Tests ============


class TestControllerInitialization:
    """Tests for DepotController initialization."""

    def test_controller_initialization(self, mock_db_pool, depot_config, controller_config):
        """Test basic controller initialization."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        assert controller.depot_id == depot_id
        assert controller.config == depot_config
        assert controller.controller_config == controller_config
        assert controller._running is False
        assert controller.last_schedule is None
        assert controller.last_result is None

    def test_controller_initialization_with_uuid(
        self, mock_db_pool, depot_config, controller_config
    ):
        """Test controller initialization with UUID object."""
        pool, _ = mock_db_pool
        depot_uuid = uuid4()

        controller = DepotController(
            pools=pool,
            depot_id=depot_uuid,
            config=depot_config,
            controller_config=controller_config,
        )

        assert controller.depot_id == str(depot_uuid)

    def test_controller_initialization_default_config(self, mock_db_pool, depot_config):
        """Test controller uses default config when not provided."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        with patch.object(ControllerConfig, "from_env") as mock_from_env:
            mock_from_env.return_value = ControllerConfig()

            DepotController(
                pools=pool,
                depot_id=depot_id,
                config=depot_config,
            )

            mock_from_env.assert_called_once()

    def test_controller_initializes_assembler(self, mock_db_pool, depot_config, controller_config):
        """Test controller creates StateAssembler."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        assert controller.assembler is not None
        assert controller.assembler.depot_id == depot_id

    def test_controller_initializes_trigger_monitor(
        self, mock_db_pool, depot_config, controller_config
    ):
        """Test controller creates TriggerMonitor."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        assert controller.trigger_monitor is not None

    def test_controller_passes_cooldown_config_to_trigger_monitor(
        self, mock_db_pool, depot_config, controller_config
    ):
        """Test controller passes trigger_cooldown_minutes from config to TriggerMonitor.

        Per PRD alignment fix, controller should pass trigger_cooldown_minutes
        from ControllerConfig to TriggerMonitor, not use hardcoded value.
        """
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        # Set custom cooldown in controller config
        controller_config.trigger_cooldown_minutes = 7

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        # Verify TriggerMonitor uses the cooldown from controller config
        assert (
            controller.trigger_monitor._trigger_cooldown_sec == 420.0
        ), "TriggerMonitor should use cooldown from ControllerConfig (7 minutes = 420 seconds)"
        assert (
            controller.trigger_monitor.config.trigger_cooldown_minutes == 7
        ), "TriggerMonitor config should have trigger_cooldown_minutes from ControllerConfig"


# ============ Optimization Retry Logic Tests ============


class TestOptimizationRetryLogic:
    """Tests for optimization retry logic."""

    @pytest.mark.asyncio
    async def test_optimization_retries_on_state_assembly_failure(
        self, mock_db_pool, depot_config, controller_config, sample_optimization_result
    ):
        """Test that optimization retries when state assembly fails."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        # First call fails, second succeeds
        call_count = [0]

        async def mock_get_state(horizon):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Database error")
            return MagicMock()

        controller.assembler.get_current_state = mock_get_state

        with patch("src.core.controller.optimize") as mock_optimize:
            mock_optimize.return_value = sample_optimization_result

            result = await controller.run_optimization("test")

            assert call_count[0] == 2  # Was retried
            assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_optimization_retries_on_solver_timeout(
        self,
        mock_db_pool,
        depot_config,
        controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test that optimization retries on solver timeout."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        # First call times out, second succeeds
        call_count = [0]

        def mock_optimize(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Time limit exceeded")
            return sample_optimization_result

        with patch("src.core.controller.optimize", side_effect=mock_optimize):
            result = await controller.run_optimization("test")

            assert call_count[0] == 2
            assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_optimization_fails_after_max_retries(
        self, mock_db_pool, depot_config, controller_config, sample_depot_state
    ):
        """Test that optimization fails after exhausting retries."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        with patch("src.core.controller.optimize", side_effect=Exception("Persistent error")):
            with pytest.raises(Exception, match="Persistent error"):
                await controller.run_optimization("test")


# ============ Circuit Breaker Tests ============


class TestCircuitBreaker:
    """Tests for circuit breaker functionality."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_after_max_failures(
        self, mock_db_pool, depot_config, controller_config, sample_depot_state
    ):
        """Test circuit breaker opens after max failures."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        # Use lower max failures for testing
        controller_config.max_optimization_failures = 2

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        with patch("src.core.controller.optimize", side_effect=Exception("Error")):
            # First failure
            try:
                await controller.run_optimization("test1")
            except Exception:
                pass

            # Second failure should trigger circuit breaker
            try:
                await controller.run_optimization("test2")
            except Exception:
                pass

        assert controller._circuit_breaker_open is True
        assert controller._circuit_breaker_reset_time is not None

    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks_triggers(
        self, mock_db_pool, depot_config, controller_config, sample_depot_state
    ):
        """Test that circuit breaker blocks optimization triggers."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        # Open circuit breaker manually
        controller._circuit_breaker_open = True
        controller._circuit_breaker_reset_time = datetime.utcnow() + timedelta(minutes=30)

        # Trigger should be ignored
        optimization_called = False

        original_run_optimization = controller.run_optimization

        async def mock_run_optimization(*args, **kwargs):
            nonlocal optimization_called
            optimization_called = True
            return await original_run_optimization(*args, **kwargs)

        controller.run_optimization = mock_run_optimization

        await controller._handle_trigger("test_trigger")

        assert optimization_called is False

    @pytest.mark.asyncio
    async def test_circuit_breaker_resets_on_success(
        self,
        mock_db_pool,
        depot_config,
        controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test circuit breaker resets on successful optimization."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        # Set up some failures
        controller._optimization_failures = 2
        controller._circuit_breaker_open = True
        controller._circuit_breaker_reset_time = datetime.utcnow() - timedelta(
            minutes=1
        )  # Already expired

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        with patch("src.core.controller.optimize", return_value=sample_optimization_result):
            await controller.run_optimization("test")

        assert controller._optimization_failures == 0
        assert controller._circuit_breaker_open is False


# ============ Cooldown Tests ============


class TestCooldownEnforcement:
    """Tests for trigger cooldown enforcement."""

    @pytest.mark.asyncio
    async def test_cooldown_blocks_rapid_triggers(
        self,
        mock_db_pool,
        depot_config,
        controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test that cooldown blocks rapid successive triggers."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller_config.trigger_cooldown_minutes = 5  # 5 minute cooldown

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        optimization_count = [0]

        async def mock_run_optimization(*args, **kwargs):
            optimization_count[0] += 1
            return sample_optimization_result

        controller.run_optimization = mock_run_optimization

        # First trigger should work
        await controller._handle_trigger("trigger_1")
        assert optimization_count[0] == 1

        # Second trigger within cooldown should be blocked
        await controller._handle_trigger("trigger_2")
        assert optimization_count[0] == 1  # Still 1

    @pytest.mark.asyncio
    async def test_cooldown_allows_after_period(
        self,
        mock_db_pool,
        depot_config,
        controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test that cooldown allows triggers after period expires."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller_config.trigger_cooldown_minutes = 0  # No cooldown for test

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        optimization_count = [0]

        async def mock_run_optimization(*args, **kwargs):
            optimization_count[0] += 1
            return sample_optimization_result

        controller.run_optimization = mock_run_optimization

        # Both triggers should work
        await controller._handle_trigger("trigger_1")
        await controller._handle_trigger("trigger_2")

        assert optimization_count[0] == 2


# ============ Graceful Shutdown Tests ============


class TestGracefulShutdown:
    """Tests for graceful shutdown."""

    @pytest.mark.asyncio
    async def test_stop_async_stops_running_controller(
        self, mock_db_pool, depot_config, controller_config
    ):
        """Test stop_async stops a running controller."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        # Simulate running state
        controller._running = True
        controller._monitor_task = None
        controller._control_loop_task = None

        await controller.stop_async(timeout=1.0)

        assert controller._running is False

    @pytest.mark.asyncio
    async def test_stop_async_cancels_pending_tasks(
        self, mock_db_pool, depot_config, controller_config
    ):
        """Test stop_async cancels pending tasks."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        controller._running = True

        # Create a mock task
        async def long_running():
            await asyncio.sleep(100)

        task = asyncio.create_task(long_running())
        controller._current_optimization_task = task

        await controller.stop_async(timeout=1.0)

        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_stop_async_handles_timeout(self, mock_db_pool, depot_config, controller_config):
        """Test stop_async handles timeout gracefully."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        controller._running = True

        # Create a task that ignores cancellation
        async def stubborn_task():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                # Don't re-raise
                await asyncio.sleep(100)

        task = asyncio.create_task(stubborn_task())
        controller._control_loop_task = task

        # Should complete without raising
        await controller.stop_async(timeout=0.1)

        task.cancel()  # Cleanup


# ============ Dispatch Command Tests ============


class TestDispatchCommands:
    """Tests for OCPP dispatch commands."""

    @pytest.mark.asyncio
    async def test_dispatch_enqueues_without_ocpp_server(
        self, mock_db_pool, depot_config, controller_config, sample_optimization_result
    ):
        """Production runs with ocpp_server=None: dispatch enqueues, never blocks."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
            ocpp_server=None,
        )

        with patch.object(
            controller.assembler.__class__, "load_depot_config", new_callable=AsyncMock
        ) as mock_load:
            # Map every vehicle in the schedule to a charge_point_id.
            mock_load.return_value = (
                depot_config,
                {vid: f"CP_{vid}" for vid in sample_optimization_result.schedule},
            )

            # Should not raise — every row goes to charging_command_queue.
            await controller._dispatch_commands(sample_optimization_result)

        # ``conn.fetchval`` returns the queue_id; we expect at least one
        # INSERT per vehicle with positive charging power.
        assert conn.fetchval.await_count >= 1

    @pytest.mark.asyncio
    async def test_dispatch_handles_missing_charge_point(
        self, mock_db_pool, depot_config, controller_config, sample_optimization_result
    ):
        """Test dispatch handles missing charge point gracefully."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())

        mock_ocpp = MagicMock()
        mock_ocpp.get_charge_point.return_value = None

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
            ocpp_server=mock_ocpp,
        )

        # Mock load_depot_config
        with patch.object(
            controller.assembler.__class__, "load_depot_config", new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {"bus_1": "charger_1"})

            # Should not raise
            await controller._dispatch_commands(sample_optimization_result)


# ============ State Update Tests ============


class TestStateUpdates:
    """Tests for controller state updates."""

    @pytest.mark.asyncio
    async def test_optimization_updates_last_schedule(
        self,
        mock_db_pool,
        depot_config,
        controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test that successful optimization updates last_schedule."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        with patch("src.core.controller.optimize", return_value=sample_optimization_result):
            await controller.run_optimization("test")

        assert controller.last_schedule == sample_optimization_result.schedule

    @pytest.mark.asyncio
    async def test_optimization_updates_last_run_time(
        self,
        mock_db_pool,
        depot_config,
        controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test that successful optimization updates last_run_time."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        before = datetime.utcnow()

        with patch("src.core.controller.optimize", return_value=sample_optimization_result):
            await controller.run_optimization("test")

        after = datetime.utcnow()

        assert controller.last_run_time is not None
        assert before <= controller.last_run_time <= after


# ============ Charging Profile Validation Tests ============


class TestChargingProfileValidation:
    """Tests for charging profile validation."""

    def test_validate_charging_profile_valid(self, mock_db_pool, depot_config, controller_config):
        """Test validation of valid charging profile."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        valid_profile = [
            {"start_period": 0, "limit": 80000, "number_phases": 3},
            {"start_period": 900, "limit": 60000, "number_phases": 3},
        ]

        assert controller._validate_charging_profile(valid_profile) is True

    def test_validate_charging_profile_empty(self, mock_db_pool, depot_config, controller_config):
        """Test validation of empty charging profile."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pools=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        # Empty profile should be invalid (nothing to dispatch)
        assert controller._validate_charging_profile([]) is False
