"""Realistic test fixtures for Phase 5 and Phase 6 tests.

Reference: Development Plan Step 5.1 - Realistic Test Fixtures

These fixtures use production-like data including:
- UUIDs for depot, vehicle, and charger identifiers
- Realistic TOU pricing patterns
- Building load profiles
- Vehicle availability patterns
- Inter-depot handoff scenarios
"""

import pytest
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from src.core.models import (
    DepotConfig,
    DepotState,
    IncomingVehicle,
    OptimizationResult,
)


# ============ Stable UUIDs for consistent testing ============

# Depot identifiers
DEPOT_ID = UUID('550e8400-e29b-41d4-a716-446655440000')
DEPOT_A_ID = UUID('550e8400-e29b-41d4-a716-446655440001')
DEPOT_B_ID = UUID('550e8400-e29b-41d4-a716-446655440002')

# Vehicle identifiers (20 vehicles for realistic fleet)
VEHICLE_IDS = [
    UUID(f'660e8400-e29b-41d4-a716-44665544{i:04d}')
    for i in range(20)
]

# Vehicle external IDs (human-readable)
VEHICLE_EXTERNAL_IDS = [f'bus_{i}' for i in range(20)]

# Charger identifiers (10 chargers)
CHARGER_IDS = [
    UUID(f'770e8400-e29b-41d4-a716-44665544{i:04d}')
    for i in range(10)
]

# Charger external IDs
CHARGER_EXTERNAL_IDS = [f'charger_{i}' for i in range(10)]


# ============ TOU Pricing Patterns ============

def generate_tou_prices(n_timesteps: int = 96) -> list[float]:
    """Generate realistic TOU prices for 24-hour horizon.

    Pricing pattern (typical California TOU):
    - Off-peak (12am-9am, 9pm-12am): $0.08/kWh
    - Partial-peak (9am-4pm): $0.15/kWh
    - Peak (4pm-9pm): $0.25/kWh

    Args:
        n_timesteps: Number of 15-minute timesteps (default 96 = 24 hours)

    Returns:
        List of prices in $/kWh
    """
    prices = []
    for t in range(n_timesteps):
        hour = (t * 0.25) % 24
        if 16 <= hour < 21:  # Peak: 4pm-9pm
            prices.append(0.25)
        elif 9 <= hour < 16:  # Partial-peak: 9am-4pm
            prices.append(0.15)
        else:  # Off-peak: 9pm-9am
            prices.append(0.08)
    return prices


def generate_price_spike_prices(
    n_timesteps: int = 96,
    spike_start: int = 64,  # 4pm
    spike_end: int = 84,    # 9pm
    spike_price: float = 0.50,
) -> list[float]:
    """Generate prices with a spike period.

    Args:
        n_timesteps: Number of timesteps
        spike_start: Timestep where spike begins
        spike_end: Timestep where spike ends
        spike_price: Price during spike period ($/kWh)

    Returns:
        List of prices with spike
    """
    prices = [0.10] * n_timesteps
    for t in range(spike_start, min(spike_end, n_timesteps)):
        prices[t] = spike_price
    return prices


# ============ Building Load Patterns ============

def generate_building_load(n_timesteps: int = 96) -> list[float]:
    """Generate realistic building load profile.

    Pattern:
    - Base load: 30 kW (overnight)
    - Morning ramp: 30 -> 80 kW (6am-9am)
    - Daytime: 60-80 kW (9am-5pm)
    - Evening: 50 kW (5pm-10pm)
    - Night: 30 kW (10pm-6am)

    Args:
        n_timesteps: Number of 15-minute timesteps

    Returns:
        List of building loads in kW
    """
    loads = []
    for t in range(n_timesteps):
        hour = (t * 0.25) % 24
        if 6 <= hour < 9:  # Morning ramp
            loads.append(30.0 + (hour - 6) * 16.67)  # Ramp to 80
        elif 9 <= hour < 12:  # Morning peak
            loads.append(80.0)
        elif 12 <= hour < 13:  # Lunch dip
            loads.append(60.0)
        elif 13 <= hour < 17:  # Afternoon
            loads.append(70.0)
        elif 17 <= hour < 22:  # Evening
            loads.append(50.0)
        else:  # Night
            loads.append(30.0)
    return loads


# ============ Pytest Fixtures ============

