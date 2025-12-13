"""Rate limiting for API endpoints.

See PRD_v2.md Section 10.4 for rate limit specifications.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID
import logging

logger = logging.getLogger(__name__)


@dataclass
class RateLimitConfig:
    """Rate limit configuration per PRD Section 10.4."""
    # General API endpoints
    api_requests_per_minute: int = 100

    # POST /optimize endpoint (more expensive)
    optimize_requests_per_minute: int = 10

    # Trigger-induced optimizations (per depot)
    trigger_optimization_cooldown_seconds: int = 300  # 5 minutes


@dataclass
class RateLimiter:
    """Token bucket rate limiter."""
    config: RateLimitConfig = field(default_factory=RateLimitConfig)
    _api_buckets: dict = field(default_factory=lambda: defaultdict(list))
    _optimize_buckets: dict = field(default_factory=lambda: defaultdict(list))
    _last_trigger_optimization: dict = field(default_factory=dict)

    def _clean_bucket(self, bucket: list, window_seconds: int) -> list:
        """Remove entries older than window."""
        cutoff = time.time() - window_seconds
        return [t for t in bucket if t > cutoff]

    def check_api_limit(self, client_id: str) -> bool:
        """Check if client is within API rate limit.

        Args:
            client_id: Client identifier (IP or API key)

        Returns:
            True if request allowed, False if rate limited
        """
        bucket = self._clean_bucket(self._api_buckets[client_id], 60)
        self._api_buckets[client_id] = bucket

        if len(bucket) >= self.config.api_requests_per_minute:
            logger.warning(f"Rate limit exceeded for client {client_id}")
            return False

        self._api_buckets[client_id].append(time.time())
        return True

    def check_optimize_limit(self, client_id: str) -> bool:
        """Check if client is within optimization rate limit.

        POST /optimize is expensive, so stricter limits apply.
        """
        bucket = self._clean_bucket(self._optimize_buckets[client_id], 60)
        self._optimize_buckets[client_id] = bucket

        if len(bucket) >= self.config.optimize_requests_per_minute:
            logger.warning(f"Optimize rate limit exceeded for client {client_id}")
            return False

        self._optimize_buckets[client_id].append(time.time())
        return True

    def check_trigger_cooldown(self, depot_id: UUID) -> bool:
        """Check if depot is within trigger optimization cooldown.

        Prevents rapid re-optimization from trigger events.

        Returns:
            True if optimization allowed, False if in cooldown
        """
        last_time = self._last_trigger_optimization.get(depot_id)
        if last_time is None:
            return True

        elapsed = time.time() - last_time
        if elapsed < self.config.trigger_optimization_cooldown_seconds:
            logger.info(
                f"Depot {depot_id} in trigger cooldown, "
                f"{self.config.trigger_optimization_cooldown_seconds - elapsed:.0f}s remaining"
            )
            return False

        return True

    def record_trigger_optimization(self, depot_id: UUID) -> None:
        """Record that a trigger-induced optimization occurred."""
        self._last_trigger_optimization[depot_id] = time.time()


# Global rate limiter instance
rate_limiter = RateLimiter()

