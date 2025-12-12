"""Unit tests for data models.

Reference: PRD.md#6-2-python-data-classes
"""

import json
import pytest
from dataclasses import asdict
from datetime import datetime
from uuid import UUID, uuid4

from src.core.models import (
    BatteryStorage,
    Charger,
    Depot,
    DepotConfig,
    DepotState,
    OptimizationResult,
    Schedule,
    Vehicle,
)


# ============ DepotConfig Tests ============

class TestDepotConfig:
    """Tests for DepotConfig dataclass."""

    def test_depot_config_creation(self):
        """Test basic DepotConfig creation."""
        config = DepotConfig(
            vehicle_capacities={'bus_1': 324.0, 'bus_2': 300.0},
            charger_power=80.0,
            charger_efficiency=0.95,
            n_chargers=5,
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=800.0,
        )
        
        assert config.vehicle_capacities == {'bus_1': 324.0, 'bus_2': 300.0}
        assert config.charger_power == 80.0
        assert config.charger_efficiency == 0.95
        assert config.n_chargers == 5
        assert config.battery_capacity == 500.0
        assert config.battery_power == 100.0
        assert config.max_site_power == 800.0

    def test_depot_config_defaults(self):
        """Test DepotConfig default values."""
        config = DepotConfig(
            vehicle_capacities={},
            charger_power=80.0,
            charger_efficiency=0.95,
            n_chargers=5,
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=800.0,
        )
        
        assert config.delta_t == 0.25  # Default 15 minutes
        assert config.n_timesteps == 96  # Default 24 hours

    def test_depot_config_custom_timesteps(self):
        """Test DepotConfig with custom timesteps."""
        config = DepotConfig(
            vehicle_capacities={},
            charger_power=80.0,
            charger_efficiency=0.95,
            n_chargers=5,
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=800.0,
            delta_t=0.5,  # 30 minutes
            n_timesteps=48,  # 24 hours with 30-min steps
        )
        
        assert config.delta_t == 0.5
        assert config.n_timesteps == 48

    def test_depot_config_to_dict(self):
        """Test DepotConfig serialization to dict."""
        config = DepotConfig(
            vehicle_capacities={'bus_1': 324.0},
            charger_power=80.0,
            charger_efficiency=0.95,
            n_chargers=5,
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=800.0,
        )
        
        config_dict = asdict(config)
        
        assert isinstance(config_dict, dict)
        assert config_dict['charger_power'] == 80.0
        assert config_dict['vehicle_capacities'] == {'bus_1': 324.0}

    def test_depot_config_large_fleet(self):
        """Test DepotConfig with large fleet."""
        vehicle_caps = {f'bus_{i}': 324.0 for i in range(100)}
        config = DepotConfig(
            vehicle_capacities=vehicle_caps,
            charger_power=80.0,
            charger_efficiency=0.95,
            n_chargers=50,
            battery_capacity=2000.0,
            battery_power=500.0,
            max_site_power=5000.0,
        )
        
        assert len(config.vehicle_capacities) == 100
        assert config.n_chargers == 50


# ============ DepotState Tests ============

