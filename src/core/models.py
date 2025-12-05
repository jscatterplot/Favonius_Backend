"""Data models for Favonius Energy platform.

Reference: PRD.md#6-2-python-data-classes
"""

from dataclasses import dataclass
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
    ocpp_id: Optional[str] = None


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


@dataclass
class DepotConfig:
    """Static configuration for optimization."""

    vehicle_capacities: dict[str, float]  # vehicle_id -> kWh
    charger_power: float
    charger_efficiency: float
    n_chargers: int
    battery_capacity: float
    battery_power: float
    max_site_power: float
    delta_t: float = 0.25  # hours (15 min)
    n_timesteps: int = 96  # 24 hours


@dataclass
class DepotState:
    """Dynamic state for optimization."""

    vehicle_socs: dict[str, float]  # vehicle_id -> SoC [0,1]
    battery_soc: float
    prices: list[float]  # $/kWh per timestep
    demand_charge_rate: float  # $/kW
    current_month_peak: float  # kW
    vehicle_availability: dict[str, list[bool]]
    energy_requirements: dict[str, float]  # vehicle_id -> kWh needed
    departure_times: dict[str, int]  # vehicle_id -> timestep index
    building_power: list[float]  # Building load per timestep (kW)


@dataclass
class OptimizationResult:
    """Output from optimization."""

    run_id: UUID
    schedule: dict[str, dict]  # vehicle_id -> {charging_power: [], soc: []}
    battery_dispatch: list[float]
    grid_power: list[float]
    peak_demand: float
    objective_value: float
    solve_time: float
    status: str

