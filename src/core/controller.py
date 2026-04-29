"""Main control loop for depot optimization.

Reference: Development plan Step 5.2, PRD_v2.md#5-system-architecture
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Optional

from .controller_config import ControllerConfig
from .models import DepotConfig, OptimizationInputSnapshot, OptimizationResult
from .optimizer import optimize
from .state.assembler import StateAssembler
from .state.readiness import build_snapshot, evaluate_readiness
from .state.triggers import TriggerConfig, TriggerMonitor
from ..db.pools import DatabasePools
from ..db.snapshot_store import link_snapshot_to_run, persist_snapshot
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


def _stub_snapshot(
    depot_id: str, horizon_start: datetime, horizon_end: datetime
) -> "OptimizationInputSnapshot":
    """Fallback snapshot used when real construction fails.

    Returns a ``ready`` snapshot so the controller continues; the failure
    is already logged. The stub is *not* persisted.
    """
    from uuid import UUID, uuid4

    from .models import OptimizationInputSnapshot, ReadinessReport

    try:
        depot_uuid = UUID(depot_id)
    except (ValueError, TypeError):
        depot_uuid = uuid4()
    return OptimizationInputSnapshot(
        snapshot_id=uuid4(),
        depot_id=depot_uuid,
        organization_id=None,
        captured_at=datetime.utcnow(),
        horizon_start=horizon_start,
        horizon_end=horizon_end,
        readiness=ReadinessReport(status="ready", building_load_source="absent"),
        depot={},
        vehicles=[],
        chargers={},
        charger_vehicle_access={},
        schedules=[],
        prices=[],
        telemetry={},
        building_load={},
    )


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

                # Persist a full input snapshot before solving so post-mortem
                # replay works even when the solver crashes/times out.
                snapshot = await self._capture_snapshot(state, effective_horizon)
                if snapshot.readiness.is_blocking:
                    raise RuntimeError(
                        "Optimization inputs not ready: "
                        f"missing={snapshot.readiness.missing_inputs}"
                    )

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

                # If readiness flagged a degraded run (e.g. building load
                # forecast fallback), record that on the result so the
                # downstream optimization_runs row reflects reality.
                if snapshot.readiness.is_degraded and result.status in (
                    "optimal",
                    "feasible",
                ):
                    result.status = "degraded"

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

                # Link snapshot to the optimization run row.
                try:
                    await link_snapshot_to_run(
                        self.pools, snapshot.snapshot_id, result.run_id
                    )
                except Exception as e:
                    logger.warning(
                        "Failed to link snapshot %s to run %s: %s",
                        snapshot.snapshot_id,
                        result.run_id,
                        e,
                    )

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

    async def _capture_snapshot(
        self, state, horizon_hours: int
    ) -> OptimizationInputSnapshot:
        """Build, persist, and return the input snapshot for one optimization.

        Readiness is evaluated against ``self.config`` plus the freshly
        assembled ``DepotState`` and the metadata recorded on the assembler
        (building-load source, schedule presence, weather features). The
        snapshot is written to ``optimization_input_snapshots`` immediately
        — even when readiness is ``not_ready`` — so we always have an
        artefact for replay/diagnostics. ``run_id`` is back-filled later.

        Snapshot construction failures must never break optimization, so
        the whole body is wrapped: on error we fall back to a stub
        ``ready`` snapshot and log loudly.
        """
        horizon = self.assembler.last_horizon
        if horizon is None:
            now = datetime.utcnow()
            horizon = (now, now + timedelta(hours=horizon_hours))

        # Best-effort: fetch weather + organization_id for the snapshot.
        # Failures here must not derail the run.
        try:
            await self.assembler.fetch_snapshot_extras(horizon[0], horizon[1])
        except Exception as e:
            logger.debug(
                "fetch_snapshot_extras failed for depot %s: %s",
                self.depot_id,
                e,
            )

        try:
            readiness = evaluate_readiness(
                self.config,
                state,
                building_load_source=self.assembler.last_building_load_source,
                schedules_present=self.assembler.last_schedules_present,
            )
            snapshot = build_snapshot(
                depot_id=self.depot_id,
                organization_id=self.assembler.last_organization_id,
                config=self.config,
                state=state,
                horizon_start=horizon[0],
                horizon_end=horizon[1],
                schedules=self.assembler.last_schedules,
                weather_features=self.assembler.last_weather_features,
                readiness=readiness,
            )
        except Exception as e:
            logger.error(
                "Failed to build optimization input snapshot for depot %s: %s",
                self.depot_id,
                e,
                exc_info=True,
            )
            return _stub_snapshot(self.depot_id, horizon[0], horizon[1])

        try:
            await persist_snapshot(self.pools, snapshot)
        except Exception as e:
            # Snapshot persistence must never abort an otherwise-runnable
            # optimization — log loudly and continue.
            logger.error(
                "Failed to persist optimization input snapshot for depot %s: %s",
                self.depot_id,
                e,
                exc_info=True,
            )
        return snapshot

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
