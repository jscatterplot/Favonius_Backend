"""Optimization engine with heuristic fallback.

Note: Julia MIP solver removed. This engine uses heuristic optimization.
For production-grade optimization, use the main optimizer in src/core/optimizer/.
"""

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp

from .config import MainApiConfig, OptimizationServiceConfig
from .connection_manager import ConnectionManager
from .monitoring import get_logger
from .supabase_client import SupabaseClient
from .timescale_client import TimescaleClient


@dataclass
class StationState:
    station_id: str
    evse_id: int
    connector_id: int
    soc: float
    power_kw: float
    max_charge_kw: float
    max_discharge_kw: float


class OptimizationEngine:
    """Optimization engine with heuristic approach.

    Note: Julia MIP solver removed. Uses heuristic optimization only.
    For production-grade optimization, delegate to main optimizer in src/core/optimizer/.
    """

    def __init__(
        self,
        config: OptimizationServiceConfig,
        timescale_client: TimescaleClient,
        supabase_client: SupabaseClient,
        connection_manager: Optional[ConnectionManager],
        main_api_config: Optional[MainApiConfig] = None,
    ) -> None:
        self.config = config
        self.timescale_client = timescale_client
        self.supabase_client = supabase_client
        self.connection_manager = connection_manager
        self.main_api_config = main_api_config
        self.logger = get_logger(__name__)
        self._lock = asyncio.Lock()
        self._pending_reason: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # Tracks the last time an HTTP push to the main API succeeded.
        # Used as the fast-path (Layer 1) in _main_api_alive().
        self._last_successful_push: Optional[datetime] = None

    def set_connection_manager(self, connection_manager: ConnectionManager) -> None:
        """Set the connection manager (called after server initialization)."""
        self.connection_manager = connection_manager
        self.logger.info("Connection manager set for optimization engine")

    async def start(self) -> None:
        if not self.config.enabled:
            self.logger.info("Optimization engine disabled via configuration")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self.logger.info("Optimization engine started (heuristic mode)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.logger.info("Optimization engine stopped")

    async def request_run(self, reason: str) -> None:
        async with self._lock:
            self._pending_reason = reason

    async def _loop(self) -> None:
        await asyncio.sleep(5)
        while self._running:
            reason = None
            async with self._lock:
                reason = self._pending_reason
                self._pending_reason = None
            if reason:
                try:
                    await self._run_optimization(reason)
                except Exception as exc:
                    self.logger.error(f"Optimization run failed: {exc}")
            await asyncio.sleep(30)  # Run every 30 seconds

    def record_successful_push(self) -> None:
        """Record that a push to the main API just succeeded.

        Updates the fast-path timestamp used by Layer 1 of _main_api_alive()
        so we skip the DB and HTTP checks when events are flowing normally.
        """
        self._last_successful_push = datetime.now(timezone.utc)

    async def _main_api_alive(self) -> bool:
        """Three-layer liveness check for the main API optimizer.

        Layer 1 — recent successful push (no I/O, fastest):
            A push succeeded within heartbeat_stale_seconds → definitely alive.
        Layer 2 — DB heartbeat (one async DB query):
            Read the service_heartbeat row the main API writes every 30 s.
        Layer 3 — HTTP health check (final fallback, used when DB is also down):
            GET /health on the main API directly.

        Returns False (→ activate heuristic) only when all three fail.
        """
        cfg = self.main_api_config
        if cfg is None or not cfg.enabled:
            return False  # No main API configured — always use heuristic.

        stale_threshold = cfg.heartbeat_stale_seconds

        # Layer 1: fast-path — recent push proves the main API was alive moments ago.
        if self._last_successful_push is not None:
            age = (datetime.now(timezone.utc) - self._last_successful_push).total_seconds()
            if age < stale_threshold:
                return True

        # Layer 2: DB heartbeat written by the main API every 30 s.
        try:
            if self.timescale_client.pg_pool:
                async with self.timescale_client.pg_pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT last_seen FROM service_heartbeat WHERE service = 'optimizer'"
                    )
                if row and row["last_seen"]:
                    last_seen = row["last_seen"]
                    if last_seen.tzinfo is None:
                        last_seen = last_seen.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - last_seen).total_seconds()
                    if age < stale_threshold:
                        self.logger.debug("Main API alive via DB heartbeat (age=%.0fs)", age)
                        return True
        except Exception as e:
            self.logger.warning("DB heartbeat check failed: %s", e)

        # Layer 3: direct HTTP health check — used when DB is also unreachable.
        try:
            headers = {"X-Internal-Token": cfg.internal_token} if cfg.internal_token else {}
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{cfg.url}/health",
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=5.0),
                ) as resp:
                    if resp.status == 200:
                        self.logger.info(
                            "Main API alive via HTTP health check "
                            "(DB heartbeat stale or unreachable)"
                        )
                        return True
        except Exception as e:
            self.logger.warning("HTTP health check failed: %s", e)

        return False

    async def _run_optimization(self, trigger_reason: str) -> None:
        self.logger.info("Running optimization triggered by %s", trigger_reason)

        if await self._main_api_alive():
            self.logger.info(
                "Main API optimizer is active — deferring heuristic fallback (trigger: %s)",
                trigger_reason,
            )
            return

        self.logger.warning(
            "Main API optimizer unavailable — activating heuristic fallback (trigger: %s)",
            trigger_reason,
        )

        now = datetime.now(timezone.utc)
        horizon_end = now + timedelta(hours=self.config.horizon_hours)

        # Get active vehicles and routes from Supabase (static reference data)
        vehicles = await self.supabase_client.get_vehicles()
        routes = await self.supabase_client.get_active_routes()

        if not vehicles:
            self.logger.info("No active vehicles to optimize")
            return

        # Get electricity prices
        prices = await self.timescale_client.get_latest_prices(
            nodes=["TH_SP15_GEN-APND", "TH_NP15_GEN-APND"],
            start=now - timedelta(hours=1),
        )

        # Use heuristic optimization (Julia MIP solver removed)
        await self._run_heuristic_optimization(
            vehicles, routes, prices, now, horizon_end, trigger_reason
        )

    # MIP optimization using Julia removed - use main optimizer in src/core/optimizer/ for production

    async def _run_heuristic_optimization(
        self,
        vehicles: List[Dict[str, Any]],
        routes: List[Dict[str, Any]],
        prices: List[Dict[str, Any]],
        now: datetime,
        horizon_end: datetime,
        trigger_reason: str,
    ) -> None:
        """Run optimization using heuristic approach (fallback)."""
        self.logger.info("Running heuristic optimization (fallback)")

        price_by_time: Dict[datetime, float] = {}
        for price in prices:
            price_by_time[price["time"]] = price.get("lmp_price_mwh", 0.0)

        schedules = []
        for vehicle in vehicles:
            station_id = vehicle["station_id"]
            evse_id = 1  # Default EVSE ID
            start_time = now
            end_time = horizon_end

            periods = []
            current_time = start_time
            soc = vehicle.get("current_soc_kwh", 30.0) / vehicle.get("battery_capacity_kwh", 75.0)

            while current_time < end_time:
                price = price_by_time.get(current_time.replace(second=0, microsecond=0), 0.0)
                power_kw = self._determine_power(price, soc, vehicle)
                periods.append(
                    {
                        "startPeriod": int((current_time - start_time).total_seconds()),
                        "limit": power_kw * 1000,
                        "numberPhases": 3,
                    }
                )
                soc = min(
                    1.0,
                    max(
                        0.0,
                        soc
                        + (power_kw * (self.config.timestep_minutes / 60.0))
                        / vehicle.get("battery_capacity_kwh", 75.0),
                    ),
                )
                current_time += timedelta(minutes=self.config.timestep_minutes)

            schedule = {
                "station_id": station_id,
                "evse_id": evse_id,
                "decision_id": None,
                "profile_id": f"heuristic-{vehicle['vehicle_id']}-{int(now.timestamp())}",
                "start_time": start_time,
                "end_time": end_time,
                "schedule_periods": periods,
                "priority": 0,
                "stacking_level": 0,
                "purpose": "heuristic_fallback",
            }
            schedules.append(schedule)
            await self.timescale_client.store_charging_schedule(schedule)

        await self.timescale_client.store_optimization_decision(
            {
                "time": now,
                "optimization_window_start": now,
                "optimization_window_end": horizon_end,
                "fleet_operator_id": None,
                "site_id": None,
                "algorithm_version": "heuristic-v1",
                "objective_function": "price_driven",
                "objective_value": None,
                "computation_time_ms": 0,
                "constraints_satisfied": True,
                "decision_payload": {"schedules": schedules, "trigger_reason": trigger_reason},
                "sync_status": "completed",
            }
        )
        self.logger.info("Heuristic optimization created %d schedules", len(schedules))

    def _determine_power(self, price: float, soc: float, vehicle: Dict[str, Any]) -> float:
        """Enhanced heuristic mapping price and SOC to power setpoints."""
        battery_capacity = vehicle.get("battery_capacity_kwh", 75.0)
        max_charge_rate = vehicle.get("max_charge_rate_kw", 22.0)
        max_discharge_rate = vehicle.get("max_discharge_rate_kw", 10.0)
        min_soc = vehicle.get("min_soc_kwh", 15.0) / battery_capacity

        if soc < min_soc:
            return max_charge_rate
        if price < 0:
            return max_charge_rate
        if price > 150:
            return -min(max_discharge_rate, soc * max_charge_rate)
        return 0.0
