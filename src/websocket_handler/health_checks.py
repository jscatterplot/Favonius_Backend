"""Health checks for system components."""

import asyncio
import time
from typing import Any, Dict, List


from .config import Config
from .resilience_manager import HealthCheck
from .supabase_client import SupabaseClient
from .timescale_client import TimescaleClient


def create_health_checks(config: Config) -> List[HealthCheck]:
    """Create health checks for all system components."""
    health_checks = []

    # TimescaleDB health check
    health_checks.append(
        HealthCheck(
            name="timescale",
            check_func=lambda: _check_timescale_health(config),
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
            check_func=lambda: _check_supabase_health(config),
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
            check_func=lambda: _check_websocket_health(config),
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
            check_func=lambda: _check_system_resources(),
            timeout=5.0,
            interval=60.0,
            failure_threshold=2,
            recovery_threshold=1,
        )
    )

    return health_checks


def _check_timescale_health(config: Config) -> bool:
    """Check TimescaleDB health."""
    try:

        async def _run_check() -> bool:
            timescale_client = TimescaleClient(config.timescale)
            await timescale_client.connect()
            try:
                return await timescale_client.health_check()
            finally:
                await timescale_client.disconnect()

        return asyncio.run(_run_check())

    except Exception:
        return False


def _check_supabase_health(config: Config) -> bool:
    """Check Supabase health."""
    try:
        async def _run_check() -> bool:
            supabase_client = SupabaseClient(config.supabase)
            await supabase_client.connect()
            try:
                result = await supabase_client.fetch_one("SELECT 1")
                return result is not None
            finally:
                await supabase_client.close()

        return asyncio.run(_run_check())

    except Exception:
        return False


def _check_websocket_health(config: Config) -> bool:
    """Check WebSocket server health."""
    try:

        async def _run_check() -> bool:
            # Service-level health means the websocket listener is reachable.
            # Use a raw TCP probe so this check does not depend on a specific path/subprotocol.
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(config.websocket.host, config.websocket.port), timeout=5
            )
            writer.close()
            await writer.wait_closed()
            return reader is not None

        return asyncio.run(_run_check())

    except Exception:
        return False


def _check_system_resources() -> bool:
    """Check system resource availability."""
    try:
        import psutil

        # Check memory usage
        memory = psutil.virtual_memory()
        if memory.percent > 90:
            return False

        # Check disk usage
        disk = psutil.disk_usage("/")
        if disk.percent > 95:
            return False

        # Check CPU usage (average over 1 second)
        cpu_percent = psutil.cpu_percent(interval=1)
        if cpu_percent > 95:
            return False

        return True

    except ImportError:
        # psutil not available, assume healthy
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