@pytest.fixture
def realistic_depot_config() -> DepotConfig:
    """Realistic depot configuration with 20 vehicles and 10 chargers.

    Configuration:
    - 20 vehicles (324 kWh battery each)
    - 10 chargers at 80 kW (CCS)
    - 5 chargers at 150 kW (DC fast)
    - Site-level battery: 1000 kWh, 200 kW
    - Max site power: 1500 kW
    """
    vehicle_ids = VEHICLE_EXTERNAL_IDS

    return DepotConfig(
        vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
        vehicle_max_charge_kw={vid: 150.0 for vid in vehicle_ids},
        charger_groups={
            80.0: 10,   # 10 standard chargers
            150.0: 5,   # 5 DC fast chargers
        },
        charger_efficiency=0.95,
        charger_vehicle_access={},  # All vehicles can use all chargers
        battery_capacity=1000.0,
        battery_power=200.0,
        battery_efficiency=0.92,
        battery_soc_min=0.2,
        battery_soc_max=0.8,
        max_site_power=1500.0,
        delta_t=0.25,
        n_timesteps=96,
    )


@pytest.fixture
def realistic_depot_state(realistic_depot_config) -> DepotState:
    """Realistic depot state with TOU pricing and building load.

    State simulates 10 PM previous day (t=0):
    - 20 vehicles returned from routes with varying SoC
    - 10 vehicles depart at 6 AM, 10 at 7 AM
    - TOU pricing structure
    - Realistic building load profile
    """
    n_t = realistic_depot_config.n_timesteps
    vehicle_ids = VEHICLE_EXTERNAL_IDS

    # Vehicle SoCs - vehicles returned from routes
    vehicle_socs = {}
    for i, vid in enumerate(vehicle_ids):
        # SoC varies between 0.35 and 0.55 based on route length
        base_soc = 0.35 + (i % 5) * 0.05
        vehicle_socs[vid] = base_soc

    # Vehicle availability - all available until departure
    vehicle_availability = {}
    for i, vid in enumerate(vehicle_ids):
        availability = [True] * n_t
        # First 10 vehicles depart at 6 AM (timestep 32)
        # Next 10 vehicles depart at 7 AM (timestep 36)
        departure_t = 32 if i < 10 else 36
        for t in range(departure_t, n_t):
            availability[t] = False
        vehicle_availability[vid] = availability

    # Departure times
    departure_times = {}
    for i, vid in enumerate(vehicle_ids):
        departure_times[vid] = 32 if i < 10 else 36

    # Energy requirements
    energy_requirements = {vid: 160.0 + (i % 4) * 20.0 for i, vid in enumerate(vehicle_ids)}

    return DepotState(
        vehicle_socs=vehicle_socs,
        battery_soc=0.5,
        prices=generate_tou_prices(n_t),
        demand_charge_rate=20.0,  # $20/kW
        current_month_peak=300.0,  # Current month peak: 300 kW
        vehicle_availability=vehicle_availability,
        energy_requirements=energy_requirements,
        departure_times=departure_times,
        building_power=generate_building_load(n_t),
    )


@pytest.fixture
def state_with_incoming_vehicle(realistic_depot_config) -> tuple[DepotState, IncomingVehicle]:
    """State with an incoming vehicle from another depot (for AT-06 handoff test).

    Simulates:
    - bus_20 arriving from depot_A at expected time
    - Expected SoC: 0.35
    - Battery: 324 kWh
    - Max charge: 150 kW
    """
    n_t = realistic_depot_config.n_timesteps
    vehicle_ids = VEHICLE_EXTERNAL_IDS

    # Base state with 19 existing vehicles
    vehicle_socs = {vid: 0.45 + (i % 5) * 0.03 for i, vid in enumerate(vehicle_ids[:19])}
    vehicle_availability = {vid: [True] * n_t for vid in vehicle_ids[:19]}
    departure_times = {vid: 48 + i * 2 for i, vid in enumerate(vehicle_ids[:19])}
    energy_requirements = {vid: 150.0 for vid in vehicle_ids[:19]}

    state = DepotState(
        vehicle_socs=vehicle_socs,
        battery_soc=0.5,
        prices=generate_tou_prices(n_t),
        demand_charge_rate=20.0,
        current_month_peak=250.0,
        vehicle_availability=vehicle_availability,
        energy_requirements=energy_requirements,
        departure_times=departure_times,
        building_power=generate_building_load(n_t),
    )

    # Incoming vehicle from depot_A
    incoming_vehicle = IncomingVehicle(
        vehicle_id=VEHICLE_IDS[19],  # bus_19
        external_id='bus_19',
        expected_soc=0.35,
        arrival_time=datetime.utcnow() + timedelta(hours=2),
        battery_kwh=324.0,
        max_charge_kw=150.0,
        origin_depot_id=DEPOT_A_ID,
    )

    return state, incoming_vehicle


