"""Integration tests for Phase 4: State Assembler & Trigger Monitor.

Tests the full StateAssembler → TriggerMonitor → Controller pipeline.

Reference: Development plan Phase 4, PRD.md#11-3-integration-test-requirements
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest

from src.core.controller import DepotController
from src.core.models import DepotConfig, OptimizationResult
from src.core.state.assembler import StateAssembler
from src.core.state.triggers import TriggerConfig, TriggerMonitor


@pytest.fixture
def mock_db_pool():
    """Mock asyncpg connection pool."""
    pool = MagicMock(spec=asyncpg.Pool)
    return pool


@pytest.fixture
def depot_config():
    """Depot configuration for testing."""
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
        delta_t=0.25,
    )


@pytest.fixture
def depot_id():
    """Test depot ID."""
    return str(uuid4())


@pytest.fixture
def assembler(mock_db_pool, depot_id, depot_config):
    """StateAssembler instance for testing."""
    return StateAssembler(mock_db_pool, depot_id, depot_config)


class TestStateAssemblerTriggerMonitorIntegration:
    """Test StateAssembler and TriggerMonitor integration."""

    @pytest.mark.asyncio
    async def test_assembler_to_trigger_monitor_flow(
        self, assembler, mock_db_pool, depot_id, depot_config
    ):
        """Test StateAssembler provides data to TriggerMonitor correctly."""
        config = TriggerConfig(soc_deviation_threshold=0.05)
        callback = AsyncMock()

        monitor = TriggerMonitor(config, callback, assembler=assembler)

        # Mock assembler methods
        assembler._get_vehicle_socs = AsyncMock(return_value={"bus_1": 0.50, "bus_2": 0.70})
        assembler.get_current_state = AsyncMock()
        mock_state = MagicMock()
        mock_state.prices = [0.10] * 24
        assembler.config = depot_config
        assembler.get_current_state.return_value = mock_state

        # Set expected state
        monitor.update_expected_state({"bus_1": 0.60, "bus_2": 0.70}, {})

        # Get current SoCs via monitor
        current_socs = await monitor._get_current_vehicle_socs()

        assert current_socs == {"bus_1": 0.50, "bus_2": 0.70}
        assembler._get_vehicle_socs.assert_called_once()

        # Check deviation
        trigger = await monitor.check_soc_deviation(current_socs)
        assert trigger is not None
        assert "bus_1" in trigger

    @pytest.mark.asyncio
    async def test_expected_state_updates_propagate(self, assembler, mock_db_pool, depot_id):
        """Test expected state updates propagate correctly."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        # Initial expected state
        expected_socs = {"bus_1": 0.60, "bus_2": 0.70}
        expected_returns = {
            "bus_1": datetime.utcnow() + timedelta(hours=2),
            "bus_2": datetime.utcnow() + timedelta(hours=4),
        }

        monitor.update_expected_state(expected_socs, expected_returns)

        assert monitor.expected_socs == expected_socs
        assert monitor.expected_return_times == expected_returns

        # Update with new values
        new_socs = {"bus_1": 0.65, "bus_2": 0.75}
        monitor.update_expected_state(new_socs, {})

        assert monitor.expected_socs == new_socs
        assert monitor.expected_return_times == {}

    @pytest.mark.asyncio
    async def test_price_baseline_updates(self, assembler, mock_db_pool, depot_id, depot_config):
        """Test price baseline updates work correctly."""
        config = TriggerConfig()
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        # Initial prices
        base_time = datetime.utcnow()
        initial_prices = {
            base_time: 0.10,
            base_time + timedelta(hours=1): 0.12,
            base_time + timedelta(hours=2): 0.15,
        }

        monitor.update_prices(initial_prices)
        assert monitor.last_prices == initial_prices

        # Update with new prices
        new_prices = {
            base_time: 0.11,
            base_time + timedelta(hours=1): 0.13,
            base_time + timedelta(hours=2): 0.16,
        }

        monitor.update_prices(new_prices)
        assert monitor.last_prices == new_prices


