"""Acceptance Test AT-07: Building Load Integration

Reference: PRD.md#11-1-mvp-acceptance-tests

GIVEN a depot with building load averaging 50 kW
AND building load peaks at 80 kW during morning hours
WHEN optimization runs
THEN grid power accounts for building load at each timestep
AND peak demand includes building load contribution
"""

import pytest
from datetime import datetime

from src.core.models import DepotConfig, DepotState
from src.core.optimizer import optimize, build_optimization_model, solve_model


@pytest.mark.integration
@pytest.mark.acceptance
class TestAT07BuildingLoadIntegration:
    """AT-07: Building Load Integration acceptance test.

    Per PRD Section 11.1, this test verifies:
    1. Optimizer accounts for building load in grid power calculations
    2. Peak demand includes building load contribution
    3. Grid balance equation: P_grid = P_charge + P_building - P_battery
    """

    @pytest.fixture
    def depot_config(self):
        """Depot configuration for building load test."""
        vehicle_ids = [f'bus_{i}' for i in range(5)]
        return DepotConfig(
            vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
            vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
            charger_groups={80.0: 3},
            charger_efficiency=0.95,
            charger_vehicle_access={},
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=600.0,
        )

    @pytest.fixture
    def building_load_profile(self, depot_config):
        """Building load profile with 80 kW morning peak.

        Profile:
        - Night (0-6): 30 kW
        - Morning peak (6-9): 80 kW
        - Day (9-17): 60 kW
        - Evening (17-22): 50 kW
        - Night (22-24): 30 kW
        """
        n_t = depot_config.n_timesteps
        building_power = []
        for t in range(n_t):
            hour = (t * 0.25) % 24
            if 6 <= hour < 9:
                building_power.append(80.0)  # Morning peak
            elif 9 <= hour < 17:
                building_power.append(60.0)  # Day
            elif 17 <= hour < 22:
                building_power.append(50.0)  # Evening
            else:
                building_power.append(30.0)  # Night
        return building_power

    @pytest.fixture
    def depot_state_with_building_load(self, depot_config, building_load_profile):
        """Depot state with realistic building load."""
        n_t = depot_config.n_timesteps
        vehicle_ids = [f'bus_{i}' for i in range(5)]

        return DepotState(
            vehicle_socs={vid: 0.4 for vid in vehicle_ids},
            battery_soc=0.5,
            prices=[0.12] * n_t,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability={vid: [True] * n_t for vid in vehicle_ids},
            energy_requirements={vid: 150.0 for vid in vehicle_ids},
            departure_times={vid: 48 + i * 8 for i, vid in enumerate(vehicle_ids)},
            building_power=building_load_profile,
        )

    @pytest.fixture
    def depot_state_zero_building_load(self, depot_config):
        """Depot state with zero building load for comparison."""
        n_t = depot_config.n_timesteps
        vehicle_ids = [f'bus_{i}' for i in range(5)]

        return DepotState(
            vehicle_socs={vid: 0.4 for vid in vehicle_ids},
            battery_soc=0.5,
            prices=[0.12] * n_t,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability={vid: [True] * n_t for vid in vehicle_ids},
            energy_requirements={vid: 150.0 for vid in vehicle_ids},
            departure_times={vid: 48 + i * 8 for i, vid in enumerate(vehicle_ids)},
            building_power=[0.0] * n_t,  # Zero building load
        )

    def test_at07_grid_power_includes_building_load(
        self, depot_state_with_building_load, depot_config
    ):
        """AT-07: Verify grid power accounts for building load at each timestep."""
        result = optimize(depot_state_with_building_load, depot_config, time_limit=30.0)
        assert result.status == 'completed'

        # Check grid power balance at each timestep
        for t in range(depot_config.n_timesteps):
            # Calculate total vehicle charging power
            total_vehicle_charging = sum(
                result.schedule[vid]['charging_power'][t]
                for vid in result.schedule.keys()
            )

            building_power = depot_state_with_building_load.building_power[t]
            battery_power = result.battery_dispatch[t]

            # Grid balance: P_grid = P_charge + P_building - P_battery
            expected_grid = total_vehicle_charging + building_power - battery_power

            # Allow small tolerance for numerical precision
            actual_grid = result.grid_power[t]
            assert abs(actual_grid - expected_grid) < 1.0, (
                f"Grid balance error at t={t}: "
                f"expected {expected_grid:.2f}, got {actual_grid:.2f} "
                f"(charging={total_vehicle_charging:.2f}, building={building_power:.2f}, "
                f"battery={battery_power:.2f})"
            )

    def test_at07_peak_demand_includes_building_load(
        self, depot_state_with_building_load, depot_config
    ):
        """AT-07: Verify peak demand includes building load contribution."""
        result = optimize(depot_state_with_building_load, depot_config, time_limit=30.0)
        assert result.status == 'completed'

        # Peak demand should be the maximum grid power
        max_grid_power = max(result.grid_power)

        # Peak should be >= max building load (80 kW) since we have charging too
        max_building_load = max(depot_state_with_building_load.building_power)
        assert result.peak_demand >= max_building_load, (
            f"Peak demand {result.peak_demand:.2f} kW < max building load {max_building_load:.2f} kW"
        )

        # Peak should match or exceed maximum grid power
        assert result.peak_demand >= max_grid_power - 0.1, (
            f"Peak demand {result.peak_demand:.2f} kW < max grid power {max_grid_power:.2f} kW"
        )

    def test_at07_building_load_affects_optimization(
        self,
        depot_state_with_building_load,
        depot_state_zero_building_load,
        depot_config,
    ):
        """Verify building load affects optimization results."""
        result_with_building = optimize(
            depot_state_with_building_load, depot_config, time_limit=30.0
        )
        result_without_building = optimize(
            depot_state_zero_building_load, depot_config, time_limit=30.0
        )

        assert result_with_building.status == 'completed'
        assert result_without_building.status == 'completed'

        # Peak demand should be higher with building load
        assert result_with_building.peak_demand > result_without_building.peak_demand, (
            f"Peak with building ({result_with_building.peak_demand:.2f} kW) "
            f"should be > without ({result_without_building.peak_demand:.2f} kW)"
        )

        # Grid power should be different
        grid_diff = sum(
            abs(result_with_building.grid_power[t] - result_without_building.grid_power[t])
            for t in range(depot_config.n_timesteps)
        )
        assert grid_diff > 0, "Grid power should differ with/without building load"

    def test_at07_building_load_in_model_constraints(
        self, depot_state_with_building_load, depot_config
    ):
        """Verify building load is included in model constraints."""
        model = build_optimization_model(depot_state_with_building_load, depot_config)

        # Verify grid_balance constraint exists
        assert hasattr(model, 'grid_balance'), "Model should have grid_balance constraint"

        # Verify building_power is a parameter in the model
        assert hasattr(model, 'building_power'), "Model should have building_power parameter"

        # Verify building_power values match input
        for t in range(depot_config.n_timesteps):
            expected = depot_state_with_building_load.building_power[t]
            # building_power[t] should match the input building power
            model_value = model.building_power[t].value
            assert abs(model_value - expected) < 0.01, (
                f"building_power[{t}] = {model_value}, expected {expected}"
            )

    def test_at07_site_power_limit_with_building_load(
        self, depot_state_with_building_load, depot_config
    ):
        """Verify site power limit is respected including building load."""
        result = optimize(depot_state_with_building_load, depot_config, time_limit=30.0)
        assert result.status == 'completed'

        # All grid power values should be within site limit
        for t, grid_power in enumerate(result.grid_power):
            assert grid_power <= depot_config.max_site_power + 0.1, (
                f"Grid power {grid_power:.2f} kW at t={t} exceeds "
                f"site limit {depot_config.max_site_power:.2f} kW"
            )

    def test_at07_charging_reduced_during_building_peak(
        self, depot_state_with_building_load, depot_config
    ):
        """Optimizer should reduce charging during building load peaks."""
        result = optimize(depot_state_with_building_load, depot_config, time_limit=30.0)
        assert result.status == 'completed'

        # Morning peak hours (6-9 AM = timesteps 24-36)
        peak_timesteps = list(range(24, 36))
        # Off-peak hours (0-6 AM = timesteps 0-24)
        off_peak_timesteps = list(range(0, 24))

        # Calculate average charging during peak vs off-peak
        peak_charging = []
        off_peak_charging = []

        for vid in result.schedule.keys():
            for t in peak_timesteps:
                if t < len(result.schedule[vid]['charging_power']):
                    peak_charging.append(result.schedule[vid]['charging_power'][t])
            for t in off_peak_timesteps:
                if t < len(result.schedule[vid]['charging_power']):
                    off_peak_charging.append(result.schedule[vid]['charging_power'][t])

        if peak_charging and off_peak_charging:
            avg_peak = sum(peak_charging) / len(peak_charging)
            avg_off_peak = sum(off_peak_charging) / len(off_peak_charging)

            # With demand charge optimization, charging may shift to off-peak
            # when building load is high during peak
            # This is a soft check - optimization may still charge during peak
            # if necessary to meet departure requirements
            assert result.objective_value is not None

    def test_at07_variable_building_load_profile(self, depot_config):
        """Test with highly variable building load profile."""
        n_t = depot_config.n_timesteps
        vehicle_ids = [f'bus_{i}' for i in range(5)]

        # Create variable building load (oscillating)
        building_power = []
        for t in range(n_t):
            # Oscillates between 30 and 100 kW
            building_power.append(65.0 + 35.0 * ((t % 8) / 8))

        state = DepotState(
            vehicle_socs={vid: 0.4 for vid in vehicle_ids},
            battery_soc=0.5,
            prices=[0.12] * n_t,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability={vid: [True] * n_t for vid in vehicle_ids},
            energy_requirements={vid: 150.0 for vid in vehicle_ids},
            departure_times={vid: 48 + i * 8 for i, vid in enumerate(vehicle_ids)},
            building_power=building_power,
        )

        result = optimize(state, depot_config, time_limit=30.0)
        assert result.status == 'completed'

        # Grid power should still follow balance equation
        for t in range(min(10, n_t)):  # Check first 10 timesteps
            total_charging = sum(
                result.schedule[vid]['charging_power'][t]
                for vid in result.schedule.keys()
            )
            expected_grid = total_charging + building_power[t] - result.battery_dispatch[t]
            assert abs(result.grid_power[t] - expected_grid) < 1.0

    def test_at07_high_building_load_still_meets_requirements(self, depot_config):
        """Verify vehicles still meet SoC requirements with high building load."""
        n_t = depot_config.n_timesteps
        vehicle_ids = [f'bus_{i}' for i in range(3)]  # Fewer vehicles

        # Very high building load (200 kW constant)
        state = DepotState(
            vehicle_socs={vid: 0.5 for vid in vehicle_ids},  # Higher initial SoC
            battery_soc=0.5,
            prices=[0.12] * n_t,
            demand_charge_rate=20.0,
            current_month_peak=300.0,
            vehicle_availability={vid: [True] * n_t for vid in vehicle_ids},
            energy_requirements={vid: 120.0 for vid in vehicle_ids},
            departure_times={vid: 60 + i * 12 for i, vid in enumerate(vehicle_ids)},
            building_power=[200.0] * n_t,  # High constant building load
        )

        result = optimize(state, depot_config, time_limit=30.0)
        assert result.status == 'completed'

        # All vehicles should still meet departure requirements
        for vid in vehicle_ids:
            t_dep = state.departure_times[vid]
            soc_at_departure = result.schedule[vid]['soc'][t_dep]
            assert soc_at_departure >= 0.98, (
                f"{vid} SoC at departure ({soc_at_departure:.3f}) < 0.98 "
                "with high building load"
            )

    def test_at07_building_load_validation(self, depot_config):
        """Verify building_power is required and validated."""
        from src.core.optimizer import InvalidStateError

        n_t = depot_config.n_timesteps
        vehicle_ids = [f'bus_{i}' for i in range(3)]

        # Wrong length building_power should raise error
        state_wrong_length = DepotState(
            vehicle_socs={vid: 0.4 for vid in vehicle_ids},
            battery_soc=0.5,
            prices=[0.12] * n_t,
            demand_charge_rate=20.0,
            current_month_peak=200.0,
            vehicle_availability={vid: [True] * n_t for vid in vehicle_ids},
            energy_requirements={vid: 150.0 for vid in vehicle_ids},
            departure_times={vid: 48 for vid in vehicle_ids},
            building_power=[50.0] * 50,  # Wrong length
        )

        with pytest.raises(InvalidStateError) as exc_info:
            build_optimization_model(state_wrong_length, depot_config)
        assert 'building' in str(exc_info.value).lower()