@pytest.fixture
def state_with_high_building_load(realistic_depot_config) -> DepotState:
    """State with elevated building load for AT-07 test.

    Building load peaks at 80 kW during morning hours,
    which must be accounted for in grid power calculations.
    """
    n_t = realistic_depot_config.n_timesteps
    vehicle_ids = VEHICLE_EXTERNAL_IDS[:10]  # Use 10 vehicles

    vehicle_socs = {vid: 0.4 for vid in vehicle_ids}
    vehicle_availability = {vid: [True] * n_t for vid in vehicle_ids}
    departure_times = {vid: 48 + i * 4 for i, vid in enumerate(vehicle_ids)}
    energy_requirements = {vid: 180.0 for vid in vehicle_ids}

    # High building load profile
    building_load = generate_building_load(n_t)

    return DepotState(
        vehicle_socs=vehicle_socs,
        battery_soc=0.5,
        prices=generate_tou_prices(n_t),
        demand_charge_rate=20.0,
        current_month_peak=200.0,
        vehicle_availability=vehicle_availability,
        energy_requirements=energy_requirements,
        departure_times=departure_times,
        building_power=building_load,
    )


@pytest.fixture
def state_for_return_time_deviation(realistic_depot_config) -> DepotState:
    """State for testing AT-05 Return Time Deviation Handling.

    Simulates bus_0 expected to return at 2:00 PM but actually
    returns at 2:30 PM (30 min late, > 15 min threshold).
    """
    n_t = realistic_depot_config.n_timesteps
    vehicle_ids = VEHICLE_EXTERNAL_IDS[:5]  # Use 5 vehicles

    # Vehicle SoCs - bus_0 expected to be at depot by now
    vehicle_socs = {vid: 0.5 for vid in vehicle_ids}

    # Availability - all available
    vehicle_availability = {vid: [True] * n_t for vid in vehicle_ids}

    # Departure times - staggered
    departure_times = {vid: 60 + i * 8 for i, vid in enumerate(vehicle_ids)}

    # Energy requirements
    energy_requirements = {vid: 150.0 for vid in vehicle_ids}

    return DepotState(
        vehicle_socs=vehicle_socs,
        battery_soc=0.5,
        prices=generate_tou_prices(n_t),
        demand_charge_rate=20.0,
        current_month_peak=150.0,
        vehicle_availability=vehicle_availability,
        energy_requirements=energy_requirements,
        departure_times=departure_times,
        building_power=generate_building_load(n_t),
    )


# ============ Helper Functions ============

def create_optimization_result_for_test(
    config: DepotConfig,
    state: DepotState,
) -> OptimizationResult:
    """Create a mock optimization result for testing warm-start.

    Args:
        config: Depot configuration
        state: Depot state

    Returns:
        Mock OptimizationResult with valid schedule
    """
    n_t = config.n_timesteps
    vehicle_ids = list(state.vehicle_socs.keys())

    schedule = {}
    for vid in vehicle_ids:
        init_soc = state.vehicle_socs[vid]
        charging_power = []
        soc = []
        current_soc = init_soc

        for t in range(n_t):
            # Simple heuristic: charge at 80 kW until 99% SoC
            if current_soc < 0.99 and state.vehicle_availability[vid][t]:
                power = 80.0
            else:
                power = 0.0

            charging_power.append(power)
            soc.append(current_soc)

            # Update SoC
            capacity = config.vehicle_capacities[vid]
            current_soc += (power * config.charger_efficiency * config.delta_t) / capacity
            current_soc = min(1.0, current_soc)

        schedule[vid] = {
            'charging_power': charging_power,
            'soc': soc,
        }

    return OptimizationResult(
        run_id=uuid4(),
        schedule=schedule,
        battery_dispatch=[0.0] * n_t,
        grid_power=[200.0] * n_t,
        peak_demand=300.0,
        objective_value=1500.0,
        solve_time=5.0,
        status='completed',
    )
