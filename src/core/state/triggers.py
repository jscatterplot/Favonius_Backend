"""Re-optimization trigger monitoring.

Reference: Development plan Step 4.2, PRD.md#5-system-architecture
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

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
    """

    soc_deviation_threshold: float = 0.05  # 5%
    price_change_percent: float = 0.25  # 25%
    price_change_absolute: float = 25.0  # $25/MWh
    return_time_deviation_min: float = 15.0  # 15 minutes
    check_interval_sec: float = 60.0  # 1 minute


class TriggerMonitor:
    """Monitors conditions that trigger re-optimization.

    Reference: PRD Section 5.1, Development plan Step 4.2

    Monitors:
    - Vehicle SoC deviations from expected
    - Electricity price changes
    - Vehicle return time delays

    Triggers re-optimization when thresholds are exceeded.
    """

    def __init__(
        self,
        config: TriggerConfig,
        on_trigger: Callable[[str], None],  # async callback
    ):
        """Initialize trigger monitor.

        Args:
            config: Trigger configuration
            on_trigger: Async callback function(reason: str) -> None
        """
        self.config = config
        self.on_trigger = on_trigger
        self.last_prices: dict[datetime, float] = {}
        self.expected_socs: dict[str, float] = {}
        self.expected_return_times: dict[str, datetime] = {}
        self._running = False
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

                # Combined threshold (both must be met per PRD Section 4.2)
                if (
                    pct_change > self.config.price_change_percent
                    and abs_change > self.config.price_change_absolute
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

    async def run(self) -> None:
        """Main monitoring loop.

        Continuously checks trigger conditions and invokes callback when
        thresholds are exceeded. Runs until stop() is called.
        """
        self._running = True
        logger.info("Starting trigger monitor")

        while self._running:
            try:
                # Note: In production, these would be injected via methods
                # that fetch current state from database/telemetry
                # For now, the controller will call check methods directly
                # with current state data

                await asyncio.sleep(self.config.check_interval_sec)

            except Exception as e:
                logger.error(f"Trigger monitor error: {e}", exc_info=True)

        logger.info("Trigger monitor stopped")

    def stop(self) -> None:
        """Stop monitoring."""
        self._running = False
        logger.info("Stopping trigger monitor")

