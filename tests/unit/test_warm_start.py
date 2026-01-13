"""Unit tests for warm-start optimization functionality.

Reference: Development plan Step 1.2, PRD Section 8.3
"""

import pytest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pyomo.environ as pyo

from src.core.models import DepotConfig, DepotState, OptimizationResult
from src.core.optimizer.warm_start import warm_start_model


# ============ Fixtures ============

@pytest.fixture
def simple_config():
    """Simple depot configuration."""
    vehicle_ids = ['bus_1', 'bus_2']
    return DepotConfig(
        vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
        vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
        charger_groups={80.0: 2},
        charger_efficiency=0.95,
        charger_vehicle_access={},
        battery_capacity=500.0,
        battery_power=100.0,
        max_site_power=500.0,
        n_timesteps=96,
    )


@pytest.fixture
def simple_state():
    """Simple depot state."""
    n_t = 96
    return DepotState(
        vehicle_socs={'bus_1': 0.3, 'bus_2': 0.5},
        battery_soc=0.5,
        prices=[0.10] * n_t,
        demand_charge_rate=15.0,
        current_month_peak=100.0,
        vehicle_availability={
            'bus_1': [True] * n_t,
            'bus_2': [True] * n_t,
        },
        energy_requirements={'bus_1': 200.0, 'bus_2': 150.0},
        departure_times={'bus_1': 48, 'bus_2': 60},
        building_power=[50.0] * n_t,
    )


@pytest.fixture
def previous_result():
    """Previous optimization result."""
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
def mock_model(simple_config):
    """Mock Pyomo model with necessary variables."""
    model = MagicMock(spec=pyo.ConcreteModel)
    model.T = range(simple_config.n_timesteps)
    model.B = ['bus_1', 'bus_2']
    
    # Mock variables
    model.P_charge = {}
    model.y_charge = {}
    model.SoC = {}
    model.P_batt = {}
    model.SoC_batt = {}
    model.P_grid = {}
    
    for b in model.B:
        for t in model.T:
            model.P_charge[b, t] = MagicMock()
            model.P_charge[b, t].value = None
            model.y_charge[b, t] = MagicMock()
            model.y_charge[b, t].value = None
            model.SoC[b, t] = MagicMock()
            model.SoC[b, t].value = None
    
    for t in model.T:
        model.P_batt[t] = MagicMock()
        model.P_batt[t].value = None
        model.SoC_batt[t] = MagicMock()
        model.SoC_batt[t].value = None
        model.P_grid[t] = MagicMock()
        model.P_grid[t].value = None
    
    model.P_peak = MagicMock()
    model.P_peak.value = None
    
    return model


# ============ Basic Warm-Start Tests ============

class TestWarmStartBasic:
    """Basic warm-start functionality tests."""

    def test_warm_start_initializes_charging_power(
        self, mock_model, previous_result, simple_state, simple_config
    ):
        """Test that warm-start initializes charging power values."""
        warm_start_model(mock_model, previous_result, simple_state, simple_config)
        
        # Check that P_charge values were set
        assert mock_model.P_charge['bus_1', 0].value == 80.0
        assert mock_model.P_charge['bus_1', 1].value == 80.0
        assert mock_model.P_charge['bus_2', 0].value == 70.0

    def test_warm_start_initializes_binary_indicator(
        self, mock_model, previous_result, simple_state, simple_config
    ):
        """Test that warm-start sets y_charge correctly."""
        warm_start_model(mock_model, previous_result, simple_state, simple_config)
        
        # y_charge should be 1 for power > 0.1
        assert mock_model.y_charge['bus_1', 0].value == 1
        assert mock_model.y_charge['bus_1', 3].value == 0  # power was 0
        assert mock_model.y_charge['bus_2', 2].value == 0  # power was 0

    def test_warm_start_initializes_soc(
        self, mock_model, previous_result, simple_state, simple_config
    ):
        """Test that warm-start initializes SoC values."""
        warm_start_model(mock_model, previous_result, simple_state, simple_config)
        
        # Check SoC values were set
        assert mock_model.SoC['bus_1', 0].value == 0.3
        assert mock_model.SoC['bus_2', 0].value == 0.5

    def test_warm_start_initializes_battery_dispatch(
        self, mock_model, previous_result, simple_state, simple_config
    ):
        """Test that warm-start initializes battery dispatch."""
        warm_start_model(mock_model, previous_result, simple_state, simple_config)
        
        # Check battery dispatch values
        assert mock_model.P_batt[0].value == 10.0
        assert mock_model.P_batt[1].value == -5.0

    def test_warm_start_initializes_peak_demand(
        self, mock_model, previous_result, simple_state, simple_config
    ):
        """Test that warm-start initializes peak demand."""
        warm_start_model(mock_model, previous_result, simple_state, simple_config)
        
        assert mock_model.P_peak.value == 200.0


