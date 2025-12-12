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

    Example:
        ```python
        from src.core.models import DepotConfig
        from src.core.state.assembler import StateAssembler
        import asyncpg

        # Initialize
        pool = await asyncpg.create_pool("postgresql://...")
        config = DepotConfig(
            vehicle_capacities={'bus_1': 324.0},
            charger_power=80.0,
            charger_efficiency=0.95,
            n_chargers=5,
            battery_capacity=500.0,
            battery_power=100.0,
            max_site_power=800.0,
        )
        assembler = StateAssembler(pool, depot_id="depot_123", config=config)

        # Assemble state
        state = await assembler.get_current_state(horizon_hours=24)

        # Use state for optimization
        # ...
        ```
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
            ValueError: If horizon_hours is invalid
            Exception: If database queries fail
        """
        # Validate horizon
        if horizon_hours <= 0 or horizon_hours > 48:
            raise ValueError(
                f"horizon_hours must be in (0, 48], got {horizon_hours}"
            )

        import time

        start_time = time.time()
        now = datetime.utcnow()
        horizon_end = now + timedelta(hours=horizon_hours)
        n_steps = int(horizon_hours / self.config.delta_t)

        logger.info(
            f"Assembling state for depot {self.depot_id}, "
            f"horizon: {now} to {horizon_end} ({n_steps} timesteps)"
        )

        # Fetch vehicle SoCs from latest telemetry
        vehicle_socs = await self._get_vehicle_socs()

        # Validate vehicle_socs matches config
        if not vehicle_socs:
            logger.warning(
                f"No vehicle SoC data found for depot {self.depot_id}, "
                "using default SoC for all configured vehicles"
            )
            # Use default SoC for all configured vehicles
            vehicle_socs = {
                vid: 0.5  # Default SoC
                for vid in self.config.vehicle_capacities.keys()
            }

        # Ensure all vehicles in config have SoC data
        missing_vehicles = set(self.config.vehicle_capacities.keys()) - set(
            vehicle_socs.keys()
        )
        if missing_vehicles:
            logger.warning(
                f"Missing SoC for vehicles: {missing_vehicles}, using default 0.5"
            )
            for vid in missing_vehicles:
                vehicle_socs[vid] = 0.5

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

        assembly_time = time.time() - start_time
        logger.info(
            f"State assembled: {len(vehicle_socs)} vehicles, "
            f"{len(prices)} price points, {len(schedules)} schedules "
            f"(took {assembly_time:.3f}s)",
            extra={
                'depot_id': self.depot_id,
                'assembly_time_seconds': assembly_time,
                'n_vehicles': len(vehicle_socs),
                'n_price_points': len(prices),
                'n_schedules': len(schedules),
            },
        )
        return state

    async def _get_vehicle_socs(self) -> dict[str, float]:
        """Get latest SoC for all vehicles.

        Returns:
            Dictionary mapping vehicle_id (str) to SoC (float 0-1)

        Raises:
            asyncpg.PostgresError: If database query fails
        """
        query = """
        SELECT DISTINCT ON (vehicle_id) 
            vehicle_id::text, soc
        FROM telemetry t
        JOIN vehicles v ON t.vehicle_id = v.vehicle_id
        WHERE v.depot_id = $1
        ORDER BY vehicle_id, time DESC
        """
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, self.depot_id)

            result = {str(row['vehicle_id']): float(row['soc']) for row in rows}
            logger.debug(f"Retrieved SoC for {len(result)} vehicles")
            return result
        except asyncpg.PostgresError as e:
            logger.error(
                f"Database error fetching vehicle SoCs for depot {self.depot_id}: {e}"
            )
            raise

    async def _get_battery_soc(self) -> float:
        """Get stationary battery SoC.

        Returns:
            Battery SoC (float 0-1)

        Note:
            For MVP, returns fixed value. Future: query from battery telemetry table.
            Battery SoC would be tracked in a time-series table similar to vehicle
            telemetry, or computed from last optimization result's battery_dispatch.
        """
        # TODO: Query from battery_telemetry table when implemented
        # For now, could query from optimization_runs to get last known battery state
        # Or compute from last optimization result's battery_dispatch

        # MVP: return fixed value
        logger.debug("Using default battery SoC 0.5 (MVP)")
        return 0.5

    async def _get_prices(
        self, start: datetime, end: datetime, n_steps: int
    ) -> list[float]:
        """Get electricity prices for horizon with proper interpolation.

        Args:
            start: Start time
            end: End time
            n_steps: Number of timesteps

        Returns:
            List of prices in $/kWh, one per timestep

        Note:
            Handles missing price data by forward-filling from last known price
            within 1 hour. Falls back to default $0.15/kWh if no data available.

        Raises:
            asyncpg.PostgresError: If database query fails
        """
        query = """
        SELECT time, energy_kwh as price_per_kwh
        FROM prices
        WHERE depot_id = $1 AND time >= $2 AND time < $3
        ORDER BY time
        """
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, self.depot_id, start, end)
        except asyncpg.PostgresError as e:
            logger.error(
                f"Database error fetching prices for depot {self.depot_id}: {e}. "
                "Using default prices."
            )
            # Fallback to default prices on database error
            return [0.15] * n_steps

        if not rows:
            logger.warning(
                f"No price data found for depot {self.depot_id} "
                f"between {start} and {end}, using default $0.15/kWh"
            )
            return [0.15] * n_steps  # Default price

        # Build time-indexed price map
        price_map = {row['time']: float(row['price_per_kwh']) for row in rows}

        # Generate prices for each timestep
        delta_t = timedelta(hours=self.config.delta_t)
        prices = []
        last_price = 0.15  # Default fallback

        for t in range(n_steps):
            step_time = start + t * delta_t

            # Find closest price (exact match or forward-fill)
            if step_time in price_map:
                last_price = price_map[step_time]
            # Forward-fill: use last known price if within 1 hour
            elif price_map:
                closest_time = min(
                    price_map.keys(),
                    key=lambda x: abs((x - step_time).total_seconds()),
                )
                time_diff = abs((closest_time - step_time).total_seconds())
                if time_diff < 3600:  # Within 1 hour
                    last_price = price_map[closest_time]

            prices.append(last_price)

        logger.debug(
            f"Price interpolation: {len(price_map)} price points -> {len(prices)} timesteps"
        )
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

        Raises:
            asyncpg.PostgresError: If database query fails
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
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, self.depot_id, start, end)

            schedules = [dict(row) for row in rows]
            logger.debug(f"Retrieved {len(schedules)} schedules")
            return schedules
        except asyncpg.PostgresError as e:
            logger.error(
                f"Database error fetching schedules for depot {self.depot_id}: {e}"
            )
            raise

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
        """Get demand charge rate ($/kW) from depot configuration.

        Returns:
            Demand charge rate in $/kW

        Note:
            Falls back to default $20/kW if depot not found or rate is NULL.
        """
        query = """
        SELECT demand_charge_rate_kw
        FROM depots
        WHERE depot_id = $1
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, self.depot_id)

        if row and row['demand_charge_rate_kw'] is not None:
            rate = float(row['demand_charge_rate_kw'])
            logger.debug(f"Demand charge rate from DB: ${rate}/kW")
            return rate

        # Fallback to default if not found
        logger.warning(
            f"Depot {self.depot_id} not found or rate is NULL, using default $20/kW"
        )
        return 20.0  # Default PG&E E-19 rate

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

    @classmethod
    async def load_depot_config(
        cls, pool: asyncpg.Pool, depot_id: str | UUID
    ) -> tuple[DepotConfig, dict[str, str]]:
        """Load depot configuration from database.

        This is an optional enhancement that allows loading DepotConfig
        from the database instead of passing it in during initialization.

        Args:
            pool: Database connection pool
            depot_id: Depot identifier

        Returns:
            Tuple of (DepotConfig, vehicle_id_to_ocpp_id mapping)

        Raises:
            ValueError: If depot not found or configuration is invalid

        Note:
            This method queries depots, vehicles, chargers, and battery_storage
            tables to construct a complete DepotConfig object.
        """
        depot_id_str = str(depot_id)

        # Query depot configuration
        depot_query = """
        SELECT max_grid_kw, demand_charge_rate_kw
        FROM depots
        WHERE depot_id = $1
        """
        async with pool.acquire() as conn:
            depot_row = await conn.fetchrow(depot_query, depot_id_str)

        if not depot_row:
            raise ValueError(f"Depot {depot_id_str} not found")

        max_site_power = float(depot_row['max_grid_kw'])

        # Query vehicles
        vehicles_query = """
        SELECT vehicle_id::text, battery_kwh, max_charge_kw, ocpp_id
        FROM vehicles
        WHERE depot_id = $1
        """
        async with pool.acquire() as conn:
            vehicle_rows = await conn.fetch(vehicles_query, depot_id_str)

        if not vehicle_rows:
            raise ValueError(f"No vehicles found for depot {depot_id_str}")

        vehicle_capacities = {}
        vehicle_to_ocpp = {}
        for row in vehicle_rows:
            vid = row['vehicle_id']
            vehicle_capacities[vid] = float(row['battery_kwh'])
            if row['ocpp_id']:
                vehicle_to_ocpp[vid] = row['ocpp_id']

        # Query chargers
        chargers_query = """
        SELECT rated_kw, efficiency
        FROM chargers
        WHERE depot_id = $1
        ORDER BY rated_kw DESC
        LIMIT 1
        """
        async with pool.acquire() as conn:
            charger_row = await conn.fetchrow(chargers_query, depot_id_str)

        if charger_row:
            charger_power = float(charger_row['rated_kw'])
            charger_efficiency = float(charger_row['efficiency'])
        else:
            # Defaults if no chargers found
            logger.warning(f"No chargers found for depot {depot_id_str}, using defaults")
            charger_power = 80.0
            charger_efficiency = 0.95

        # Count chargers
        n_chargers_query = """
        SELECT COUNT(*) as count
        FROM chargers
        WHERE depot_id = $1
        """
        async with pool.acquire() as conn:
            n_chargers_row = await conn.fetchrow(n_chargers_query, depot_id_str)
        n_chargers = int(n_chargers_row['count']) if n_chargers_row else 0

        # Query battery storage
        battery_query = """
        SELECT capacity_kwh, max_power_kw
        FROM battery_storage
        WHERE depot_id = $1
        LIMIT 1
        """
        async with pool.acquire() as conn:
            battery_row = await conn.fetchrow(battery_query, depot_id_str)

        if battery_row:
            battery_capacity = float(battery_row['capacity_kwh'])
            battery_power = float(battery_row['max_power_kw'])
        else:
            # Defaults if no battery found
            logger.warning(f"No battery storage found for depot {depot_id_str}, using defaults")
            battery_capacity = 0.0
            battery_power = 0.0

        config = DepotConfig(
            vehicle_capacities=vehicle_capacities,
            charger_power=charger_power,
            charger_efficiency=charger_efficiency,
            n_chargers=n_chargers,
            battery_capacity=battery_capacity,
            battery_power=battery_power,
            max_site_power=max_site_power,
        )

        logger.info(
            f"Loaded depot config: {len(vehicle_capacities)} vehicles, "
            f"{n_chargers} chargers, {battery_capacity} kWh battery"
        )

        return config, vehicle_to_ocpp

