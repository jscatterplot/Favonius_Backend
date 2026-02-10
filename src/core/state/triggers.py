"""Re-optimization trigger monitoring.

Reference: Development plan Step 4.2, PRD.md#5-system-architecture
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable, Optional
from uuid import UUID

import asyncpg

if TYPE_CHECKING:
    from .assembler import StateAssembler

logger = logging.getLogger(__name__)


@dataclass
class TriggerConfig:
    """Configuration for re-optimization triggers.

    Attributes:
        soc_deviation_threshold: SoC deviation threshold (default: 0.05 = 5%)
        price_change_percent: Price change percentage threshold (default: 0.25 = 25%)
        price_change_absolute: Price change absolute threshold in $/MWh (default: 25.0)
        return_time_deviation_min: Return time deviation in minutes (default: 15.0)
        check_interval_sec: Monitoring check interval in seconds (default: 60.0)
        trigger_cooldown_minutes: Cooldown period after trigger in minutes (default: 5)
    """

    soc_deviation_threshold: float = 0.05  # 5%
    price_change_percent: float = 0.25  # 25%
    price_change_absolute: float = 25.0  # $25/MWh
    return_time_deviation_min: float = 15.0  # 15 minutes
    check_interval_sec: float = 60.0  # 1 minute
    trigger_cooldown_minutes: int = 5  # 5 minutes


class TriggerMonitor:
    """Monitors conditions that trigger re-optimization.

    Reference: PRD Section 5.1, Development plan Step 4.2

    Monitors:
    - Vehicle SoC deviations from expected
    - Electricity price changes
    - Vehicle return time delays

    Triggers re-optimization when thresholds are exceeded.

    Example:
        ```python
        from src.core.state.triggers import TriggerConfig, TriggerMonitor
        from src.core.state.assembler import StateAssembler

        # Initialize
        config = TriggerConfig(
            soc_deviation_threshold=0.05,  # 5%
            price_change_percent=0.25,     # 25%
            price_change_absolute=25.0,    # $25/MWh
        )

        async def on_trigger(reason: str):
            print(f"Re-optimization triggered: {reason}")
            # Run optimization...

        assembler = StateAssembler(pool, depot_id, depot_config)
        monitor = TriggerMonitor(config, on_trigger, assembler=assembler)

        # Update expected state after optimization
        monitor.update_expected_state(
            expected_socs={'bus_1': 0.60},
            expected_return_times={'bus_1': datetime.utcnow() + timedelta(hours=2)}
        )

        # Start monitoring (runs in background)
        await monitor.run()
        ```
    """

    def __init__(
        self,
        config: TriggerConfig,
        on_trigger: Callable[[str], None],  # async callback
        assembler: Optional['StateAssembler'] = None,
        pool: Optional[asyncpg.Pool] = None,
        depot_id: Optional[str] = None,
    ):
        """Initialize trigger monitor.

        Args:
            config: Trigger configuration
            on_trigger: Async callback function(reason: str) -> None
            assembler: Optional StateAssembler for fetching current state
            pool: Optional database pool (required if assembler not provided)
            depot_id: Optional depot ID (required if assembler not provided)

        Raises:
            ValueError: If neither assembler nor (pool + depot_id) provided
        """
        self.config = config
        self.on_trigger = on_trigger
        self.assembler = assembler
        self.pool = pool
        self.depot_id = depot_id
        self.last_prices: dict[datetime, float] = {}
        self.expected_socs: dict[str, float] = {}
        self.expected_return_times: dict[str, datetime] = {}
        self._running = False
        self._last_trigger_time: Optional[datetime] = None
        self._trigger_cooldown_sec: float = config.trigger_cooldown_minutes * 60.0
        self._last_scheduled_hour: Optional[int] = None  # Track last scheduled trigger hour
        self._last_vdv463_update_at: Optional[datetime] = None  # VDV 463 charging request change trigger

        # Validate that we have a way to fetch state
        if assembler is None and (pool is None or depot_id is None):
            raise ValueError(
                "Either assembler or (pool + depot_id) must be provided"
            )

        logger.info("Initialized TriggerMonitor")

    def update_expected_state(
        self,
        expected_socs: dict[str, float],
        expected_return_times: dict[str, datetime],
    ) -> None:
        """Update expected state from optimization results.

        Args:
            expected_socs: Expected SoC for each vehicle
            expected_return_times: Expected return time for each vehicle
        """
        self.expected_socs = expected_socs
        self.expected_return_times = expected_return_times
        logger.debug(
            f"Updated expected state: {len(expected_socs)} vehicles, "
            f"{len(expected_return_times)} return times"
        )

    def update_prices(self, prices: dict[datetime, float]) -> None:
        """Update price baseline.

        Args:
            prices: Dictionary mapping timestamp to price ($/kWh)
        """
        self.last_prices = prices
        logger.debug(f"Updated price baseline: {len(prices)} price points")

    async def check_soc_deviation(
        self, current_socs: dict[str, float]
    ) -> Optional[str]:
        """Check if any vehicle SoC deviates from expected.

        Args:
            current_socs: Current SoC for each vehicle

        Returns:
            Trigger reason string if deviation detected, None otherwise
        """
        for vid, current in current_socs.items():
            expected = self.expected_socs.get(vid)
            if expected is not None:
                deviation = abs(current - expected)
                if deviation > self.config.soc_deviation_threshold:
                    reason = (
                        f"SoC deviation: {vid} expected {expected:.2f}, "
                        f"got {current:.2f} (deviation: {deviation:.2%})"
                    )
                    logger.warning(reason)
                    return reason
        return None

    async def check_price_change(
        self, current_prices: dict[datetime, float]
    ) -> Optional[str]:
        """Check if prices changed significantly.

        Args:
            current_prices: Current prices by timestamp

        Returns:
            Trigger reason string if price change detected, None otherwise
        """
        for ts, current in current_prices.items():
            last = self.last_prices.get(ts)
            if last is not None and last > 0:
                pct_change = abs(current - last) / last
                # Convert $/kWh to $/MWh for absolute comparison
                abs_change = abs(current - last) * 1000.0

                # OR threshold (either condition triggers per PRD Section 5.3)
                if (
                    pct_change > self.config.price_change_percent
                    or abs_change > self.config.price_change_absolute
                ):
                    reason = (
                        f"Price change: {ts} was ${last:.4f}/kWh, "
                        f"now ${current:.4f}/kWh "
                        f"({pct_change:.1%} change, ${abs_change:.1f}/MWh)"
                    )
                    logger.warning(reason)
                    return reason
        return None

    async def check_return_time_deviation(
        self, actual_return_times: dict[str, datetime]
    ) -> Optional[str]:
        """Check if any vehicle returned significantly late.

        Args:
            actual_return_times: Actual return time for each vehicle

        Returns:
            Trigger reason string if delay detected, None otherwise
        """
        threshold = timedelta(minutes=self.config.return_time_deviation_min)

        for vid, actual in actual_return_times.items():
            expected = self.expected_return_times.get(vid)
            if expected is not None:
                deviation = actual - expected
                if deviation > threshold:
                    reason = (
                        f"Return delay: {vid} expected {expected}, "
                        f"returned {actual} (delay: {deviation})"
                    )
                    logger.warning(reason)
                    return reason
        return None

    def trigger_interdepot_handoff(
        self, message_id: UUID, vehicle_id: UUID
    ) -> str:
        """Create trigger for inter-depot handoff receipt.
        
        Per PRD Section 5.3, inter-depot handoff messages trigger
        re-optimization immediately (event-driven trigger).
        
        Args:
            message_id: Handoff message identifier
            vehicle_id: Vehicle identifier arriving from another depot
            
        Returns:
            Trigger reason string
        """
        reason = (
            f"interdepot_handoff: message_id={message_id}, "
            f"vehicle_id={vehicle_id}"
        )
        logger.info(f"Inter-depot handoff trigger: {reason}")
        return reason

    async def _get_current_vehicle_socs(self) -> dict[str, float]:
        """Get current vehicle SoCs from database.

        Returns:
            Dictionary mapping vehicle_id to current SoC

        Note:
            Returns empty dict on error to allow monitoring to continue.
        """
        if self.assembler:
            # Use assembler if available
            try:
                return await self.assembler._get_vehicle_socs()
            except Exception as e:
                logger.error(
                    f"Error fetching vehicle SoCs via assembler: {e}",
                    exc_info=True,
                )
                return {}

        # Otherwise query directly
        if not self.pool or not self.depot_id:
            logger.warning("Cannot fetch SoCs: missing pool or depot_id")
            return {}

        query = """
        SELECT DISTINCT ON (t.vehicle_id)
            t.vehicle_id::text AS vehicle_id,
            t.soc
        FROM telemetry t
        JOIN vehicles v ON t.vehicle_id = v.vehicle_id
        WHERE v.depot_id = $1
        ORDER BY t.vehicle_id, t.time DESC
        """
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, self.depot_id)
            return {str(row['vehicle_id']): float(row['soc']) for row in rows}
        except asyncio.TimeoutError as e:
            logger.warning(f"Timeout fetching vehicle SoCs: {e}")
            return {}
        except asyncpg.PostgresError as e:
            logger.error(f"Database error fetching vehicle SoCs: {e}")
            return {}
        except Exception as e:
            logger.error(f"Unexpected error fetching vehicle SoCs: {e}", exc_info=True)
            return {}

    async def _get_current_prices(self) -> dict[datetime, float]:
        """Get current prices from database.

        Returns:
            Dictionary mapping timestamp to price ($/kWh)

        Note:
            Returns empty dict on error to allow monitoring to continue.
        """
        if self.assembler:
            # Use assembler to get state
            try:
                state = await self.assembler.get_current_state(horizon_hours=24)
                # Convert price list to dict with timestamps
                now = datetime.utcnow()
                delta_t_hours = self.assembler.config.delta_t
                return {
                    now + timedelta(hours=i * delta_t_hours): price
                    for i, price in enumerate(state.prices[:24])
                }
            except Exception as e:
                logger.error(
                    f"Error fetching prices via assembler: {e}",
                    exc_info=True,
                )
                return {}

        # Otherwise query directly
        if not self.pool or not self.depot_id:
            logger.warning("Cannot fetch prices: missing pool or depot_id")
            return {}

        query = """
        SELECT time, energy_kwh as price_per_kwh
        FROM prices
        WHERE depot_id = $1 
          AND time >= NOW() 
          AND time < NOW() + INTERVAL '24 hours'
        ORDER BY time
        """
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, self.depot_id)
            return {row['time']: float(row['price_per_kwh']) for row in rows}
        except Exception as e:
            logger.error(f"Error fetching prices: {e}")
            return {}

    async def _get_actual_return_times(self) -> dict[str, datetime]:
        """Get actual return times for vehicles that have returned.

        Returns:
            Dictionary mapping vehicle_id to actual return time

        Note:
            Returns empty dict on error to allow monitoring to continue.
        """
        if not self.pool or not self.depot_id:
            logger.warning("Cannot fetch return times: missing pool or depot_id")
            return {}

        # Query schedules where return_time has passed recently
        query = """
        SELECT DISTINCT ON (s.vehicle_id)
            s.vehicle_id::text, s.return_time
        FROM schedules s
        JOIN vehicles v ON s.vehicle_id = v.vehicle_id
        WHERE v.depot_id = $1
          AND s.return_time <= NOW()
          AND s.return_time >= NOW() - INTERVAL '1 hour'
        ORDER BY s.vehicle_id, s.return_time DESC
        """
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, self.depot_id)
            return {str(row['vehicle_id']): row['return_time'] for row in rows}
        except asyncio.TimeoutError as e:
            logger.warning(f"Timeout fetching return times: {e}")
            return {}
        except asyncpg.PostgresError as e:
            logger.error(f"Database error fetching return times: {e}")
            return {}
        except Exception as e:
            logger.error(f"Unexpected error fetching return times: {e}", exc_info=True)
            return {}

    async def check_vdv463_charging_request_change(self) -> Optional[str]:
        """Check if VDV 463 charging requests were updated since last check.

        Reads vdv463_depot_updates.last_update_at for this depot; if it changed,
        returns 'vdv463_charging_request_change' to fire re-optimization.
        """
        if not self.pool or not self.depot_id:
            return None
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT last_update_at FROM vdv463_depot_updates
                    WHERE depot_id = $1::uuid
                    """,
                    self.depot_id,
                )
            if row is None:
                return None
            last_update = row["last_update_at"]
            if (
                self._last_vdv463_update_at is not None
                and last_update > self._last_vdv463_update_at
            ):
                self._last_vdv463_update_at = last_update
                return "vdv463_charging_request_change"
            self._last_vdv463_update_at = last_update
            return None
        except asyncpg.PostgresError as e:
            if "vdv463_depot_updates" in str(e) and "does not exist" in str(e).lower():
                return None
            logger.warning(f"Error checking VDV 463 update: {e}")
            return None
        except Exception as e:
            logger.warning(f"Unexpected error checking VDV 463 update: {e}")
            return None

    async def run(self) -> None:
        """Main monitoring loop.

        Continuously checks trigger conditions and invokes callback when
        thresholds are exceeded. Runs until stop() is called.
        """
        self._running = True
        logger.info("Starting trigger monitor")

        while self._running:
            try:
                now = datetime.utcnow()
                current_hour = now.hour
                
                # Scheduled trigger: hourly 24/7 (once per hour)
                scheduled_trigger = None
                if self._last_scheduled_hour is None or self._last_scheduled_hour != current_hour:
                    scheduled_trigger = "scheduled"
                    self._last_scheduled_hour = current_hour
                    logger.info(f"Scheduled trigger fired for hour {current_hour}")
                
                # Fetch current state
                current_socs = await self._get_current_vehicle_socs()
                current_prices = await self._get_current_prices()
                actual_returns = await self._get_actual_return_times()

                # Check all triggers
                soc_trigger = await self.check_soc_deviation(current_socs)
                price_trigger = await self.check_price_change(current_prices)
                return_trigger = await self.check_return_time_deviation(
                    actual_returns
                )
                vdv463_trigger = await self.check_vdv463_charging_request_change()

                # Fire callback if any trigger detected (with cooldown)
                # Scheduled triggers bypass cooldown (they're already rate-limited to once per hour)
                if scheduled_trigger:
                    await self.on_trigger(scheduled_trigger)
                    self._last_trigger_time = now
                elif soc_trigger or price_trigger or return_trigger or vdv463_trigger:
                    reason = (
                        soc_trigger
                        or price_trigger
                        or return_trigger
                        or vdv463_trigger
                    )

                    # Check cooldown to prevent rapid-fire triggers
                    if (
                        self._last_trigger_time is None
                        or (now - self._last_trigger_time).total_seconds()
                        >= self._trigger_cooldown_sec
                    ):
                        logger.info(
                            f"Trigger fired: {reason}",
                            extra={
                                'trigger_type': (
                                    'soc_deviation'
                                    if soc_trigger
                                    else 'price_change'
                                    if price_trigger
                                    else 'return_delay'
                                    if return_trigger
                                    else 'vdv463_charging_request_change'
                                ),
                                'depot_id': self.depot_id,
                            },
                        )
                        await self.on_trigger(reason)
                        self._last_trigger_time = now
                    else:
                        time_since_last = (
                            now - self._last_trigger_time
                        ).total_seconds()
                        logger.debug(
                            f"Trigger suppressed (cooldown): {reason} "
                            f"(last trigger {time_since_last:.0f}s ago, "
                            f"cooldown: {self._trigger_cooldown_sec}s)"
                        )

                await asyncio.sleep(self.config.check_interval_sec)

            except Exception as e:
                logger.error(f"Trigger monitor error: {e}", exc_info=True)
                # Continue monitoring even if one check fails
                await asyncio.sleep(self.config.check_interval_sec)

        logger.info("Trigger monitor stopped")

    def stop(self) -> None:
        """Stop monitoring."""
        self._running = False
        logger.info("Stopping trigger monitor")