# ============ Partial Solution Tests ============

class TestWarmStartPartialSolution:
    """Tests for warm-starting with partial previous solutions."""

    def test_warm_start_partial_solution_missing_vehicle(
        self, mock_model, simple_state, simple_config
    ):
        """Test warm-start when previous result is missing a vehicle."""
        # Previous result only has bus_1
        partial_result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {
                    'charging_power': [80.0, 60.0] * 48,
                    'soc': [0.3, 0.35] * 48,
                },
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[100.0] * 96,
            peak_demand=150.0,
            objective_value=800.0,
            solve_time=4.0,
            status='completed',
        )
        
        warm_start_model(mock_model, partial_result, simple_state, simple_config)
        
        # bus_1 should be initialized from previous result
        assert mock_model.P_charge['bus_1', 0].value == 80.0
        
        # bus_2 should be initialized with defaults (new vehicle)
        assert mock_model.P_charge['bus_2', 0].value == 0.0
        assert mock_model.y_charge['bus_2', 0].value == 0
        assert mock_model.SoC['bus_2', 0].value == 0.5  # current SoC

    def test_warm_start_with_new_vehicle_added(
        self, simple_config
    ):
        """Test warm-start when a new vehicle is added to fleet."""
        # State has 3 vehicles, previous result only had 2
        n_t = 96
        state_with_new_vehicle = DepotState(
            vehicle_socs={'bus_1': 0.3, 'bus_2': 0.5, 'bus_3': 0.4},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={
                'bus_1': [True] * n_t,
                'bus_2': [True] * n_t,
                'bus_3': [True] * n_t,
            },
            energy_requirements={'bus_1': 200.0, 'bus_2': 150.0, 'bus_3': 180.0},
            departure_times={'bus_1': 48, 'bus_2': 60, 'bus_3': 72},
            building_power=[50.0] * n_t,
        )
        
        previous_result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {'charging_power': [80.0] * 96, 'soc': [0.3] * 96},
                'bus_2': {'charging_power': [70.0] * 96, 'soc': [0.5] * 96},
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[150.0] * 96,
            peak_demand=180.0,
            objective_value=900.0,
            solve_time=5.0,
            status='completed',
        )
        
        # Create model with 3 vehicles
        model = MagicMock(spec=pyo.ConcreteModel)
        model.T = range(simple_config.n_timesteps)
        model.B = ['bus_1', 'bus_2', 'bus_3']
        
        model.P_charge = {}
        model.y_charge = {}
        model.SoC = {}
        model.P_batt = {}
        model.SoC_batt = {}
        model.P_grid = {}
        
        for b in model.B:
            for t in model.T:
                model.P_charge[b, t] = MagicMock()
                model.P_charge[b, t].value = None
                model.y_charge[b, t] = MagicMock()
                model.y_charge[b, t].value = None
                model.SoC[b, t] = MagicMock()
                model.SoC[b, t].value = None
        
        for t in model.T:
            model.P_batt[t] = MagicMock()
            model.P_batt[t].value = None
            model.SoC_batt[t] = MagicMock()
            model.SoC_batt[t].value = None
            model.P_grid[t] = MagicMock()
            model.P_grid[t].value = None
        
        model.P_peak = MagicMock()
        model.P_peak.value = None
        
        warm_start_model(model, previous_result, state_with_new_vehicle, simple_config)
        
        # bus_3 should be initialized with defaults
        assert model.P_charge['bus_3', 0].value == 0.0
        assert model.y_charge['bus_3', 0].value == 0
        assert model.SoC['bus_3', 0].value == 0.4  # current SoC


# ============ Invalid Values Tests ============

