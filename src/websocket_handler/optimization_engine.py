"""Simplified optimization engine coordinating charging schedules."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from .config import OptimizationServiceConfig
from .monitoring import get_logger
from .timescale_client import TimescaleClient
from .supabase_client import SupabaseClient
from .connection_manager import ConnectionManager


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
    """Simplified optimization engine producing charging schedules."""

    def __init__(
        self,
        config: OptimizationServiceConfig,
        timescale_client: TimescaleClient,
        supabase_client: SupabaseClient,
        connection_manager: Optional[ConnectionManager],
    ) -> None:
        self.config = config
        self.timescale_client = timescale_client
        self.supabase_client = supabase_client
        self.connection_manager = connection_manager
        self.logger = get_logger(__name__)
        self._lock = asyncio.Lock()
        self._pending_reason: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        if not self.config.enabled:
            self.logger.info("Optimization engine disabled via configuration")
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self.logger.info("Optimization engine started")

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
            await asyncio.sleep(10)

    async def _run_optimization(self, trigger_reason: str) -> None:
        self.logger.info("Running optimization triggered by %s", trigger_reason)

        now = datetime.now(timezone.utc)
        horizon_end = now + timedelta(hours=self.config.horizon_hours)

        sessions = await self.timescale_client.get_active_charging_sessions(now - timedelta(hours=1), horizon_end)
        prices = await self.timescale_client.get_latest_prices(
            nodes=["TH_SP15_GEN-APND", "TH_NP15_GEN-APND"],
            start=now - timedelta(hours=1),
        )

        if not sessions:
            self.logger.info("No active sessions to optimize")
            return

        price_by_time: Dict[datetime, float] = {}
        for price in prices:
            price_by_time[price["time"]] = price.get("lmp_price_mwh", 0.0)

        schedules = []
        for session in sessions:
            station_id = session["station_id"]
            evse_id = session["evse_id"]
            connector_id = session["connector_id"]
            start_time = max(session["start_time"], now)
            end_time = horizon_end if session["end_time"] is None else min(session["end_time"], horizon_end)

            periods = []
            current_time = start_time
            soc = session.get("start_soc_percent", 50.0) / 100.0
            while current_time < end_time:
                price = price_by_time.get(current_time.replace(second=0, microsecond=0), 0.0)
                power_kw = self._determine_power(price, soc)
                periods.append(
                    {
                        "startPeriod": int((current_time - start_time).total_seconds()),
                        "limit": power_kw * 1000,
                        "numberPhases": 3,
                    }
                )
                soc = min(1.0, max(0.0, soc + (power_kw * (self.config.timestep_minutes / 60.0)) / self.config.charge_power_kw))
                current_time += timedelta(minutes=self.config.timestep_minutes)

            schedule = {
                "station_id": station_id,
                "evse_id": evse_id,
                "decision_id": None,
                "profile_id": f"opt-{station_id}-{int(now.timestamp())}",
                "start_time": start_time,
                "end_time": end_time,
                "schedule_periods": periods,
                "priority": 0,
                "stacking_level": 0,
                "purpose": "v2g_schedule",
            }
            schedules.append(schedule)
            await self.timescale_client.store_charging_schedule(schedule)
            # Note: Charging profile sending is now handled by the OCPP handler
            # The optimization engine should trigger the profile sending through the server

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
                "decision_payload": {"schedules": schedules},
                "sync_status": "completed",
            }
        )
        self.logger.info("Optimization created %d schedules", len(schedules))

    def _determine_power(self, price: float, soc: float) -> float:
        """Simple heuristic mapping price and SOC to power setpoints."""
        if soc < self.config.soc_minimum:
            return self.config.charge_power_kw
        if price < 0:
            return self.config.charge_power_kw
        if price > 150:
            return -min(self.config.discharge_power_kw, soc * self.config.charge_power_kw)
        return 0.0
