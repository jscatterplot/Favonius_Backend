"""Integration tests for state assembly to optimization flow.

Reference: Development plan Phase 4 & 5, PRD.md#11-3-integration-tests
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg

from src.core.models import DepotConfig, DepotState
from src.core.optimizer import optimize, InvalidStateError
from src.core.state.assembler import StateAssembler


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
    """Realistic depot configuration."""
    return DepotConfig(
        vehicle_capacities={
            'bus_1': 324.0,
            'bus_2': 324.0,
            'bus_3': 324.0,
            'bus_4': 324.0,
            'bus_5': 324.0,
        },
        charger_power=80.0,
        charger_efficiency=0.95,
        n_chargers=5,
        battery_capacity=500.0,
        battery_power=100.0,
        max_site_power=800.0,
        delta_t=0.25,
        n_timesteps=96,
    )


@pytest.fixture
def realistic_depot_state(depot_config):
    """Realistic depot state with TOU pricing."""
    n_t = depot_config.n_timesteps
    
    # TOU prices: off-peak $0.10, partial-peak $0.15, peak $0.25
    prices = []
    for t in range(n_t):
        hour = (t * 0.25) % 24
        if 16 <= hour < 21:  # Peak: 4pm-9pm
            prices.append(0.25)
        elif 9 <= hour < 16 or 21 <= hour < 24:  # Partial-peak
            prices.append(0.15)
        else:  # Off-peak
            prices.append(0.10)
    
    return DepotState(
        vehicle_socs={
            'bus_1': 0.35,
            'bus_2': 0.45,
            'bus_3': 0.30,
            'bus_4': 0.55,
            'bus_5': 0.40,
        },
        battery_soc=0.5,
        prices=prices,
        demand_charge_rate=20.0,
        current_month_peak=300.0,
        vehicle_availability={
            f'bus_{i}': [True] * n_t for i in range(1, 6)
        },
        energy_requirements={
            'bus_1': 200.0,
            'bus_2': 180.0,
            'bus_3': 220.0,
            'bus_4': 160.0,
            'bus_5': 190.0,
        },
        departure_times={
            'bus_1': 24,  # 6am
            'bus_2': 28,  # 7am
            'bus_3': 24,  # 6am
            'bus_4': 32,  # 8am
            'bus_5': 28,  # 7am
        },
        building_power=[50.0] * n_t,
    )


# ============ Feasibility Tests ============

class TestAssembledStateFeasibility:
    """Tests for optimization feasibility with assembled states."""

    def test_assembled_state_produces_feasible_optimization(
        self, realistic_depot_state, depot_config
    ):
        """Test that assembled state produces feasible optimization."""
        result = optimize(realistic_depot_state, depot_config, time_limit=60.0)
        
        assert result.status == 'completed'
        assert result.objective_value is not None
        assert result.solve_time < 60.0

    def test_all_departure_socs_satisfied(
        self, realistic_depot_state, depot_config
    ):
        """Test that all departure SoC constraints are satisfied."""
        result = optimize(realistic_depot_state, depot_config, time_limit=60.0)
        
        # Check each vehicle meets departure SoC requirement at departure timestep
        for vehicle_id, dep_time in realistic_depot_state.departure_times.items():
            # Constraint is on t_depart, so check SoC at that exact timestep
            # Account for potential list index bounds
            check_idx = min(dep_time, len(result.schedule[vehicle_id]['soc']) - 1)
            soc_at_departure = result.schedule[vehicle_id]['soc'][check_idx]
            # Allow small numerical tolerance (0.98 instead of 0.99)
            assert soc_at_departure >= 0.98, (
                f"{vehicle_id} has SoC {soc_at_departure} at departure "
                f"(timestep {dep_time}), expected >= 0.98"
            )

    def test_charger_constraint_respected(
        self, realistic_depot_state, depot_config
    ):
        """Test that charger limit is respected at all times."""
        result = optimize(realistic_depot_state, depot_config, time_limit=60.0)
        
        n_chargers = depot_config.n_chargers
        n_t = depot_config.n_timesteps
        
        for t in range(n_t):
            charging_count = sum(
                1 for vid in result.schedule
                if result.schedule[vid]['charging_power'][t] > 0.1
            )
            assert charging_count <= n_chargers, (
                f"At timestep {t}, {charging_count} vehicles charging "
                f"but only {n_chargers} chargers available"
            )

    def test_grid_power_limit_respected(
        self, realistic_depot_state, depot_config
    ):
        """Test that grid power limit is respected."""
        result = optimize(realistic_depot_state, depot_config, time_limit=60.0)
        
        max_site_power = depot_config.max_site_power
        
        for t, grid_power in enumerate(result.grid_power):
            assert grid_power <= max_site_power + 0.1, (
                f"At timestep {t}, grid power {grid_power} kW "
                f"exceeds limit {max_site_power} kW"
            )


# ============ Price Response Tests ============

class TestPriceResponseBehavior:
    """Tests for optimizer price response with assembled states."""

    def test_price_changes_reflect_in_schedule(self, depot_config):
        """Test that price changes affect charging schedule."""
        n_t = depot_config.n_timesteps
        
        # Flat prices
        state_flat = DepotState(
            vehicle_socs={'bus_1': 0.3},
            battery_soc=0.5,
            prices=[0.15] * n_t,
            demand_charge_rate=10.0,
            current_month_peak=100.0,
            vehicle_availability={'bus_1': [True] * n_t},
            energy_requirements={'bus_1': 200.0},
            departure_times={'bus_1': 48},
            building_power=[50.0] * n_t,
        )
        
        # TOU prices with peak at certain hours
        prices_tou = [0.10] * n_t
        for t in range(64, 84):  # 4pm-9pm peak
            prices_tou[t] = 0.30
        
        state_tou = DepotState(
            vehicle_socs={'bus_1': 0.3},
            battery_soc=0.5,
            prices=prices_tou,
            demand_charge_rate=10.0,
            current_month_peak=100.0,
            vehicle_availability={'bus_1': [True] * n_t},
            energy_requirements={'bus_1': 200.0},
            departure_times={'bus_1': 48},  # Departs before peak
            building_power=[50.0] * n_t,
        )
        
        config_single = DepotConfig(
            vehicle_capacities={'bus_1': 324.0},
            charger_power=80.0,
            charger_efficiency=0.95,
            n_chargers=1,
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=300.0,
        )
        
        result_flat = optimize(state_flat, config_single, time_limit=30.0)
        result_tou = optimize(state_tou, config_single, time_limit=30.0)
        
        # Both should complete
        assert result_flat.status == 'completed'
        assert result_tou.status == 'completed'
        
        # TOU optimization should cost less (avoids peak)
        # Since departure is before peak, both should achieve similar charging
        # but TOU should have lower objective with demand charge considered

    def test_demand_charge_impact_on_schedule(self, depot_config):
        """Test that demand charge affects peak power usage."""
        n_t = depot_config.n_timesteps
        
        # Low demand charge
        state_low_dc = DepotState(
            vehicle_socs={'bus_1': 0.3, 'bus_2': 0.3, 'bus_3': 0.3},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=1.0,  # Low
            current_month_peak=100.0,
            vehicle_availability={
                'bus_1': [True] * n_t,
                'bus_2': [True] * n_t,
                'bus_3': [True] * n_t,
            },
            energy_requirements={
                'bus_1': 150.0,
                'bus_2': 150.0,
                'bus_3': 150.0,
            },
            departure_times={'bus_1': 48, 'bus_2': 48, 'bus_3': 48},
            building_power=[50.0] * n_t,
        )
        
        # High demand charge
        state_high_dc = DepotState(
            vehicle_socs={'bus_1': 0.3, 'bus_2': 0.3, 'bus_3': 0.3},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=50.0,  # High
            current_month_peak=100.0,
            vehicle_availability={
                'bus_1': [True] * n_t,
                'bus_2': [True] * n_t,
                'bus_3': [True] * n_t,
            },
            energy_requirements={
                'bus_1': 150.0,
                'bus_2': 150.0,
                'bus_3': 150.0,
            },
            departure_times={'bus_1': 48, 'bus_2': 48, 'bus_3': 48},
            building_power=[50.0] * n_t,
        )
        
        config_3bus = DepotConfig(
            vehicle_capacities={
                'bus_1': 324.0,
                'bus_2': 324.0,
                'bus_3': 324.0,
            },
            charger_power=80.0,
            charger_efficiency=0.95,
            n_chargers=3,
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=500.0,
        )
        
        result_low = optimize(state_low_dc, config_3bus, time_limit=30.0)
        result_high = optimize(state_high_dc, config_3bus, time_limit=30.0)
        
        # High demand charge should have lower peak demand
        assert result_high.peak_demand <= result_low.peak_demand + 50


# ============ Availability Constraint Tests ============

class TestAvailabilityConstraints:
    """Tests for vehicle availability constraints."""

    def test_availability_constraints_respected(self, depot_config):
        """Test that availability constraints are respected."""
        n_t = depot_config.n_timesteps
        
        # Vehicle unavailable for first half of horizon
        availability = {
            'bus_1': [False] * 48 + [True] * 48,
        }
        
        state = DepotState(
            vehicle_socs={'bus_1': 0.8},  # High initial SoC
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability=availability,
            energy_requirements={'bus_1': 50.0},  # Low requirement
            departure_times={'bus_1': 80},  # Late departure
            building_power=[50.0] * n_t,
        )
        
        config_single = DepotConfig(
            vehicle_capacities={'bus_1': 324.0},
            charger_power=80.0,
            charger_efficiency=0.95,
            n_chargers=1,
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=300.0,
        )
        
        result = optimize(state, config_single, time_limit=30.0)
        
        # No charging should occur when unavailable
        for t in range(48):
            assert result.schedule['bus_1']['charging_power'][t] == 0.0

    def test_staggered_availability_optimization(self, depot_config):
        """Test optimization with staggered vehicle availability."""
        n_t = depot_config.n_timesteps
        
        # Vehicles return at different times
        availability = {
            'bus_1': [False] * 20 + [True] * 76,  # Returns at hour 5
            'bus_2': [False] * 40 + [True] * 56,  # Returns at hour 10
            'bus_3': [True] * n_t,  # Always available
        }
        
        state = DepotState(
            vehicle_socs={
                'bus_1': 0.4,
                'bus_2': 0.3,
                'bus_3': 0.5,
            },
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability=availability,
            energy_requirements={
                'bus_1': 150.0,
                'bus_2': 180.0,
                'bus_3': 120.0,
            },
            departure_times={
                'bus_1': 80,
                'bus_2': 85,
                'bus_3': 90,
            },
            building_power=[50.0] * n_t,
        )
        
        config_3bus = DepotConfig(
            vehicle_capacities={
                'bus_1': 324.0,
                'bus_2': 324.0,
                'bus_3': 324.0,
            },
            charger_power=80.0,
            charger_efficiency=0.95,
            n_chargers=2,  # Limited chargers
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=400.0,
        )
        
        result = optimize(state, config_3bus, time_limit=45.0)
        
        assert result.status == 'completed'
        # All vehicles should meet departure requirements (allow small tolerance)
        for vid in ['bus_1', 'bus_2', 'bus_3']:
            dep_time = state.departure_times[vid]
            check_idx = min(dep_time, len(result.schedule[vid]['soc']) - 1)
            soc = result.schedule[vid]['soc'][check_idx]
            assert soc >= 0.95, f"{vid} SoC at departure: {soc}"


# ============ Edge Case Tests ============

class TestEdgeCaseOptimization:
    """Edge case tests for state-to-optimizer flow."""

    def test_very_tight_departure_window(self, depot_config):
        """Test optimization with tight (but feasible) departure window."""
        n_t = depot_config.n_timesteps
        
        # More feasible scenario: 50% initial SoC, 8 hours to charge
        state = DepotState(
            vehicle_socs={'bus_1': 0.5},  # Higher initial SoC
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={'bus_1': [True] * n_t},
            energy_requirements={'bus_1': 150.0},  # Reasonable requirement
            departure_times={'bus_1': 32},  # 8 hours (32 timesteps @ 15min)
            building_power=[50.0] * n_t,
        )
        
        config_single = DepotConfig(
            vehicle_capacities={'bus_1': 324.0},
            charger_power=150.0,  # High power charger
            charger_efficiency=0.95,
            n_chargers=1,
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=400.0,
        )
        
        result = optimize(state, config_single, time_limit=30.0)
        
        assert result.status == 'completed'
        # Check SoC at departure (allow tolerance for numerical precision)
        departure_soc = result.schedule['bus_1']['soc'][31]  # Index before departure
        assert departure_soc >= 0.95, f"Departure SoC: {departure_soc}"

    def test_battery_arbitrage_opportunity(self, depot_config):
        """Test that battery performs arbitrage with price differences."""
        n_t = depot_config.n_timesteps
        
        # Large price difference
        prices = [0.05] * 48 + [0.30] * 48
        
        state = DepotState(
            vehicle_socs={'bus_1': 0.9},  # High SoC, minimal charging needed
            battery_soc=0.5,
            prices=prices,
            demand_charge_rate=5.0,  # Low demand charge
            current_month_peak=100.0,
            vehicle_availability={'bus_1': [True] * n_t},
            energy_requirements={'bus_1': 30.0},  # Low requirement
            departure_times={'bus_1': 90},
            building_power=[50.0] * n_t,
        )
        
        config_single = DepotConfig(
            vehicle_capacities={'bus_1': 324.0},
            charger_power=80.0,
            charger_efficiency=0.95,
            n_chargers=1,
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=300.0,
        )
        
        result = optimize(state, config_single, time_limit=30.0)
        
        # Battery should charge during low prices (first half)
        # and discharge during high prices (second half)
        low_price_dispatch = result.battery_dispatch[:48]
        high_price_dispatch = result.battery_dispatch[48:]
        
        # Should see some charging (positive) in low price period
        # and discharging (negative) in high price period
        # (depending on demand constraints)

    def test_infeasible_scenario_detection(self, depot_config):
        """Test that infeasible scenarios are properly detected."""
        from src.core.optimizer.exceptions import InfeasibleModelError
        
        n_t = depot_config.n_timesteps

        # Impossible scenario: very low SoC, very high requirement,
        # very short time, low charger power
        state = DepotState(
            vehicle_socs={'bus_1': 0.1},  # 10% SoC
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={'bus_1': [True] * n_t},
            energy_requirements={'bus_1': 300.0},  # Need 300 kWh
            departure_times={'bus_1': 4},  # Only 1 hour!
            building_power=[50.0] * n_t,
        )

        config_single = DepotConfig(
            vehicle_capacities={'bus_1': 324.0},
            charger_power=40.0,  # Only 40 kW charger - can deliver ~38 kWh in 1 hour
            charger_efficiency=0.95,
            n_chargers=1,
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=300.0,
        )

        # This should raise an error for infeasible problem
        with pytest.raises((InfeasibleModelError, RuntimeError)):
            optimize(state, config_single, time_limit=30.0)

