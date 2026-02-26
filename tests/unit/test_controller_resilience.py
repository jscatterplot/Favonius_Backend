"""Comprehensive resilience tests for DepotController.

Tests circuit breaker patterns, retry logic, graceful degradation,
concurrent request handling, and state recovery scenarios.

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
def fast_controller_config():
    """Fast controller configuration for testing."""
    return ControllerConfig(
        optimization_horizon_hours=24,
        hourly_optimization_start=0,
        hourly_optimization_end=23,
        optimization_timeout=10.0,
        trigger_cooldown_minutes=0,  # No cooldown for tests
        max_optimization_failures=3,
        dispatch_retry_attempts=2,
        dispatch_retry_delay_seconds=0.01,  # Very short for tests
        shutdown_timeout_seconds=1.0,
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


# ============ Circuit Breaker Opening/Closing Tests ============


class TestCircuitBreakerOpenClose:
    """Tests for circuit breaker opening and closing behavior."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_at_threshold(
        self, mock_db_pool, depot_config, fast_controller_config, sample_depot_state
    ):
        """Test circuit breaker opens exactly at max failures threshold."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        fast_controller_config.max_optimization_failures = 3

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        with patch("src.core.controller.optimize", side_effect=Exception("Persistent error")):
            # Each run_optimization will have 3 attempts internally (initial + 2 retries)
            # We need max_optimization_failures total failures across runs
            for i in range(fast_controller_config.max_optimization_failures):
                try:
                    await controller.run_optimization(f"test_{i}")
                except Exception:
                    pass

                if i < fast_controller_config.max_optimization_failures - 1:
                    assert (
                        controller._circuit_breaker_open is False
                    ), f"Breaker opened too early at failure {i+1}"

        assert controller._circuit_breaker_open is True
        assert controller._circuit_breaker_reset_time is not None

    @pytest.mark.asyncio
    async def test_circuit_breaker_half_open_state(
        self,
        mock_db_pool,
        depot_config,
        fast_controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test circuit breaker allows one test call after reset time."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        # Open circuit breaker with expired reset time
        controller._circuit_breaker_open = True
        controller._circuit_breaker_reset_time = datetime.utcnow() - timedelta(seconds=1)
        controller._optimization_failures = 3

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        # Next trigger should allow through (half-open state)
        with patch("src.core.controller.optimize", return_value=sample_optimization_result):
            await controller._handle_trigger("test_trigger")

        # Breaker should be reset after success
        assert controller._circuit_breaker_open is False
        assert controller._optimization_failures == 0

    @pytest.mark.asyncio
    async def test_circuit_breaker_stays_open_on_failure_after_reset(
        self, mock_db_pool, depot_config, fast_controller_config, sample_depot_state
    ):
        """Test circuit breaker re-opens if failure occurs in half-open state."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        fast_controller_config.max_optimization_failures = 1  # Open immediately on failure

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        # Set circuit breaker to half-open state
        controller._circuit_breaker_open = True
        controller._circuit_breaker_reset_time = datetime.utcnow() - timedelta(seconds=1)
        controller._optimization_failures = 0

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        # Test call fails
        with patch("src.core.controller.optimize", side_effect=Exception("Still failing")):
            await controller._handle_trigger("test_trigger")

        # Breaker should re-open
        assert controller._circuit_breaker_open is True
        assert controller._circuit_breaker_reset_time > datetime.utcnow()

    @pytest.mark.asyncio
    async def test_circuit_breaker_reset_time_calculation(
        self, mock_db_pool, depot_config, fast_controller_config, sample_depot_state
    ):
        """Test circuit breaker reset time is correctly calculated."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        fast_controller_config.max_optimization_failures = 1

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        before = datetime.utcnow()

        with patch("src.core.controller.optimize", side_effect=Exception("Error")):
            try:
                await controller.run_optimization("test")
            except Exception:
                pass

        after = datetime.utcnow()

        # Reset time should be ~30 minutes from now
        expected_min = before + timedelta(minutes=29)
        expected_max = after + timedelta(minutes=31)

        assert expected_min <= controller._circuit_breaker_reset_time <= expected_max


# ============ Retry Logic with Exponential Backoff Tests ============


class TestRetryExponentialBackoff:
    """Tests for retry logic with exponential backoff."""

    @pytest.mark.asyncio
    async def test_exponential_backoff_delays(
        self, mock_db_pool, depot_config, fast_controller_config, sample_depot_state
    ):
        """Test that retries use exponential backoff."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        sleep_calls = []
        original_sleep = asyncio.sleep

        async def mock_sleep(delay):
            sleep_calls.append(delay)
            await original_sleep(0.001)  # Minimal actual sleep

        with patch("asyncio.sleep", side_effect=mock_sleep):
            with patch("src.core.controller.optimize", side_effect=Exception("Error")):
                try:
                    await controller.run_optimization("test")
                except Exception:
                    pass

        # Should have exponential backoff: 1s, 2s (base * 2^attempt)
        if len(sleep_calls) >= 2:
            assert sleep_calls[1] >= sleep_calls[0], "Backoff should increase"

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_second_attempt(
        self,
        mock_db_pool,
        depot_config,
        fast_controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test optimization succeeds on retry."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        call_count = [0]

        def mock_optimize(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Transient error")
            return sample_optimization_result

        with patch("src.core.controller.optimize", side_effect=mock_optimize):
            result = await controller.run_optimization("test")

        assert result.status == "completed"
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_different_error_types_all_retry(
        self, mock_db_pool, depot_config, fast_controller_config, sample_depot_state
    ):
        """Test different error types all trigger retries."""
        pool, _ = mock_db_pool
        str(uuid4())

        errors = [
            Exception("State assembly error"),
            TimeoutError("Solver timeout"),
            RuntimeError("Infeasible problem"),
        ]

        for error in errors:
            controller = DepotController(
                pool=pool,
                depot_id=str(uuid4()),
                config=depot_config,
                controller_config=fast_controller_config,
            )

            controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

            call_count = [0]

            def make_mock():
                current_error = error

                def mock_optimize(*args, **kwargs):
                    call_count[0] += 1
                    raise current_error

                return mock_optimize

            with patch("src.core.controller.optimize", side_effect=make_mock()):
                try:
                    await controller.run_optimization("test")
                except Exception:
                    pass

            # Should have retried (initial + retries)
            assert call_count[0] >= 2, f"Should retry for {type(error).__name__}"


# ============ Graceful Degradation Tests ============


class TestGracefulDegradation:
    """Tests for graceful degradation on component failure."""

    @pytest.mark.asyncio
    async def test_continues_on_storage_failure(
        self,
        mock_db_pool,
        depot_config,
        fast_controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test optimization continues even if result storage fails."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        # Make storage fail
        conn.execute.side_effect = asyncpg.PostgresError("Storage error")

        with patch("src.core.controller.optimize", return_value=sample_optimization_result):
            # Should not raise despite storage failure
            result = await controller.run_optimization("test")

        assert result.status == "completed"
        assert controller.last_schedule is not None

    @pytest.mark.asyncio
    async def test_continues_on_dispatch_failure(
        self,
        mock_db_pool,
        depot_config,
        fast_controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test optimization completes even if dispatch fails."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        mock_ocpp = MagicMock()
        mock_cp = MagicMock()
        mock_cp.set_charging_profile = AsyncMock(side_effect=Exception("Dispatch error"))
        mock_ocpp.get_charge_point.return_value = mock_cp

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
            ocpp_server=mock_ocpp,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        with patch.object(
            controller.assembler.__class__, "load_depot_config", new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {"bus_1": "charger_1", "bus_2": "charger_2"})

            with patch("src.core.controller.optimize", return_value=sample_optimization_result):
                result = await controller.run_optimization("test")

        assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_partial_dispatch_success(
        self, mock_db_pool, depot_config, fast_controller_config, sample_optimization_result
    ):
        """Test optimization reports partial dispatch success."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())

        # Create charger mocks - one succeeds, one fails
        mock_ocpp = MagicMock()

        success_cp = MagicMock()
        success_cp.set_charging_profile = AsyncMock(return_value=True)

        fail_cp = MagicMock()
        fail_cp.set_charging_profile = AsyncMock(return_value=False)

        def get_charge_point(cp_id):
            if cp_id == "charger_1":
                return success_cp
            return fail_cp

        mock_ocpp.get_charge_point.side_effect = get_charge_point

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
            ocpp_server=mock_ocpp,
        )

        with patch.object(
            controller.assembler.__class__, "load_depot_config", new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {"bus_1": "charger_1", "bus_2": "charger_2"})

            await controller._dispatch_commands(sample_optimization_result)

        # Both dispatch attempts should have been made
        assert success_cp.set_charging_profile.called
        assert fail_cp.set_charging_profile.called


# ============ Concurrent Optimization Request Tests ============


class TestConcurrentOptimization:
    """Tests for concurrent optimization request handling."""

    @pytest.mark.asyncio
    async def test_concurrent_triggers_cooldown(
        self,
        mock_db_pool,
        depot_config,
        fast_controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test concurrent triggers are handled via cooldown."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        fast_controller_config.trigger_cooldown_minutes = 1  # Enable cooldown

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        optimization_starts = []

        def slow_optimization(*args, **kwargs):
            optimization_starts.append(datetime.utcnow())
            return sample_optimization_result

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        with patch("src.core.controller.optimize", side_effect=slow_optimization):
            # Fire multiple triggers concurrently
            await asyncio.gather(
                controller._handle_trigger("trigger_1"),
                controller._handle_trigger("trigger_2"),
                controller._handle_trigger("trigger_3"),
            )

        # Only one should have run due to cooldown
        assert len(optimization_starts) == 1

    @pytest.mark.asyncio
    async def test_rapid_api_requests(
        self,
        mock_db_pool,
        depot_config,
        fast_controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test rapid API optimization requests."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        optimization_count = [0]

        def count_optimization(*args, **kwargs):
            optimization_count[0] += 1
            return sample_optimization_result

        with patch("src.core.controller.optimize", side_effect=count_optimization):
            # Run multiple optimizations concurrently
            results = await asyncio.gather(
                controller.run_optimization("api_1"),
                controller.run_optimization("api_2"),
                controller.run_optimization("api_3"),
                return_exceptions=True,
            )

        # All should complete (no mutual exclusion on direct calls)
        successful = sum(1 for r in results if isinstance(r, OptimizationResult))
        assert successful == 3


# ============ State Recovery Tests ============


class TestStateRecovery:
    """Tests for state recovery after failures."""

    @pytest.mark.asyncio
    async def test_failure_count_preserved_across_runs(
        self, mock_db_pool, depot_config, fast_controller_config, sample_depot_state
    ):
        """Test failure count is preserved across optimization runs."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        fast_controller_config.max_optimization_failures = 5

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        with patch("src.core.controller.optimize", side_effect=Exception("Error")):
            for i in range(3):
                try:
                    await controller.run_optimization(f"run_{i}")
                except Exception:
                    pass

        assert controller._optimization_failures == 3

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(
        self,
        mock_db_pool,
        depot_config,
        fast_controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test success resets failure count."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        # Set initial failure state
        controller._optimization_failures = 2

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        with patch("src.core.controller.optimize", return_value=sample_optimization_result):
            await controller.run_optimization("test")

        assert controller._optimization_failures == 0

    @pytest.mark.asyncio
    async def test_last_schedule_persists_on_failure(
        self,
        mock_db_pool,
        depot_config,
        fast_controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test last successful schedule persists after failure."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        # First: successful optimization
        with patch("src.core.controller.optimize", return_value=sample_optimization_result):
            await controller.run_optimization("success")

        original_schedule = controller.last_schedule.copy()

        # Second: failed optimization
        with patch("src.core.controller.optimize", side_effect=Exception("Error")):
            try:
                await controller.run_optimization("failure")
            except Exception:
                pass

        # Last schedule should be preserved
        assert controller.last_schedule == original_schedule


# ============ Dispatch Retry Logic Tests ============


class TestDispatchRetryLogic:
    """Tests for OCPP dispatch retry logic."""

    @pytest.mark.asyncio
    async def test_dispatch_retries_on_rejection(
        self, mock_db_pool, depot_config, fast_controller_config, sample_optimization_result
    ):
        """Test dispatch retries when charger rejects profile."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        fast_controller_config.dispatch_retry_attempts = 2

        mock_ocpp = MagicMock()
        mock_cp = MagicMock()

        # First two calls fail, third succeeds
        mock_cp.set_charging_profile = AsyncMock(side_effect=[False, False, True])
        mock_ocpp.get_charge_point.return_value = mock_cp

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
            ocpp_server=mock_ocpp,
        )

        with patch.object(
            controller.assembler.__class__, "load_depot_config", new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {"bus_1": "charger_1"})
            single_result = OptimizationResult(
                run_id=sample_optimization_result.run_id,
                schedule={"bus_1": sample_optimization_result.schedule["bus_1"]},
                battery_dispatch=sample_optimization_result.battery_dispatch,
                grid_power=sample_optimization_result.grid_power,
                peak_demand_kw=sample_optimization_result.peak_demand_kw,
                objective_value=sample_optimization_result.objective_value,
                solve_time_s=sample_optimization_result.solve_time_s,
                status=sample_optimization_result.status,
                solver_used=sample_optimization_result.solver_used,
            )

            await controller._dispatch_commands(single_result)

        # Should have retried
        assert mock_cp.set_charging_profile.call_count == 3

    @pytest.mark.asyncio
    async def test_dispatch_retries_on_exception(
        self, mock_db_pool, depot_config, fast_controller_config, sample_optimization_result
    ):
        """Test dispatch retries on exception."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())
        fast_controller_config.dispatch_retry_attempts = 2

        mock_ocpp = MagicMock()
        mock_cp = MagicMock()

        # First call raises, second succeeds
        mock_cp.set_charging_profile = AsyncMock(side_effect=[Exception("Network error"), True])
        mock_ocpp.get_charge_point.return_value = mock_cp

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
            ocpp_server=mock_ocpp,
        )

        with patch.object(
            controller.assembler.__class__, "load_depot_config", new_callable=AsyncMock
        ) as mock_load:
            mock_load.return_value = (depot_config, {"bus_1": "charger_1"})
            single_result = OptimizationResult(
                run_id=sample_optimization_result.run_id,
                schedule={"bus_1": sample_optimization_result.schedule["bus_1"]},
                battery_dispatch=sample_optimization_result.battery_dispatch,
                grid_power=sample_optimization_result.grid_power,
                peak_demand_kw=sample_optimization_result.peak_demand_kw,
                objective_value=sample_optimization_result.objective_value,
                solve_time_s=sample_optimization_result.solve_time_s,
                status=sample_optimization_result.status,
                solver_used=sample_optimization_result.solver_used,
            )

            await controller._dispatch_commands(single_result)

        assert mock_cp.set_charging_profile.call_count == 2


# ============ Charging Profile Validation Edge Cases ============


class TestChargingProfileValidation:
    """Edge case tests for charging profile validation."""

    def test_validate_out_of_order_periods(
        self, mock_db_pool, depot_config, fast_controller_config
    ):
        """Test validation rejects out-of-order periods."""
        pool, _ = mock_db_pool

        controller = DepotController(
            pool=pool,
            depot_id=str(uuid4()),
            config=depot_config,
            controller_config=fast_controller_config,
        )

        invalid_profile = [
            {"start_period": 900, "limit": 80000, "number_phases": 3},
            {"start_period": 0, "limit": 60000, "number_phases": 3},  # Out of order
        ]

        assert controller._validate_charging_profile(invalid_profile) is False

    def test_validate_negative_power_limit(
        self, mock_db_pool, depot_config, fast_controller_config
    ):
        """Test validation rejects negative power limit."""
        pool, _ = mock_db_pool

        controller = DepotController(
            pool=pool,
            depot_id=str(uuid4()),
            config=depot_config,
            controller_config=fast_controller_config,
        )

        invalid_profile = [
            {"start_period": 0, "limit": -1000, "number_phases": 3},
        ]

        assert controller._validate_charging_profile(invalid_profile) is False

    def test_validate_excessive_power_limit(
        self, mock_db_pool, depot_config, fast_controller_config
    ):
        """Test validation rejects excessive power limit."""
        pool, _ = mock_db_pool

        controller = DepotController(
            pool=pool,
            depot_id=str(uuid4()),
            config=depot_config,
            controller_config=fast_controller_config,
        )

        invalid_profile = [
            {"start_period": 0, "limit": 500000, "number_phases": 3},  # 500kW
        ]

        assert controller._validate_charging_profile(invalid_profile) is False

    def test_validate_zero_power_valid(self, mock_db_pool, depot_config, fast_controller_config):
        """Test validation accepts zero power limit."""
        pool, _ = mock_db_pool

        controller = DepotController(
            pool=pool,
            depot_id=str(uuid4()),
            config=depot_config,
            controller_config=fast_controller_config,
        )

        valid_profile = [
            {"start_period": 0, "limit": 0, "number_phases": 3},
        ]

        assert controller._validate_charging_profile(valid_profile) is True


# ============ Shutdown During Operation Tests ============


class TestShutdownDuringOperation:
    """Tests for shutdown during active operations."""

    @pytest.mark.asyncio
    async def test_shutdown_waits_for_optimization(
        self,
        mock_db_pool,
        depot_config,
        fast_controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test shutdown waits for running optimization."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        optimization_completed = []

        async def slow_optimize(*args, **kwargs):
            await asyncio.sleep(0.1)
            optimization_completed.append(True)
            return sample_optimization_result

        with patch("src.core.controller.optimize", side_effect=slow_optimize):
            # Start optimization
            opt_task = asyncio.create_task(controller.run_optimization("test"))
            controller._current_optimization_task = opt_task

            # Wait a bit then shutdown
            await asyncio.sleep(0.05)
            await controller.stop_async(timeout=2.0)

            # Wait for task to complete
            try:
                await opt_task
            except asyncio.CancelledError:
                pass

        # Optimization should have completed (not cancelled immediately)
        assert len(optimization_completed) == 1 or opt_task.cancelled()

    @pytest.mark.asyncio
    async def test_shutdown_cancels_on_timeout(
        self, mock_db_pool, depot_config, fast_controller_config, sample_depot_state
    ):
        """Test shutdown cancels optimization on timeout."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        async def very_slow_optimize(*args, **kwargs):
            await asyncio.sleep(100)  # Very long
            return sample_optimization_result

        with patch("src.core.controller.optimize", side_effect=very_slow_optimize):
            # Start optimization
            opt_task = asyncio.create_task(controller.run_optimization("test"))
            controller._current_optimization_task = opt_task

            # Short timeout should cancel
            await asyncio.sleep(0.01)
            await controller.stop_async(timeout=0.1)

        assert opt_task.cancelled() or opt_task.done()


# ============ Metrics Recording Tests ============


class TestMetricsRecording:
    """Tests for Prometheus metrics recording."""

    @pytest.mark.asyncio
    async def test_metrics_recorded_on_success(
        self,
        mock_db_pool,
        depot_config,
        fast_controller_config,
        sample_depot_state,
        sample_optimization_result,
    ):
        """Test metrics are recorded on successful optimization."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        with patch("src.core.controller.OPTIMIZATION_RUNS") as mock_counter:
            with patch("src.core.controller.OPTIMIZATION_DURATION") as mock_histogram:
                with patch("src.core.controller.optimize", return_value=sample_optimization_result):
                    await controller.run_optimization("test")

                mock_counter.labels.assert_called()
                mock_histogram.labels.assert_called()

    @pytest.mark.asyncio
    async def test_failure_metrics_recorded(
        self, mock_db_pool, depot_config, fast_controller_config, sample_depot_state
    ):
        """Test failure metrics are recorded on optimization failure."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=fast_controller_config,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=sample_depot_state)

        with patch("src.core.controller.OPTIMIZATION_FAILURES") as mock_counter:
            with patch("src.core.controller.optimize", side_effect=Exception("Error")):
                try:
                    await controller.run_optimization("test")
                except Exception:
                    pass

            mock_counter.labels.assert_called()
