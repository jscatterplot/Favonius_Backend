"""Main control loop for depot optimization.

Reference: Development plan Step 5.2, PRD_v2.md#5-system-architecture
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional
from uuid import UUID

from .controller_config import ControllerConfig
from .models import DepotConfig, OptimizationResult
from .optimizer import optimize
from .state.assembler import StateAssembler
from .state.triggers import TriggerConfig, TriggerMonitor
from ..db.pools import DatabasePools
from ..monitoring.metrics import (
    CONTROL_LOOP_UPTIME,
    CONTROLLER_STATE,
    OCPP_DISPATCH_FAILURES,
    OCPP_DISPATCH_SUCCESS,
    OPTIMIZATION_DURATION,
    OPTIMIZATION_FAILURES,
    OPTIMIZATION_RUNS,
)

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
        depot_id: str | UUID,
        config: DepotConfig,
        pools: Optional[DatabasePools] = None,
        ocpp_server: Optional["OCPPServer"] = None,
        controller_config: Optional[ControllerConfig] = None,
        pool=None,
    ):
        """Initialize depot controller.

        Args:
            pools: Dual database connection pools (static=Supabase, ts=TimescaleDB)
            depot_id: Depot identifier
            config: Depot configuration
            ocpp_server: Optional OCPP server for charger communication
            controller_config: Optional controller configuration
            pool: Legacy single-pool argument; used for both static and timeseries pools
        """
        if pools is None:
            if pool is None:
                raise ValueError("Either `pools` or legacy `pool` must be provided")
            pools = DatabasePools(static=pool, ts=pool)

        self.pools = pools
        self.depot_id = str(depot_id)
        self.config = config
        self.ocpp_server = ocpp_server
        self.controller_config = controller_config or ControllerConfig.from_env()
        self.assembler = StateAssembler(pools, self.depot_id, config)

        self.trigger_monitor = TriggerMonitor(
            TriggerConfig(trigger_cooldown_minutes=self.controller_config.trigger_cooldown_minutes),
            on_trigger=self._handle_trigger,
            assembler=self.assembler,
        )

        self.last_schedule: Optional[dict] = None
        self.last_run_time: Optional[datetime] = None
        self.last_result: Optional[OptimizationResult] = None
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None

        # Error handling and resilience
        self._optimization_failures = 0
        self._last_trigger_time: Optional[datetime] = None
        self._circuit_breaker_open = False
        self._circuit_breaker_reset_time: Optional[datetime] = None
        self._current_optimization_task: Optional[asyncio.Task] = None
        self._control_loop_task: Optional[asyncio.Task] = None
        self._start_time: Optional[datetime] = None

        logger.info(f"Initialized DepotController for depot {self.depot_id}")

    async def _handle_trigger(self, reason: str) -> None:
        """Handle re-optimization trigger.

        Args:
            reason: Trigger reason string
        """
        # Check cooldown period
        now = datetime.utcnow()
        cooldown = timedelta(minutes=self.controller_config.trigger_cooldown_minutes)

        if self._last_trigger_time is not None:
            time_since_trigger = now - self._last_trigger_time
            if time_since_trigger < cooldown:
                logger.debug(
                    f"Trigger {reason} ignored due to cooldown "
                    f"({time_since_trigger.total_seconds():.0f}s < "
                    f"{cooldown.total_seconds():.0f}s)"
                )
                return

        # Check circuit breaker
        if self._circuit_breaker_open:
            if self._circuit_breaker_reset_time and now < self._circuit_breaker_reset_time:
                logger.warning(
                    f"Circuit breaker open, ignoring trigger {reason}. "
                    f"Reset in {(self._circuit_breaker_reset_time - now).total_seconds():.0f}s"
                )
                return
            else:
                # Reset circuit breaker
                logger.info("Resetting circuit breaker")
                self._circuit_breaker_open = False
                self._circuit_breaker_reset_time = None
                self._optimization_failures = 0

        self._last_trigger_time = now
        logger.info(f"Trigger fired: {reason}")

        try:
            await self.run_optimization(reason)
        except Exception as e:
            logger.error(f"Triggered optimization failed: {e}", exc_info=True)

    async def run_optimization(
        self, trigger_reason: str = "scheduled", horizon_hours: int | None = None
    ) -> OptimizationResult:
        """Run optimization and dispatch commands with retry logic.

        Args:
            trigger_reason: Reason for optimization run
            horizon_hours: Optional override for optimization horizon in hours

        Returns:
            OptimizationResult with schedule and metrics

        Raises:
            Exception: If optimization fails after all retries
        """
        logger.info(
            f"Running optimization for depot {self.depot_id}, " f"trigger: {trigger_reason}"
        )

        # Track optimization start
        optimization_start = time.time()

        max_retries = 2  # Initial attempt + 2 retries
        retry_delay = 1.0  # Start with 1 second delay

        for attempt in range(max_retries + 1):
            try:
                # Assemble state
                effective_horizon = (
                    horizon_hours
                    if horizon_hours is not None
                    else self.controller_config.optimization_horizon_hours
                )
                try:
                    state = await self.assembler.get_current_state(effective_horizon)
                except Exception as e:
                    logger.error(
                        f"State assembly failed: {e}",
                        exc_info=True,
                        extra={"depot_id": self.depot_id, "attempt": attempt + 1},
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(retry_delay * (2**attempt))
                        continue
                    raise

                # Build and solve
                try:
                    result = optimize(
                        state, self.config, time_limit=self.controller_config.optimization_timeout
                    )
                except Exception as e:
                    error_msg = str(e).lower()
                    if "timeout" in error_msg or "time limit" in error_msg:
                        logger.warning(
                            f"Optimization timeout (attempt {attempt + 1}/{max_retries + 1})"
                        )
                        if attempt < max_retries:
                            await asyncio.sleep(retry_delay * (2**attempt))
                            continue
                    logger.error(
                        f"Optimization failed: {e}",
                        exc_info=True,
                        extra={"depot_id": self.depot_id, "attempt": attempt + 1},
                    )
                    if attempt < max_retries:
                        await asyncio.sleep(retry_delay * (2**attempt))
                        continue
                    raise

                # Store result
                self.last_schedule = result.schedule
                self.last_result = result
                self.last_run_time = datetime.utcnow()

                # Store in database
                try:
                    await self._store_result(result, trigger_reason)
                except Exception as e:
                    logger.error(
                        f"Failed to store result: {e}",
                        exc_info=True,
                        extra={"depot_id": self.depot_id},
                    )
                    # Continue even if storage fails

                # Enqueue commands for the legacy WS handler to push.
                # Production runs the FastAPI service with ocpp_server=None,
                # so we always go through charging_command_queue (migration
                # 013/014). The in-process server is only used in dev/test.
                try:
                    await self._dispatch_commands(result)
                except Exception as e:
                    logger.error(
                        f"OCPP dispatch failed: {e}",
                        exc_info=True,
                        extra={"depot_id": self.depot_id},
                    )
                    # Continue even if dispatch fails (will retry later)

                # Update trigger monitor expected state
                expected_socs = {
                    vid: sched["soc"][1] if len(sched["soc"]) > 1 else sched["soc"][0]
                    for vid, sched in result.schedule.items()
                }
                self.trigger_monitor.update_expected_state(expected_socs, {})

                # Update price baseline for trigger monitor
                price_dict = {
                    datetime.utcnow() + timedelta(hours=i * self.config.delta_t): price
                    for i, price in enumerate(state.prices[:24])  # Next 24 hours
                }
                self.trigger_monitor.update_prices(price_dict)

                # Reset failure counter on success
                self._optimization_failures = 0
                self._circuit_breaker_open = False
                self._circuit_breaker_reset_time = None

                # Record metrics
                optimization_duration = time.time() - optimization_start
                OPTIMIZATION_RUNS.labels(
                    depot_id=self.depot_id, trigger_reason=trigger_reason
                ).inc()
                OPTIMIZATION_DURATION.labels(depot_id=self.depot_id).observe(optimization_duration)

                logger.info(
                    f"Optimization complete ({result.solver_used}). Objective: ${result.objective_value:.2f}, "
                    f"solve time: {result.solve_time_s:.2f}s, "
                    f"total duration: {optimization_duration:.2f}s"
                )
                return result

            except Exception as e:
                self._optimization_failures += 1

                # Determine failure type
                error_msg = str(e).lower()
                if "timeout" in error_msg or "time limit" in error_msg:
                    failure_type = "timeout"
                elif "infeasible" in error_msg:
                    failure_type = "infeasible"
                elif "state" in error_msg or "assembly" in error_msg:
                    failure_type = "state_assembly"
                else:
                    failure_type = "other"

                # Record failure metric
                OPTIMIZATION_FAILURES.labels(
                    depot_id=self.depot_id, failure_type=failure_type
                ).inc()

                logger.error(
                    f"Optimization failed (attempt {attempt + 1}/{max_retries + 1}): {e}",
                    exc_info=True,
                    extra={
                        "depot_id": self.depot_id,
                        "failures": self._optimization_failures,
                        "trigger_reason": trigger_reason,
                        "failure_type": failure_type,
                    },
                )

                # Check circuit breaker
                if self._optimization_failures >= self.controller_config.max_optimization_failures:
                    self._circuit_breaker_open = True
                    # Reset after 30 minutes
                    self._circuit_breaker_reset_time = datetime.utcnow() + timedelta(minutes=30)
                    logger.error(
                        f"Circuit breaker opened after {self._optimization_failures} failures. "
                        f"Will reset at {self._circuit_breaker_reset_time.isoformat()}"
                    )

                if attempt < max_retries:
                    delay = retry_delay * (2**attempt)
                    logger.info(f"Retrying optimization in {delay:.1f}s...")
                    await asyncio.sleep(delay)
                else:
                    # All retries exhausted
                    raise

    async def _dispatch_commands(self, result: OptimizationResult) -> None:
        """Enqueue charging profiles for the WebSocket handler to deliver.

        Production runs the FastAPI service with ``OCPP_SERVER_ENABLED=false``
        — chargers connect to the *legacy* websocket_handler service. We can
        therefore not call ``set_charging_profile`` in-process. Instead we
        write one row per scheduled vehicle to ``charging_command_queue``;
        the legacy handler's ``ChargingCommandQueueConsumer`` (and the
        BootNotification replay path) push the profile when the charger
        is reachable.

        Args:
            result: Optimization result with charging schedule
        """
        from ..adapters.ocpp.dispatch import dispatch_charging_profiles

        logger.info(
            "Enqueuing charging commands for depot %s (%d vehicles)",
            self.depot_id,
            len(result.schedule),
        )

        enqueue_results = await dispatch_charging_profiles(
            result,
            pools=self.pools,
            depot_id=self.depot_id,
            vehicle_to_charger_map=None,
            delta_t=self.config.delta_t,
            expires_in_min=60,
        )
        successful = sum(1 for dispatched in enqueue_results.values() if dispatched)
        total = len(enqueue_results)
        failed = total - successful
        if successful:
            OCPP_DISPATCH_SUCCESS.labels(depot_id=self.depot_id).inc(successful)
        if failed:
            OCPP_DISPATCH_FAILURES.labels(depot_id=self.depot_id, error_type="enqueue_failed").inc(
                failed
            )
        logger.info(
            "Enqueue complete: %d/%d enqueued for depot %s",
            successful,
            total,
            self.depot_id,
        )

    def _validate_charging_profile(self, charging_schedule: list[dict]) -> bool:
        """Validate charging profile before dispatch.

        Args:
            charging_schedule: List of charging schedule periods

        Returns:
            True if valid, False otherwise
        """
        if not charging_schedule:
            return False

        # Check that periods are in order
        last_period = -1
        for period in charging_schedule:
            start = period.get("start_period", -1)
            if start <= last_period:
                logger.warning(f"Invalid period order: {start} <= {last_period}")
                return False
            last_period = start

            # Check power limits are reasonable
            limit = period.get("limit", 0)
            if limit < 0 or limit > 200000:  # 200kW max
                logger.warning(f"Invalid power limit: {limit}W")
                return False

        return True

    async def _store_dispatch_results(
        self, run_id: UUID, dispatch_results: dict[str, dict]
    ) -> None:
        """Store OCPP dispatch results in database.

        Args:
            run_id: Optimization run ID
            dispatch_results: Dictionary of vehicle_id -> dispatch result
        """
        import json

        query = """
        INSERT INTO charging_commands 
            (run_id, charger_id, vehicle_id, issued_at, profile_json, status)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT DO NOTHING
        """

        now = datetime.utcnow()

        for vehicle_id, result in dispatch_results.items():
            try:
                # Get charger_id from vehicle (would need to query vehicles table)
                # For now, use vehicle_id as placeholder
                charger_id = None  # TODO: Query from vehicles table

                status = "accepted" if result.get("success") else "rejected"
                profile_json = json.dumps(result)

                async with self.pools.ts.acquire() as conn:
                    await conn.execute(
                        query,
                        run_id,
                        charger_id,
                        vehicle_id,
                        now,
                        profile_json,
                        status,
                    )
            except Exception as e:
                logger.error(
                    f"Failed to store dispatch result for {vehicle_id}: {e}", exc_info=True
                )

    async def _store_result(self, result: OptimizationResult, trigger_reason: str) -> None:
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

        async with self.pools.ts.acquire() as conn:
            await conn.execute(
                query,
                result.run_id,
                self.depot_id,
                now,
                trigger_reason,
                horizon_start,
                horizon_end,
                result.solve_time_s,
                result.objective_value,
                result.peak_demand_kw,
                result.status,
                json.dumps(
                    {
                        "schedule": result.schedule,
                        "battery_dispatch": result.battery_dispatch,
                        "grid_power": result.grid_power,
                        "solver_used": result.solver_used,
                    }
                ),
            )

        logger.debug(f"Stored optimization result {result.run_id}")

    async def run(self) -> None:
        """Main control loop.

        Runs hourly optimizations (7am-11pm) and monitors for triggers.
        """
        self._running = True
        self._start_time = datetime.utcnow()
        time.time()

        logger.info(f"Starting control loop for depot {self.depot_id}")

        # Update state metric
        CONTROLLER_STATE.labels(depot_id=self.depot_id).set(1)

        # Start trigger monitor
        self._monitor_task = asyncio.create_task(self.trigger_monitor.run())

        # Store control loop task for graceful shutdown
        self._control_loop_task = asyncio.current_task()

        while self._running:
            try:
                now = datetime.utcnow()

                # Update uptime metric
                if self._start_time:
                    uptime = (now - self._start_time).total_seconds()
                    CONTROL_LOOP_UPTIME.labels(depot_id=self.depot_id).set(uptime)

                # Run hourly optimization (configurable hours per PRD)
                opt_start = self.controller_config.hourly_optimization_start
                opt_end = self.controller_config.hourly_optimization_end

                if opt_start <= now.hour <= opt_end:
                    if self.last_run_time is None or now - self.last_run_time > timedelta(hours=1):
                        await self.run_optimization("hourly")

                await asyncio.sleep(60)  # Check every minute

            except Exception as e:
                logger.error(f"Control loop error: {e}", exc_info=True)
                await asyncio.sleep(60)

        # Update state metric
        CONTROLLER_STATE.labels(depot_id=self.depot_id).set(0)
        logger.info(f"Control loop stopped for depot {self.depot_id}")

    def stop(self) -> None:
        """Stop controller (synchronous).

        For async graceful shutdown, use stop_async().
        """
        self._running = False
        self.trigger_monitor.stop()
        if self._monitor_task:
            self._monitor_task.cancel()
        if self._control_loop_task:
            self._control_loop_task.cancel()
        logger.info(f"Stopped controller for depot {self.depot_id}")

    async def stop_async(self, timeout: float = 30.0) -> None:
        """Stop controller gracefully with timeout.

        Args:
            timeout: Maximum time to wait for operations to complete
        """
        logger.info(f"Stopping controller for depot {self.depot_id} gracefully...")
        self._running = False

        # Wait for current optimization to complete (if any)
        if self._current_optimization_task and not self._current_optimization_task.done():
            try:
                await asyncio.wait_for(
                    self._current_optimization_task,
                    timeout=timeout,
                )
                logger.debug("Current optimization completed before shutdown")
            except asyncio.TimeoutError:
                logger.warning(f"Optimization did not complete within {timeout}s, cancelling")
                self._current_optimization_task.cancel()
                try:
                    await self._current_optimization_task
                except asyncio.CancelledError:
                    pass

        # Stop trigger monitor
        self.trigger_monitor.stop()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        # Stop control loop task
        if self._control_loop_task and not self._control_loop_task.done():
            self._control_loop_task.cancel()
            try:
                await self._control_loop_task
            except asyncio.CancelledError:
                pass

        # Update state metric
        CONTROLLER_STATE.labels(depot_id=self.depot_id).set(0)

        logger.info(f"Controller stopped gracefully for depot {self.depot_id}")
