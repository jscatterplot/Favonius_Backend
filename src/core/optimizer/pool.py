"""Process-pool wrapper for the MILP solver.

The solver is CPU-bound Python plus Pyomo/Gurobi C extensions that hold the
GIL for stretches. Running it on the same asyncio loop as the FastAPI app
freezes ``/health``, OCPP same-port WebSockets, and Prometheus scrapes for
the duration of a solve (up to 60 s). This module dispatches each solve to a
separate worker process so the API loop stays responsive.

Reference: docs/CONTROL_LOOP.md, PRD §8.2, §8.3, §10.2.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import signal
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime
from typing import Any, Callable, Optional

from ...monitoring.metrics import SOLVER_POOL_BROKEN, SOLVER_POOL_INFLIGHT
from ..models import DepotConfig, DepotState, OptimizationResult
from .exceptions import SolverError, SolverTimeoutError

logger = logging.getLogger(__name__)

DEFAULT_WORKER_AS_LIMIT_BYTES = 1_500 * 1024 * 1024  # ~1.5 GiB per worker
DEFAULT_WORKER_ALARM_GRACE_S = 15
DEFAULT_MAX_WORKERS = 2
# Recycle each worker after this many solves so Pyomo/Gurobi memory that
# accumulates across solves is returned to the OS. ``None`` keeps workers for
# the life of the pool (the pre-3.11 behaviour).
DEFAULT_WORKER_MAX_TASKS: Optional[int] = 20


def _worker_init(as_limit_bytes: int) -> None:
    """Child process initializer: hard memory ceiling, sane logging."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (as_limit_bytes, as_limit_bytes))
    except (ImportError, ValueError, OSError):
        # macOS doesn't honour RLIMIT_AS reliably; the parent's asyncio timeout
        # still protects the loop.
        pass
    logging.basicConfig(
        level=os.getenv("SOLVER_WORKER_LOG_LEVEL", "INFO"),
        format="%(asctime)s [solver-worker pid=%(process)d] %(levelname)s: %(message)s",
    )


def _worker_solve(
    state: DepotState,
    config: DepotConfig,
    time_limit: float,
    previous_result: Optional[OptimizationResult],
    horizon_start: Optional[datetime],
    alarm_grace_s: int,
) -> OptimizationResult:
    """Run one MILP solve in this worker. SIGALRM is the last-resort kill."""
    try:
        signal.alarm(int(time_limit + alarm_grace_s))
    except (AttributeError, ValueError):
        # Windows or non-main thread; parent-side wait_for still bounds runtime.
        pass
    try:
        # Lazy import keeps Pyomo/Gurobi out of the parent process — a missing
        # solver license fails the first solve, not API startup.
        from .milp_model import optimize as _optimize

        return _optimize(
            state,
            config,
            time_limit=time_limit,
            previous_result=previous_result,
            horizon_start=horizon_start,
        )
    finally:
        try:
            signal.alarm(0)
        except (AttributeError, ValueError):
            pass


