"""Depot simulation for testing optimization.

Reference: PRD_v2.md Section 11.1 (MVP Acceptance Tests)
           docs/SIMULATION.md
           favonius_development_plan_v2.md Step 6.1
"""

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

logger = None  # Will be set if logging is needed


@dataclass
class RouteSchedule:
    """Route schedule for a vehicle.

    Attributes:
        vehicle_id: Vehicle identifier
        departure_time: When vehicle departs
        return_time: When vehicle returns
        energy_kwh: Energy consumed on route
        route_id: Unique route identifier
    """

    vehicle_id: str
    departure_time: datetime
    return_time: datetime
    energy_kwh: float
    route_id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class SimulationMetrics:
    """Metrics collected during simulation.

    Attributes:
        total_energy_cost: Total energy cost in dollars
        total_demand_cost: Total demand charge cost in dollars
        peak_demand_kw: Peak demand in kW
        optimization_count: Number of optimizations run
        total_solve_time: Total solve time in seconds
        vehicles_ready_at_departure: Number of vehicles ready at departure
        vehicles_not_ready: Number of vehicles not ready at departure
        avg_solve_time: Average solve time in seconds
    """

    total_energy_cost: float = 0.0
    total_demand_cost: float = 0.0
    peak_demand_kw: float = 0.0
    optimization_count: int = 0
    total_solve_time: float = 0.0
    vehicles_ready_at_departure: int = 0
    vehicles_not_ready: int = 0
    avg_solve_time: float = 0.0

    def update_avg_solve_time(self) -> None:
        """Update average solve time."""
        if self.optimization_count > 0:
            self.avg_solve_time = self.total_solve_time / self.optimization_count


@dataclass
class BatteryStorage:
    """Simulated battery storage.

    Attributes:
        capacity_kwh: Battery capacity in kWh
        max_power_kw: Maximum charge/discharge power in kW
        efficiency: Round-trip efficiency (default: 0.92)
        current_soc: Current state of charge (0-1)
        soc_min: Minimum SoC (default: 0.2)
        soc_max: Maximum SoC (default: 0.8)
    """

    capacity_kwh: float
    max_power_kw: float
    efficiency: float = 0.92
    current_soc: float = 0.5
    soc_min: float = 0.2
    soc_max: float = 0.8


@dataclass
class VehicleSim:
    """Simulated vehicle.

    Attributes:
        vehicle_id: Vehicle identifier
        battery_capacity_kwh: Battery capacity in kWh
        current_soc: Current state of charge (0-1)
        is_charging: Whether vehicle is currently charging
        is_on_route: Whether vehicle is currently on route
    """

    vehicle_id: str
    battery_capacity_kwh: float
    current_soc: float = 0.8
    is_charging: bool = False
    is_on_route: bool = False


