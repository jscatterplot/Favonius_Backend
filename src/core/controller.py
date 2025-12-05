"""Main control loop for depot optimization.

Reference: Development plan Step 5.2, PRD.md#5-system-architecture
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional
from uuid import UUID, uuid4

import asyncpg

from ..models import DepotConfig, OptimizationResult
from .optimizer import build_optimization_model, optimize
from .state.assembler import StateAssembler
from .state.triggers import TriggerConfig, TriggerMonitor

if TYPE_CHECKING:
    from ..adapters.ocpp.server import OCPPServer

logger = logging.getLogger(__name__)


class DepotController:
    """Main controller for depot charging optimization.

    Coordinates optimization runs, state assembly, trigger monitoring,
    and command dispatch to chargers.

    Reference: PRD Section 5.2, Development plan Step 5.2
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        depot_id: str | UUID,
        config: DepotConfig,
        ocpp_server: Optional['OCPPServer'] = None,
    ):
        """Initialize depot controller.

        Args:
            pool: Database connection pool
            depot_id: Depot identifier
            config: Depot configuration
            ocpp_server: Optional OCPP server for charger communication
        """
        self.pool = pool
        self.depot_id = str(depot_id)
        self.config = config
        self.ocpp_server = ocpp_server
        self.assembler = StateAssembler(pool, self.depot_id, config)

        self.trigger_monitor = TriggerMonitor(
            TriggerConfig(),
            on_trigger=self._handle_trigger,
        )

        self.last_schedule: Optional[dict] = None
        self.last_run_time: Optional[datetime] = None
        self.last_result: Optional[OptimizationResult] = None
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None

        logger.info(f"Initialized DepotController for depot {self.depot_id}")

    async def _handle_trigger(self, reason: str) -> None:
        """Handle re-optimization trigger.

        Args:
            reason: Trigger reason string
        """
        logger.info(f"Trigger fired: {reason}")
        await self.run_optimization(reason)

    async def run_optimization(
        self, trigger_reason: str = "scheduled"
    ) -> OptimizationResult:
        """Run optimization and dispatch commands.

        Args:
            trigger_reason: Reason for optimization run

        Returns:
            OptimizationResult with schedule and metrics
        """
        logger.info(
            f"Running optimization for depot {self.depot_id}, "
            f"trigger: {trigger_reason}"
        )

        try:
            # Assemble state
            state = await self.assembler.get_current_state(24)

            # Build and solve
            result = optimize(state, self.config, time_limit=30.0)

            # Store result
            self.last_schedule = result.schedule
            self.last_result = result
            self.last_run_time = datetime.utcnow()

            # Store in database
            await self._store_result(result, trigger_reason)

            # Dispatch commands to chargers
            if self.ocpp_server:
                await self._dispatch_commands(result)

            # Update trigger monitor expected state
            expected_socs = {
                vid: sched['soc'][1] if len(sched['soc']) > 1 else sched['soc'][0]
                for vid, sched in result.schedule.items()
            }
            self.trigger_monitor.update_expected_state(expected_socs, {})

            # Update price baseline for trigger monitor
            price_dict = {
                datetime.utcnow() + timedelta(hours=i * self.config.delta_t): price
                for i, price in enumerate(state.prices[:24])  # Next 24 hours
            }
            self.trigger_monitor.update_prices(price_dict)

            logger.info(
                f"Optimization complete. Objective: ${result.objective_value:.2f}, "
                f"solve time: {result.solve_time:.2f}s"
            )
            return result

        except Exception as e:
            logger.error(f"Optimization failed: {e}", exc_info=True)
            raise

    async def _dispatch_commands(self, result: OptimizationResult) -> None:
        """Send charging commands via OCPP.

        Args:
            result: Optimization result with charging schedule
        """
        if not self.ocpp_server:
            logger.warning("OCPP server not available, skipping command dispatch")
            return

        logger.info("Dispatching charging commands to chargers")

        for vehicle_id, schedule in result.schedule.items():
            # Find corresponding charge point
            # Note: This assumes vehicle_id maps to charge point ID
            # In production, would query vehicles table for ocpp_id
            cp = getattr(self.ocpp_server, 'charge_points', {}).get(vehicle_id)
            if cp is None:
                logger.debug(f"No charge point found for vehicle {vehicle_id}")
                continue

            # Build charging schedule for next hour (4 x 15min periods)
            charging_schedule = []
            for t, power in enumerate(schedule['charging_power'][:4]):
                if power > 0:
                    charging_schedule.append({
                        'start_period': t * 900,  # seconds
                        'limit': int(power * 1000),  # Watts
                        'number_phases': 3,
                    })

            if charging_schedule:
                try:
                    # Use OCPP SetChargingProfile
                    success = await cp.set_charging_profile(1, charging_schedule)
                    if success:
                        logger.info(
                            f"Set charging profile for {vehicle_id}: "
                            f"{len(charging_schedule)} periods"
                        )
                    else:
                        logger.warning(
                            f"Failed to set charging profile for {vehicle_id}"
                        )
                except Exception as e:
                    logger.error(
                        f"Error setting charging profile for {vehicle_id}: {e}"
                    )

    async def _store_result(
        self, result: OptimizationResult, trigger_reason: str
    ) -> None:
        """Store optimization result in database.

        Args:
            result: Optimization result
            trigger_reason: Reason for optimization run
        """
        query = """
        INSERT INTO optimization_runs 
            (run_id, depot_id, run_time, trigger_reason, horizon_start, horizon_end,
             solve_time_s, objective_value, peak_demand_kw, status, schedule_json)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """
        import json

        now = datetime.utcnow()
        horizon_start = now
        horizon_end = now + timedelta(hours=24)

        async with self.pool.acquire() as conn:
            await conn.execute(
                query,
                result.run_id,
                self.depot_id,
                now,
                trigger_reason,
                horizon_start,
                horizon_end,
                result.solve_time,
                result.objective_value,
                result.peak_demand,
                result.status,
                json.dumps({
                    'schedule': result.schedule,
                    'battery_dispatch': result.battery_dispatch,
                    'grid_power': result.grid_power,
                }),
            )

        logger.debug(f"Stored optimization result {result.run_id}")

    async def run(self) -> None:
        """Main control loop.

        Runs hourly optimizations (7am-11pm) and monitors for triggers.
        """
        self._running = True
        logger.info(f"Starting control loop for depot {self.depot_id}")

        # Start trigger monitor
        self._monitor_task = asyncio.create_task(self.trigger_monitor.run())

        while self._running:
            try:
                now = datetime.utcnow()

                # Run hourly optimization (7am-11pm per PRD)
                if 7 <= now.hour <= 23:
                    if (
                        self.last_run_time is None
                        or now - self.last_run_time > timedelta(hours=1)
                    ):
                        await self.run_optimization("hourly")

                await asyncio.sleep(60)  # Check every minute

            except Exception as e:
                logger.error(f"Control loop error: {e}", exc_info=True)
                await asyncio.sleep(60)

        logger.info(f"Control loop stopped for depot {self.depot_id}")

    def stop(self) -> None:
        """Stop controller."""
        self._running = False
        self.trigger_monitor.stop()
        if self._monitor_task:
            self._monitor_task.cancel()
        logger.info(f"Stopped controller for depot {self.depot_id}")