class TestWarmStartInvalidValues:
    """Tests for handling invalid values in previous solutions."""

    def test_warm_start_clamps_soc_above_bounds(
        self, mock_model, simple_state, simple_config
    ):
        """Test that SoC values above 1.0 are clamped."""
        result_with_high_soc = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {
                    'charging_power': [80.0] * 96,
                    'soc': [1.5] * 96,  # Invalid: > 1.0
                },
                'bus_2': {
                    'charging_power': [70.0] * 96,
                    'soc': [0.5] * 96,
                },
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[100.0] * 96,
            peak_demand=150.0,
            objective_value=800.0,
            solve_time=4.0,
            status='completed',
        )
        
        warm_start_model(mock_model, result_with_high_soc, simple_state, simple_config)
        
        # SoC should be clamped to 1.0
        assert mock_model.SoC['bus_1', 0].value == 1.0

    def test_warm_start_clamps_soc_below_bounds(
        self, mock_model, simple_state, simple_config
    ):
        """Test that SoC values below 0.1 are clamped."""
        result_with_low_soc = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {
                    'charging_power': [80.0] * 96,
                    'soc': [0.05] * 96,  # Invalid: < 0.1
                },
                'bus_2': {
                    'charging_power': [70.0] * 96,
                    'soc': [0.5] * 96,
                },
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[100.0] * 96,
            peak_demand=150.0,
            objective_value=800.0,
            solve_time=4.0,
            status='completed',
        )
        
        warm_start_model(mock_model, result_with_low_soc, simple_state, simple_config)
        
        # SoC should be clamped to 0.1
        assert mock_model.SoC['bus_1', 0].value == 0.1

    def test_warm_start_clamps_battery_power(
        self, mock_model, simple_state, simple_config
    ):
        """Test that battery power is clamped to bounds."""
        result_with_high_batt = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {'charging_power': [80.0] * 96, 'soc': [0.3] * 96},
                'bus_2': {'charging_power': [70.0] * 96, 'soc': [0.5] * 96},
            },
            battery_dispatch=[500.0] * 96,  # Invalid: > battery_power (100)
            grid_power=[100.0] * 96,
            peak_demand=150.0,
            objective_value=800.0,
            solve_time=4.0,
            status='completed',
        )
        
        warm_start_model(mock_model, result_with_high_batt, simple_state, simple_config)
        
        # Battery power should be clamped to 100.0
        assert mock_model.P_batt[0].value == 100.0

    def test_warm_start_handles_none_values_in_schedule(
        self, mock_model, simple_state, simple_config
    ):
        """Test that None values in schedule are handled."""
        result_with_none = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {
                    'charging_power': [80.0, None, 60.0] + [0.0] * 93,
                    'soc': [0.3, None, 0.4] + [0.5] * 93,
                },
                'bus_2': {'charging_power': [70.0] * 96, 'soc': [0.5] * 96},
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[100.0] * 96,
            peak_demand=150.0,
            objective_value=800.0,
            solve_time=4.0,
            status='completed',
        )
        
        # Should not raise exception
        warm_start_model(mock_model, result_with_none, simple_state, simple_config)
        
        # Values with None should be handled gracefully
        assert mock_model.P_charge['bus_1', 0].value == 80.0
        # t=1 with None should default
        assert mock_model.P_charge['bus_1', 1].value == 0.0

    def test_warm_start_handles_negative_grid_power(
        self, mock_model, simple_state, simple_config
    ):
        """Test that negative grid power is clamped to zero."""
        result_with_neg_grid = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {'charging_power': [80.0] * 96, 'soc': [0.3] * 96},
                'bus_2': {'charging_power': [70.0] * 96, 'soc': [0.5] * 96},
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[-50.0] * 96,  # Invalid negative
            peak_demand=150.0,
            objective_value=800.0,
            solve_time=4.0,
            status='completed',
        )
        
        warm_start_model(mock_model, result_with_neg_grid, simple_state, simple_config)
        
        # Grid power should be clamped to 0
        assert mock_model.P_grid[0].value == 0.0

    def test_warm_start_handles_negative_peak_demand(
        self, mock_model, simple_state, simple_config
    ):
        """Test that negative peak demand is clamped to zero."""
        result_with_neg_peak = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {'charging_power': [80.0] * 96, 'soc': [0.3] * 96},
                'bus_2': {'charging_power': [70.0] * 96, 'soc': [0.5] * 96},
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[100.0] * 96,
            peak_demand=-50.0,  # Invalid negative
            objective_value=800.0,
            solve_time=4.0,
            status='completed',
        )
        
        warm_start_model(mock_model, result_with_neg_peak, simple_state, simple_config)
        
        assert mock_model.P_peak.value == 0.0


# ============ Mismatched Vehicles Tests ============