class TestDepotState:
    """Tests for DepotState dataclass."""

    def test_depot_state_creation(self):
        """Test basic DepotState creation."""
        n_t = 96
        state = DepotState(
            vehicle_socs={'bus_1': 0.3, 'bus_2': 0.5},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=200.0,
            vehicle_availability={
                'bus_1': [True] * n_t,
                'bus_2': [True] * n_t,
            },
            energy_requirements={'bus_1': 200.0, 'bus_2': 150.0},
            departure_times={'bus_1': 48, 'bus_2': 60},
            building_power=[50.0] * n_t,
        )
        
        assert state.vehicle_socs == {'bus_1': 0.3, 'bus_2': 0.5}
        assert state.battery_soc == 0.5
        assert len(state.prices) == n_t
        assert state.demand_charge_rate == 15.0

    def test_depot_state_soc_range(self):
        """Test DepotState with various SoC values."""
        n_t = 96
        state = DepotState(
            vehicle_socs={'bus_1': 0.0, 'bus_2': 1.0},  # Edge values
            battery_soc=0.2,  # Min bound
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=0.0,
            vehicle_availability={
                'bus_1': [True] * n_t,
                'bus_2': [True] * n_t,
            },
            energy_requirements={'bus_1': 200.0, 'bus_2': 150.0},
            departure_times={},
            building_power=[50.0] * n_t,
        )
        
        assert state.vehicle_socs['bus_1'] == 0.0
        assert state.vehicle_socs['bus_2'] == 1.0
        assert state.battery_soc == 0.2

    def test_depot_state_variable_prices(self):
        """Test DepotState with TOU pricing."""
        n_t = 96
        prices = []
        for t in range(n_t):
            hour = (t * 0.25) % 24
            if 16 <= hour < 21:  # Peak
                prices.append(0.25)
            elif 9 <= hour < 16 or 21 <= hour < 24:  # Partial-peak
                prices.append(0.15)
            else:  # Off-peak
                prices.append(0.10)
        
        state = DepotState(
            vehicle_socs={'bus_1': 0.5},
            battery_soc=0.5,
            prices=prices,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={'bus_1': [True] * n_t},
            energy_requirements={'bus_1': 200.0},
            departure_times={'bus_1': 48},
            building_power=[50.0] * n_t,
        )
        
        assert len(state.prices) == n_t
        assert max(state.prices) == 0.25  # Peak price
        assert min(state.prices) == 0.10  # Off-peak price

    def test_depot_state_partial_availability(self):
        """Test DepotState with partial vehicle availability."""
        n_t = 96
        # Bus_1 unavailable from timestep 24 to 48
        availability = [True] * 24 + [False] * 24 + [True] * 48
        
        state = DepotState(
            vehicle_socs={'bus_1': 0.5},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={'bus_1': availability},
            energy_requirements={'bus_1': 200.0},
            departure_times={'bus_1': 24},  # Departs at timestep 24
            building_power=[50.0] * n_t,
        )
        
        assert state.vehicle_availability['bus_1'][0] is True
        assert state.vehicle_availability['bus_1'][30] is False
        assert state.vehicle_availability['bus_1'][70] is True

    def test_depot_state_to_dict(self):
        """Test DepotState serialization to dict."""
        n_t = 96
        state = DepotState(
            vehicle_socs={'bus_1': 0.5},
            battery_soc=0.5,
            prices=[0.10] * n_t,
            demand_charge_rate=15.0,
            current_month_peak=100.0,
            vehicle_availability={'bus_1': [True] * n_t},
            energy_requirements={'bus_1': 200.0},
            departure_times={'bus_1': 48},
            building_power=[50.0] * n_t,
        )
        
        state_dict = asdict(state)
        
        assert isinstance(state_dict, dict)
        assert state_dict['battery_soc'] == 0.5


# ============ OptimizationResult Tests ============

class TestOptimizationResult:
    """Tests for OptimizationResult dataclass."""

    def test_optimization_result_creation(self):
        """Test basic OptimizationResult creation."""
        run_id = uuid4()
        result = OptimizationResult(
            run_id=run_id,
            schedule={
                'bus_1': {
                    'charging_power': [80.0, 60.0, 0.0] * 32,
                    'soc': [0.3, 0.4, 0.5] * 32,
                }
            },
            battery_dispatch=[10.0, -5.0, 0.0] * 32,
            grid_power=[150.0, 100.0, 50.0] * 32,
            peak_demand=200.0,
            objective_value=1500.0,
            solve_time=12.5,
            status='completed',
        )
        
        assert result.run_id == run_id
        assert result.status == 'completed'
        assert result.objective_value == 1500.0
        assert result.solve_time == 12.5

    def test_optimization_result_schedule_structure(self):
        """Test OptimizationResult schedule structure."""
        result = OptimizationResult(
            run_id=uuid4(),
            schedule={
                'bus_1': {'charging_power': [80.0] * 96, 'soc': [0.5] * 96},
                'bus_2': {'charging_power': [60.0] * 96, 'soc': [0.6] * 96},
            },
            battery_dispatch=[0.0] * 96,
            grid_power=[140.0] * 96,
            peak_demand=200.0,
            objective_value=1000.0,
            solve_time=5.0,
            status='completed',
        )
        
        assert 'bus_1' in result.schedule
        assert 'bus_2' in result.schedule
        assert 'charging_power' in result.schedule['bus_1']
        assert 'soc' in result.schedule['bus_1']
        assert len(result.schedule['bus_1']['charging_power']) == 96

    def test_optimization_result_status_values(self):
        """Test various OptimizationResult status values."""
        for status in ['completed', 'timeout', 'infeasible', 'error']:
            result = OptimizationResult(
                run_id=uuid4(),
                schedule={},
                battery_dispatch=[],
                grid_power=[],
                peak_demand=0.0,
                objective_value=0.0,
                solve_time=0.0,
                status=status,
            )
            assert result.status == status

    def test_optimization_result_serialization(self):
        """Test OptimizationResult can be serialized to JSON-compatible dict."""
        result = OptimizationResult(
            run_id=uuid4(),
            schedule={'bus_1': {'charging_power': [80.0], 'soc': [0.5]}},
            battery_dispatch=[10.0],
            grid_power=[90.0],
            peak_demand=100.0,
            objective_value=500.0,
            solve_time=3.0,
            status='completed',
        )
        
        result_dict = asdict(result)
        
        # Convert UUID to string for JSON serialization
        result_dict['run_id'] = str(result_dict['run_id'])
        
        # Should be JSON serializable
        json_str = json.dumps(result_dict)
        assert isinstance(json_str, str)
        
        # Can be deserialized
        parsed = json.loads(json_str)
        assert parsed['objective_value'] == 500.0


