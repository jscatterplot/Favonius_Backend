"""Integration tests for error propagation across modules.

Reference: Development plan Phase 5, PRD.md#11-3-integration-tests
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest

from src.core.controller import DepotController
from src.core.controller_config import ControllerConfig
from src.core.models import DepotConfig, DepotState, OptimizationResult
from src.core.optimizer import InfeasibleModelError

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
def controller_config():
    """Controller configuration for testing."""
    return ControllerConfig(
        optimization_horizon_hours=24,
        hourly_optimization_start=7,
        hourly_optimization_end=23,
        optimization_timeout=30.0,
        trigger_cooldown_minutes=1,
        max_optimization_failures=3,
        dispatch_retry_attempts=1,
        dispatch_retry_delay_seconds=0.1,
        shutdown_timeout_seconds=2.0,
    )


# ============ Optimizer Error Propagation Tests ============


class TestOptimizerErrorPropagation:
    """Tests for optimizer error propagation."""

    @pytest.mark.asyncio
    async def test_optimizer_error_propagates_to_controller(
        self, mock_db_pool, depot_config, controller_config
    ):
        """Test that optimizer errors propagate to controller."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        # Mock state assembly to succeed
        n_t = 96
        mock_state = DepotState(
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

        controller.assembler.get_current_state = AsyncMock(return_value=mock_state)

        # Mock optimizer to raise error
        with patch("src.core.controller.optimize") as mock_optimize:
            mock_optimize.side_effect = InfeasibleModelError("Model infeasible")

            with pytest.raises(InfeasibleModelError):
                await controller.run_optimization("test")

    @pytest.mark.asyncio
    async def test_optimizer_timeout_recorded(self, mock_db_pool, depot_config, controller_config):
        """Test that optimizer timeout is properly recorded."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        n_t = 96
        mock_state = DepotState(
            vehicle_socs={"bus_1": 0.3},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={"bus_1": [True] * n_t},
            energy_requirements={"bus_1": 200.0},
            departure_times={"bus_1": 48},
            building_power=[50.0] * n_t,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=mock_state)

        with patch("src.core.controller.optimize") as mock_optimize:
            mock_optimize.side_effect = Exception("Time limit exceeded")

            try:
                await controller.run_optimization("test")
            except Exception:
                pass

            # Failure count should be incremented
            assert controller._optimization_failures >= 1


# ============ State Assembly Error Propagation Tests ============


class TestStateAssemblyErrorPropagation:
    """Tests for state assembly error propagation."""

    @pytest.mark.asyncio
    async def test_state_assembly_error_propagates(
        self, mock_db_pool, depot_config, controller_config
    ):
        """Test that state assembly errors propagate correctly."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        # Mock state assembly to fail
        controller.assembler.get_current_state = AsyncMock(
            side_effect=asyncpg.PostgresError("Database connection lost")
        )

        with pytest.raises(asyncpg.PostgresError):
            await controller.run_optimization("test")

    @pytest.mark.asyncio
    async def test_state_assembly_retry_on_transient_error(
        self, mock_db_pool, depot_config, controller_config
    ):
        """Test that state assembly retries on transient errors."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        n_t = 96
        mock_state = DepotState(
            vehicle_socs={"bus_1": 0.3},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={"bus_1": [True] * n_t},
            energy_requirements={"bus_1": 200.0},
            departure_times={"bus_1": 48},
            building_power=[50.0] * n_t,
        )

        # First call fails, second succeeds
        call_count = [0]

        async def mock_get_state(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise asyncpg.PostgresError("Transient error")
            return mock_state

        controller.assembler.get_current_state = mock_get_state

        with patch("src.core.controller.optimize") as mock_optimize:
            mock_optimize.return_value = OptimizationResult(
                run_id=uuid4(),
                schedule={"bus_1": {"charging_power": [0] * n_t, "soc": [0.5] * n_t}},
                battery_dispatch=[0] * n_t,
                grid_power=[0] * n_t,
                peak_demand=0,
                objective_value=0,
                solve_time=1.0,
                status="completed",
            )

            result = await controller.run_optimization("test")

            # Should have retried
            assert call_count[0] >= 2
            assert result.status == "completed"


# ============ OCPP Error Propagation Tests ============


class TestOCPPErrorPropagation:
    """Tests for OCPP error propagation."""

    @pytest.mark.asyncio
    async def test_ocpp_error_does_not_fail_optimization(
        self, mock_db_pool, depot_config, controller_config
    ):
        """Test that OCPP errors don't fail the optimization."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        mock_ocpp = MagicMock()
        mock_ocpp.get_charge_point.return_value = None  # No charger connected

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
            ocpp_server=mock_ocpp,
        )

        n_t = 96
        mock_state = DepotState(
            vehicle_socs={"bus_1": 0.3},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={"bus_1": [True] * n_t},
            energy_requirements={"bus_1": 200.0},
            departure_times={"bus_1": 48},
            building_power=[50.0] * n_t,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=mock_state)

        with patch("src.core.controller.optimize") as mock_optimize:
            mock_optimize.return_value = OptimizationResult(
                run_id=uuid4(),
                schedule={"bus_1": {"charging_power": [80] * n_t, "soc": [0.5] * n_t}},
                battery_dispatch=[0] * n_t,
                grid_power=[80] * n_t,
                peak_demand=100,
                objective_value=500,
                solve_time=2.0,
                status="completed",
            )

            # Should complete despite OCPP dispatch failing
            result = await controller.run_optimization("test")

            assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_ocpp_timeout_handled(self, mock_db_pool, depot_config, controller_config):
        """Test that OCPP timeout is handled gracefully."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        # Mock charge point that times out
        mock_cp = MagicMock()
        mock_cp.set_charging_profile = AsyncMock(side_effect=asyncio.TimeoutError("OCPP timeout"))

        mock_ocpp = MagicMock()
        mock_ocpp.get_charge_point.return_value = mock_cp

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
            ocpp_server=mock_ocpp,
        )

        n_t = 96
        result = OptimizationResult(
            run_id=uuid4(),
            schedule={"bus_1": {"charging_power": [80] * n_t, "soc": [0.5] * n_t}},
            battery_dispatch=[0] * n_t,
            grid_power=[80] * n_t,
            peak_demand=100,
            objective_value=500,
            solve_time=2.0,
            status="completed",
        )

        # Dispatch should handle timeout without crashing
        await controller._dispatch_commands(result)
        # Should complete (dispatch failures are logged, not raised)


# ============ Surrogate Model Error Propagation Tests ============


class TestSurrogateErrorPropagation:
    """Tests for surrogate model error propagation."""

    def test_surrogate_prediction_error_handled(self):
        """Test that surrogate model prediction errors are handled."""
        from src.core.surrogate import EnergySurrogateModel, PredictionInput

        # Create unfitted model
        model = EnergySurrogateModel(known_routes=["route_1"])

        # Prediction on unfitted model should raise
        with pytest.raises((ValueError, RuntimeError)):
            input_data = PredictionInput(
                bus_size="large",
                route_id="route_1",
                temp_avg_f=70.0,
                temp_max_f=80.0,
                temp_min_f=60.0,
                rain_inches=0.0,
                solar_radiation=500.0,
                is_school_day=True,
            )
            model.predict([input_data])


# ============ Controller Error Recovery Tests ============


class TestControllerErrorRecovery:
    """Tests for controller error recovery."""

    @pytest.mark.asyncio
    async def test_controller_recovers_from_transient_error(
        self, mock_db_pool, depot_config, controller_config
    ):
        """Test that controller recovers from transient errors."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        n_t = 96
        mock_state = DepotState(
            vehicle_socs={"bus_1": 0.3},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={"bus_1": [True] * n_t},
            energy_requirements={"bus_1": 200.0},
            departure_times={"bus_1": 48},
            building_power=[50.0] * n_t,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=mock_state)

        call_count = [0]

        def mock_optimize(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] <= 2:
                raise Exception("Transient error")
            return OptimizationResult(
                run_id=uuid4(),
                schedule={"bus_1": {"charging_power": [0] * n_t, "soc": [0.5] * n_t}},
                battery_dispatch=[0] * n_t,
                grid_power=[0] * n_t,
                peak_demand=0,
                objective_value=0,
                solve_time=1.0,
                status="completed",
            )

        with patch("src.core.controller.optimize", side_effect=mock_optimize):
            result = await controller.run_optimization("test")

            # Should have recovered after retries
            assert result.status == "completed"

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_on_persistent_errors(
        self, mock_db_pool, depot_config, controller_config
    ):
        """Test that circuit breaker opens on persistent errors."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller_config.max_optimization_failures = 2

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        n_t = 96
        mock_state = DepotState(
            vehicle_socs={"bus_1": 0.3},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={"bus_1": [True] * n_t},
            energy_requirements={"bus_1": 200.0},
            departure_times={"bus_1": 48},
            building_power=[50.0] * n_t,
        )

        controller.assembler.get_current_state = AsyncMock(return_value=mock_state)

        with patch("src.core.controller.optimize") as mock_optimize:
            mock_optimize.side_effect = Exception("Persistent error")

            # First failure
            try:
                await controller.run_optimization("test1")
            except Exception:
                pass

            # Second failure
            try:
                await controller.run_optimization("test2")
            except Exception:
                pass

        # Circuit breaker should be open
        assert controller._circuit_breaker_open is True


# ============ Database Error Propagation Tests ============


class TestDatabaseErrorPropagation:
    """Tests for database error propagation."""

    @pytest.mark.asyncio
    async def test_database_connection_error_propagates(
        self, mock_db_pool, depot_config, controller_config
    ):
        """Test that database connection errors propagate correctly."""
        pool, _ = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        # Mock database connection failure
        controller.assembler.get_current_state = AsyncMock(
            side_effect=asyncpg.PostgresConnectionError("Connection refused")
        )

        with pytest.raises(asyncpg.PostgresConnectionError):
            await controller.run_optimization("test")

    @pytest.mark.asyncio
    async def test_database_query_error_handled(
        self, mock_db_pool, depot_config, controller_config
    ):
        """Test that database query errors are handled."""
        pool, conn = mock_db_pool
        depot_id = str(uuid4())

        controller = DepotController(
            pool=pool,
            depot_id=depot_id,
            config=depot_config,
            controller_config=controller_config,
        )

        # Mock query failure
        controller.assembler.get_current_state = AsyncMock(
            side_effect=asyncpg.PostgresSyntaxError("Invalid SQL")
        )

        with pytest.raises(asyncpg.PostgresSyntaxError):
            await controller.run_optimization("test")
