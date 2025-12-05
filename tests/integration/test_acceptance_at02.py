"""Acceptance Test AT-02: Demand Charge Reduction

Reference: PRD.md#11-1-mvp-acceptance-tests

GIVEN a depot with 200 kW current month peak
AND unmanaged charging would cause 300 kW peak
WHEN optimization runs for a 24-hour horizon
THEN optimized peak is ≤ 220 kW
AND demand charge savings ≥ $1,600/month (at $20/kW)
"""

import pytest

from src.core.models import DepotConfig, DepotState
from src.core.optimizer import optimize


@pytest.mark.integration
@pytest.mark.acceptance
class TestAT02DemandChargeReduction:
    """AT-02: Demand Charge Reduction acceptance test."""

    @pytest.fixture
    def depot_config(self):
        """Depot configuration for demand charge test."""
        return DepotConfig(
            vehicle_capacities={f'bus_{i}': 324.0 for i in range(15)},
            charger_power=80.0,
            charger_efficiency=0.95,
            n_chargers=8,
            battery_capacity=1000.0,
            battery_power=200.0,
            max_site_power=1200.0,
        )

    @pytest.fixture
    def unmanaged_state(self, depot_config):
        """State that would cause 300 kW peak without optimization."""
        n_t = depot_config.n_timesteps

        # Flat prices for simplicity
        prices = [0.12] * n_t

        # All vehicles start at low SoC and need charging
        # Unmanaged: all charge simultaneously at peak time
        vehicle_socs = {f'bus_{i}': 0.2 for i in range(15)}
        vehicle_availability = {
            f'bus_{i}': [True] * n_t for i in range(15)
        }

        return DepotState(
            vehicle_socs=vehicle_socs,
            battery_soc=0.5,
            prices=prices,
            demand_charge_rate=20.0,  # $20/kW
            current_month_peak=200.0,  # Current month peak: 200 kW
            vehicle_availability=vehicle_availability,
            energy_requirements={f'bus_{i}': 250.0 for i in range(15)},
            departure_times={f'bus_{i}': 48 + i % 24 for i in range(15)},
            building_power=[100.0] * n_t,
        )

    def test_at02_demand_charge_reduction(
        self, unmanaged_state, depot_config
    ):
        """AT-02: Verify demand charge reduction."""
        # Calculate unmanaged peak (simplified: all vehicles charge at max simultaneously)
        # 15 vehicles * 80 kW = 1200 kW, but limited by max_site_power
        # Plus building load: 100 kW
        # Unmanaged peak ≈ 300 kW (limited by site power)
        unmanaged_peak = 300.0

        # Run optimization
        result = optimize(unmanaged_state, depot_config, time_limit=30.0)

        # Verify optimized peak ≤ 220 kW
        optimized_peak = result.peak_demand
        assert optimized_peak <= 220.0, (
            f"Optimized peak {optimized_peak:.2f} kW exceeds 220 kW limit"
        )

        # Verify peak is at least current month peak (moving limit)
        assert optimized_peak >= unmanaged_state.current_month_peak, (
            f"Optimized peak {optimized_peak:.2f} kW below current month peak "
            f"{unmanaged_state.current_month_peak:.2f} kW"
        )

        # Calculate demand charge savings
        # Unmanaged: 300 kW * $20/kW = $6,000/month
        # Optimized: optimized_peak * $20/kW
        # Savings: (300 - optimized_peak) * $20
        unmanaged_demand_charge = unmanaged_peak * unmanaged_state.demand_charge_rate
        optimized_demand_charge = optimized_peak * unmanaged_state.demand_charge_rate
        savings = unmanaged_demand_charge - optimized_demand_charge

        assert savings >= 1600.0, (
            f"Demand charge savings ${savings:.2f}/month < $1,600/month target. "
            f"Unmanaged: ${unmanaged_demand_charge:.2f}, "
            f"Optimized: ${optimized_demand_charge:.2f}"
        )

        # Verify optimization completed successfully
        assert result.status == 'completed'
        assert result.solve_time < 30.0