# ============ Vehicle Tests ============

class TestVehicle:
    """Tests for Vehicle dataclass."""

    def test_vehicle_creation(self):
        """Test basic Vehicle creation."""
        vehicle = Vehicle(
            vehicle_id=uuid4(),
            depot_id=uuid4(),
            external_id='BUS-001',
            vehicle_type='bus_large',
            battery_kwh=324.0,
            max_charge_kw=80.0,
        )
        
        assert vehicle.external_id == 'BUS-001'
        assert vehicle.vehicle_type == 'bus_large'
        assert vehicle.battery_kwh == 324.0

    def test_vehicle_with_ocpp_id(self):
        """Test Vehicle with OCPP ID."""
        vehicle = Vehicle(
            vehicle_id=uuid4(),
            depot_id=uuid4(),
            external_id='BUS-002',
            vehicle_type='bus_small',
            battery_kwh=200.0,
            max_charge_kw=60.0,
            ocpp_id='charger_001_connector_1',
        )
        
        assert vehicle.ocpp_id == 'charger_001_connector_1'

    def test_vehicle_default_ocpp_id(self):
        """Test Vehicle without OCPP ID defaults to None."""
        vehicle = Vehicle(
            vehicle_id=uuid4(),
            depot_id=uuid4(),
            external_id='BUS-003',
            vehicle_type='van',
            battery_kwh=150.0,
            max_charge_kw=50.0,
        )
        
        assert vehicle.ocpp_id is None


# ============ Charger Tests ============

class TestCharger:
    """Tests for Charger dataclass."""

    def test_charger_creation(self):
        """Test basic Charger creation."""
        charger = Charger(
            charger_id=uuid4(),
            depot_id=uuid4(),
            ocpp_id='charger_001',
            rated_kw=80.0,
        )
        
        assert charger.ocpp_id == 'charger_001'
        assert charger.rated_kw == 80.0

    def test_charger_defaults(self):
        """Test Charger default values."""
        charger = Charger(
            charger_id=uuid4(),
            depot_id=uuid4(),
            ocpp_id='charger_002',
            rated_kw=150.0,
        )
        
        assert charger.efficiency == 0.95
        assert charger.status == 'Available'

    def test_charger_custom_status(self):
        """Test Charger with custom status."""
        charger = Charger(
            charger_id=uuid4(),
            depot_id=uuid4(),
            ocpp_id='charger_003',
            rated_kw=80.0,
            efficiency=0.92,
            status='Occupied',
        )
        
        assert charger.efficiency == 0.92
        assert charger.status == 'Occupied'


# ============ BatteryStorage Tests ============

class TestBatteryStorage:
    """Tests for BatteryStorage dataclass."""

    def test_battery_storage_creation(self):
        """Test basic BatteryStorage creation."""
        battery = BatteryStorage(
            battery_id=uuid4(),
            depot_id=uuid4(),
            capacity_kwh=500.0,
            max_power_kw=100.0,
        )
        
        assert battery.capacity_kwh == 500.0
        assert battery.max_power_kw == 100.0

    def test_battery_storage_defaults(self):
        """Test BatteryStorage default values."""
        battery = BatteryStorage(
            battery_id=uuid4(),
            depot_id=uuid4(),
            capacity_kwh=1000.0,
            max_power_kw=200.0,
        )
        
        assert battery.efficiency == 0.92
        assert battery.soc_min == 0.2
        assert battery.soc_max == 0.8

    def test_battery_storage_custom_bounds(self):
        """Test BatteryStorage with custom SoC bounds."""
        battery = BatteryStorage(
            battery_id=uuid4(),
            depot_id=uuid4(),
            capacity_kwh=500.0,
            max_power_kw=100.0,
            efficiency=0.95,
            soc_min=0.1,
            soc_max=0.9,
        )
        
        assert battery.soc_min == 0.1
        assert battery.soc_max == 0.9


# ============ Depot Tests ============

