"""Health checks for system components."""

import asyncio
import time
from typing import Any, Dict, List, Optional


from .config import Config
from .resilience_manager import HealthCheck


# Module-level timestamp set when the WebSocket server binds its port.
# The WebSocket health check skips failures during the grace period so
# the resilience manager does not log spurious warnings on every boot.
_ws_bind_time: Optional[float] = None
_WS_GRACE_SECONDS = 30.0


def notify_websocket_ready() -> None:
    """Call this once the WebSocket server has successfully bound its port."""
    global _ws_bind_time
    _ws_bind_time = time.monotonic()


def create_health_checks(
    config: Config,
    timescale_client: Optional[Any] = None,
    supabase_client: Optional[Any] = None,
) -> List[HealthCheck]:
    """Create health checks for all system components.

    When *timescale_client* and *supabase_client* are supplied the checks
    reuse the live connection pools instead of creating disposable ones on
    every cycle.  Pass them in after the clients are initialised.
    """
    health_checks = []

    # TimescaleDB health check
    health_checks.append(
        HealthCheck(
            name="timescale",
            check_func=_make_timescale_check(config, timescale_client),
            timeout=10.0,
            interval=30.0,
            failure_threshold=3,
            recovery_threshold=2,
        )
    )

    # Supabase health check
    health_checks.append(
        HealthCheck(
            name="supabase",
            check_func=_make_supabase_check(config, supabase_client),
            timeout=10.0,
            interval=30.0,
            failure_threshold=3,
            recovery_threshold=2,
        )
    )

    # WebSocket server health check
    health_checks.append(
        HealthCheck(
            name="websocket",
            check_func=_make_websocket_check(config),
            timeout=5.0,
            interval=15.0,
            failure_threshold=5,
            recovery_threshold=2,
        )
    )

    # System resources health check
    health_checks.append(
        HealthCheck(
            name="system_resources",
            check_func=_check_system_resources,
            timeout=5.0,
            interval=60.0,
            failure_threshold=2,
            recovery_threshold=1,
        )
    )

    return health_checks


def _make_timescale_check(config: Config, client: Optional[Any]) -> Any:
    """Return an async health-check function for TimescaleDB.

    Uses the live *client* pool when available; falls back to a one-shot
    connection (same as before) when the client is not yet initialised.
    """
    if client is not None:
        async def _check_with_live_client() -> bool:
            return await client.health_check()
        return _check_with_live_client

    # Fallback: create a disposable connection (used during early startup)
    from .timescale_client import TimescaleClient

    async def _check_fallback() -> bool:
        try:
            tc = TimescaleClient(config.timescale)
            await tc.connect()
            try:
                return await tc.health_check()
            finally:
                await tc.disconnect()
        except Exception:
            return False

    return _check_fallback


def _make_supabase_check(config: Config, client: Optional[Any]) -> Any:
    """Return an async health-check function for Supabase.

    Uses the live *client* pool when available.
    """
    if client is not None:
        async def _check_with_live_client() -> bool:
            return await client.health_check()
        return _check_with_live_client

    from .supabase_client import SupabaseClient

    async def _check_fallback() -> bool:
        try:
            sc = SupabaseClient(config.supabase)
            await sc.connect()
            try:
                return await sc.health_check()
            finally:
                await sc.close()
        except Exception:
            return False

    return _check_fallback


def _make_websocket_check(config: Config) -> Any:
    """Return an async TCP-probe health-check for the WebSocket server.

    Returns True during the startup grace period so the resilience manager
    does not log spurious failures before the server has bound its port.
    """
    async def _check() -> bool:
        # Honour startup grace period
        if _ws_bind_time is None:
            elapsed = 0.0
        else:
            elapsed = time.monotonic() - _ws_bind_time

        if _ws_bind_time is None or elapsed < 0:
            # Server not yet up; treat as healthy to avoid noise
            return True

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(config.websocket.host, config.websocket.port),
                timeout=5,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    return _check


def _check_system_resources() -> bool:
    """Check system resource availability (sync, runs in threadpool)."""
    try:
        import psutil

        memory = psutil.virtual_memory()
        if memory.percent > 90:
            return False

        disk = psutil.disk_usage("/")
        if disk.percent > 95:
            return False

        cpu_percent = psutil.cpu_percent(interval=1)
        if cpu_percent > 95:
            return False

        return True

    except ImportError:
        return True
    except Exception:
        return False


class HealthCheckRunner:
    """Runs health checks and provides status."""

    def __init__(self, health_checks: List[HealthCheck]):
        self.health_checks = health_checks
        self.last_results: Dict[str, bool] = {}
        self.last_check_times: Dict[str, float] = {}

    def run_all_checks(self) -> Dict[str, Any]:
        """Run all health checks and return results."""
        results = {}

        for health_check in self.health_checks:
            if not health_check.enabled:
                continue

            start_time = time.time()
            try:
                result = health_check.check_func()
                self.last_results[health_check.name] = result
                self.last_check_times[health_check.name] = time.time()

                results[health_check.name] = {
                    "status": "healthy" if result else "unhealthy",
                    "response_time_ms": (time.time() - start_time) * 1000,
                    "last_check": time.time(),
                }

            except Exception as e:
                self.last_results[health_check.name] = False
                self.last_check_times[health_check.name] = time.time()

                results[health_check.name] = {
                    "status": "error",
                    "error": str(e),
                    "response_time_ms": (time.time() - start_time) * 1000,
                    "last_check": time.time(),
                }

        return results

    def get_overall_status(self) -> str:
        """Get overall system status."""
        if not self.last_results:
            return "unknown"

        healthy_count = sum(1 for result in self.last_results.values() if result)
        total_count = len(self.last_results)

        if healthy_count == total_count:
            return "healthy"
        elif healthy_count > 0:
            return "degraded"
        else:
            return "unhealthy"