class SolverPool:
    """Owns a ``ProcessPoolExecutor`` running MILP solves out of the API process.

    A single child crash poisons the whole CPython executor, so this wrapper
    catches ``BrokenProcessPool`` (and parent-side ``TimeoutError``) and
    recreates the pool transparently. One retry per submission; the second
    failure surfaces as ``SolverError`` / ``SolverTimeoutError``.

    ``worker_fn`` is injectable to keep unit tests free of the Pyomo/Gurobi
    import path. Production callers leave it at the default.
    """

    def __init__(
        self,
        max_workers: int = DEFAULT_MAX_WORKERS,
        worker_fn: Callable[..., Any] = _worker_solve,
        worker_init_fn: Callable[..., None] = _worker_init,
        worker_as_limit_bytes: int = DEFAULT_WORKER_AS_LIMIT_BYTES,
        worker_alarm_grace_s: int = DEFAULT_WORKER_ALARM_GRACE_S,
        parent_timeout_buffer_s: float = 5.0,
        worker_max_tasks: Optional[int] = DEFAULT_WORKER_MAX_TASKS,
    ) -> None:
        self._max_workers = max_workers
        self._worker_fn = worker_fn
        self._worker_init_fn = worker_init_fn
        self._worker_as_limit_bytes = worker_as_limit_bytes
        self._worker_alarm_grace_s = worker_alarm_grace_s
        self._parent_timeout_buffer_s = parent_timeout_buffer_s
        # ``<= 0`` disables recycling (workers live for the pool's lifetime).
        self._worker_max_tasks = worker_max_tasks if (worker_max_tasks or 0) > 0 else None
        self._executor: Optional[ProcessPoolExecutor] = None
        self._lock = asyncio.Lock()
        self._mp_context = multiprocessing.get_context("spawn")

    def _make_executor(self) -> ProcessPoolExecutor:
        # ``max_tasks_per_child`` (Python 3.11+) replaces each worker after a
        # bounded number of solves, releasing the Pyomo/Gurobi heap it built up.
        # Safe with our explicit "spawn" context; the recreate path already
        # tolerates workers coming and going.
        return ProcessPoolExecutor(
            max_workers=self._max_workers,
            mp_context=self._mp_context,
            initializer=self._worker_init_fn,
            initargs=(self._worker_as_limit_bytes,),
            max_tasks_per_child=self._worker_max_tasks,
        )

    async def start(self) -> None:
        async with self._lock:
            if self._executor is not None:
                return
            self._executor = self._make_executor()
        logger.info(
            "SolverPool started (workers=%d, AS limit=%d MiB)",
            self._max_workers,
            self._worker_as_limit_bytes // (1024 * 1024),
        )

    async def stop(self) -> None:
        async with self._lock:
            executor = self._executor
            self._executor = None
        if executor is not None:
            # Run shutdown off-loop — it can block briefly waiting on children.
            await asyncio.to_thread(executor.shutdown, True, cancel_futures=True)
            logger.info("SolverPool stopped")

    async def _recreate(self, reason: str, failed_executor: Optional[ProcessPoolExecutor] = None) -> None:
        SOLVER_POOL_BROKEN.labels(reason=reason).inc()
        async with self._lock:
            # If another task already rotated the pool, do not replace it again.
            if failed_executor is not None and self._executor is not failed_executor:
                return
            old = self._executor
            self._executor = self._make_executor()
        if old is not None:
            await asyncio.to_thread(self._shutdown_executor, old)

    def _shutdown_executor(self, executor: ProcessPoolExecutor) -> None:
        """Stop queued work, then hard-stop live worker processes."""
        processes = list(getattr(executor, "_processes", {}).values())
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # pragma: no cover — defensive
            pass

        # ``shutdown(wait=False)`` does not stop running calls. Force-terminate
        # worker processes so timeouts don't accumulate orphaned CPU/memory use.
        for process in processes:
            if process is None or process.exitcode is not None:
                continue
            try:
                process.terminate()
            except Exception:  # pragma: no cover — best effort
                pass

    async def solve(
        self,
        state: DepotState,
        config: DepotConfig,
        time_limit: float,
        previous_result: Optional[OptimizationResult] = None,
        horizon_start: Optional[datetime] = None,
    ) -> OptimizationResult:
        """Dispatch one solve. Recreates the pool on broken-pool or timeout."""
        parent_timeout = time_limit + self._worker_alarm_grace_s + self._parent_timeout_buffer_s

        last_exc: Optional[BaseException] = None
        for attempt in (1, 2):
            executor = self._executor
            if executor is None:
                raise RuntimeError("SolverPool is not started")
            loop = asyncio.get_event_loop()
            SOLVER_POOL_INFLIGHT.inc()
            try:
                future = loop.run_in_executor(
                    executor,
                    self._worker_fn,
                    state,
                    config,
                    time_limit,
                    previous_result,
                    horizon_start,
                    self._worker_alarm_grace_s,
                )
                return await asyncio.wait_for(future, timeout=parent_timeout)
            except BrokenProcessPool as exc:
                last_exc = exc
                logger.error(
                    "SolverPool broken (attempt=%d); recreating executor.",
                    attempt,
                    exc_info=True,
                )
                await self._recreate(reason="broken_pool", failed_executor=executor)
            except asyncio.TimeoutError as exc:
                last_exc = exc
                logger.error(
                    "SolverPool wait_for timeout after %.1fs (attempt=%d); recreating executor.",
                    parent_timeout,
                    attempt,
                )
                # The runaway worker may still be holding a slot. Burn the
                # executor so the OS reaps the orphan and the next call starts
                # clean.
                await self._recreate(reason="timeout", failed_executor=executor)
                if attempt == 2:
                    raise SolverTimeoutError(time_limit) from exc
            finally:
                SOLVER_POOL_INFLIGHT.dec()

        raise SolverError(f"Solver pool broken twice; last error: {last_exc}", "broken_pool")


_solver_pool: Optional[SolverPool] = None


def set_solver_pool(pool: Optional[SolverPool]) -> None:
    """Install (or clear) the process-wide solver pool. Called from lifespan."""
    global _solver_pool
    _solver_pool = pool


def get_solver_pool() -> Optional[SolverPool]:
    """Return the active solver pool, or None if solves should run in-process."""
    return _solver_pool