class TestWarmStartMismatchedVehicles:
    """Tests for vehicle set differences."""

    def test_warm_start_with_removed_vehicle(
        self, simple_config
    ):
        """Test warm-start when a vehicle was removed from fleet."""
        # Previous result had 3 vehicles, current state only has 2
        n_t = 96
        state_with_removed = DepotState(
            vehicle_socs={'bus_1': 0.3, 'bus_2': 0.5},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={
                'bus_1': [True] * n_t,
                'bus_2': [True] * n_t,
            },
            energy_requirements={'bus_1': 200.0, 'bus_2': 150.0},
            departure_times={'bus_1': 48, 'bus_2': 60},
            building_power=[50.0] * n_t,
        )
        
        previous_result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {'charging_power': [80.0] * 96, 'soc': [0.3] * 96},
                'bus_2': {'charging_power': [70.0] * 96, 'soc': [0.5] * 96},
                'bus_3': {'charging_power': [60.0] * 96, 'soc': [0.4] * 96},
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[200.0] * 96,
            peak_demand=200.0,
            objective_value=1000.0,
            solve_time=5.0,
            status='completed',
        )
        
        # Create model with only 2 vehicles
        model = MagicMock(spec=pyo.ConcreteModel)
        model.T = range(simple_config.n_timesteps)
        model.B = ['bus_1', 'bus_2']
        
        model.P_charge = {}
        model.y_charge = {}
        model.SoC = {}
        model.P_batt = {}
        model.SoC_batt = {}
        model.P_grid = {}
        
        for b in model.B:
            for t in model.T:
                model.P_charge[b, t] = MagicMock()
                model.P_charge[b, t].value = None
                model.y_charge[b, t] = MagicMock()
                model.y_charge[b, t].value = None
                model.SoC[b, t] = MagicMock()
                model.SoC[b, t].value = None
        
        for t in model.T:
            model.P_batt[t] = MagicMock()
            model.P_batt[t].value = None
            model.SoC_batt[t] = MagicMock()
            model.SoC_batt[t].value = None
            model.P_grid[t] = MagicMock()
            model.P_grid[t].value = None
        
        model.P_peak = MagicMock()
        model.P_peak.value = None
        
        # Should not raise exception, bus_3 is ignored
        warm_start_model(model, previous_result, state_with_removed, simple_config)
        
        # bus_1 and bus_2 should be initialized
        assert model.P_charge['bus_1', 0].value == 80.0
        assert model.P_charge['bus_2', 0].value == 70.0


# ============ Different Time Horizon Tests ============

class TestWarmStartDifferentHorizon:
    """Tests for solutions from different time horizons."""

    def test_warm_start_shorter_previous_horizon(
        self, mock_model, simple_state, simple_config
    ):
        """Test warm-start when previous solution had fewer timesteps."""
        result_short = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {
                    'charging_power': [80.0] * 48,  # Only 48 timesteps
                    'soc': [0.3] * 48,
                },
                'bus_2': {
                    'charging_power': [70.0] * 48,
                    'soc': [0.5] * 48,
                },
            },
            battery_dispatch=[0.0] * 48,
            grid_power=[100.0] * 48,
            peak_demand=150.0,
            objective_value=500.0,
            solve_time=3.0,
            status='completed',
        )
        
        warm_start_model(mock_model, result_short, simple_state, simple_config)
        
        # First 48 timesteps should be from previous result
        assert mock_model.P_charge['bus_1', 0].value == 80.0
        assert mock_model.P_charge['bus_1', 47].value == 80.0
        
        # Remaining timesteps should be initialized with defaults
        assert mock_model.P_charge['bus_1', 48].value == 0.0
        assert mock_model.y_charge['bus_1', 48].value == 0


# ============ Empty/Edge Cases ============

class TestWarmStartEdgeCases:
    """Edge case tests for warm-start."""

    def test_warm_start_empty_schedule(
        self, mock_model, simple_state, simple_config
    ):
        """Test warm-start with empty schedule."""
        empty_result = OptimizationResult(
            run_id=uuid4(),
            schedule={},
            battery_dispatch=[0.0] * 96,
            grid_power=[0.0] * 96,
            peak_demand=0.0,
            objective_value=0.0,
            solve_time=1.0,
            status='completed',
        )
        
        # Should not raise, all vehicles are "new"
        warm_start_model(mock_model, empty_result, simple_state, simple_config)
        
        # All vehicles should be initialized as new
        assert mock_model.P_charge['bus_1', 0].value == 0.0
        assert mock_model.SoC['bus_1', 0].value == 0.3  # current SoC

    def test_warm_start_empty_battery_dispatch(
        self, mock_model, previous_result, simple_state, simple_config
    ):
        """Test warm-start with empty battery dispatch list."""
        previous_result.battery_dispatch = []
        
        # Should not raise
        warm_start_model(mock_model, previous_result, simple_state, simple_config)

    def test_warm_start_empty_grid_power(
        self, mock_model, previous_result, simple_state, simple_config
    ):
        """Test warm-start with empty grid power list."""
        previous_result.grid_power = []
        
        # Should not raise
        warm_start_model(mock_model, previous_result, simple_state, simple_config)

    def test_warm_start_preserves_existing_values(
        self, mock_model, previous_result, simple_state, simple_config
    ):
        """Test that warm-start sets values without error."""
        # Set some initial values
        mock_model.P_charge['bus_1', 0].value = 999.0
        
        warm_start_model(mock_model, previous_result, simple_state, simple_config)
        
        # Value should be overwritten
        assert mock_model.P_charge['bus_1', 0].value == 80.0

