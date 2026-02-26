"""Connection monitoring and automatic reconnection service."""

import asyncio
import random
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .monitoring import get_logger


class ConnectionMonitor:
    """Monitors database connections and handles automatic reconnection."""

    def __init__(self, timescale_client=None, supabase_client=None, check_interval: int = 30):
        """Initialize connection monitor."""
        self.timescale_client = timescale_client
        self.supabase_client = supabase_client
        self.check_interval = check_interval
        self.logger = get_logger(__name__)

        # Monitoring state
        self.monitoring = False
        self.monitor_task: Optional[asyncio.Task] = None

        # Connection health tracking
        self.last_health_check = {"timescale": None, "supabase": None}
        self.connection_failures = {"timescale": 0, "supabase": 0}
        self.max_failures_before_reconnect = 3

    async def start_monitoring(self) -> None:
        """Start connection monitoring."""
        if self.monitoring:
            return

        self.monitoring = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        self.logger.info("Connection monitoring started")

    async def stop_monitoring(self) -> None:
        """Stop connection monitoring."""
        self.monitoring = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        self.logger.info("Connection monitoring stopped")

    async def _monitor_loop(self) -> None:
        """Main monitoring loop."""
        while self.monitoring:
            try:
                await self._check_connections()
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Error in connection monitoring loop: {e}")
                await asyncio.sleep(self.check_interval)

    async def _check_connections(self) -> None:
        """Check all database connections."""
        current_time = datetime.now(timezone.utc)

        # Check TimescaleDB connection
        if self.timescale_client:
            await self._check_timescale_connection(current_time)

        # Check Supabase connection
        if self.supabase_client:
            await self._check_supabase_connection(current_time)

    async def _check_timescale_connection(self, current_time: datetime) -> None:
        """Check TimescaleDB connection health."""
        try:
            is_healthy = await self.timescale_client.health_check()
            self.last_health_check["timescale"] = current_time

            if is_healthy:
                self.connection_failures["timescale"] = 0
            else:
                self.connection_failures["timescale"] += 1
                self.logger.warning(
                    f"TimescaleDB health check failed (failure #{self.connection_failures['timescale']})"
                )

                if self.connection_failures["timescale"] >= self.max_failures_before_reconnect:
                    self.logger.error("TimescaleDB connection unhealthy, attempting reconnection")
                    # Add jitter to prevent connection storms after outages
                    jitter_delay = random.uniform(0, 5.0)
                    await asyncio.sleep(jitter_delay)
                    await self._reconnect_timescale()

        except Exception as e:
            self.connection_failures["timescale"] += 1
            self.logger.error(f"TimescaleDB health check error: {e}")

            if self.connection_failures["timescale"] >= self.max_failures_before_reconnect:
                # Add jitter to prevent connection storms after outages
                jitter_delay = random.uniform(0, 5.0)
                await asyncio.sleep(jitter_delay)
                await self._reconnect_timescale()

    async def _check_supabase_connection(self, current_time: datetime) -> None:
        """Check Supabase connection health."""
        try:
            is_healthy = await self.supabase_client.health_check()
            self.last_health_check["supabase"] = current_time

            if is_healthy:
                self.connection_failures["supabase"] = 0
            else:
                self.connection_failures["supabase"] += 1
                self.logger.warning(
                    f"Supabase health check failed (failure #{self.connection_failures['supabase']})"
                )

                if self.connection_failures["supabase"] >= self.max_failures_before_reconnect:
                    self.logger.error("Supabase connection unhealthy, attempting reconnection")
                    # Add jitter to prevent connection storms after outages
                    jitter_delay = random.uniform(0, 5.0)
                    await asyncio.sleep(jitter_delay)
                    await self._reconnect_supabase()

        except Exception as e:
            self.connection_failures["supabase"] += 1
            self.logger.error(f"Supabase health check error: {e}")

            if self.connection_failures["supabase"] >= self.max_failures_before_reconnect:
                # Add jitter to prevent connection storms after outages
                jitter_delay = random.uniform(0, 5.0)
                await asyncio.sleep(jitter_delay)
                await self._reconnect_supabase()

    async def _reconnect_timescale(self) -> None:
        """Reconnect to TimescaleDB."""
        try:
            await self.timescale_client.reconnect()
            self.connection_failures["timescale"] = 0
            self.logger.info("TimescaleDB reconnection successful")
        except Exception as e:
            self.logger.error(f"TimescaleDB reconnection failed: {e}")

    async def _reconnect_supabase(self) -> None:
        """Reconnect to Supabase."""
        try:
            await self.supabase_client.reconnect()
            self.connection_failures["supabase"] = 0
            self.logger.info("Supabase reconnection successful")
        except Exception as e:
            self.logger.error(f"Supabase reconnection failed: {e}")

    def get_connection_status(self) -> Dict[str, Any]:
        """Get current connection status."""
        return {
            "monitoring": self.monitoring,
            "last_health_check": {
                "timescale": (
                    self.last_health_check["timescale"].isoformat()
                    if self.last_health_check["timescale"]
                    else None
                ),
                "supabase": (
                    self.last_health_check["supabase"].isoformat()
                    if self.last_health_check["supabase"]
                    else None
                ),
            },
            "connection_failures": self.connection_failures.copy(),
            "check_interval": self.check_interval,
        }
