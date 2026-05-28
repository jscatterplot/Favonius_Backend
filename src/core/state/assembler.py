"""State assembler for optimization inputs.

Reference: Development plan Step 4.1, PRD.md#5-system-architecture
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from uuid import UUID

import asyncpg

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...adapters.weather.storage import DEFAULT_WEATHER_SOURCE
from ..models import DepotConfig, DepotState, IncomingVehicle
from ..scheduling.recurring import (
    RecurringTemplate,
    ScheduleCancellation,
    expand_recurring_templates,
    merge_recurring_with_manual,
)
from ...db.pools import DatabasePools

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Sentinel for the lazy bidding-zone cache on StateAssembler instances:
# ``None`` is a valid resolved value meaning "this depot has no zone";
# we need a distinct marker to distinguish "not yet resolved" from
# "resolved to nothing".
_SENTINEL = object()


def _as_aware_utc(value: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC.

    ``get_current_state`` works in naive UTC (``datetime.utcnow()``) while
    schedule rows come back tz-aware from Postgres ``timestamptz``; comparing
    the two raises ``TypeError: can't compare offset-naive and offset-aware
    datetimes``. Naive inputs are assumed to already be UTC.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


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

    def __init__(self, pools: DatabasePools, depot_id: str | UUID, config: DepotConfig):
        """Initialize state assembler.

        Args:
            pools: Dual database connection pools (static=Supabase, ts=TimescaleDB)
            depot_id: Depot identifier
            config: Depot configuration
        """
        self.pools = pools
        self.depot_id = str(depot_id)
        self.config = config
        # Snapshot metadata captured during the most recent assembly. Read
        # by DepotController to build OptimizationInputSnapshot rows.
        self._last_building_load_source: str = "absent"
        self._last_schedules: list[dict] = []
        self._last_schedules_present: bool = False
        self._last_weather_features: list[dict] = []
        # forecast_id of the bundle the weather_features were drawn from.
        # None when no bundle was visible at horizon_start (and so
        # weather_features is the empty list). Used by the controller to
        # populate optimization_input_snapshots.weather_forecast_id.
        self._last_weather_forecast_id: UUID | None = None
        self._last_horizon: tuple[datetime, datetime] | None = None
        self._last_organization_id: str | None = None
        self._last_recent_telemetry: list[dict] = []
        logger.info(f"Initialized StateAssembler for depot {self.depot_id}")

    @property
    def last_building_load_source(self) -> str:
        return self._last_building_load_source

    @property
    def last_schedules(self) -> list[dict]:
        return list(self._last_schedules)

    @property
    def last_schedules_present(self) -> bool:
        return self._last_schedules_present

    @property
    def last_weather_features(self) -> list[dict]:
        return list(self._last_weather_features)

    @property
    def last_weather_forecast_id(self) -> UUID | None:
        """forecast_id of the bundle backing ``last_weather_features``.

        ``None`` when no bundle was visible at ``horizon_start``. The
        controller writes this onto ``optimization_input_snapshots``
        only when ``last_weather_features`` is non-empty so the FK
        always points to the captured bundle.
        """
        return self._last_weather_forecast_id

    @property
    def last_horizon(self) -> tuple[datetime, datetime] | None:
        return self._last_horizon

    @property
    def last_organization_id(self) -> str | None:
        return self._last_organization_id

    @property
    def last_recent_telemetry(self) -> list[dict]:
        """Telemetry rows captured for the most recent snapshot context.

        See :meth:`_get_recent_telemetry`. Empty until ``fetch_snapshot_extras``
        runs.
        """
        return list(self._last_recent_telemetry)

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
            raise ValueError(f"horizon_hours must be in (0, 48], got {horizon_hours}")

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
                vid: 0.5 for vid in self.config.vehicle_capacities.keys()  # Default SoC
            }

        # Ensure all vehicles in config have SoC data
        missing_vehicles = set(self.config.vehicle_capacities.keys()) - set(vehicle_socs.keys())
        if missing_vehicles:
            logger.warning(f"Missing SoC for vehicles: {missing_vehicles}, using default 0.5")
            for vid in missing_vehicles:
                vehicle_socs[vid] = 0.5

        # Fetch battery SoC
        battery_soc = await self._get_battery_soc()

        # Fetch prices
        prices = await self._get_prices(now, horizon_end, n_steps)

        # Fetch schedules and VDV 463 charging requests; merge (VDV 463 overrides for same vehicle)
        schedules = await self._get_schedules(now, horizon_end)
        # Capture presence for snapshot/readiness *before* VDV 463 merge —
        # a VDV 463 request alone shouldn't make the system claim a route
        # schedule was loaded.
        self._last_schedules_present = bool(schedules)
        vdv463_requests = await self._get_vdv463_charging_requests(now, horizon_end)
        (
            schedules,
            vehicle_departure_soc_min,
            vehicle_departure_soc_max,
            vehicle_priorities,
            preconditioning_requests,
        ) = self._merge_vdv463_into_schedules(schedules, vdv463_requests, now)
        availability = self._compute_availability(schedules, now, n_steps)
        departure_times = self._compute_departure_times(schedules, now)
        energy_requirements = self._compute_energy_requirements(schedules)

        # Get current month peak
        current_month_peak = await self._get_current_month_peak()

        # Get demand charge rate
        demand_charge_rate = await self._get_demand_charge_rate()

        # Cumulative kWh consumed in the current billing period — only
        # meaningful for the energy_cap tariff. For simple_demand depots we
        # skip the query entirely and leave the value at 0.
        if self.config.tariff_type == "energy_cap":
            cumulative_kwh_period = await self._get_cumulative_kwh_period(now)
        else:
            cumulative_kwh_period = 0.0

        # Get building power (REQUIRED per PRD Section 9.4)
        building_power = await self._get_building_power(now, horizon_end, n_steps)

        # Snapshot metadata: only the cheap in-memory bits here. Weather
        # features and organization_id are fetched separately by the
        # controller (see ``fetch_snapshot_extras``) so existing callers
        # of get_current_state see no additional DB traffic.
        self._last_horizon = (now, horizon_end)
        # Schedules used downstream may have been merged with VDV 463
        # entries; snapshot the merged list since that's what the MILP
        # actually consumed.
        self._last_schedules = list(schedules)

        # Get incoming vehicles from inter-depot handoffs (per PRD Section 5.3)
        incoming_vehicles = await self._get_incoming_vehicles(now, horizon_end)

        # Integrate incoming vehicles into state
        # Per PRD Section 8.1 Constraint 12: Add to vehicle_socs with expected_soc at arrival
        for incoming in incoming_vehicles:
            vehicle_id_str = str(incoming.vehicle_id)
            # Add to vehicle_socs with expected_soc (will be fixed at arrival time)
            vehicle_socs[vehicle_id_str] = incoming.expected_soc
            # Add to vehicle_capacities if not present
            if vehicle_id_str not in self.config.vehicle_capacities:
                self.config.vehicle_capacities[vehicle_id_str] = incoming.battery_kwh
            # Add to vehicle_max_charge_kw if not present
            if vehicle_id_str not in self.config.vehicle_max_charge_kw:
                self.config.vehicle_max_charge_kw[vehicle_id_str] = incoming.max_charge_kw
            # Set availability: False before arrival, True after (per PRD Section 8.1 Constraint 12)
            arrival_timestep = int(
                (incoming.arrival_time - now).total_seconds() / (self.config.delta_t * 3600)
            )
            if 0 <= arrival_timestep < n_steps:
                if vehicle_id_str not in availability:
                    availability[vehicle_id_str] = [False] * n_steps
                # Vehicle unavailable before arrival
                for t in range(min(arrival_timestep, n_steps)):
                    availability[vehicle_id_str][t] = False
                # Vehicle available after arrival
                for t in range(arrival_timestep, n_steps):
                    availability[vehicle_id_str][t] = True

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
            incoming_vehicles=incoming_vehicles,
            vehicle_departure_soc_min=vehicle_departure_soc_min,
            vehicle_departure_soc_max=vehicle_departure_soc_max,
            vehicle_priorities=vehicle_priorities,
            preconditioning_requests=preconditioning_requests,
            cumulative_kwh_period=cumulative_kwh_period,
        )

        assembly_time = time.time() - start_time
        logger.info(
            f"State assembled: {len(vehicle_socs)} vehicles, "
            f"{len(prices)} price points, {len(schedules)} schedules "
            f"(took {assembly_time:.3f}s)",
            extra={
                "depot_id": self.depot_id,
                "assembly_time_seconds": assembly_time,
                "n_vehicles": len(vehicle_socs),
                "n_price_points": len(prices),
                "n_schedules": len(schedules),
            },
        )
        return state

    async def _get_vehicle_socs(self) -> dict[str, float]:
        """Get latest SoC for all vehicles.

        Vehicles live in Supabase (static pool); telemetry lives in TimescaleDB (ts pool).
        We fetch vehicle_ids from the static pool first, then query the ts pool.

        Returns:
            Dictionary mapping vehicle_id (str) to SoC (float 0-1)

        Raises:
            asyncpg.PostgresError: If database query fails
        """
        try:
            # Step 1: Get vehicle_ids for this depot from Supabase (static)
            async with self.pools.static.acquire() as conn:
                vehicle_rows = await conn.fetch(
                    "SELECT id::text AS vehicle_id FROM vehicles WHERE site_id = $1",
                    self.depot_id,
                )
            vehicle_ids = [row["vehicle_id"] for row in vehicle_rows]
            if not vehicle_ids:
                return {}

            # Step 2: Get latest SoC per vehicle from TimescaleDB (ts).
            # Two sources: charger-side `telemetry` (OCPP MeterValues) and
            # `vehicle_telemetry` (Navirec telematics feed, migration 044).
            # UNION both and keep the freshest reading per vehicle so a vehicle
            # that's unplugged (out on a route) still has a live SoC. The 24h
            # lower bound confines the DISTINCT ON scan to recent chunks;
            # telematics rows carry true device timestamps, so the downstream
            # 15-min freshness check (data_freshness.MAX_TELEMETRY_AGE) still
            # governs whether a SoC is fresh enough to optimize on. On an exact
            # (vehicle_id, time) tie, src_priority makes charger telemetry win
            # deterministically (ground truth when the vehicle is plugged in).
            merged_sql = """
                SELECT DISTINCT ON (vehicle_id)
                    vehicle_id::text AS vehicle_id,
                    soc
                FROM (
                    SELECT vehicle_id, soc, time, 0 AS src_priority
                    FROM telemetry
                    WHERE vehicle_id = ANY($1::uuid[])
                      AND soc IS NOT NULL
                      AND time > now() - INTERVAL '24 hours'
                    UNION ALL
                    SELECT vehicle_id, soc, time, 1 AS src_priority
                    FROM vehicle_telemetry
                    WHERE vehicle_id = ANY($1::uuid[])
                      AND soc IS NOT NULL
                      AND time > now() - INTERVAL '24 hours'
                ) merged
                ORDER BY vehicle_id, time DESC, src_priority
            """
            # Fallback when vehicle_telemetry doesn't exist yet (migration 044
            # not applied / app-rollout skew). Without it, every depot's state
            # assembly would break — even with Navirec polling disabled.
            telemetry_only_sql = """
                SELECT DISTINCT ON (vehicle_id)
                    vehicle_id::text AS vehicle_id,
                    soc
                FROM telemetry
                WHERE vehicle_id = ANY($1::uuid[])
                  AND soc IS NOT NULL
                  AND time > now() - INTERVAL '24 hours'
                ORDER BY vehicle_id, time DESC
            """
            try:
                async with self.pools.ts.acquire() as conn:
                    rows = await conn.fetch(merged_sql, vehicle_ids)
            except asyncpg.exceptions.UndefinedTableError:
                logger.warning(
                    "vehicle_telemetry missing (migration 044 not applied?); "
                    "falling back to charger telemetry only for depot %s",
                    self.depot_id,
                )
                async with self.pools.ts.acquire() as conn:
                    rows = await conn.fetch(telemetry_only_sql, vehicle_ids)

            result = {
                str(row["vehicle_id"]): float(row["soc"]) for row in rows if row["soc"] is not None
            }
            logger.debug(f"Retrieved SoC for {len(result)} vehicles")
            return result
        except asyncpg.PostgresError as e:
            logger.error(f"Database error fetching vehicle SoCs for depot {self.depot_id}: {e}")
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

    async def _get_prices(self, start: datetime, end: datetime, n_steps: int) -> list[float]:
        """Get electricity prices for horizon with proper interpolation.

        Args:
            start: Start time
            end: End time
            n_steps: Number of timesteps

        Returns:
            List of prices in $/kWh, one per timestep

        Note:
            Normalizes ``start`` / ``end`` to aware UTC before calling
            the price helper. asyncpg returns ``electricity_prices.time``
            as aware TIMESTAMPTZ, and the helper now returns a dict
            keyed by aware datetimes; controllers built around naive
            ``datetime.utcnow()`` would otherwise look up aware keys
            with naive ones and miss every match.

        Note:
            Resolves the depot's ENTSO-E bidding zone via
            :func:`src.db.queries.resolve_bidding_zone` (cached on the
            assembler instance), then delegates to
            :func:`src.db.queries.fetch_prices_by_zone`. Hours that
            helper can't fill (no known price within 1h) get the
            optimizer's $0.15/kWh default — this default is a
            solver-input concern and lives here, not in the helper,
            because billing code in ``src/core/billing/`` must NOT
            fabricate prices the same way.

        Raises:
            asyncpg.PostgresError: If database query fails
        """
        from ...db.queries import (
            _as_utc_aware,
            _hour_floor_utc,
            fetch_or_pull_prices_by_zone,
            resolve_bidding_zone,
        )

        # Normalize to aware UTC so dict lookups against the helper's
        # aware-keyed map line up regardless of caller-side flavor.
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)

        # Cache the zone on the instance — sites.tariff_config / sites.timezone
        # don't change inside a controller's lifetime, and this avoids a
        # static-pool round-trip on every solve.
        zone = getattr(self, "_bidding_zone_cache", _SENTINEL)
        if zone is _SENTINEL:
            try:
                async with self.pools.static.acquire() as static_conn:
                    zone = await resolve_bidding_zone(static_conn, self.depot_id)
            except asyncpg.PostgresError as e:
                # Don't poison the cache on a transient DB error —
                # the next solve should retry the static-pool lookup
                # once the DB recovers. ``None`` is a legitimate
                # ``resolve_bidding_zone`` result meaning "this depot
                # has no zone configured", and we DO want to cache
                # that on the success path below. The sentinel stays
                # set here so the next call hits this branch again.
                logger.error(
                    f"Failed to resolve bidding zone for depot {self.depot_id}: {e}. "
                    "Using default prices for this solve; will retry on next call."
                )
                return [0.15] * n_steps
            self._bidding_zone_cache = zone

        if not zone:
            logger.warning(
                f"No bidding zone configured for depot {self.depot_id} "
                "(set sites.tariff_config.entsoe_zone or sites.timezone); "
                "using default $0.15/kWh."
            )
            return [0.15] * n_steps

        start_utc = _as_utc_aware(start)
        end_utc = _as_utc_aware(end)

        try:
            async with self.pools.ts.acquire() as conn:
                price_map = await fetch_or_pull_prices_by_zone(
                    conn, zone, start_utc, end_utc,
                )
        except asyncpg.PostgresError as e:
            logger.error(
                f"Database error fetching prices for depot {self.depot_id} "
                f"(zone {zone}): {e}. Using default prices."
            )
            return [0.15] * n_steps

        if not price_map:
            logger.warning(
                f"No price data found for depot {self.depot_id} "
                f"(zone {zone}) between {start} and {end}, "
                "using default $0.15/kWh"
            )
            return [0.15] * n_steps

        default_price_kwh = 0.15
        delta_t = timedelta(hours=self.config.delta_t)
        prices: list[float] = []
        # Per-step lookup: each timestep maps to its hour-floor and is
        # priced from price_map at that hour. Earlier draft carried a
        # ``last_price`` cursor that survived across hour gaps, so a
        # multi-hour price gap silently extended the most recent priced
        # hour's value across all later timesteps instead of reverting
        # to default. fetch_prices_by_zone already forward-fills within
        # 1h before the dict reaches us; if the hour is still missing
        # we hunt for the closest known hour within 1h (defense in
        # depth) and finally fall back to ``default_price_kwh``.
        for t in range(n_steps):
            step_time = start_utc + t * delta_t
            hour_floor = _hour_floor_utc(step_time)
            if hour_floor in price_map:
                price = price_map[hour_floor]
            elif price_map:
                closest_time = min(
                    price_map.keys(),
                    key=lambda x: abs((x - hour_floor).total_seconds()),
                )
                if abs((closest_time - hour_floor).total_seconds()) < 3600:
                    price = price_map[closest_time]
                else:
                    price = default_price_kwh
            else:
                price = default_price_kwh
            prices.append(price)

        logger.debug(
            f"Price interpolation: {len(price_map)} priced hours -> {len(prices)} timesteps"
        )
        return prices[:n_steps]

    async def _get_vdv463_charging_requests(self, start: datetime, end: datetime) -> list[dict]:
        """Get active VDV 463 charging requests whose window overlaps [start, end].

        Returns list of dicts with: vehicle_id (str), expected_arrival, requested_departure,
        min_target_soc, max_target_soc, priority, preconditioning_type, preconditioning_start,
        ambient_temperature, requested_start_time, requested_finish_time, hvac_aux_power,
        system_aux_power, charging_point_id.
        """
        try:
            async with self.pools.ts.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT vehicle_id::text, expected_arrival, requested_departure,
                           min_target_soc, max_target_soc, priority,
                           preconditioning_type, preconditioning_start,
                           ambient_temperature, requested_start_time, requested_finish_time,
                           hvac_aux_power, system_aux_power, charging_point_id
                    FROM vdv463_charging_requests
                    WHERE depot_id = $1::uuid AND status = 'active'
                      AND requested_departure >= $2 AND expected_arrival <= $3
                    ORDER BY expected_arrival
                    """,
                    self.depot_id,
                    start,
                    end,
                )
            return [dict(row) for row in rows]
        except asyncpg.PostgresError as e:
            if "vdv463_charging_requests" in str(e) and "does not exist" in str(e).lower():
                logger.debug(
                    f"VDV 463 table not present, skipping: {e}",
                    extra={"depot_id": self.depot_id},
                )
                return []
            logger.error(f"Database error fetching VDV 463 requests for depot {self.depot_id}: {e}")
            raise

    def _merge_vdv463_into_schedules(
        self,
        schedules: list[dict],
        vdv463_requests: list[dict],
        horizon_start: datetime,
    ) -> tuple[
        list[dict],
        dict[str, float],
        dict[str, float],
        dict[str, int],
        list[dict],
    ]:
        """Merge VDV 463 requests into schedule list; build SoC/priority/preconditioning dicts.

        VDV 463 is authoritative for vehicles that have an active request; their schedule
        entry uses expected_arrival as return_time and requested_departure as departure_time.
        Returns (merged_schedules, vehicle_departure_soc_min, vehicle_departure_soc_max,
        vehicle_priorities, preconditioning_requests).
        """
        vehicle_departure_soc_min: dict[str, float] = {}
        vehicle_departure_soc_max: dict[str, float] = {}
        vehicle_priorities: dict[str, int] = {}
        preconditioning_requests: list[dict] = []
        {r["vehicle_id"] for r in vdv463_requests}

        # Schedule dicts: vehicle_id, return_time, departure_time, estimated_energy_kwh
        schedule_by_vehicle: dict[str, dict] = {}
        for s in schedules:
            vid = s["vehicle_id"]
            schedule_by_vehicle[vid] = {
                "vehicle_id": vid,
                "return_time": s["return_time"],
                "departure_time": s["departure_time"],
                "estimated_energy_kwh": s.get("estimated_energy_kwh") or 100.0,
            }

        for r in vdv463_requests:
            vid = r["vehicle_id"]
            ret = r.get("expected_arrival")
            dep = r.get("requested_departure")
            if ret is None or dep is None:
                continue
            # Normalize SoC to 0-1 if stored as 0-100
            min_soc = r.get("min_target_soc")
            max_soc = r.get("max_target_soc")
            if min_soc is not None:
                vehicle_departure_soc_min[vid] = min_soc if min_soc <= 1 else min_soc / 100.0
            if max_soc is not None:
                vehicle_departure_soc_max[vid] = max_soc if max_soc <= 1 else max_soc / 100.0
            if r.get("priority") is not None:
                vehicle_priorities[vid] = int(r["priority"])

            schedule_by_vehicle[vid] = {
                "vehicle_id": vid,
                "return_time": ret,
                "departure_time": dep,
                "estimated_energy_kwh": 100.0,
            }

            # Preconditioning: build list of {vehicle_id, start_time, end_time, power_kw}
            precond_type = r.get("preconditioning_type")
            if (
                precond_type == "manual"
                and r.get("preconditioning_start")
                and r.get("hvac_aux_power") is not None
            ):
                start_ts = r["preconditioning_start"]
                end_ts = dep
                power_kw = (r["hvac_aux_power"] or 0) / 1000.0
                preconditioning_requests.append(
                    {
                        "vehicle_id": vid,
                        "start_time": start_ts,
                        "end_time": end_ts,
                        "power_kw": power_kw,
                    }
                )
            elif (
                precond_type == "automatic"
                and r.get("requested_finish_time")
                and r.get("ambient_temperature") is not None
            ):
                # Duration ~ (targetTemp - ambientTemp) * 3 min/°C; target default 18°C (AT-10)
                ambient = float(r["ambient_temperature"])
                target = 18.0
                duration_min = max(0, (target - ambient) * 3.0)
                finish_ts = r["requested_finish_time"]
                if hasattr(finish_ts, "timestamp"):
                    start_ts = finish_ts - timedelta(minutes=duration_min)
                else:
                    start_ts = finish_ts
                preconditioning_requests.append(
                    {
                        "vehicle_id": vid,
                        "start_time": start_ts,
                        "end_time": finish_ts,
                        "power_kw": 10.0,
                    }
                )

        merged = list(schedule_by_vehicle.values())
        return (
            merged,
            vehicle_departure_soc_min,
            vehicle_departure_soc_max,
            vehicle_priorities,
            preconditioning_requests,
        )

    async def _get_depot_timezone(self) -> ZoneInfo:
        """Return the depot's IANA timezone (cached per assembler).

        Falls back to UTC with a warning if ``sites.timezone`` is missing or
        names an unknown zone — recurring expansion needs *some* zone to
        materialise local-wall-clock occurrences and UTC is the safest
        default since it round-trips cleanly.
        """
        cached = getattr(self, "_depot_timezone_cache", _SENTINEL)
        if cached is not _SENTINEL:
            return cached
        try:
            async with self.pools.static.acquire() as conn:
                tz_name = await conn.fetchval(
                    "SELECT timezone FROM sites WHERE id = $1", self.depot_id
                )
        except asyncpg.PostgresError as e:
            logger.warning(
                f"Failed to load depot timezone for {self.depot_id}: {e}; "
                "defaulting to UTC."
            )
            tz = ZoneInfo("UTC")
            self._depot_timezone_cache = tz
            return tz
        if not tz_name:
            logger.warning(
                f"sites.timezone is empty for depot {self.depot_id}; defaulting to UTC."
            )
            tz = ZoneInfo("UTC")
        else:
            try:
                tz = ZoneInfo(tz_name)
            except ZoneInfoNotFoundError:
                logger.warning(
                    f"Unknown IANA timezone '{tz_name}' for depot {self.depot_id}; "
                    "defaulting to UTC."
                )
                tz = ZoneInfo("UTC")
        self._depot_timezone_cache = tz
        return tz

    async def _get_schedules(self, start: datetime, end: datetime) -> list[dict]:
        """Get vehicle schedules merged from one-off rows + recurring templates.

        Pulls concrete ``schedules`` rows whose departure falls inside the
        horizon, then materialises any active recurring templates that
        match the horizon (DST + cancellation aware — see
        ``src/core/scheduling/recurring.py``). When both sources would
        yield trips for the same vehicle on the same depot-local day, the
        row with the later ``created_at`` wins.

        Args:
            start: Start time (timezone-aware UTC).
            end: End time (timezone-aware UTC).

        Returns:
            List of merged schedule dicts in the same shape the optimizer
            consumes (``vehicle_id``, ``departure_time``, ``return_time``,
            ``estimated_energy_kwh``, ``route_id``).

        Raises:
            asyncpg.PostgresError: If a database query fails.
        """
        # get_current_state passes naive UTC (datetime.utcnow()); the
        # recurring expander requires tz-aware bounds. Normalize here so
        # both the SQL query and the expansion see aware UTC.
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        manual_query = """
        SELECT s.vehicle_id::text, s.departure_time, s.return_time,
               s.energy_kwh AS estimated_energy_kwh, s.route_id, s.created_at
        FROM schedules s
        JOIN vehicles v ON s.vehicle_id = v.id
        WHERE v.site_id = $1
          AND s.departure_time >= $2
          AND s.departure_time < $3
        ORDER BY departure_time
        """
        try:
            async with self.pools.static.acquire() as conn:
                manual_rows = await conn.fetch(manual_query, self.depot_id, start, end)
                # Fetch templates + cancellations on the same connection to
                # avoid a second pool checkout.
                from ...db.queries import fetch_recurring_horizon_data
                template_rows, cancellation_rows = await fetch_recurring_horizon_data(
                    conn,
                    depot_id=UUID(self.depot_id),
                    horizon_start=start,
                    horizon_end=end,
                )
        except asyncpg.PostgresError as e:
            logger.error(f"Database error fetching schedules for depot {self.depot_id}: {e}")
            raise

        manual = [dict(row) for row in manual_rows]
        if not template_rows:
            # No recurring templates → strip created_at from manual rows so
            # downstream callers see the original schema.
            cleaned = [
                {k: v for k, v in row.items() if k != "created_at"}
                for row in manual
            ]
            logger.debug(f"Retrieved {len(cleaned)} schedules (no templates)")
            return cleaned

        depot_tz = await self._get_depot_timezone()
        templates = [
            RecurringTemplate(
                template_id=row["id"],
                depot_id=row["depot_id"],
                vehicle_id=row["vehicle_id"],
                route_id=row["route_id"],
                departure_time_of_day=row["departure_time_of_day"],
                return_time_of_day=row["return_time_of_day"],
                days_of_week=tuple(row["days_of_week"]),
                start_date=row["start_date"],
                end_date=row["end_date"],
                required_soc=float(row["required_soc"]),
                energy_kwh=(
                    float(row["energy_kwh"]) if row["energy_kwh"] is not None else None
                ),
                active=bool(row["active"]),
                created_at=row["created_at"],
            )
            for row in template_rows
        ]
        cancellations = [
            ScheduleCancellation(
                template_id=row["template_id"], occurrence_date=row["occurrence_date"]
            )
            for row in cancellation_rows
        ]
        recurring_rows = expand_recurring_templates(
            templates,
            cancellations,
            depot_tz=depot_tz,
            horizon_start=start,
            horizon_end=end,
        )
        merged = merge_recurring_with_manual(
            manual, recurring_rows, depot_tz=depot_tz
        )
        logger.debug(
            f"Retrieved {len(merged)} schedules "
            f"({len(manual)} manual + {len(recurring_rows)} recurring expansions)"
        )
        return merged

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
        # Schedule datetimes are tz-aware (Postgres timestamptz) while ``start``
        # may be naive UTC (datetime.utcnow()); coerce both so the comparison
        # below can't raise on a naive/aware mismatch.
        start = _as_aware_utc(start)

        # Initialize all vehicles as available
        availability = {vid: [True] * n_steps for vid in self.config.vehicle_capacities.keys()}

        for sched in schedules:
            vid = sched["vehicle_id"]
            if vid not in availability:
                # Ignore unknown vehicles not in depot config
                continue

            dep = _as_aware_utc(sched["departure_time"])
            ret = _as_aware_utc(sched["return_time"])

            for t in range(n_steps):
                step_time = start + t * delta_t
                # Vehicle is unavailable if on route
                if dep <= step_time < ret:
                    availability[vid][t] = False

        return availability

    def _compute_departure_times(self, schedules: list[dict], start: datetime) -> dict[str, int]:
        """Compute timestep index for each vehicle's next departure.

        Args:
            schedules: List of schedule dictionaries
            start: Start time of optimization horizon

        Returns:
            Dictionary mapping vehicle_id to timestep index
        """
        delta_t = timedelta(hours=self.config.delta_t)
        # See _compute_availability: coerce to tz-aware UTC so a naive ``start``
        # and tz-aware schedule rows can be subtracted without raising.
        start = _as_aware_utc(start)
        departures = {}

        for sched in schedules:
            vid = sched["vehicle_id"]
            dep = _as_aware_utc(sched["departure_time"])
            t_idx = int((dep - start) / delta_t)

            # Keep earliest departure for each vehicle
            if vid not in departures or t_idx < departures[vid]:
                departures[vid] = t_idx

        return departures

    def _compute_energy_requirements(self, schedules: list[dict]) -> dict[str, float]:
        """Compute energy needed for each vehicle's next trip.

        Args:
            schedules: List of schedule dictionaries

        Returns:
            Dictionary mapping vehicle_id to energy requirement (kWh)
        """
        requirements = {}
        for sched in schedules:
            vid = sched["vehicle_id"]
            energy = sched.get("estimated_energy_kwh") or 100.0  # default
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
        async with self.pools.ts.acquire() as conn:
            row = await conn.fetchrow(query, self.depot_id)

        peak = float(row["peak"]) if row and row["peak"] else 0.0
        logger.debug(f"Current month peak: {peak} kW")
        return peak

    async def _get_demand_charge_rate(self) -> float:
        """Get demand charge rate ($/kW) with PRD-compliant priority.

        Per PRD Section 8.1, priority is:
        1. prices.demand_kw (most recent price row)
        2. depots.demand_charge_rate_kw
        3. Default $20/kW

        Returns:
            Demand charge rate in $/kW
        """
        # Priority 1: Check prices.demand_kw from most recent price row (TimescaleDB)
        price_query = """
        SELECT demand_kw
        FROM prices
        WHERE depot_id = $1
          AND demand_kw IS NOT NULL
        ORDER BY time DESC
        LIMIT 1
        """
        async with self.pools.ts.acquire() as conn:
            price_row = await conn.fetchrow(price_query, self.depot_id)
            if price_row and price_row["demand_kw"] is not None:
                rate = float(price_row["demand_kw"])
                logger.debug(f"Demand charge rate from prices: ${rate}/kW")
                return rate

        # Priority 2: Fall back to depots.demand_charge_rate_kw (Supabase)
        depot_query = """
        SELECT demand_charge_rate_kw
        FROM sites
        WHERE id = $1
        """
        async with self.pools.static.acquire() as conn:
            row = await conn.fetchrow(depot_query, self.depot_id)
            if row and row["demand_charge_rate_kw"] is not None:
                rate = float(row["demand_charge_rate_kw"])
                logger.debug(f"Demand charge rate from depot config: ${rate}/kW")
                return rate

        # Priority 3: Fallback to default
        logger.warning(f"Depot {self.depot_id} not found or rate is NULL, using default $20/kW")
        return 20.0  # Default PG&E E-19 rate

    async def _get_cumulative_kwh_period(self, now: datetime) -> float:
        """Sum kWh delivered to this depot's chargers since the start of the
        current cap_billing_period.

        Used by the energy_cap tariff to compute remaining headroom against
        :attr:`DepotConfig.energy_cap_kwh`. Returns 0.0 if no telemetry is
        available. On DB errors, falls back to energy_cap_kwh (if configured)
        so the optimizer uses a no-headroom, conservative cost assumption.

        Args:
            now: current wall-clock time (UTC; horizon start)

        Returns:
            Cumulative kWh delivered in the current billing period.
        """
        # cap_billing_period semantics: 'monthly' resets on the first of
        # each calendar month in UTC. We don't yet support week / quarterly
        # cycles — treat anything else as monthly so this stays safe.
        period = (self.config.cap_billing_period or "monthly").lower()
        if period == "monthly":
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            logger.warning(
                f"Unknown cap_billing_period '{period}' for depot {self.depot_id}, "
                "treating as monthly"
            )
            period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        # Resolve depot charger IDs from static DB, then query telemetry from
        # TimescaleDB to avoid cross-database references.
        try:
            async with self.pools.static.acquire() as static_conn:
                charger_rows = await static_conn.fetch(
                    "SELECT id AS charger_id FROM charging_stations WHERE site_id = $1",
                    self.depot_id,
                )
            charger_ids = [row["charger_id"] for row in charger_rows]
        except Exception as exc:
            fallback_kwh = (
                float(self.config.energy_cap_kwh) if self.config.energy_cap_kwh is not None else 0.0
            )
            logger.warning(
                "Failed to fetch chargers for depot %s: %s; defaulting cumulative kWh to %.2f",
                self.depot_id,
                exc,
                fallback_kwh,
            )
            return fallback_kwh

        if not charger_ids:
            return 0.0

        # telemetry stores instantaneous charging_kw samples at charger-defined
        # intervals. Convert to energy by integrating each sample across the
        # elapsed time until the next sample, capped so stale final rows don't
        # extrapolate non-zero power all the way to "now".
        query = """
        WITH depot_telemetry AS (
            SELECT
                t.charger_id,
                t.vehicle_id,
                t.time,
                t.charging_kw,
                LEAD(t.time) OVER (
                    PARTITION BY t.charger_id, t.vehicle_id
                    ORDER BY t.time
                ) AS next_time
            FROM telemetry t
            WHERE t.charger_id = ANY($1::uuid[])
              AND t.time >= $2
              AND t.time <= $3
              AND t.charging_kw IS NOT NULL
        )
        SELECT COALESCE(
            SUM(
                dt.charging_kw * GREATEST(
                    EXTRACT(
                        EPOCH FROM (
                            LEAST(
                                COALESCE(dt.next_time, $3),
                                dt.time + $4::interval,
                                $3
                            ) - dt.time
                        )
                    ) / 3600.0,
                    0.0
                )
            ),
            0.0
        ) AS sum_kwh
        FROM depot_telemetry dt
        """
        try:
            async with self.pools.ts.acquire() as conn:
                row = await conn.fetchrow(
                    query,
                    charger_ids,
                    period_start,
                    now,
                    "30 minutes",
                )
            sum_kwh = float(row["sum_kwh"]) if row and row["sum_kwh"] is not None else 0.0
        except Exception as exc:
            fallback_kwh = (
                float(self.config.energy_cap_kwh) if self.config.energy_cap_kwh is not None else 0.0
            )
            logger.warning(
                "Failed to compute cumulative kWh for depot %s: %s; defaulting cumulative kWh to %.2f",
                self.depot_id,
                exc,
                fallback_kwh,
            )
            return fallback_kwh
        return sum_kwh

    async def _get_building_power(
        self, start: datetime, end: datetime, n_steps: int
    ) -> list[float]:
        """Get building power load for horizon.

        Args:
            start: Start time
            end: End time
            n_steps: Number of timesteps

        Returns:
            List of building power in kW, one per timestep. When the depot
            uses a static-assumption derate (no live meter/forecast), the
            returned series is all zeros and the depot-level
            ``building_load_assumption_kw`` field on ``DepotConfig`` is
            applied as a constant by the MILP grid-balance constraint.

        Note:
            PRD §9.4: building load is OPTIONAL for initial onboarding.
            When no live source is configured the optimizer derates
            ``max_grid_kw`` by ``building_load_assumption_kw`` so the
            site-power constraint is still respected.
        """
        if self.config.building_load_assumption_kw > 0.0:
            # Prevent double-counting when static derate is enabled, and avoid
            # querying TimescaleDB for data the optimizer intentionally ignores.
            self._last_building_load_source = "static_assumption"
            return [0.0] * n_steps

        query = """
        SELECT time, power_kw
        FROM building_load
        WHERE depot_id = $1 AND time >= $2 AND time < $3
        ORDER BY time
        """
        try:
            async with self.pools.ts.acquire() as conn:
                rows = await conn.fetch(query, self.depot_id, start, end)
        except asyncpg.PostgresError as e:
            logger.warning(
                f"Database error fetching building load for depot {self.depot_id}: {e}. "
                "Using fallback source."
            )
            # Explicit degraded mode (PRD 9.4): meter unavailable, fall back
            # to the deterministic business-hours pattern. Readiness will
            # downgrade the run to 'degraded'.
            self._last_building_load_source = "forecast_fallback"
            return self._get_building_power_forecast(start, end, n_steps)

        if not rows:
            logger.warning(
                f"No building load data found for depot {self.depot_id} "
                f"between {start} and {end}, using fallback source"
            )
            self._last_building_load_source = "forecast_fallback"
            return self._get_building_power_forecast(start, end, n_steps)

        self._last_building_load_source = "meter"

        # Build time-indexed power map
        power_map = {row["time"]: float(row["power_kw"]) for row in rows}

        # Generate power for each timestep with interpolation
        delta_t = timedelta(hours=self.config.delta_t)
        building_power = []
        last_power = 0.0  # Default fallback

        for t in range(n_steps):
            step_time = start + t * delta_t

            # Find closest power reading (exact match or forward-fill)
            if step_time in power_map:
                last_power = power_map[step_time]
            # Forward-fill: use last known power if within 30 minutes (per PRD Section 5.3)
            elif power_map:
                closest_time = min(
                    power_map.keys(),
                    key=lambda x: abs((x - step_time).total_seconds()),
                )
                time_diff = abs((closest_time - step_time).total_seconds())
                if time_diff < 1800:  # Within 30 minutes
                    last_power = power_map[closest_time]

            building_power.append(last_power)

        logger.debug(
            f"Building load interpolation: {len(power_map)} data points -> "
            f"{len(building_power)} timesteps, avg={sum(building_power)/len(building_power):.1f}kW"
        )
        return building_power[:n_steps]

    def _get_building_power_forecast(
        self, start: datetime, end: datetime, n_steps: int
    ) -> list[float]:
        """Forecast building load using simplified pattern (fallback).

        Per PRD Section 9.4, if meter unavailable, use forecast model.
        This is a simplified implementation - production would use historical patterns.

        Args:
            start: Start time
            end: End time
            n_steps: Number of timesteps

        Returns:
            List of forecasted building power in kW
        """
        # Simplified forecast: higher during business hours (8 AM - 6 PM)
        building_power = []
        delta_t = timedelta(hours=self.config.delta_t)

        for t in range(n_steps):
            step_time = start + t * delta_t
            hour = step_time.hour

            # Business hours pattern: 50-80 kW during day, 20 kW baseline
            if 8 <= hour < 18:
                power = 50.0 + (hour - 8) * 3  # 50-80 kW
            else:
                power = 20.0  # Baseline

            building_power.append(power)

        logger.info(
            f"Using building load forecast (meter unavailable): "
            f"avg={sum(building_power)/len(building_power):.1f}kW"
        )
        return building_power

    async def fetch_snapshot_extras(self, horizon_start: datetime, horizon_end: datetime) -> None:
        """Populate snapshot-only metadata (weather + organization_id +
        recent telemetry).

        Called by ``DepotController._capture_snapshot`` after
        ``get_current_state`` so the extra DB roundtrips don't show up
        for callers that only need the optimization state.
        """
        features, forecast_id = await self._get_weather_features(horizon_start, horizon_end)
        self._last_weather_features = features
        self._last_weather_forecast_id = forecast_id
        self._last_organization_id = await self._get_organization_id()
        self._last_recent_telemetry = await self._get_recent_telemetry(horizon_start)

    async def _get_recent_telemetry(
        self, now: datetime, lookback_seconds: int = 3600
    ) -> list[dict]:
        """Fetch recent telemetry rows for the snapshot.

        Replay-friendly: captures the (vehicle_id, time, soc, charging_kw,
        is_plugged) rows that drove this optimization, so SoC-deviation
        triggers and other dynamics can be reconstructed months later.

        Args:
            now: Reference time. Returns rows with ``time >= now - lookback``.
            lookback_seconds: Lookback window in seconds (default 1 hour).

        Returns:
            List of dicts (possibly empty). Failures are non-fatal —
            snapshot construction must never break optimization.
        """
        cutoff = now - timedelta(seconds=lookback_seconds)
        try:
            async with self.pools.static.acquire() as conn:
                vehicle_rows = await conn.fetch(
                    "SELECT id::text AS vehicle_id FROM vehicles WHERE site_id = $1",
                    self.depot_id,
                )
            vehicle_ids = [row["vehicle_id"] for row in vehicle_rows]
            if not vehicle_ids:
                return []

            async with self.pools.ts.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT time, vehicle_id::text AS vehicle_id,
                           soc, charging_kw, is_plugged
                    FROM telemetry
                    WHERE vehicle_id = ANY($1::uuid[]) AND time >= $2
                    ORDER BY time
                    """,
                    vehicle_ids,
                    cutoff,
                )
        except asyncpg.PostgresError as e:
            logger.debug(f"recent_telemetry unavailable for depot {self.depot_id}: {e}")
            return []
        return [
            {
                "time": row["time"].isoformat() if row["time"] else None,
                "vehicle_id": row["vehicle_id"],
                "soc": float(row["soc"]) if row["soc"] is not None else None,
                "charging_kw": (
                    float(row["charging_kw"]) if row["charging_kw"] is not None else None
                ),
                "is_plugged": (bool(row["is_plugged"]) if row["is_plugged"] is not None else None),
            }
            for row in rows
        ]

    async def _get_weather_features(
        self, start: datetime, end: datetime
    ) -> tuple[list[dict], UUID | None]:
        """Fetch weather features for the horizon (snapshot context only).

        After migration 021 ``weather_forecasts`` is an insert-only
        history of forecast bundles, each tagged with ``fetched_at``
        (when we asked) and ``forecast_for`` (the timestamp the
        forecast is for). We pin the snapshot to the **forecast bundle
        that was current at horizon_start** by filtering:

            fetched_at = (
                SELECT MAX(fetched_at) FROM weather_forecasts
                WHERE depot_id = $1 AND source = $2 AND fetched_at <= $start
            )

        That guarantees:

        * the optimizer and the snapshot share the exact same bundle
          (no race against a fresh fetch landing mid-assembly), and
        * surrogate training can replay the bundle a historical run
          actually saw by re-running the same query with the run's
          ``horizon_start`` as the upper bound.

        Returns:
            ``(features, forecast_id)`` where ``forecast_id`` is the
            UUID of *one* row from the bundle (any row works — every
            row in a bundle shares fetched_at and depot_id, and the
            FK target is just used to load the bundle later). Returns
            ``([], None)`` if no bundle is visible at ``horizon_start``
            or the table is unavailable.
        """
        try:
            async with self.pools.ts.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT forecast_id, forecast_for, fetched_at,
                           temp_f, temp_max_f, temp_min_f,
                           precip_in, solar_rad
                    FROM weather_forecasts
                    WHERE depot_id = $1::uuid
                      AND source = $2
                      AND fetched_at = (
                          SELECT MAX(fetched_at)
                          FROM weather_forecasts
                          WHERE depot_id = $1::uuid
                            AND source = $2
                            AND fetched_at <= $3
                      )
                      AND forecast_for >= $3
                      AND forecast_for <  $4
                    ORDER BY forecast_for
                    """,
                    self.depot_id,
                    DEFAULT_WEATHER_SOURCE,
                    start,
                    end,
                )
        except asyncpg.PostgresError as e:
            logger.debug(f"weather_forecasts unavailable for depot {self.depot_id}: {e}")
            return [], None
        if not rows:
            return [], None

        forecast_id: UUID = rows[0]["forecast_id"]
        features = [
            {
                # ``time`` is preserved as the public payload key for
                # backwards compatibility with the snapshot schema.
                "time": (row["forecast_for"].isoformat() if row["forecast_for"] else None),
                "temp_f": (float(row["temp_f"]) if row["temp_f"] is not None else None),
                "temp_max_f": (float(row["temp_max_f"]) if row["temp_max_f"] is not None else None),
                "temp_min_f": (float(row["temp_min_f"]) if row["temp_min_f"] is not None else None),
                "precip_in": (float(row["precip_in"]) if row["precip_in"] is not None else None),
                "solar_rad": (float(row["solar_rad"]) if row["solar_rad"] is not None else None),
            }
            for row in rows
        ]
        return features, forecast_id

    async def _get_organization_id(self) -> str | None:
        """Look up the depot's organization_id (Supabase). None if unset."""
        try:
            async with self.pools.static.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT organization_id::text AS organization_id " "FROM sites WHERE id = $1",
                    self.depot_id,
                )
        except asyncpg.PostgresError as e:
            logger.debug(f"organization_id lookup failed for depot {self.depot_id}: {e}")
            return None
        if not row:
            return None
        return row["organization_id"]

    async def _get_incoming_vehicles(self, start: datetime, end: datetime) -> list[IncomingVehicle]:
        """Get incoming vehicles from inter-depot handoffs.

        Per PRD Section 5.3, queries pending inter-depot arrivals where
        arrival_time < horizon_end.

        Args:
            start: Start time of optimization horizon
            end: End time of optimization horizon

        Returns:
            List of IncomingVehicle objects

        Note:
            Only includes vehicles with status='acknowledged' or 'pending'
            that arrive within the optimization horizon.
        """
        # interdepot_messages lives in TimescaleDB; vehicles.external_id lives in Supabase.
        # We fetch the messages first, then batch-resolve external_ids from Supabase.
        try:
            async with self.pools.ts.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT vehicle_id, expected_soc, arrival_time,
                           battery_kwh, max_charge_kw, origin_depot_id
                    FROM interdepot_messages
                    WHERE dest_depot_id = $1
                      AND arrival_time >= $2
                      AND arrival_time < $3
                      AND status IN ('pending', 'acknowledged')
                    ORDER BY arrival_time
                    """,
                    self.depot_id,
                    start,
                    end,
                )

            if not rows:
                return []

            # Batch-resolve external_ids from Supabase (static pool)
            vehicle_ids = [str(row["vehicle_id"]) for row in rows]
            async with self.pools.static.acquire() as conn:
                ext_rows = await conn.fetch(
                    "SELECT id::text AS vehicle_id, external_id FROM vehicles WHERE id = ANY($1::uuid[])",
                    vehicle_ids,
                )
            external_id_map = {r["vehicle_id"]: r["external_id"] for r in ext_rows}

            incoming_vehicles = []
            for row in rows:
                vid_str = str(row["vehicle_id"])
                incoming = IncomingVehicle(
                    vehicle_id=row["vehicle_id"],
                    external_id=external_id_map.get(vid_str) or f"vehicle_{vid_str}",
                    expected_soc=float(row["expected_soc"]),
                    arrival_time=row["arrival_time"],
                    battery_kwh=float(row["battery_kwh"]),
                    max_charge_kw=float(row["max_charge_kw"]),
                    origin_depot_id=row["origin_depot_id"],
                )
                incoming_vehicles.append(incoming)

            if incoming_vehicles:
                logger.info(
                    f"Found {len(incoming_vehicles)} incoming vehicles from inter-depot handoffs"
                )
            return incoming_vehicles
        except asyncpg.PostgresError as e:
            logger.error(
                f"Database error fetching incoming vehicles for depot {self.depot_id}: {e}"
            )
            return []

    @classmethod
    async def load_depot_config(
        cls, pools: DatabasePools, depot_id: str | UUID
    ) -> tuple[DepotConfig, dict[str, str]]:
        """Load depot configuration from database.

        All config tables (depots, vehicles, chargers, battery_storage,
        charger_vehicle_access) live in Supabase (pools.static).

        Args:
            pools: Dual database connection pools
            depot_id: Depot identifier

        Returns:
            Tuple of (DepotConfig, vehicle_id_to_id_tag mapping)

        Raises:
            ValueError: If depot not found or configuration is invalid
        """
        depot_id_str = str(depot_id)
        pool = pools.static  # All config tables are in Supabase

        # Query depot configuration. building_load_assumption_kw was added
        # by migration 020 — COALESCE handles the brief window after that
        # migration runs on a depot row inserted before it.
        depot_query = """
        SELECT s.max_grid_kw,
               s.demand_charge_rate_kw,
               COALESCE(s.building_load_assumption_kw, 0.0) AS building_load_assumption_kw,
               CASE
                   WHEN COALESCE(to_jsonb(s)->>'charger_vehicle_access_default', '')
                        IN ('all_to_all', 'explicit_matrix')
                       THEN to_jsonb(s)->>'charger_vehicle_access_default'
                   WHEN COALESCE(to_jsonb(s)->>'charger_vehicle_access_default', '')
                        IN ('true', 't', '1')
                       THEN 'all_to_all'
                   ELSE 'explicit_matrix'
               END AS charger_vehicle_access_default,
               COALESCE(to_jsonb(s)->>'tariff_type', 'simple_demand') AS tariff_type,
               NULLIF(to_jsonb(s)->>'energy_cap_kwh', '')::DOUBLE PRECISION AS energy_cap_kwh,
               NULLIF(to_jsonb(s)->>'under_cap_rate_per_kwh', '')::DOUBLE PRECISION
                   AS under_cap_rate_per_kwh,
               NULLIF(to_jsonb(s)->>'over_cap_penalty_per_kwh', '')::DOUBLE PRECISION
                   AS over_cap_penalty_per_kwh,
               COALESCE(to_jsonb(s)->>'cap_billing_period', 'monthly') AS cap_billing_period
        FROM sites s
        WHERE id = $1
        """
        async with pool.acquire() as conn:
            depot_row = await conn.fetchrow(depot_query, depot_id_str)

        if not depot_row:
            raise ValueError(f"Depot {depot_id_str} not found")

        raw_max_grid_kw = depot_row["max_grid_kw"]
        if raw_max_grid_kw is None:
            raise ValueError(
                f"Depot {depot_id_str} has NULL max_grid_kw; set sites.max_grid_kw before optimization"
            )
        max_site_power = float(raw_max_grid_kw)
        building_load_assumption_kw = float(depot_row["building_load_assumption_kw"])
        access_default = str(depot_row["charger_vehicle_access_default"])
        tariff_type = str(depot_row["tariff_type"])
        energy_cap_kwh = (
            float(depot_row["energy_cap_kwh"]) if depot_row["energy_cap_kwh"] is not None else None
        )
        under_cap_rate = (
            float(depot_row["under_cap_rate_per_kwh"])
            if depot_row["under_cap_rate_per_kwh"] is not None
            else None
        )
        over_cap_penalty = (
            float(depot_row["over_cap_penalty_per_kwh"])
            if depot_row["over_cap_penalty_per_kwh"] is not None
            else None
        )
        cap_billing_period = str(depot_row["cap_billing_period"])

        # Query vehicles
        vehicles_query = """
        SELECT id::text AS vehicle_id,
               battery_capacity_kwh AS battery_kwh,
               max_charge_rate_kw AS max_charge_kw,
               id_tag
        FROM vehicles
        WHERE site_id = $1
        """
        async with pool.acquire() as conn:
            vehicle_rows = await conn.fetch(vehicles_query, depot_id_str)

        # An empty fleet is a valid state for a freshly-onboarded depot — the
        # config still loads, downstream callers (state, optimize) decide how
        # to handle no vehicles. Previously raised, surfacing as HTTP 500 on
        # GET /depots/{id}/state.
        if not vehicle_rows:
            logger.info(f"Depot {depot_id_str} has no vehicles configured")

        vehicle_capacities = {}
        vehicle_to_ocpp = {}
        for row in vehicle_rows:
            vid = row["vehicle_id"]
            raw_battery_kwh = row["battery_kwh"]
            if raw_battery_kwh is None:
                raise ValueError(
                    f"Vehicle {vid} in depot {depot_id_str} has NULL battery_capacity_kwh; "
                    "set vehicles.battery_capacity_kwh before optimization"
                )
            vehicle_capacities[vid] = float(raw_battery_kwh)
            if row["id_tag"]:
                vehicle_to_ocpp[vid] = row["id_tag"]

        # Query chargers - aggregate by rated_kw per PRD Section 8.3
        chargers_query = """
        SELECT max_power_kw AS rated_kw, efficiency, COUNT(*) as count
        FROM charging_stations
        WHERE site_id = $1
        GROUP BY max_power_kw, efficiency
        ORDER BY max_power_kw DESC
        """
        async with pool.acquire() as conn:
            charger_rows = await conn.fetch(chargers_query, depot_id_str)

        if not charger_rows:
            logger.warning(f"No chargers found for depot {depot_id_str}, using defaults")
            charger_groups = {80.0: 1}  # Default: 1 charger at 80kW
            charger_efficiency = 0.95
        else:
            # Build charger_groups dict: rated_kw -> count
            charger_groups = {}
            charger_efficiency = None
            for row in charger_rows:
                raw_rated_kw = row["rated_kw"]
                if raw_rated_kw is None:
                    raise ValueError(
                        f"Depot {depot_id_str} has NULL max_power_kw in charging_stations; "
                        "set charging_stations.max_power_kw before optimization"
                    )
                rated_kw = float(raw_rated_kw)
                count = int(row["count"])
                charger_groups[rated_kw] = count
                # Use efficiency from first charger (assumed uniform per PRD)
                if charger_efficiency is None:
                    charger_efficiency = float(row["efficiency"])

            if charger_efficiency is None:
                charger_efficiency = 0.95  # Default

        # Query battery storage
        battery_query = """
        SELECT capacity_kwh, max_power_kw, efficiency, soc_min, soc_max
        FROM battery_storage
        WHERE site_id = $1
        LIMIT 1
        """
        async with pool.acquire() as conn:
            battery_row = await conn.fetchrow(battery_query, depot_id_str)

        if battery_row:
            battery_capacity = float(battery_row["capacity_kwh"])
            battery_power = float(battery_row["max_power_kw"])
            battery_efficiency = float(battery_row.get("efficiency", 0.92))
            battery_soc_min = float(battery_row.get("soc_min", 0.2))
            battery_soc_max = float(battery_row.get("soc_max", 0.8))
        else:
            # Defaults if no battery found
            logger.warning(f"No battery storage found for depot {depot_id_str}, using defaults")
            battery_capacity = 0.0
            battery_power = 0.0
            battery_efficiency = 0.92
            battery_soc_min = 0.2
            battery_soc_max = 0.8

        # Charger-vehicle accessibility (per PRD Section 6.1).
        # In 'all_to_all' mode the depot has opted out of an explicit
        # matrix — every charger reaches every vehicle in the depot, so we
        # synthesize the dict in-memory and skip the DB lookup. Otherwise
        # we use the explicit rows from charger_vehicle_access.
        charger_vehicle_access: dict[str, set[str]] = {}
        if access_default == "all_to_all":
            charger_id_query = """
            SELECT id::text AS charger_id FROM charging_stations WHERE site_id = $1
            """
            async with pool.acquire() as conn:
                charger_id_rows = await conn.fetch(charger_id_query, depot_id_str)
            all_vehicle_ids = set(vehicle_capacities.keys())
            for row in charger_id_rows:
                charger_vehicle_access[row["charger_id"]] = set(all_vehicle_ids)
        else:
            access_query = """
            SELECT charging_station_id::text AS charger_id, vehicle_id::text
            FROM charger_vehicle_access
            WHERE charging_station_id IN (
                SELECT id FROM charging_stations WHERE site_id = $1
            )
            AND is_accessible = TRUE
            """
            async with pool.acquire() as conn:
                access_rows = await conn.fetch(access_query, depot_id_str)
            for row in access_rows:
                charger_id = row["charger_id"]
                vehicle_id = row["vehicle_id"]
                if charger_id not in charger_vehicle_access:
                    charger_vehicle_access[charger_id] = set()
                charger_vehicle_access[charger_id].add(vehicle_id)

        # Build vehicle_max_charge_kw dict
        vehicle_max_charge_kw = {}
        for row in vehicle_rows:
            vid = row["vehicle_id"]
            vehicle_max_charge_kw[vid] = float(row["max_charge_kw"])

        config = DepotConfig(
            vehicle_capacities=vehicle_capacities,
            vehicle_max_charge_kw=vehicle_max_charge_kw,
            charger_groups=charger_groups,
            charger_efficiency=charger_efficiency,
            charger_vehicle_access=charger_vehicle_access,
            battery_capacity=battery_capacity,
            battery_power=battery_power,
            battery_efficiency=battery_efficiency,
            battery_soc_min=battery_soc_min,
            battery_soc_max=battery_soc_max,
            max_site_power=max_site_power,
            building_load_assumption_kw=building_load_assumption_kw,
            delta_t=0.25,  # 15 minutes per PRD
            n_timesteps=96,  # 24 hours
            charger_vehicle_access_default=access_default,
            tariff_type=tariff_type,
            energy_cap_kwh=energy_cap_kwh,
            under_cap_rate_per_kwh=under_cap_rate,
            over_cap_penalty_per_kwh=over_cap_penalty,
            cap_billing_period=cap_billing_period,
        )

        total_chargers = sum(charger_groups.values())
        logger.info(
            f"Loaded depot config: {len(vehicle_capacities)} vehicles, "
            f"{total_chargers} chargers ({charger_groups}), {battery_capacity} kWh battery"
        )

        return config, vehicle_to_ocpp
