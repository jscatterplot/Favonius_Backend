"""Data freshness validation.

See PRD_v2.md Section 10.4 for staleness thresholds.
See PRD_v2.md Section 5.3 for data freshness requirements.

Reference: Development Plan Step 4.5.8
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger(__name__)


# Maximum age for data to be considered fresh (per PRD Section 5.3)
MAX_TELEMETRY_AGE = timedelta(minutes=15)
MAX_PRICE_AGE = timedelta(hours=24)
MAX_WEATHER_AGE = timedelta(hours=6)
MAX_BUILDING_LOAD_AGE = timedelta(minutes=30)
MAX_SCHEDULE_AGE = timedelta(hours=1)


@dataclass
class FreshnessStatus:
    """Status of data freshness checks."""
    telemetry_fresh: bool
    price_fresh: bool
    weather_fresh: bool
    building_load_fresh: bool
    schedule_fresh: bool
    all_fresh: bool
    warnings: list[str]


def check_data_freshness(
    telemetry_time: Optional[datetime] = None,
    price_time: Optional[datetime] = None,
    weather_time: Optional[datetime] = None,
    building_load_time: Optional[datetime] = None,
    schedule_time: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> FreshnessStatus:
    """Check if input data is fresh enough for optimization.

    Per PRD Section 5.3, optimization should use recent data.
    Stale data generates warnings but doesn't block optimization.

    Args:
        telemetry_time: Most recent telemetry timestamp
        price_time: Most recent price timestamp
        weather_time: Most recent weather timestamp
        building_load_time: Most recent building load timestamp
        schedule_time: Most recent schedule timestamp
        now: Current time (defaults to utcnow)

    Returns:
        FreshnessStatus with freshness status for each data type
    """
    if now is None:
        now = datetime.utcnow()

    warnings = []

    # Check telemetry freshness
    if telemetry_time:
        age = now - telemetry_time
        telemetry_fresh = age <= MAX_TELEMETRY_AGE
        if not telemetry_fresh:
            warnings.append(
                f"Telemetry data stale: {age.total_seconds() / 60:.1f} min old "
                f"(max: {MAX_TELEMETRY_AGE.total_seconds() / 60:.0f} min)"
            )
            logger.warning(warnings[-1])
    else:
        telemetry_fresh = False
        warnings.append("No telemetry data available")
        logger.warning(warnings[-1])

    # Check price freshness
    if price_time:
        age = now - price_time
        price_fresh = age <= MAX_PRICE_AGE
        if not price_fresh:
            warnings.append(
                f"Price data stale: {age.total_seconds() / 3600:.1f} hours old "
                f"(max: {MAX_PRICE_AGE.total_seconds() / 3600:.0f} hours)"
            )
            logger.warning(warnings[-1])
    else:
        price_fresh = False
        warnings.append("No price data available")
        logger.warning(warnings[-1])

    # Check weather freshness
    if weather_time:
        age = now - weather_time
        weather_fresh = age <= MAX_WEATHER_AGE
        if not weather_fresh:
            warnings.append(
                f"Weather data stale: {age.total_seconds() / 3600:.1f} hours old "
                f"(max: {MAX_WEATHER_AGE.total_seconds() / 3600:.0f} hours)"
            )
            logger.warning(warnings[-1])
    else:
        weather_fresh = False
        # Weather is optional, log at debug level
        logger.debug("No weather data available")

    # Check building load freshness
    if building_load_time:
        age = now - building_load_time
        building_load_fresh = age <= MAX_BUILDING_LOAD_AGE
        if not building_load_fresh:
            warnings.append(
                f"Building load data stale: {age.total_seconds() / 60:.1f} min old "
                f"(max: {MAX_BUILDING_LOAD_AGE.total_seconds() / 60:.0f} min)"
            )
            logger.warning(warnings[-1])
    else:
        building_load_fresh = False
        # Building load is optional, log at debug level
        logger.debug("No building load data available")

    # Check schedule freshness
    if schedule_time:
        age = now - schedule_time
        schedule_fresh = age <= MAX_SCHEDULE_AGE
        if not schedule_fresh:
            warnings.append(
                f"Schedule data stale: {age.total_seconds() / 60:.1f} min old "
                f"(max: {MAX_SCHEDULE_AGE.total_seconds() / 60:.0f} min)"
            )
            logger.warning(warnings[-1])
    else:
        schedule_fresh = False
        warnings.append("No schedule data available")
        logger.warning(warnings[-1])

    # All data is fresh if critical data is fresh
    # (telemetry, price, schedule are critical; weather, building_load are optional)
    all_fresh = telemetry_fresh and price_fresh and schedule_fresh

    return FreshnessStatus(
        telemetry_fresh=telemetry_fresh,
        price_fresh=price_fresh,
        weather_fresh=weather_fresh,
        building_load_fresh=building_load_fresh,
        schedule_fresh=schedule_fresh,
        all_fresh=all_fresh,
        warnings=warnings,
    )


def check_optimization_prerequisites(
    telemetry_time: Optional[datetime],
    price_time: Optional[datetime],
    schedule_time: Optional[datetime],
    now: Optional[datetime] = None,
) -> tuple[bool, list[str]]:
    """Check minimum prerequisites for running optimization.

    Optimization requires at minimum:
    - Recent telemetry (for current SoC)
    - Price data (can be slightly stale)
    - Schedule data (for departure times)

    Args:
        telemetry_time: Most recent telemetry timestamp
        price_time: Most recent price timestamp
        schedule_time: Most recent schedule timestamp
        now: Current time (defaults to utcnow)

    Returns:
        Tuple of (can_optimize, list of error messages)
    """
    if now is None:
        now = datetime.utcnow()

    errors = []

    # Telemetry is critical for SoC
    if telemetry_time is None:
        errors.append("No telemetry data available - cannot determine vehicle SoC")
    elif now - telemetry_time > MAX_TELEMETRY_AGE * 2:
        errors.append(
            f"Telemetry too stale: {(now - telemetry_time).total_seconds() / 60:.1f} min old"
        )

    # Price is needed for optimization objective
    if price_time is None:
        errors.append("No price data available - cannot optimize for cost")
    elif now - price_time > MAX_PRICE_AGE * 2:
        errors.append(
            f"Price data too stale: {(now - price_time).total_seconds() / 3600:.1f} hours old"
        )

    # Schedule is needed for departure constraints
    if schedule_time is None:
        errors.append("No schedule data available - cannot determine departure times")

    can_optimize = len(errors) == 0

    if not can_optimize:
        logger.error(
            f"Optimization prerequisites not met: {', '.join(errors)}"
        )

    return can_optimize, errors


class DataFreshnessMonitor:
    """Monitor data freshness and alert on staleness.

    Use this class to track data freshness over time and
    generate alerts when data becomes stale.
    """

    def __init__(
        self,
        alert_callback: Optional[callable] = None,
    ):
        """Initialize freshness monitor.

        Args:
            alert_callback: Optional callback for freshness alerts
        """
        self.alert_callback = alert_callback
        self._last_telemetry_time: Optional[datetime] = None
        self._last_price_time: Optional[datetime] = None
        self._last_weather_time: Optional[datetime] = None
        self._last_building_load_time: Optional[datetime] = None
        self._last_schedule_time: Optional[datetime] = None
        self._alert_sent: dict[str, datetime] = {}

    def update_telemetry_time(self, timestamp: datetime) -> None:
        """Update last telemetry timestamp."""
        self._last_telemetry_time = timestamp

    def update_price_time(self, timestamp: datetime) -> None:
        """Update last price timestamp."""
        self._last_price_time = timestamp

    def update_weather_time(self, timestamp: datetime) -> None:
        """Update last weather timestamp."""
        self._last_weather_time = timestamp

    def update_building_load_time(self, timestamp: datetime) -> None:
        """Update last building load timestamp."""
        self._last_building_load_time = timestamp

    def update_schedule_time(self, timestamp: datetime) -> None:
        """Update last schedule timestamp."""
        self._last_schedule_time = timestamp

    def check_and_alert(self) -> FreshnessStatus:
        """Check freshness and send alerts for stale data.

        Returns:
            Current freshness status
        """
        status = check_data_freshness(
            telemetry_time=self._last_telemetry_time,
            price_time=self._last_price_time,
            weather_time=self._last_weather_time,
            building_load_time=self._last_building_load_time,
            schedule_time=self._last_schedule_time,
        )

        if self.alert_callback and status.warnings:
            now = datetime.utcnow()
            cooldown = timedelta(minutes=15)  # Don't spam alerts

            for warning in status.warnings:
                # Extract data type from warning
                data_type = warning.split()[0].lower()

                last_alert = self._alert_sent.get(data_type)
                if last_alert is None or now - last_alert > cooldown:
                    self.alert_callback(warning)
                    self._alert_sent[data_type] = now

        return status
