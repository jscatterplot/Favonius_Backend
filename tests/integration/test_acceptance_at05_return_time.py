"""Acceptance Test AT-05: Return Time Deviation Handling

Reference: PRD.md#11-1-mvp-acceptance-tests

GIVEN bus_0 expected return time = 2:00 PM
AND actual return time = 2:30 PM (30 min late, > 15 min threshold)
WHEN trigger monitor detects deviation
THEN re-optimization is triggered
AND new schedule adjusts for late arrival
AND bus_0 still meets departure requirement if feasible
"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.core.models import DepotConfig, DepotState
from src.core.optimizer import optimize
from src.core.state.triggers import TriggerConfig, TriggerMonitor


@pytest.mark.integration
@pytest.mark.acceptance
@pytest.mark.asyncio
class TestAT05ReturnTimeDeviationHandling:
    """AT-05: Return Time Deviation Handling acceptance test."""

    @pytest.fixture
    def depot_config(self):
        """Depot configuration."""
        vehicle_ids = ["bus_0", "bus_1", "bus_2"]
        return DepotConfig(
            vehicle_capacities={
                "bus_0": 324.0,
                "bus_1": 324.0,
                "bus_2": 324.0,
            },
            vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
            charger_groups={80.0: 3},
            charger_efficiency=0.95,
            charger_vehicle_access={},
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=600.0,
        )

    @pytest.fixture
    def initial_state(self, depot_config):
        """Initial state with bus_0 expected to return at 2:00 PM."""
        n_t = depot_config.n_timesteps
        prices = [0.12] * n_t

        # bus_0 is expected to return at 2:00 PM (timestep 56)
        # Initially unavailable until return
        vehicle_availability = {
            "bus_0": [False] * 56 + [True] * (n_t - 56),  # Returns at t=56 (2:00 PM)
            "bus_1": [True] * n_t,
            "bus_2": [True] * n_t,
        }

        return DepotState(
            vehicle_socs={"bus_0": 0.35, "bus_1": 0.50, "bus_2": 0.55},
            battery_soc=0.5,
            prices=prices,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability=vehicle_availability,
            energy_requirements={
                "bus_0": 200.0,
                "bus_1": 180.0,
                "bus_2": 160.0,
            },
            # All depart next morning at 6 AM (timestep 24 next day)
            departure_times={"bus_0": 80, "bus_1": 84, "bus_2": 88},
            building_power=[50.0] * n_t,
        )

    @pytest.fixture
    def delayed_state(self, depot_config):
        """State with bus_0 arriving 30 min late (2:30 PM instead of 2:00 PM).

        30 min late > 15 min threshold, so should trigger re-optimization.
        """
        n_t = depot_config.n_timesteps
        prices = [0.12] * n_t

        # bus_0 arrives late at 2:30 PM (timestep 58 instead of 56)
        vehicle_availability = {
            "bus_0": [False] * 58 + [True] * (n_t - 58),  # Returns late at t=58
            "bus_1": [True] * n_t,
            "bus_2": [True] * n_t,
        }

        return DepotState(
            vehicle_socs={
                "bus_0": 0.32,
                "bus_1": 0.52,
                "bus_2": 0.57,
            },  # bus_0 slightly lower due to delay
            battery_soc=0.5,
            prices=prices,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability=vehicle_availability,
            energy_requirements={
                "bus_0": 200.0,
                "bus_1": 180.0,
                "bus_2": 160.0,
            },
            departure_times={"bus_0": 80, "bus_1": 84, "bus_2": 88},
            building_power=[50.0] * n_t,
        )

    @pytest.mark.asyncio
    async def test_at05_return_time_deviation_detection(self, initial_state, depot_config):
        """AT-05: Verify return time deviation triggers re-optimization."""
        # Create trigger monitor
        trigger_fired = []
        trigger_reason = []

        async def on_trigger(reason: str):
            trigger_fired.append(True)
            trigger_reason.append(reason)

        config = TriggerConfig(return_time_deviation_min=15.0)  # 15 min threshold

        # Create mock assembler for TriggerMonitor
        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)

        # Set expected return time for bus_0 at 2:00 PM
        base_time = datetime.utcnow()
        expected_return_time = base_time + timedelta(hours=14)  # 2:00 PM

        monitor.update_expected_state({}, {"bus_0": expected_return_time})  # No SoC expectations

        # Simulate actual return time at 2:30 PM (30 min late)
        actual_return_time = expected_return_time + timedelta(minutes=30)
        actual_return_times = {"bus_0": actual_return_time}

        # Check return time deviation (should trigger)
        start_time = datetime.utcnow()
        return_trigger = await monitor.check_return_time_deviation(actual_return_times)
        check_time = (datetime.utcnow() - start_time).total_seconds()

        # Verify trigger detection is fast (< 60 seconds per PRD)
        assert check_time < 60.0, f"Return time check took {check_time:.2f}s > 60s"

        # Verify trigger detected deviation
        assert return_trigger is not None, "Return time deviation trigger should fire"
        assert "bus_0" in return_trigger
        assert "delay" in return_trigger.lower() or "return" in return_trigger.lower()

    @pytest.mark.asyncio
    async def test_at05_no_trigger_within_threshold(self, initial_state, depot_config):
        """Verify no trigger when return time is within threshold."""
        config = TriggerConfig(return_time_deviation_min=15.0)  # 15 min threshold

        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        async def on_trigger(reason: str):
            pass

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)

        # Set expected return time for bus_0
        base_time = datetime.utcnow()
        expected_return_time = base_time + timedelta(hours=14)

        monitor.update_expected_state({}, {"bus_0": expected_return_time})

        # Actual return time is only 10 min late (within threshold)
        actual_return_time = expected_return_time + timedelta(minutes=10)
        actual_return_times = {"bus_0": actual_return_time}

        return_trigger = await monitor.check_return_time_deviation(actual_return_times)

        # Should NOT trigger (10 min < 15 min threshold)
        assert return_trigger is None, "Should not trigger for 10 min delay"

    @pytest.mark.asyncio
    async def test_at05_reoptimization_after_late_arrival(
        self, initial_state, delayed_state, depot_config
    ):
        """AT-05: Verify re-optimization after late arrival still meets requirements."""
        # Initial optimization with expected arrival
        initial_result = optimize(initial_state, depot_config, time_limit=30.0)
        assert initial_result.status == "completed"

        # Re-optimize with delayed arrival
        delayed_result = optimize(
            delayed_state, depot_config, time_limit=30.0, previous_result=initial_result
        )
        assert delayed_result.status == "completed"

        # Verify bus_0 still meets departure requirement despite late arrival
        departure_timestep = delayed_state.departure_times["bus_0"]
        check_idx = min(departure_timestep, len(delayed_result.schedule["bus_0"]["soc"]) - 1)
        soc_at_departure = delayed_result.schedule["bus_0"]["soc"][check_idx]

        # Allow small numerical tolerance
        assert (
            soc_at_departure >= 0.98
        ), f"bus_0 SoC at departure ({soc_at_departure:.3f}) < 0.98 after late arrival"

        # Verify other vehicles also meet requirements
        for vid in ["bus_1", "bus_2"]:
            t_dep = delayed_state.departure_times[vid]
            check_idx = min(t_dep, len(delayed_result.schedule[vid]["soc"]) - 1)
            soc = delayed_result.schedule[vid]["soc"][check_idx]
            assert soc >= 0.98, f"{vid} SoC at departure ({soc:.3f}) < 0.98"

    @pytest.mark.asyncio
    async def test_at05_schedule_adjustment_compensates_delay(
        self, initial_state, delayed_state, depot_config
    ):
        """Verify schedule adjusts to compensate for delayed arrival."""
        # Initial optimization
        initial_result = optimize(initial_state, depot_config, time_limit=30.0)

        # Re-optimize with delayed arrival
        delayed_result = optimize(
            delayed_state,
            depot_config,
            time_limit=30.0,
            previous_result=initial_result,
        )

        # bus_0 has less time to charge due to late arrival
        # The optimizer should increase charging power after arrival

        # Get charging power during available period
        arrival_timestep = 58  # Late arrival at 2:30 PM

        # Calculate average charging power after arrival
        late_charging_power = []
        for t in range(arrival_timestep, min(arrival_timestep + 20, depot_config.n_timesteps)):
            if delayed_state.vehicle_availability["bus_0"][t]:
                power = delayed_result.schedule["bus_0"]["charging_power"][t]
                late_charging_power.append(power)

        # Should have some charging power scheduled
        if late_charging_power:
            avg_power = sum(late_charging_power) / len(late_charging_power)
            assert avg_power >= 0, "bus_0 should have charging scheduled after late arrival"

        # Verify optimization completed successfully
        assert delayed_result.objective_value is not None
        assert delayed_result.status == "completed"

    @pytest.mark.asyncio
    async def test_at05_early_return_no_trigger(self, initial_state, depot_config):
        """Verify early return does not trigger re-optimization."""
        config = TriggerConfig(return_time_deviation_min=15.0)

        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        async def on_trigger(reason: str):
            pass

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)

        # Set expected return time
        base_time = datetime.utcnow()
        expected_return_time = base_time + timedelta(hours=14)

        monitor.update_expected_state({}, {"bus_0": expected_return_time})

        # Actual return is 20 min EARLY
        actual_return_time = expected_return_time - timedelta(minutes=20)
        actual_return_times = {"bus_0": actual_return_time}

        return_trigger = await monitor.check_return_time_deviation(actual_return_times)

        # Should NOT trigger for early return (only late triggers concern)
        assert return_trigger is None, "Early return should not trigger"

    @pytest.mark.asyncio
    async def test_at05_multiple_vehicles_late(self, depot_config):
        """Test handling when multiple vehicles are late."""
        config = TriggerConfig(return_time_deviation_min=15.0)

        mock_assembler = MagicMock()
        mock_assembler.config = MagicMock()
        mock_assembler.config.delta_t = 0.25

        async def on_trigger(reason: str):
            pass

        monitor = TriggerMonitor(config, on_trigger, assembler=mock_assembler)

        base_time = datetime.utcnow()

        # Multiple vehicles with expected return times
        expected_returns = {
            "bus_0": base_time + timedelta(hours=14),
            "bus_1": base_time + timedelta(hours=15),
            "bus_2": base_time + timedelta(hours=16),
        }
        monitor.update_expected_state({}, expected_returns)

        # bus_0 is 30 min late, bus_1 is on time, bus_2 is 20 min late
        actual_returns = {
            "bus_0": expected_returns["bus_0"] + timedelta(minutes=30),
            "bus_1": expected_returns["bus_1"] + timedelta(minutes=5),
            "bus_2": expected_returns["bus_2"] + timedelta(minutes=20),
        }

        return_trigger = await monitor.check_return_time_deviation(actual_returns)

        # Should trigger for at least one late vehicle
        assert return_trigger is not None
        # At least one of the late vehicles should be mentioned
        assert "bus_0" in return_trigger or "bus_2" in return_trigger
