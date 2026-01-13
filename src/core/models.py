"""Data models for Favonius Energy platform.

Reference: PRD_v2.md#6-2-python-data-classes
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from uuid import UUID


@dataclass
class Depot:
    """Depot configuration data model."""

    depot_id: UUID
    name: str
    latitude: float
    longitude: float
    timezone: str
    max_grid_kw: float
    demand_charge_rate_kw: float


@dataclass
class Vehicle:
    """Vehicle fleet data model."""

    vehicle_id: UUID
    depot_id: UUID
    external_id: str
    vehicle_type: str  # 'bus_large', 'bus_small', 'van'
    battery_kwh: float
    max_charge_kw: float
    id_tag: Optional[str] = None  # OCPP idTag used in Authorize messages to map sessions to vehicles


@dataclass
class Charger:
    """Charger configuration data model."""

    charger_id: UUID
    depot_id: UUID
    ocpp_id: str
    rated_kw: float
    efficiency: float = 0.95
    status: str = 'Available'


@dataclass
class BatteryStorage:
    """Stationary battery storage data model."""

    battery_id: UUID
    depot_id: UUID
    capacity_kwh: float
    max_power_kw: float
    efficiency: float = 0.92
    soc_min: float = 0.2
    soc_max: float = 0.8


@dataclass
class Schedule:
    """Vehicle route schedule data model."""

    schedule_id: UUID
    vehicle_id: UUID
    route_id: str
    departure_time: datetime
    return_time: datetime
    energy_kwh: float
    required_soc: float = 1.0
    dest_depot_id: Optional[UUID] = None
    actual_return_time: Optional[datetime] = None


@dataclass
class IncomingVehicle:
    """Vehicle arriving from another depot via handoff.
    
    Reference: PRD_v2.md Section 6.2
    """
    vehicle_id: UUID
    external_id: str
    expected_soc: float
    arrival_time: datetime
    battery_kwh: float
    max_charge_kw: float
    origin_depot_id: UUID


@dataclass
class DepotConfig:
    """Static configuration for optimization.
    
    Reference: PRD_v2.md Section 6.2
    Chargers are aggregated by rated_kw for optimization to reduce variable count.
    After optimization, power is allocated back to individual chargers.
    """
    vehicle_capacities: dict[str, float]  # vehicle_id -> kWh
    vehicle_max_charge_kw: dict[str, float]  # vehicle_id -> max charge rate (kW)
    charger_groups: dict[float, int]  # rated_kw -> count of chargers with that rating
    charger_efficiency: float  # Assumed uniform across all chargers
    charger_vehicle_access: dict[str, set[str]]  # charger_id -> set of accessible vehicle_ids
    battery_capacity: float  # kWh (0 if no battery)
    battery_power: float  # kW (0 if no battery)
    battery_efficiency: float = 0.92  # Round-trip efficiency for stationary battery
    battery_soc_min: float = 0.2
    battery_soc_max: float = 0.8
    max_site_power: float = 1000.0
    delta_t: float = 0.25  # hours (15 min)
    n_timesteps: int = 96  # 24 hours

    # Property aliases for backwards compatibility
    @property
    def charger_power(self) -> float:
        """Alias for charger_groups (backward compatibility).
        
        Returns the rated_kw of the first charger group.
        For single-group configs (most common), this matches the old API.
        For multi-group configs, returns the first group's rated_kw.
        
        Note: This property is deprecated. Use charger_groups directly.
        """
        if not self.charger_groups:
            raise ValueError("charger_groups is empty")
        # Return first charger group's rated_kw
        return list(self.charger_groups.keys())[0]

    @property
    def n_chargers(self) -> int:
        """Alias for charger_groups (backward compatibility).
        
        Returns the sum of all charger group counts.
        
        Note: This property is deprecated. Use sum(charger_groups.values()) directly.
        """
        return sum(self.charger_groups.values())


@dataclass
class DepotState:
    """Dynamic state for optimization.
    
    Reference: PRD_v2.md Section 6.2
    Assembled from database queries before each optimization run.
    """
    vehicle_socs: dict[str, float]  # vehicle_id -> SoC [0,1]
    battery_soc: float  # Stationary battery SoC [0,1]
    prices: list[float]  # $/kWh per timestep
    demand_charge_rate: float  # $/kW (resolved per Section 8.1 precedence rules)
    current_month_peak: float  # kW (moving limit)
    vehicle_availability: dict[str, list[bool]]  # vehicle_id -> availability per timestep
    energy_requirements: dict[str, float]  # vehicle_id -> kWh needed for next trip
    departure_times: dict[str, int]  # vehicle_id -> timestep index of departure
    building_power: list[float]  # Building load per timestep (kW) - REQUIRED
    incoming_vehicles: list[IncomingVehicle] = field(default_factory=list)  # Inter-depot arrivals


@dataclass
class OptimizationResult:
    """Output from optimization.

    Reference: PRD_v2.md Section 6.2

    Note: peak_demand and solve_time are aliases for peak_demand_kw and solve_time_s
    for backwards compatibility with existing tests.
    """

    run_id: UUID
    schedule: dict[str, dict]  # vehicle_id -> {charging_power: [], soc: []}
    battery_dispatch: list[float]  # Battery power per timestep (+discharge, -charge)
    grid_power: list[float]  # Grid power per timestep
    peak_demand_kw: float  # Maximum grid power (kW)
    objective_value: float
    solve_time_s: float  # Solve time in seconds
    status: str  # 'optimal', 'feasible', 'degraded', 'infeasible', 'timeout'
    solver_used: str = 'gurobi'  # 'gurobi' or 'highs' - tracks which solver was used for monitoring

    # Property aliases for backwards compatibility
    @property
    def peak_demand(self) -> float:
        """Alias for peak_demand_kw."""
        return self.peak_demand_kw

    @property
    def solve_time(self) -> float:
        """Alias for solve_time_s."""
        return self.solve_time_s