class TestTriggerCooldownMechanism:
    """Test trigger cooldown prevents rapid re-optimization."""

    @pytest.mark.asyncio
    async def test_cooldown_prevents_rapid_triggers(self, assembler, mock_db_pool, depot_id):
        """Test cooldown mechanism prevents rapid-fire triggers."""
        config = TriggerConfig(check_interval_sec=0.1)  # Fast for testing
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        # Set up to trigger
        monitor.update_expected_state({"bus_1": 0.60}, {})
        assembler._get_vehicle_socs = AsyncMock(return_value={"bus_1": 0.50})
        assembler._get_current_prices = AsyncMock(return_value={})
        assembler._get_actual_return_times = AsyncMock(return_value={})

        # First trigger
        monitor._running = True
        monitor._last_trigger_time = None  # No previous trigger

        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.15)  # Let it run one iteration
        monitor.stop()

        try:
            await asyncio.wait_for(task, timeout=0.5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Callback should be called once
        assert callback.call_count == 1

        # Reset and try again immediately (should be blocked by cooldown)
        callback.reset_mock()
        monitor._last_trigger_time = datetime.utcnow()  # Just triggered
        monitor._running = True

        task2 = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.15)
        monitor.stop()

        try:
            await asyncio.wait_for(task2, timeout=0.5)
        except asyncio.TimeoutError:
            task2.cancel()
            try:
                await task2
            except asyncio.CancelledError:
                pass

        # Callback should NOT be called due to cooldown
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_cooldown_expires_after_timeout(self, assembler, mock_db_pool, depot_id):
        """Test cooldown expires and allows triggers after timeout."""
        config = TriggerConfig(check_interval_sec=0.1)
        callback = AsyncMock()
        monitor = TriggerMonitor(config, callback, assembler=assembler)

        # Set trigger cooldown to short duration for testing
        monitor._trigger_cooldown_sec = 0.2

        monitor.update_expected_state({"bus_1": 0.60}, {})
        assembler._get_vehicle_socs = AsyncMock(return_value={"bus_1": 0.50})
        assembler._get_current_prices = AsyncMock(return_value={})
        assembler._get_actual_return_times = AsyncMock(return_value={})

        # First trigger
        monitor._last_trigger_time = datetime.utcnow() - timedelta(seconds=0.3)
        monitor._running = True

        task = asyncio.create_task(monitor.run())
        await asyncio.sleep(0.15)
        monitor.stop()

        try:
            await asyncio.wait_for(task, timeout=0.5)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Callback should be called (cooldown expired)
        callback.assert_called_once()


class TestControllerIntegration:
    """Test full Controller → StateAssembler → TriggerMonitor integration."""

    @pytest.mark.asyncio
    async def test_controller_integrates_assembler_and_monitor(
        self, mock_db_pool, depot_id, depot_config
    ):
        """Test DepotController properly integrates StateAssembler and TriggerMonitor."""
        controller = DepotController(
            pool=mock_db_pool,
            depot_id=depot_id,
            config=depot_config,
            ocpp_server=None,
        )

        # Verify components are initialized
        assert isinstance(controller.assembler, StateAssembler)
        assert isinstance(controller.trigger_monitor, TriggerMonitor)
        assert controller.assembler.depot_id == depot_id
        assert controller.trigger_monitor.assembler == controller.assembler

    @pytest.mark.asyncio
    async def test_controller_updates_trigger_monitor_after_optimization(
        self, mock_db_pool, depot_id, depot_config
    ):
        """Test controller updates trigger monitor expected state after optimization."""
        controller = DepotController(
            pool=mock_db_pool,
            depot_id=depot_id,
            config=depot_config,
            ocpp_server=None,
        )

        # Mock optimization result
        mock_result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                "bus_1": {
                    "charging_power": [0, 80, 80, 0] * 24,
                    "soc": [0.5, 0.52, 0.54, 0.56] * 24,
                },
                "bus_2": {
                    "charging_power": [0, 0, 80, 80] * 24,
                    "soc": [0.6, 0.6, 0.62, 0.64] * 24,
                },
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[0.0] * 96,
            peak_demand=400.0,
            objective_value=1000.0,
            solve_time=5.0,
            status="completed",
        )

        # Mock assembler to return state
        mock_state = MagicMock()
        mock_state.prices = [0.10] * 96
        controller.assembler.get_current_state = AsyncMock(return_value=mock_state)

        # Mock optimizer
        with patch("src.core.controller.optimize", return_value=mock_result):
            with patch.object(controller, "_store_result", new_callable=AsyncMock):
                await controller.run_optimization("test")

                # Verify trigger monitor was updated
                assert len(controller.trigger_monitor.expected_socs) == 2
                assert "bus_1" in controller.trigger_monitor.expected_socs
                assert "bus_2" in controller.trigger_monitor.expected_socs

                # Verify prices were updated
                assert len(controller.trigger_monitor.last_prices) > 0

    @pytest.mark.asyncio
    async def test_trigger_callback_invokes_optimization(
        self, mock_db_pool, depot_id, depot_config
    ):
        """Test trigger callback correctly invokes re-optimization."""
        controller = DepotController(
            pool=mock_db_pool,
            depot_id=depot_id,
            config=depot_config,
            ocpp_server=None,
        )

        # Mock optimization
        mock_result = OptimizationResult(
            run_id=uuid4(),
            schedule={"bus_1": {"charging_power": [0] * 96, "soc": [0.5] * 96}},
            battery_dispatch=[0.0] * 96,
            grid_power=[0.0] * 96,
            peak_demand=100.0,
            objective_value=500.0,
            solve_time=2.0,
            status="completed",
        )

        mock_state = MagicMock()
        mock_state.prices = [0.10] * 96
        controller.assembler.get_current_state = AsyncMock(return_value=mock_state)

        with patch("src.core.controller.optimize", return_value=mock_result):
            with patch.object(controller, "_store_result", new_callable=AsyncMock):
                # Trigger callback
                await controller._handle_trigger("SoC deviation: bus_1")

                # Verify optimization was called
                assert controller.last_result is not None
                assert controller.last_run_time is not None