class TestDepot:
    """Tests for Depot dataclass."""

    def test_depot_creation(self):
        """Test basic Depot creation."""
        depot = Depot(
            depot_id=uuid4(),
            name='Main Depot',
            latitude=37.7749,
            longitude=-122.4194,
            timezone='America/Los_Angeles',
            max_grid_kw=1000.0,
            demand_charge_rate_kw=20.0,
        )
        
        assert depot.name == 'Main Depot'
        assert depot.latitude == 37.7749
        assert depot.longitude == -122.4194
        assert depot.timezone == 'America/Los_Angeles'


# ============ Schedule Tests ============

class TestSchedule:
    """Tests for Schedule dataclass."""

    def test_schedule_creation(self):
        """Test basic Schedule creation."""
        schedule = Schedule(
            schedule_id=uuid4(),
            vehicle_id=uuid4(),
            route_id='route_001',
            departure_time=datetime(2025, 1, 15, 6, 0, 0),
            return_time=datetime(2025, 1, 15, 14, 0, 0),
            energy_kwh=200.0,
        )
        
        assert schedule.route_id == 'route_001'
        assert schedule.energy_kwh == 200.0
        assert schedule.required_soc == 1.0  # Default

    def test_schedule_with_custom_soc(self):
        """Test Schedule with custom required SoC."""
        schedule = Schedule(
            schedule_id=uuid4(),
            vehicle_id=uuid4(),
            route_id='route_002',
            departure_time=datetime(2025, 1, 15, 8, 0, 0),
            return_time=datetime(2025, 1, 15, 12, 0, 0),
            energy_kwh=100.0,
            required_soc=0.8,
        )
        
        assert schedule.required_soc == 0.8

    def test_schedule_inter_depot(self):
        """Test Schedule with destination depot."""
        origin_depot = uuid4()
        dest_depot = uuid4()
        
        schedule = Schedule(
            schedule_id=uuid4(),
            vehicle_id=uuid4(),
            route_id='route_inter',
            departure_time=datetime(2025, 1, 15, 6, 0, 0),
            return_time=datetime(2025, 1, 15, 18, 0, 0),
            energy_kwh=300.0,
            dest_depot_id=dest_depot,
        )
        
        assert schedule.dest_depot_id == dest_depot

    def test_schedule_default_dest_depot(self):
        """Test Schedule without destination depot defaults to None."""
        schedule = Schedule(
            schedule_id=uuid4(),
            vehicle_id=uuid4(),
            route_id='route_003',
            departure_time=datetime(2025, 1, 15, 7, 0, 0),
            return_time=datetime(2025, 1, 15, 15, 0, 0),
            energy_kwh=180.0,
        )
        
        assert schedule.dest_depot_id is None


# ============ Cross-Model Tests ============

class TestCrossModelConsistency:
    """Tests for consistency across models."""

    def test_vehicle_depot_relationship(self):
        """Test vehicle references depot correctly."""
        depot_id = uuid4()
        
        depot = Depot(
            depot_id=depot_id,
            name='Test Depot',
            latitude=37.0,
            longitude=-122.0,
            timezone='UTC',
            max_grid_kw=1000.0,
            demand_charge_rate_kw=20.0,
        )
        
        vehicle = Vehicle(
            vehicle_id=uuid4(),
            depot_id=depot_id,
            external_id='BUS-001',
            vehicle_type='bus_large',
            battery_kwh=324.0,
            max_charge_kw=80.0,
        )
        
        assert vehicle.depot_id == depot.depot_id

    def test_charger_depot_relationship(self):
        """Test charger references depot correctly."""
        depot_id = uuid4()
        
        charger = Charger(
            charger_id=uuid4(),
            depot_id=depot_id,
            ocpp_id='charger_001',
            rated_kw=80.0,
        )
        
        assert charger.depot_id == depot_id

    def test_depot_config_matches_vehicles(self):
        """Test DepotConfig vehicle capacities match Vehicle objects."""
        vehicles = [
            Vehicle(
                vehicle_id=uuid4(),
                depot_id=uuid4(),
                external_id=f'BUS-{i}',
                vehicle_type='bus_large',
                battery_kwh=324.0,
                max_charge_kw=80.0,
            )
            for i in range(5)
        ]
        
        vehicle_capacities = {
            v.external_id: v.battery_kwh for v in vehicles
        }
        
        config = DepotConfig(
            vehicle_capacities=vehicle_capacities,
            charger_power=80.0,
            charger_efficiency=0.95,
            n_chargers=5,
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=800.0,
        )
        
        assert len(config.vehicle_capacities) == len(vehicles)
        for v in vehicles:
            assert config.vehicle_capacities[v.external_id] == v.battery_kwh

