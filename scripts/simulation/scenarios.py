"""Scenario generation utilities for depot simulation.

Reference: PRD_v2.md Section 11.1 (MVP Acceptance Tests)
           docs/SIMULATION.md
           favonius_development_plan_v2.md Step 6.1
"""

import random
from datetime import datetime, timedelta
from typing import Optional

from .depot_sim import DepotSimulator


def morning_rush_scenario(
    n_vehicles: int = 10,
    n_chargers: int = 5,
    departure_hour: int = 6,
    start_time: Optional[datetime] = None,
) -> DepotSimulator:
    """Generate morning rush scenario.

    Multiple vehicles departing early morning (6-8 AM) requiring
    full charge by departure. Vehicles start with higher SoC to ensure
    feasibility.

    Args:
        n_vehicles: Number of vehicles in fleet
        n_chargers: Number of available chargers
        departure_hour: Hour when vehicles depart (default: 6 AM)
        start_time: Simulation start time (default: midnight)

    Returns:
        Configured DepotSimulator instance
    """
    if start_time is None:
        # Start at midnight so departure_hour is straightforward
        start_time = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    sim = DepotSimulator(
        n_vehicles=n_vehicles,
        n_chargers=n_chargers,
        start_time=start_time,
    )

    # Set vehicles to moderate SoC to ensure feasibility
    # Higher SoC for vehicles with earlier departures
    for i, v in enumerate(sim.vehicles):
        v.current_soc = random.uniform(0.6, 0.8)

    # Add routes for morning departures (staggered)
    departure_count = min(3, n_vehicles)  # 3 vehicles depart
    for i in range(departure_count):
        vehicle_id = sim.vehicles[i].vehicle_id
        # Stagger departures: 6 AM, 7 AM, 8 AM
        dep_hour = departure_hour + i
        departure_time = start_time + timedelta(hours=dep_hour)
        return_time = departure_time + timedelta(hours=8)
        energy_kwh = random.uniform(150, 200)

        sim.add_route(
            vehicle_id=vehicle_id,
            departure_time=departure_time,
            return_time=return_time,
            energy_kwh=energy_kwh,
        )

    return sim


def price_spike_scenario(
    n_vehicles: int = 10,
    n_chargers: int = 5,
    spike_time: Optional[datetime] = None,
    spike_price: float = 0.25,
    start_time: Optional[datetime] = None,
) -> DepotSimulator:
    """Generate price spike scenario.

    Price spike during simulation to test re-optimization triggers.

    Args:
        n_vehicles: Number of vehicles in fleet
        n_chargers: Number of available chargers
        spike_time: When price spike occurs (default: 2 PM)
        spike_price: New price during spike in $/kWh
        start_time: Simulation start time

    Returns:
        Configured DepotSimulator instance
    """
    if start_time is None:
        start_time = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    sim = DepotSimulator(
        n_vehicles=n_vehicles,
        n_chargers=n_chargers,
        start_time=start_time,
    )
    sim.price_profile = "tou"

    if spike_time is None:
        spike_time = start_time + timedelta(hours=14)  # 2 PM

    # Inject price spike
    sim.inject_price_spike(spike_time, spike_price)

    return sim


def soc_deviation_scenario(
    n_vehicles: int = 10,
    n_chargers: int = 5,
    deviation_vehicle: Optional[str] = None,
    deviation_amount: float = 0.08,
    start_time: Optional[datetime] = None,
) -> DepotSimulator:
    """Generate SoC deviation scenario.

    Vehicle SoC deviates from expected to test trigger handling.

    Args:
        n_vehicles: Number of vehicles in fleet
        n_chargers: Number of available chargers
        deviation_vehicle: Vehicle ID to deviate (default: first vehicle)
        deviation_amount: SoC deviation amount (default: 0.08 = 8%)
        start_time: Simulation start time

    Returns:
        Configured DepotSimulator instance
    """
    if start_time is None:
        start_time = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    sim = DepotSimulator(
        n_vehicles=n_vehicles,
        n_chargers=n_chargers,
        start_time=start_time,
    )

    # Set all vehicles to moderate initial SoC
    for v in sim.vehicles:
        v.current_soc = random.uniform(0.6, 0.8)

    # Set one vehicle to have lower SoC (deviation)
    target_vehicle = deviation_vehicle if deviation_vehicle else sim.vehicles[0].vehicle_id
    for v in sim.vehicles:
        if v.vehicle_id == target_vehicle:
            v.current_soc = max(0.1, v.current_soc - deviation_amount)
            break

    return sim