class DepotSimulator:
    """Simulates depot operations for testing.

    Simulates vehicle charging, route operations, and energy consumption
    for testing optimization algorithms.

    Reference: PRD_v2.md Section 11.1 (MVP Acceptance Tests)
               docs/SIMULATION.md
               favonius_development_plan_v2.md Step 6.1
    """

    def __init__(
        self,
        n_vehicles: int = 10,
        n_chargers: int = 5,
        start_time: Optional[datetime] = None,
        battery_capacity: float = 500.0,
        battery_power: float = 100.0,
    ):
        """Initialize depot simulator.

        Args:
            n_vehicles: Number of vehicles in fleet
            n_chargers: Number of available chargers
            start_time: Simulation start time (default: now)
            battery_capacity: Stationary battery capacity in kWh
            battery_power: Stationary battery max power in kW
        """
        self.vehicles = [
            VehicleSim(
                vehicle_id=f"bus_{i}",
                battery_capacity_kwh=random.choice([180, 324, 313]),
                current_soc=random.uniform(0.3, 0.9),
            )
            for i in range(n_vehicles)
        ]
        self.n_chargers = n_chargers
        self.time = start_time if start_time else datetime.utcnow()
        self.charging_power_kw = 80.0  # Standard charger power
        self.routes: list[RouteSchedule] = []
        self.delta_t_hours = 0.25  # 15 minutes
        self.price_profile: str = "tou"  # 'tou' or 'caiso'
        self.price_spikes: dict[datetime, float] = {}
        self.base_price: float = 0.12  # Base price in $/kWh
        self.battery = BatteryStorage(
            capacity_kwh=battery_capacity,
            max_power_kw=battery_power,
        )
        self.metrics = SimulationMetrics()
        self._battery_dispatch: Optional[list[float]] = None
        self._battery_dispatch_start: Optional[datetime] = None

    def add_route(
        self,
        vehicle_id: str,
        departure_time: datetime,
        return_time: datetime,
        energy_kwh: float,
        route_id: Optional[str] = None,
    ) -> RouteSchedule:
        """Add a route schedule.

        Args:
            vehicle_id: Vehicle identifier
            departure_time: When vehicle departs
            return_time: When vehicle returns
            energy_kwh: Energy consumed on route
            route_id: Optional route identifier

        Returns:
            Created RouteSchedule
        """
        route = RouteSchedule(
            vehicle_id=vehicle_id,
            departure_time=departure_time,
            return_time=return_time,
            energy_kwh=energy_kwh,
            route_id=route_id or str(uuid4()),
        )
        self.routes.append(route)
        return route

    def get_vehicle_availability(
        self, start: Optional[datetime] = None, horizon_hours: int = 24
    ) -> dict[str, list[bool]]:
        """Get availability windows for optimization.

        Args:
            start: Start time for availability (default: current time)
            horizon_hours: Hours to look ahead

        Returns:
            Dictionary mapping vehicle_id to list of availability booleans
            per timestep
        """
        if start is None:
            start = self.time

        n_timesteps = int(horizon_hours / self.delta_t_hours)
        delta_t = timedelta(hours=self.delta_t_hours)

        # Initialize all vehicles as available
        availability = {
            v.vehicle_id: [True] * n_timesteps for v in self.vehicles
        }

        # Mark vehicles as unavailable during routes
        for route in self.routes:
            if route.vehicle_id not in availability:
                continue

            route_start = route.departure_time
            route_end = route.return_time

            for t in range(n_timesteps):
                timestep_time = start + t * delta_t
                if route_start <= timestep_time < route_end:
                    availability[route.vehicle_id][t] = False

        return availability

    def get_departure_times(
        self, start: Optional[datetime] = None, horizon_hours: int = 24
    ) -> dict[str, int]:
        """Get next departure timestep index for each vehicle.

        Args:
            start: Start time (default: current time)
            horizon_hours: Hours to look ahead

        Returns:
            Dictionary mapping vehicle_id to timestep index of next departure
        """
        if start is None:
            start = self.time

        delta_t = timedelta(hours=self.delta_t_hours)
        departures: dict[str, int] = {}

        for route in self.routes:
            if route.vehicle_id not in departures:
                # Calculate timestep index
                if route.departure_time >= start:
                    t_idx = int((route.departure_time - start) / delta_t)
                    n_timesteps = int(horizon_hours / self.delta_t_hours)
                    if 0 <= t_idx < n_timesteps:
                        departures[route.vehicle_id] = t_idx

        return departures

    def step(self, dt_minutes: float = 15) -> None:
        """Advance simulation by dt_minutes.

        Updates vehicle SoC based on charging and route consumption.
        Automatically sets vehicles on/off route based on schedules.

        Args:
            dt_minutes: Time step in minutes (default: 15)
        """
        self.time += timedelta(minutes=dt_minutes)

        # Update vehicle route status based on schedules
        for v in self.vehicles:
            was_on_route = v.is_on_route
            v.is_on_route = False

            for route in self.routes:
                if route.vehicle_id == v.vehicle_id:
                    if route.departure_time <= self.time < route.return_time:
                        v.is_on_route = True
                        # Can't charge while on route
                        if v.is_on_route and not was_on_route:
                            v.is_charging = False
                        break

        # Update vehicle SoC
        for v in self.vehicles:
            if v.is_charging:
                # Charge at configured power
                energy_added = self.charging_power_kw * (dt_minutes / 60)
                v.current_soc = min(
                    1.0,
                    v.current_soc + energy_added / v.battery_capacity_kwh,
                )

            if v.is_on_route:
                # Consume energy during route
                # Find route energy consumption rate
                route_energy_rate = 0.0
                for route in self.routes:
                    if route.vehicle_id == v.vehicle_id:
                        if route.departure_time <= self.time < route.return_time:
                            route_duration_hours = (
                                route.return_time - route.departure_time
                            ).total_seconds() / 3600.0
                            if route_duration_hours > 0:
                                route_energy_rate = (
                                    route.energy_kwh / route_duration_hours
                                )
                            break

                # Default to random if no route found
                if route_energy_rate == 0.0:
                    route_energy_rate = random.uniform(10, 30) / (
                        dt_minutes / 60
                    )

                energy_used = route_energy_rate * (dt_minutes / 60)
                v.current_soc = max(
                    0.1, v.current_soc - energy_used / v.battery_capacity_kwh
                )

        # Update battery storage based on dispatch (if set)
        if hasattr(self, "_battery_dispatch") and self._battery_dispatch:
            # Get dispatch for current timestep
            timestep_idx = int(
                (self.time - self._battery_dispatch_start) / timedelta(hours=self.delta_t_hours)
            )
            if 0 <= timestep_idx < len(self._battery_dispatch):
                dispatch_kw = self._battery_dispatch[timestep_idx]
                # Positive = discharge, Negative = charge
                if dispatch_kw > 0:  # Discharging
                    energy_discharged = dispatch_kw * (dt_minutes / 60)
                    self.battery.current_soc = max(
                        self.battery.soc_min,
                        self.battery.current_soc
                        - energy_discharged / self.battery.capacity_kwh,
                    )
                elif dispatch_kw < 0:  # Charging
                    energy_charged = abs(dispatch_kw) * (dt_minutes / 60)
                    # Apply efficiency for charging
                    energy_stored = energy_charged * self.battery.efficiency
                    self.battery.current_soc = min(
                        self.battery.soc_max,
                        self.battery.current_soc
                        + energy_stored / self.battery.capacity_kwh,
                    )

    def apply_battery_dispatch(
        self, dispatch: list[float], start_time: Optional[datetime] = None
    ) -> None:
        """Apply battery dispatch schedule.

        Args:
            dispatch: List of battery power dispatch per timestep (kW)
                Positive = discharge, Negative = charge
            start_time: Start time for dispatch schedule (default: current time)
        """
        self._battery_dispatch = dispatch
        self._battery_dispatch_start = (
            start_time if start_time else self.time
        )

    def update_metrics(self, result: "OptimizationResult") -> None:
        """Update metrics from optimization result.

        Args:
            result: OptimizationResult to extract metrics from
        """
        from src.core.models import OptimizationResult

        # Update solve time metrics
        self.metrics.optimization_count += 1
        # Per PRD Section 6.2: OptimizationResult uses solve_time_s field
        self.metrics.total_solve_time += result.solve_time_s
        self.metrics.update_avg_solve_time()

        # Calculate energy cost from grid power and prices
        prices = self.get_prices(horizon_hours=24)
        n_timesteps = min(len(prices), len(result.grid_power))
        delta_t = self.delta_t_hours

        energy_cost = sum(
            prices[t] * result.grid_power[t] * delta_t
            for t in range(n_timesteps)
        )
        self.metrics.total_energy_cost += energy_cost

        # Update peak demand
        # Per PRD Section 6.2: OptimizationResult uses peak_demand_kw field
        if result.peak_demand_kw > self.metrics.peak_demand_kw:
            self.metrics.peak_demand_kw = result.peak_demand_kw

        # Calculate demand charge cost
        # Per PRD Section 6.2: OptimizationResult uses peak_demand_kw field
        demand_cost = 20.0 * result.peak_demand_kw  # $20/kW
        self.metrics.total_demand_cost += demand_cost

        # Check vehicle readiness at departures
        for vid, t_dep in self.get_departure_times().items():
            if vid in result.schedule:
                soc_at_departure = result.schedule[vid]["soc"][t_dep]
                if soc_at_departure >= 0.99:
                    self.metrics.vehicles_ready_at_departure += 1
                else:
                    self.metrics.vehicles_not_ready += 1

    def get_metrics(self) -> SimulationMetrics:
        """Get current simulation metrics.

        Returns:
            SimulationMetrics instance
        """
        return self.metrics

    def get_cost_savings(
        self, unmanaged_cost: float
    ) -> dict[str, float]:
        """Calculate cost savings vs. unmanaged baseline.

        Args:
            unmanaged_cost: Total cost for unmanaged scenario

        Returns:
            Dictionary with savings metrics
        """
        total_cost = (
            self.metrics.total_energy_cost + self.metrics.total_demand_cost
        )
        savings = unmanaged_cost - total_cost
        savings_percent = (
            (savings / unmanaged_cost * 100) if unmanaged_cost > 0 else 0.0
        )

        return {
            "unmanaged_cost": unmanaged_cost,
            "optimized_cost": total_cost,
            "savings": savings,
            "savings_percent": savings_percent,
        }

    def print_summary(self) -> None:
        """Print simulation summary."""
        print("\n" + "=" * 60)
        print("SIMULATION SUMMARY")
        print("=" * 60)
        print(f"Optimizations run: {self.metrics.optimization_count}")
        print(
            f"Average solve time: {self.metrics.avg_solve_time:.2f} seconds"
        )
        print(f"Peak demand: {self.metrics.peak_demand_kw:.2f} kW")
        print(
            f"Total energy cost: ${self.metrics.total_energy_cost:.2f}"
        )
        print(
            f"Total demand cost: ${self.metrics.total_demand_cost:.2f}"
        )
        total_cost = (
            self.metrics.total_energy_cost
            + self.metrics.total_demand_cost
        )
        print(f"Total cost: ${total_cost:.2f}")
        print(
            f"Vehicles ready at departure: "
            f"{self.metrics.vehicles_ready_at_departure}"
        )
        print(
            f"Vehicles not ready: {self.metrics.vehicles_not_ready}"
        )
        print("=" * 60)

    def export_metrics(self, filepath: str, format: str = "json") -> None:
        """Export metrics to file.

        Args:
            filepath: Path to output file
            format: Output format ('json' or 'csv')
        """
        import json
        import csv
        from pathlib import Path

        path = Path(filepath)

        if format == "json":
            data = {
                "optimization_count": self.metrics.optimization_count,
                "avg_solve_time": self.metrics.avg_solve_time,
                "peak_demand_kw": self.metrics.peak_demand_kw,
                "total_energy_cost": self.metrics.total_energy_cost,
                "total_demand_cost": self.metrics.total_demand_cost,
                "total_cost": (
                    self.metrics.total_energy_cost
                    + self.metrics.total_demand_cost
                ),
                "vehicles_ready_at_departure": (
                    self.metrics.vehicles_ready_at_departure
                ),
                "vehicles_not_ready": self.metrics.vehicles_not_ready,
            }
            with open(path, "w") as f:
                json.dump(data, f, indent=2)
        elif format == "csv":
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Metric", "Value"])
                writer.writerow(
                    ["optimization_count", self.metrics.optimization_count]
                )
                writer.writerow(
                    ["avg_solve_time", self.metrics.avg_solve_time]
                )
                writer.writerow(
                    ["peak_demand_kw", self.metrics.peak_demand_kw]
                )
                writer.writerow(
                    ["total_energy_cost", self.metrics.total_energy_cost]
                )
                writer.writerow(
                    ["total_demand_cost", self.metrics.total_demand_cost]
                )
                writer.writerow(
                    [
                        "total_cost",
                        self.metrics.total_energy_cost
                        + self.metrics.total_demand_cost,
                    ]
                )
                writer.writerow(
                    [
                        "vehicles_ready_at_departure",
                        self.metrics.vehicles_ready_at_departure,
                    ]
                )
                writer.writerow(
                    ["vehicles_not_ready", self.metrics.vehicles_not_ready]
                )

    def get_state(self) -> dict:
        """Get current simulation state.

        Returns:
            Dictionary with time and vehicle states
        """
        return {
            "time": self.time.isoformat(),
            "vehicles": [
                {
                    "id": v.vehicle_id,
                    "soc": v.current_soc,
                    "is_charging": v.is_charging,
                    "is_on_route": v.is_on_route,
            "battery_capacity_kwh": v.battery_capacity_kwh,
                }
            for v in self.vehicles
            ],
            "battery": {
                "soc": self.battery.current_soc,
                "capacity_kwh": self.battery.capacity_kwh,
            },
        }

    def apply_schedule(self, schedule: dict) -> None:
        """Apply optimization schedule to vehicles.

        Args:
            schedule: Dictionary mapping vehicle_id to schedule dict
                with 'charging_power' list
        """
        for vid, sched in schedule.items():
            v = next(
                (v for v in self.vehicles if v.vehicle_id == vid), None
            )
            if v and sched.get('charging_power'):
                # Set charging based on first timestep
                if sched['charging_power'][0] > 0:
                    v.is_charging = True
                else:
                    v.is_charging = False

    def get_prices(self, horizon_hours: int = 24) -> list[float]:
        """Generate price schedule for horizon.

        Args:
            horizon_hours: Hours to generate prices for

        Returns:
            List of prices per timestep in $/kWh
        """
        n_timesteps = int(horizon_hours / self.delta_t_hours)
        prices: list[float] = []

        for t in range(n_timesteps):
            timestep_time = self.time + timedelta(hours=t * self.delta_t_hours)
            hour = timestep_time.hour

            # Check for price spike first
            if timestep_time in self.price_spikes:
                prices.append(self.price_spikes[timestep_time])
                continue

            # Generate base price based on profile
            if self.price_profile == "tou":
                # PG&E E-19 TOU structure
                if 16 <= hour < 21:  # Peak: 4pm-9pm
                    price = self.base_price * 2.0
                elif (9 <= hour < 16) or (21 <= hour < 24):  # Partial-peak
                    price = self.base_price * 1.25
                else:  # Off-peak
                    price = self.base_price * 0.83
            elif self.price_profile == "caiso":
                # CAISO-style with more volatility
                if 16 <= hour < 21:  # Peak
                    price = self.base_price * (1.8 + random.uniform(-0.2, 0.5))
                elif 9 <= hour < 16:  # Mid-day
                    price = self.base_price * (1.2 + random.uniform(-0.1, 0.3))
                elif 6 <= hour < 9:  # Morning ramp
                    price = self.base_price * (1.0 + random.uniform(-0.1, 0.2))
                else:  # Off-peak
                    price = self.base_price * (0.7 + random.uniform(-0.1, 0.2))
            else:  # Flat pricing
                price = self.base_price

            prices.append(max(0.01, price))  # Ensure non-negative

        return prices

    def inject_price_spike(self, timestamp: datetime, new_price: float) -> None:
        """Inject a price spike for trigger testing.

        Args:
            timestamp: When the price spike occurs
            new_price: New price in $/kWh
        """
        self.price_spikes[timestamp] = new_price

    def clear_price_spikes(self) -> None:
        """Clear all injected price spikes."""
        self.price_spikes.clear()

    def set_vehicle_on_route(
        self, vehicle_id: str, is_on_route: bool
    ) -> None:
        """Set vehicle route status.

        Args:
            vehicle_id: Vehicle identifier
            is_on_route: Whether vehicle is on route
        """
        v = next(
            (v for v in self.vehicles if v.vehicle_id == vehicle_id), None
        )
        if v:
            v.is_on_route = is_on_route
            # Can't charge while on route
            if is_on_route:
                v.is_charging = False


