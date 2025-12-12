"""Configuration for depot controller.

Reference: Development plan Step 5.2
"""

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class ControllerConfig:
    """Configuration for depot controller.

    Attributes:
        optimization_horizon_hours: Default optimization horizon in hours (default: 24)
        hourly_optimization_start: Start hour for hourly optimizations (default: 7)
        hourly_optimization_end: End hour for hourly optimizations (default: 23)
        optimization_timeout: Solver timeout in seconds (default: 30.0)
        trigger_cooldown_minutes: Cooldown period after trigger in minutes (default: 5)
        max_optimization_failures: Max failures before circuit break (default: 3)
        dispatch_retry_attempts: OCPP dispatch retry attempts (default: 3)
        dispatch_retry_delay_seconds: Delay between dispatch retries (default: 2.0)
        shutdown_timeout_seconds: Timeout for graceful shutdown (default: 30.0)
    """

    optimization_horizon_hours: int = 24
    hourly_optimization_start: int = 7
    hourly_optimization_end: int = 23
    optimization_timeout: float = 30.0
    trigger_cooldown_minutes: int = 5
    max_optimization_failures: int = 3
    dispatch_retry_attempts: int = 3
    dispatch_retry_delay_seconds: float = 2.0
    shutdown_timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> 'ControllerConfig':
        """Load configuration from environment variables.

        Environment variables:
            FAVONIUS_OPTIMIZATION_HORIZON_HOURS: Optimization horizon (default: 24)
            FAVONIUS_HOURLY_OPT_START: Hourly optimization start hour (default: 7)
            FAVONIUS_HOURLY_OPT_END: Hourly optimization end hour (default: 23)
            FAVONIUS_OPTIMIZATION_TIMEOUT: Solver timeout in seconds (default: 30.0)
            FAVONIUS_TRIGGER_COOLDOWN_MIN: Trigger cooldown in minutes (default: 5)
            FAVONIUS_MAX_OPT_FAILURES: Max optimization failures (default: 3)
            FAVONIUS_DISPATCH_RETRIES: Dispatch retry attempts (default: 3)
            FAVONIUS_DISPATCH_RETRY_DELAY: Dispatch retry delay in seconds (default: 2.0)
            FAVONIUS_SHUTDOWN_TIMEOUT: Shutdown timeout in seconds (default: 30.0)

        Returns:
            ControllerConfig instance with values from environment or defaults
        """
        return cls(
            optimization_horizon_hours=int(
                os.getenv('FAVONIUS_OPTIMIZATION_HORIZON_HOURS', '24')
            ),
            hourly_optimization_start=int(
                os.getenv('FAVONIUS_HOURLY_OPT_START', '7')
            ),
            hourly_optimization_end=int(
                os.getenv('FAVONIUS_HOURLY_OPT_END', '23')
            ),
            optimization_timeout=float(
                os.getenv('FAVONIUS_OPTIMIZATION_TIMEOUT', '30.0')
            ),
            trigger_cooldown_minutes=int(
                os.getenv('FAVONIUS_TRIGGER_COOLDOWN_MIN', '5')
            ),
            max_optimization_failures=int(
                os.getenv('FAVONIUS_MAX_OPT_FAILURES', '3')
            ),
            dispatch_retry_attempts=int(
                os.getenv('FAVONIUS_DISPATCH_RETRIES', '3')
            ),
            dispatch_retry_delay_seconds=float(
                os.getenv('FAVONIUS_DISPATCH_RETRY_DELAY', '2.0')
            ),
            shutdown_timeout_seconds=float(
                os.getenv('FAVONIUS_SHUTDOWN_TIMEOUT', '30.0')
            ),
        )

    def validate(self) -> None:
        """Validate configuration values.

        Raises:
            ValueError: If any configuration value is invalid
        """
        if not (1 <= self.optimization_horizon_hours <= 48):
            raise ValueError(
                f"optimization_horizon_hours must be between 1 and 48, "
                f"got: {self.optimization_horizon_hours}"
            )
        if not (0 <= self.hourly_optimization_start <= 23):
            raise ValueError(
                f"hourly_optimization_start must be between 0 and 23, "
                f"got: {self.hourly_optimization_start}"
            )
        if not (0 <= self.hourly_optimization_end <= 23):
            raise ValueError(
                f"hourly_optimization_end must be between 0 and 23, "
                f"got: {self.hourly_optimization_end}"
            )
        if self.hourly_optimization_start >= self.hourly_optimization_end:
            raise ValueError(
                f"hourly_optimization_start ({self.hourly_optimization_start}) "
                f"must be less than hourly_optimization_end "
                f"({self.hourly_optimization_end})"
            )
        if self.optimization_timeout <= 0:
            raise ValueError(
                f"optimization_timeout must be positive, got: {self.optimization_timeout}"
            )
        if self.trigger_cooldown_minutes < 0:
            raise ValueError(
                f"trigger_cooldown_minutes must be non-negative, "
                f"got: {self.trigger_cooldown_minutes}"
            )
        if self.max_optimization_failures < 1:
            raise ValueError(
                f"max_optimization_failures must be at least 1, "
                f"got: {self.max_optimization_failures}"
            )
        if self.dispatch_retry_attempts < 0:
            raise ValueError(
                f"dispatch_retry_attempts must be non-negative, "
                f"got: {self.dispatch_retry_attempts}"
            )

