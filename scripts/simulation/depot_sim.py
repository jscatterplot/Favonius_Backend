"""Depot simulation for testing optimization.

Reference: Development plan Step 6.1, PRD.md#11-acceptance-criteria
"""

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

logger = None  # Will be set if logging is needed


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

    Reference: Development plan Step 6.1
    """

    def __init__(self, n_vehicles: int = 10, n_chargers: int = 5):
        """Initialize depot simulator.

        Args:
            n_vehicles: Number of vehicles in fleet
            n_chargers: Number of available chargers
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
        self.time = datetime.utcnow()
        self.charging_power_kw = 80.0  # Standard charger power

    def step(self, dt_minutes: float = 15) -> None:
        """Advance simulation by dt_minutes.

        Updates vehicle SoC based on charging and route consumption.

        Args:
            dt_minutes: Time step in minutes (default: 15)
        """
        self.time += timedelta(minutes=dt_minutes)

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
                energy_used = random.uniform(10, 30)  # kWh per 15 min
                v.current_soc = max(
                    0.1, v.current_soc - energy_used / v.battery_capacity_kwh
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