class SimulationOptimizer:
    """Optimizer integration for simulation.

    Converts simulator state to DepotState, runs optimization,
    and applies results back to simulator.
    """

    def __init__(
        self,
        simulator: "DepotSimulator",
        config: "DepotConfig",
    ):
        """Initialize simulation optimizer.

        Args:
            simulator: DepotSimulator instance
            config: DepotConfig for optimization
        """
        self.simulator = simulator
        self.config = config
        self.last_result: Optional["OptimizationResult"] = None

    def _assemble_state(self, horizon_hours: int = 24) -> "DepotState":
        """Convert simulator state to DepotState.

        Args:
            horizon_hours: Optimization horizon in hours

        Returns:
            DepotState for optimization
        """
        from src.core.models import DepotState

        # Get vehicle SoCs
        vehicle_socs = {
            v.vehicle_id: v.current_soc for v in self.simulator.vehicles
        }

        # Get battery SoC
        battery_soc = self.simulator.battery.current_soc

        # Get prices
        prices = self.simulator.get_prices(horizon_hours)

        # Get vehicle availability
        availability = self.simulator.get_vehicle_availability(
            start=self.simulator.time, horizon_hours=horizon_hours
        )

        # Get departure times
        departure_times = self.simulator.get_departure_times(
            start=self.simulator.time, horizon_hours=horizon_hours
        )

        # Get energy requirements from routes
        energy_requirements: dict[str, float] = {}
        for route in self.simulator.routes:
            if route.vehicle_id not in energy_requirements:
                energy_requirements[route.vehicle_id] = route.energy_kwh

        # Default energy requirement if not in routes
        for vid in vehicle_socs.keys():
            if vid not in energy_requirements:
                energy_requirements[vid] = 200.0  # Default

        # Demand charge rate (default)
        demand_charge_rate = 20.0

        # Current month peak (default)
        current_month_peak = 0.0

        # Building power (default: constant 50 kW)
        n_timesteps = int(horizon_hours / self.simulator.delta_t_hours)
        building_power = [50.0] * n_timesteps

        return DepotState(
            vehicle_socs=vehicle_socs,
            battery_soc=battery_soc,
            prices=prices,
            demand_charge_rate=demand_charge_rate,
            current_month_peak=current_month_peak,
            vehicle_availability=availability,
            energy_requirements=energy_requirements,
            departure_times=departure_times,
            building_power=building_power,
        )

    async def optimize(
        self, horizon_hours: int = 24, time_limit: float = 30.0
    ) -> "OptimizationResult":
        """Run optimization for current simulator state.

        Args:
            horizon_hours: Optimization horizon in hours
            time_limit: Maximum solve time in seconds

        Returns:
            OptimizationResult
        """
        from src.core.optimizer import optimize
        from src.core.models import OptimizationResult

        # Assemble state
        state = self._assemble_state(horizon_hours)

        # Run optimizer
        result = optimize(
            state,
            self.config,
            time_limit=time_limit,
            previous_result=self.last_result,
        )

        self.last_result = result
        return result

    def apply_result(self, result: "OptimizationResult") -> None:
        """Apply optimization result to simulator.

        Args:
            result: OptimizationResult to apply
        """
        # Apply charging schedules to vehicles
        self.simulator.apply_schedule(result.schedule)

        # Apply battery dispatch
        self.simulator.apply_battery_dispatch(
            result.battery_dispatch, start_time=self.simulator.time
        )

        # Update metrics
        self.simulator.update_metrics(result)