def demand_charge_scenario(
    n_vehicles: int = 15,
    n_chargers: int = 8,
    start_time: Optional[datetime] = None,
) -> DepotSimulator:
    """Generate high demand charge scenario.

    Scenario designed to test demand charge reduction optimization.
    Uses moderate initial SoCs and staggered departures for feasibility.

    Args:
        n_vehicles: Number of vehicles in fleet
        n_chargers: Number of available chargers
        start_time: Simulation start time

    Returns:
        Configured DepotSimulator instance
    """
    if start_time is None:
        start_time = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    sim = DepotSimulator(
        n_vehicles=n_vehicles,
        n_chargers=n_chargers,
        start_time=start_time,
    )

    # Set vehicles to moderate SoC (higher for earlier departures)
    for i, v in enumerate(sim.vehicles):
        # Vehicles with earlier departures get higher initial SoC
        base_soc = 0.6 + (i % 5) * 0.05
        v.current_soc = base_soc

    # Add routes throughout the day (staggered for feasibility)
    for i, v in enumerate(sim.vehicles[:10]):  # 10 vehicles have routes
        # Spread departures from hour 8 to 18 (8 AM to 6 PM)
        departure_hour = 8 + i  # Hours 8-17
        departure_time = start_time + timedelta(hours=departure_hour)
        return_time = departure_time + timedelta(hours=8)
        energy_kwh = random.uniform(100, 150)

        sim.add_route(
            vehicle_id=v.vehicle_id,
            departure_time=departure_time,
            return_time=return_time,
            energy_kwh=energy_kwh,
        )

    return sim


def inter_depot_scenario(
    n_vehicles: int = 10,
    n_chargers: int = 5,
    start_time: Optional[datetime] = None,
) -> DepotSimulator:
    """Generate inter-depot handoff scenario.

    Vehicle departs for another depot to test handoff messaging.

    Args:
        n_vehicles: Number of vehicles in fleet
        n_chargers: Number of available chargers
        start_time: Simulation start time

    Returns:
        Configured DepotSimulator instance
    """
    if start_time is None:
        start_time = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    sim = DepotSimulator(
        n_vehicles=n_vehicles,
        n_chargers=n_chargers,
        start_time=start_time,
    )

    # One vehicle departs for another depot
    vehicle_id = sim.vehicles[0].vehicle_id
    departure_time = start_time + timedelta(hours=8)
    # Long return time indicates inter-depot trip
    return_time = departure_time + timedelta(hours=12)
    energy_kwh = random.uniform(200, 300)

    sim.add_route(
        vehicle_id=vehicle_id,
        departure_time=departure_time,
        return_time=return_time,
        energy_kwh=energy_kwh,
    )

    return sim


def realistic_depot_scenario(
    n_vehicles: int = 20,
    n_chargers: int = 10,
    start_time: Optional[datetime] = None,
) -> DepotSimulator:
    """Generate realistic depot scenario.

    Comprehensive scenario with multiple routes, varied SoCs,
    and realistic price patterns.

    Args:
        n_vehicles: Number of vehicles in fleet
        n_chargers: Number of available chargers
        start_time: Simulation start time

    Returns:
        Configured DepotSimulator instance
    """
    if start_time is None:
        start_time = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    sim = DepotSimulator(
        n_vehicles=n_vehicles,
        n_chargers=n_chargers,
        start_time=start_time,
        battery_capacity=1000.0,
        battery_power=200.0,
    )
    sim.price_profile = "tou"

    # Varied initial SoCs
    for v in sim.vehicles:
        v.current_soc = random.uniform(0.3, 0.9)

    # Add routes throughout the day
    route_patterns = [
        (6, 8),  # Morning rush: 6-8 AM
        (9, 12),  # Mid-morning: 9 AM-12 PM
        (14, 16),  # Afternoon: 2-4 PM
        (17, 19),  # Evening rush: 5-7 PM
    ]

    route_count = 0
    for pattern_start, pattern_end in route_patterns:
        vehicles_in_pattern = n_vehicles // len(route_patterns)
        for i in range(vehicles_in_pattern):
            if route_count >= n_vehicles:
                break

            vehicle_id = sim.vehicles[route_count].vehicle_id
            departure_hour = random.randint(pattern_start, pattern_end - 1)
            departure_time = start_time + timedelta(hours=departure_hour)
            return_time = departure_time + timedelta(hours=random.uniform(6, 10))
            energy_kwh = random.uniform(150, 300)

            sim.add_route(
                vehicle_id=vehicle_id,
                departure_time=departure_time,
                return_time=return_time,
                energy_kwh=energy_kwh,
            )
            route_count += 1

    return sim
