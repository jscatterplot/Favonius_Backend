"""State assembler for optimization inputs.

Reference: Development plan Step 4.1, PRD.md#5-system-architecture
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import asyncpg

from ..models import DepotConfig, DepotState

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class StateAssembler:
    """Assembles current depot state for optimization.

    Aggregates data from multiple sources (telemetry, prices, schedules)
    into a DepotState object for the optimization engine.

    Reference: PRD Section 5.2, Development plan Step 4.1
    """

    def __init__(
        self, pool: asyncpg.Pool, depot_id: str | UUID, config: DepotConfig
    ):
        """Initialize state assembler.

        Args:
            pool: Database connection pool
            depot_id: Depot identifier
            config: Depot configuration
        """
        self.pool = pool
        self.depot_id = str(depot_id)
        self.config = config
        logger.info(f"Initialized StateAssembler for depot {self.depot_id}")

    async def get_current_state(
        self,
        horizon_hours: int = 24,
    ) -> DepotState:
        """Assemble current depot state from all sources.

        Args:
            horizon_hours: Optimization horizon in hours (default: 24)

        Returns:
            DepotState object ready for optimization

        Raises:
            Exception: If database queries fail
        """
        now = datetime.utcnow()
        horizon_end = now + timedelta(hours=horizon_hours)
        n_steps = int(horizon_hours / self.config.delta_t)

        logger.info(
            f"Assembling state for depot {self.depot_id}, "
            f"horizon: {now} to {horizon_end} ({n_steps} timesteps)"
        )

        # Fetch vehicle SoCs from latest telemetry
        vehicle_socs = await self._get_vehicle_socs()

        # Fetch battery SoC
        battery_soc = await self._get_battery_soc()

        # Fetch prices
        prices = await self._get_prices(now, horizon_end, n_steps)

        # Fetch schedules and compute availability
        schedules = await self._get_schedules(now, horizon_end)
        availability = self._compute_availability(schedules, now, n_steps)
        departure_times = self._compute_departure_times(schedules, now)
        energy_requirements = self._compute_energy_requirements(schedules)

        # Get current month peak
        current_month_peak = await self._get_current_month_peak()

        # Get demand charge rate
        demand_charge_rate = await self._get_demand_charge_rate()

        # Get building power (default to zero for MVP)
        building_power = await self._get_building_power(now, horizon_end, n_steps)

        state = DepotState(
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

        logger.info(
            f"State assembled: {len(vehicle_socs)} vehicles, "
            f"{len(prices)} price points, {len(schedules)} schedules"
        )
        return state

    async def _get_vehicle_socs(self) -> dict[str, float]:
        """Get latest SoC for all vehicles.

        Returns:
            Dictionary mapping vehicle_id (str) to SoC (float 0-1)
        """
        query = """
        SELECT DISTINCT ON (vehicle_id) 
            vehicle_id::text, soc
        FROM telemetry t
        JOIN vehicles v ON t.vehicle_id = v.vehicle_id
        WHERE v.depot_id = $1
        ORDER BY vehicle_id, time DESC
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, self.depot_id)

        result = {str(row['vehicle_id']): float(row['soc']) for row in rows}
        logger.debug(f"Retrieved SoC for {len(result)} vehicles")
        return result

    async def _get_battery_soc(self) -> float:
        """Get stationary battery SoC.

        Returns:
            Battery SoC (float 0-1)

        Note:
            For MVP, returns fixed value. Future: query from battery_storage table.
        """
        # TODO: Query from battery_storage table when implemented
        return 0.5

    async def _get_prices(
        self, start: datetime, end: datetime, n_steps: int
    ) -> list[float]:
        """Get electricity prices for horizon.

        Args:
            start: Start time
            end: End time
            n_steps: Number of timesteps

        Returns:
            List of prices in $/kWh, one per timestep
        """
        query = """
        SELECT time, energy_kwh as price_per_kwh
        FROM prices
        WHERE depot_id = $1 AND time >= $2 AND time < $3
        ORDER BY time
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, self.depot_id, start, end)

        # Interpolate to match optimization timesteps
        if not rows:
            logger.warning("No price data found, using default $0.15/kWh")
            return [0.15] * n_steps  # Default price

        # Simplified: use hourly prices, repeat for 15-min steps
        prices = []
        for row in rows:
            price = float(row['price_per_kwh'])
            # Repeat price for 4 timesteps (1 hour = 4 x 15 min)
            prices.extend([price] * 4)

        # Trim to exact number of timesteps
        return prices[:n_steps]

    async def _get_schedules(
        self, start: datetime, end: datetime
    ) -> list[dict]:
        """Get vehicle schedules.

        Args:
            start: Start time
            end: End time

        Returns:
            List of schedule dictionaries
        """
        query = """
        SELECT vehicle_id::text, departure_time, return_time, 
               energy_kwh as estimated_energy_kwh, route_id
        FROM schedules s
        JOIN vehicles v ON s.vehicle_id = v.vehicle_id
        WHERE v.depot_id = $1 
          AND s.departure_time >= $2 
          AND s.departure_time < $3
        ORDER BY departure_time
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, self.depot_id, start, end)

        schedules = [dict(row) for row in rows]
        logger.debug(f"Retrieved {len(schedules)} schedules")
        return schedules

    def _compute_availability(
        self, schedules: list[dict], start: datetime, n_steps: int
    ) -> dict[str, list[bool]]:
        """Compute per-vehicle availability for each timestep.

        Args:
            schedules: List of schedule dictionaries
            start: Start time of optimization horizon
            n_steps: Number of timesteps

        Returns:
            Dictionary mapping vehicle_id to list of availability booleans
        """
        delta_t = timedelta(hours=self.config.delta_t)

        # Initialize all vehicles as available
        availability = {
            vid: [True] * n_steps for vid in self.config.vehicle_capacities.keys()
        }

        for sched in schedules:
            vid = sched['vehicle_id']
            if vid not in availability:
                continue

            dep = sched['departure_time']
            ret = sched['return_time']

            for t in range(n_steps):
                step_time = start + t * delta_t
                # Vehicle is unavailable if on route
                if dep <= step_time < ret:
                    availability[vid][t] = False

        return availability

    def _compute_departure_times(
        self, schedules: list[dict], start: datetime
    ) -> dict[str, int]:
        """Compute timestep index for each vehicle's next departure.

        Args:
            schedules: List of schedule dictionaries
            start: Start time of optimization horizon

        Returns:
            Dictionary mapping vehicle_id to timestep index
        """
        delta_t = timedelta(hours=self.config.delta_t)
        departures = {}

        for sched in schedules:
            vid = sched['vehicle_id']
            dep = sched['departure_time']
            t_idx = int((dep - start) / delta_t)

            # Keep earliest departure for each vehicle
            if vid not in departures or t_idx < departures[vid]:
                departures[vid] = t_idx

        return departures

    def _compute_energy_requirements(
        self, schedules: list[dict]
    ) -> dict[str, float]:
        """Compute energy needed for each vehicle's next trip.

        Args:
            schedules: List of schedule dictionaries

        Returns:
            Dictionary mapping vehicle_id to energy requirement (kWh)
        """
        requirements = {}
        for sched in schedules:
            vid = sched['vehicle_id']
            energy = sched.get('estimated_energy_kwh') or 100.0  # default
            if vid not in requirements:
                requirements[vid] = float(energy)
        return requirements

    async def _get_current_month_peak(self) -> float:
        """Get maximum grid power this billing month.

        Returns:
            Peak demand in kW
        """
        query = """
        SELECT MAX(peak_demand_kw) as peak
        FROM optimization_runs
        WHERE depot_id = $1
          AND date_trunc('month', run_time) = date_trunc('month', NOW())
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, self.depot_id)

        peak = float(row['peak']) if row and row['peak'] else 0.0
        logger.debug(f"Current month peak: {peak} kW")
        return peak

    async def _get_demand_charge_rate(self) -> float:
        """Get demand charge rate ($/kW).

        Returns:
            Demand charge rate in $/kW

        Note:
            For MVP, hardcoded PG&E E-19 rate. Future: query from depot config.
        """
        # TODO: Query from depots table when demand_charge_rate_kw column exists
        return 20.0  # $/kW

    async def _get_building_power(
        self, start: datetime, end: datetime, n_steps: int
    ) -> list[float]:
        """Get building power load for horizon.

        Args:
            start: Start time
            end: End time
            n_steps: Number of timesteps

        Returns:
            List of building power in kW, one per timestep

        Note:
            For MVP, returns zero. Future: query from building_loads table.
        """
        # TODO: Query from building_loads table when implemented
        return [0.0] * n_steps