async def run_simulation(
    optimizer_func: Optional[callable] = None, steps: int = 96
) -> None:
    """Run simulation with optimization.

    Args:
        optimizer_func: Optional function to call for optimization
            Signature: (simulator: DepotSimulator) -> dict schedule
        steps: Number of simulation steps (default: 96 = 24 hours)
    """
    sim = DepotSimulator(n_vehicles=5)

    print(f"Starting simulation: {sim.time}")
    print(f"Vehicles: {len(sim.vehicles)}, Chargers: {sim.n_chargers}")

    for step in range(steps):
        print(f"\nStep {step}: {sim.time}")

        # Every 4 steps (hourly), run optimization
        if step % 4 == 0 and optimizer_func:
            try:
                schedule = await optimizer_func(sim)
                if schedule:
                    sim.apply_schedule(schedule)
                    print(f"  Applied optimization schedule")
            except Exception as e:
                print(f"  Optimization error: {e}")

        # Advance simulation
        sim.step()

        # Print state
        state = sim.get_state()
        avg_soc = sum(v['soc'] for v in state['vehicles']) / len(
            state['vehicles']
        )
        charging_count = sum(1 for v in state['vehicles'] if v['is_charging'])
        print(
            f"  Avg SoC: {avg_soc:.2f}, "
            f"Charging: {charging_count}/{sim.n_chargers}"
        )

    print(f"\nSimulation complete: {sim.time}")


if __name__ == "__main__":
    asyncio.run(run_simulation())