class TestEndToEndFlow:
    """Test complete end-to-end flow."""

    @pytest.mark.asyncio
    async def test_full_pipeline_state_to_trigger_to_optimization(
        self, mock_db_pool, depot_id, depot_config
    ):
        """Test complete pipeline: State → Trigger → Optimization."""
        controller = DepotController(
            pool=mock_db_pool,
            depot_id=depot_id,
            config=depot_config,
            ocpp_server=None,
        )

        # Mock database responses
        mock_conn = AsyncMock()
        mock_db_pool.acquire.return_value.__aenter__.return_value = mock_conn

        # Mock telemetry (vehicle SoCs)
        mock_telemetry_row = MagicMock()
        mock_telemetry_row.__getitem__.side_effect = lambda k: {
            "vehicle_id": "bus_1",
            "soc": 0.50,
        }[k]

        # Mock prices
        base_time = datetime.utcnow()
        mock_price_row = MagicMock()
        mock_price_row.__getitem__.side_effect = lambda k, t=base_time, p=0.10: {
            "time": t,
            "price_per_kwh": p,
        }[k]

        # Mock schedules
        mock_schedule_row = {
            "vehicle_id": "bus_1",
            "departure_time": base_time + timedelta(hours=6),
            "return_time": base_time + timedelta(hours=10),
            "estimated_energy_kwh": 150.0,
            "route_id": "route_1",
        }

        # Mock optimization_runs (for peak demand)
        mock_peak_row = MagicMock()
        mock_peak_row.__getitem__.side_effect = lambda k: {"peak": 200.0}[k]

        # Mock depots (for demand charge rate)
        mock_depot_row = MagicMock()
        mock_depot_row.__getitem__.side_effect = lambda k: {"demand_charge_rate_kw": 20.0}[k]

        # Set up fetch side effects
        mock_conn.fetch.side_effect = [
            [mock_telemetry_row],  # _get_vehicle_socs
            [mock_price_row] * 24,  # _get_prices
            [mock_schedule_row],  # _get_schedules
        ]

        mock_conn.fetchrow.side_effect = [
            mock_peak_row,  # _get_current_month_peak
            mock_depot_row,  # _get_demand_charge_rate
        ]

        # Mock optimizer
        mock_result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                "bus_1": {
                    "charging_power": [0, 80, 80, 0] * 24,
                    "soc": [0.5, 0.52, 0.54, 0.56] * 24,
                }
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[0.0] * 96,
            peak_demand=300.0,
            objective_value=800.0,
            solve_time=3.0,
            status="completed",
        )

        with patch("src.core.controller.optimize", return_value=mock_result):
            with patch.object(controller, "_store_result", new_callable=AsyncMock):
                # Run optimization
                result = await controller.run_optimization("test")

                # Verify state was assembled
                assert result is not None
                assert result.status == "completed"

                # Verify trigger monitor was updated
                assert len(controller.trigger_monitor.expected_socs) > 0
                assert len(controller.trigger_monitor.last_prices) > 0
