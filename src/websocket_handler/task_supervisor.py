"""Task supervisor for managing fire-and-forget async tasks with error handling."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Dict, Optional, Set

from .monitoring import get_logger


@dataclass
class TaskInfo:
    """Metadata about a supervised task."""

    name: str
    created_at: datetime
    completed_at: Optional[datetime] = None
    error: Optional[str] = None
    success: bool = False


class TaskSupervisor:
    """
    Supervises async tasks to prevent silent failures.

    Usage:
        supervisor = TaskSupervisor("ocpp_handler")
        supervisor.create_task(self._store_station_info(), "store_station_info")
    """

    def __init__(self, name: str, max_concurrent: int = 100):
        """
        Initialize task supervisor.

        Args:
            name: Name of this supervisor (for logging)
            max_concurrent: Maximum concurrent tasks before rejecting new ones
        """
        self.name = name
        self.max_concurrent = max_concurrent
        self.logger = get_logger(__name__)

        # Track active tasks (WeakSet allows GC of completed tasks)
        self._active_tasks: Set[asyncio.Task] = set()

        # Statistics
        self._stats = {
            "total_created": 0,
            "total_completed": 0,
            "total_failed": 0,
            "total_cancelled": 0,
        }

        # Recent errors for debugging (ring buffer)
        self._recent_errors: list = []
        self._max_recent_errors = 50

    def create_task(
        self,
        coro: Coroutine[Any, Any, Any],
        name: str,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> Optional[asyncio.Task]:
        """
        Create a supervised async task.

        Args:
            coro: The coroutine to run
            name: Human-readable name for the task
            on_error: Optional callback when task fails

        Returns:
            The created task, or None if max concurrent limit reached
        """
        # Check concurrent limit
        if len(self._active_tasks) >= self.max_concurrent:
            self.logger.warning(
                f"[{self.name}] Task limit reached ({self.max_concurrent}), "
                f"rejecting task '{name}'"
            )
            # Close the coroutine to avoid warning
            coro.close()
            return None

        # Create task with name
        task = asyncio.create_task(coro, name=f"{self.name}:{name}")

        # Track it
        self._active_tasks.add(task)
        self._stats["total_created"] += 1

        # Add completion callback
        task.add_done_callback(lambda t: self._handle_task_done(t, name, on_error))

        return task

    def _handle_task_done(
        self, task: asyncio.Task, name: str, on_error: Optional[Callable[[Exception], None]]
    ) -> None:
        """Handle task completion, logging any errors."""
        # Remove from active set
        self._active_tasks.discard(task)

        if task.cancelled():
            self._stats["total_cancelled"] += 1
            self.logger.debug(f"[{self.name}] Task '{name}' was cancelled")
            return

        exception = task.exception()
        if exception:
            self._stats["total_failed"] += 1
            error_msg = f"{type(exception).__name__}: {exception}"

            # Log the error
            self.logger.error(
                f"[{self.name}] Task '{name}' failed: {error_msg}", exc_info=exception
            )

            # Store in recent errors
            self._recent_errors.append(
                {
                    "task_name": name,
                    "error": error_msg,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            if len(self._recent_errors) > self._max_recent_errors:
                self._recent_errors.pop(0)

            # Call error callback if provided
            if on_error:
                try:
                    on_error(exception)
                except Exception as callback_error:
                    self.logger.error(
                        f"[{self.name}] Error callback for '{name}' failed: {callback_error}"
                    )
        else:
            self._stats["total_completed"] += 1
            self.logger.debug(f"[{self.name}] Task '{name}' completed successfully")

    @property
    def active_count(self) -> int:
        """Get number of currently active tasks."""
        return len(self._active_tasks)

    def get_stats(self) -> Dict[str, Any]:
        """Get task statistics."""
        return {
            **self._stats,
            "active_tasks": self.active_count,
            "recent_errors": self._recent_errors[-10:],  # Last 10 errors
        }

    async def cancel_all(self, timeout: float = 5.0) -> int:
        """
        Cancel all active tasks and wait for them to finish.

        Args:
            timeout: Maximum time to wait for tasks to cancel

        Returns:
            Number of tasks that were cancelled
        """
        if not self._active_tasks:
            return 0

        count = len(self._active_tasks)
        self.logger.info(f"[{self.name}] Cancelling {count} active tasks")

        # Cancel all tasks
        for task in self._active_tasks:
            task.cancel()

        # Wait for them to finish (with timeout)
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._active_tasks, return_exceptions=True), timeout=timeout
            )
        except asyncio.TimeoutError:
            self.logger.warning(
                f"[{self.name}] Timeout waiting for tasks to cancel, "
                f"{len(self._active_tasks)} tasks still running"
            )

        return count


# Global registry of supervisors for monitoring
_supervisors: Dict[str, TaskSupervisor] = {}


def get_supervisor(name: str, max_concurrent: int = 100) -> TaskSupervisor:
    """
    Get or create a named task supervisor.

    Args:
        name: Unique name for the supervisor
        max_concurrent: Maximum concurrent tasks

    Returns:
        The task supervisor instance
    """
    if name not in _supervisors:
        _supervisors[name] = TaskSupervisor(name, max_concurrent)
    return _supervisors[name]


def get_all_supervisor_stats() -> Dict[str, Dict[str, Any]]:
    """Get stats from all registered supervisors."""
    return {name: supervisor.get_stats() for name, supervisor in _supervisors.items()}


async def shutdown_all_supervisors(timeout: float = 10.0) -> None:
    """Cancel all tasks in all supervisors during shutdown."""
    logger = get_logger(__name__)
    logger.info(f"Shutting down {len(_supervisors)} task supervisors")

    for name, supervisor in _supervisors.items():
        try:
            cancelled = await supervisor.cancel_all(
                timeout=timeout / len(_supervisors) if _supervisors else timeout
            )
            logger.info(f"Supervisor '{name}' cancelled {cancelled} tasks")
        except Exception as e:
            logger.error(f"Error shutting down supervisor '{name}': {e}")
