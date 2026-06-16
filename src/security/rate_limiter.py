"""Hybrid in-memory + PostgreSQL rate limiting for API endpoints.

See PRD_v2.md Section 10.4 for rate limit specifications.

Architecture:
- In-memory sliding window for zero-latency hot path (same as before)
- Background sync to PostgreSQL every 15 seconds for:
  - Persistence across server restarts (cold-start hydration)
  - Cross-instance coordination (distributed rate limiting on Railway)
- Follows the same async batch pattern as AuditLogger (src/security/audit_log.py)

DB sync strategy:
- Flush: UPSERT with GREATEST(existing, local) — conservative merge
- Merge: read remote counts, take max of local vs remote
- Hydrate: on cold start, populate in-memory from DB
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)


@dataclass
class RateLimitConfig:
    """Rate limit configuration per PRD Section 10.4."""

    # General API endpoints
    api_requests_per_minute: int = 100

    # Admin write endpoints used by the bulk-import flows on the fleet
    # identity panel (POST/PATCH /admin/depots/{id}/{vehicles|drivers|rfid-cards}).
    # Sized for sequential xlsx imports — a 200-row file completes in ~10s at
    # the FE's serial cadence and a 5,000-row file in ~5 minutes — without
    # weakening the general 100/min limit on read-heavy admin paths. Reusable
    # for future bulk-import endpoints (drivers, chargers); extend the path
    # match in RateLimitMiddleware accordingly.
    admin_write_requests_per_minute: int = 1200

    # POST /optimize endpoint (more expensive)
    optimize_requests_per_minute: int = 10

    # /agent/* LLM endpoints (same cadence as optimize; separate bucket)
    agent_requests_per_minute: int = 10

    # Inter-depot handoff: 50 messages/hour per depot pair
    handoff_messages_per_hour: int = 50

    # Trigger-induced optimizations (per depot)
    trigger_optimization_cooldown_seconds: int = 300  # 5 minutes

    # DB sync interval (seconds)
    sync_interval_seconds: float = 15.0
    # How often to merge remote counts into local buckets (seconds).
    merge_interval_seconds: float = 60.0
    # How often to delete expired rows from rate_limit_state (seconds).
    cleanup_interval_seconds: float = 300.0


@dataclass
class RateLimitResult:
    """Result of a rate limit check.

    Attributes:
        allowed: Whether the request is allowed.
        limit: Maximum requests allowed in the window (X-RateLimit-Limit).
        remaining: Requests remaining in the window (X-RateLimit-Remaining).
        reset_at: Unix timestamp when the window resets (X-RateLimit-Reset).
    """

    allowed: bool
    limit: int = 0
    remaining: int = 0
    reset_at: float = 0.0

    def __bool__(self) -> bool:
        """Allow using RateLimitResult in boolean context.

        Enables backward compatibility with existing code:
            if not rate_limiter.check_api_limit(client_id): ...
        """
        return self.allowed


class RateLimiter:
    """Hybrid in-memory + PostgreSQL rate limiter.

    In-memory sliding window for the hot path, with optional background
    sync to PostgreSQL for persistence and cross-instance coordination.

    Usage:
        # Without DB (pure in-memory, same as before):
        limiter = RateLimiter()

        # With DB (hybrid):
        limiter = RateLimiter(db_pool=pool)
        await limiter.start()
        # ... on shutdown:
        await limiter.stop()
    """

    def __init__(
        self,
        config: Optional[RateLimitConfig] = None,
        db_pool: Any = None,
    ) -> None:
        self.config = config or RateLimitConfig()
        self._pool = db_pool

        # In-memory sliding window buckets (timestamp lists)
        self._api_buckets: dict[str, list[float]] = defaultdict(list)
        self._admin_write_buckets: dict[str, list[float]] = defaultdict(list)
        self._optimize_buckets: dict[str, list[float]] = defaultdict(list)
        self._agent_buckets: dict[str, list[float]] = defaultdict(list)
        self._handoff_buckets: dict[tuple, list[float]] = defaultdict(list)
        self._last_trigger_optimization: dict[UUID, float] = {}

        # Dirty keys pending flush: (bucket_type, bucket_key)
        self._dirty_keys: set[tuple[str, str]] = set()
        self._sync_loop_ticks: int = 0

        # Background sync task
        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background sync loop and hydrate from DB."""
        if self._running:
            return
        if self._pool is not None:
            await self._hydrate_from_db()
        self._running = True
        self._task = asyncio.create_task(self._sync_loop())
        logger.info("Rate limiter started (db_backed=%s)", self._pool is not None)

    async def stop(self) -> None:
        """Stop the sync loop and do a final flush."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Final flush
        if self._pool is not None:
            await self._flush_to_db()
        logger.info("Rate limiter stopped")

    def reset_in_memory_buckets_for_tests(self) -> None:
        """Clear sliding-window state (unit tests only; avoids cross-test 429s)."""
        self._api_buckets.clear()
        self._admin_write_buckets.clear()
        self._optimize_buckets.clear()
        self._agent_buckets.clear()
        self._handoff_buckets.clear()
        self._last_trigger_optimization.clear()
        self._dirty_keys.clear()
        self._sync_loop_ticks = 0

    def _mark_dirty(self, bucket_type: str, bucket_key: str) -> None:
        """Record that a bucket changed since the last successful DB flush."""
        self._dirty_keys.add((bucket_type, bucket_key))

    # ── Rate Limit Checks (hot path, in-memory only) ─────────────────────

    def _clean_bucket(self, bucket: list[float], window_seconds: int) -> list[float]:
        """Remove entries older than window."""
        cutoff = time.time() - window_seconds
        return [t for t in bucket if t > cutoff]

    def _make_result(
        self, allowed: bool, bucket: list[float], limit: int, window_seconds: int
    ) -> RateLimitResult:
        """Build a RateLimitResult with header values."""
        remaining = max(0, limit - len(bucket))
        if bucket:
            reset_at = bucket[0] + window_seconds
        else:
            reset_at = time.time() + window_seconds
        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=remaining,
            reset_at=reset_at,
        )

    def check_api_limit(self, client_id: str) -> RateLimitResult:
        """Check if client is within API rate limit.

        Args:
            client_id: Client identifier (IP or API key)

        Returns:
            RateLimitResult with allowed status and header values.
        """
        limit = self.config.api_requests_per_minute
        bucket = self._clean_bucket(self._api_buckets[client_id], 60)
        self._api_buckets[client_id] = bucket

        if len(bucket) >= limit:
            logger.warning("Rate limit exceeded for client %s", client_id)
            return self._make_result(False, bucket, limit, 60)

        bucket.append(time.time())
        self._mark_dirty("api", client_id)
        return self._make_result(True, bucket, limit, 60)

    def check_admin_write_limit(self, client_id: str) -> RateLimitResult:
        """Check if client is within the admin-write rate limit.

        Used by the bulk-import flows that issue sequential POST/PATCH calls
        against the fleet identity endpoints (vehicles, drivers, RFID cards).
        This bucket is separate from the general API limit so a 200-row xlsx
        import does not trip the 100/min ceiling shared with read-heavy admin
        traffic.
        """
        limit = self.config.admin_write_requests_per_minute
        bucket = self._clean_bucket(self._admin_write_buckets[client_id], 60)
        self._admin_write_buckets[client_id] = bucket

        if len(bucket) >= limit:
            logger.warning("Admin write rate limit exceeded for client %s", client_id)
            return self._make_result(False, bucket, limit, 60)

        bucket.append(time.time())
        self._mark_dirty("admin_write", client_id)
        return self._make_result(True, bucket, limit, 60)

    def check_optimize_limit(self, client_id: str) -> RateLimitResult:
        """Check if client is within optimization rate limit.

        POST /optimize is expensive, so stricter limits apply.
        """
        limit = self.config.optimize_requests_per_minute
        bucket = self._clean_bucket(self._optimize_buckets[client_id], 60)
        self._optimize_buckets[client_id] = bucket

        if len(bucket) >= limit:
            logger.warning("Optimize rate limit exceeded for client %s", client_id)
            return self._make_result(False, bucket, limit, 60)

        bucket.append(time.time())
        self._mark_dirty("optimize", client_id)
        return self._make_result(True, bucket, limit, 60)

    def check_agent_limit(self, client_id: str) -> RateLimitResult:
        """Check if client is within depot-chat agent rate limit.

        Same window/limit shape as :meth:`check_optimize_limit` but uses a
        dedicated bucket so /agent/* traffic does not compete with POST
        /optimize.
        """
        limit = self.config.agent_requests_per_minute
        bucket = self._clean_bucket(self._agent_buckets[client_id], 60)
        self._agent_buckets[client_id] = bucket

        if len(bucket) >= limit:
            logger.warning("Agent rate limit exceeded for client %s", client_id)
            return self._make_result(False, bucket, limit, 60)

        bucket.append(time.time())
        self._mark_dirty("agent", client_id)
        return self._make_result(True, bucket, limit, 60)

    def check_trigger_cooldown(self, depot_id: UUID) -> bool:
        """Check if depot is within trigger optimization cooldown.

        Returns:
            True if optimization allowed, False if in cooldown.
        """
        last_time = self._last_trigger_optimization.get(depot_id)
        if last_time is None:
            return True

        elapsed = time.time() - last_time
        if elapsed < self.config.trigger_optimization_cooldown_seconds:
            logger.info(
                "Depot %s in trigger cooldown, %.0fs remaining",
                depot_id,
                self.config.trigger_optimization_cooldown_seconds - elapsed,
            )
            return False

        return True

    def record_trigger_optimization(self, depot_id: UUID) -> None:
        """Record that a trigger-induced optimization occurred."""
        self._last_trigger_optimization[depot_id] = time.time()

    def check_handoff_limit(self, origin_depot_id: str, dest_depot_id: str) -> RateLimitResult:
        """Check if handoff rate limit is within bounds.

        Per PRD Section 10.4: 50 messages/hour per depot pair.

        Args:
            origin_depot_id: Origin depot identifier
            dest_depot_id: Destination depot identifier

        Returns:
            RateLimitResult with allowed status and header values.
        """
        limit = self.config.handoff_messages_per_hour
        depot_pair = tuple(sorted([origin_depot_id, dest_depot_id]))
        bucket = self._clean_bucket(self._handoff_buckets[depot_pair], 3600)
        self._handoff_buckets[depot_pair] = bucket

        if len(bucket) >= limit:
            logger.warning(
                "Handoff rate limit exceeded for depot pair %s <-> %s",
                origin_depot_id,
                dest_depot_id,
            )
            return self._make_result(False, bucket, limit, 3600)

        bucket.append(time.time())
        key = f"{depot_pair[0]}:{depot_pair[1]}"
        self._mark_dirty("handoff", key)
        return self._make_result(True, bucket, limit, 3600)

    # ── Background Sync ──────────────────────────────────────────────────

    async def _sync_loop(self) -> None:
        """Background loop that syncs state with PostgreSQL."""
        while self._running:
            try:
                await asyncio.sleep(self.config.sync_interval_seconds)
                if self._pool is not None:
                    self._sync_loop_ticks += 1
                    if self._dirty_keys:
                        await self._flush_to_db()
                    merge_every = max(
                        1,
                        int(
                            round(
                                self.config.merge_interval_seconds
                                / self.config.sync_interval_seconds
                            )
                        ),
                    )
                    cleanup_every = max(
                        1,
                        int(
                            round(
                                self.config.cleanup_interval_seconds
                                / self.config.sync_interval_seconds
                            )
                        ),
                    )
                    if self._sync_loop_ticks % merge_every == 0:
                        await self._merge_from_db()
                    if self._sync_loop_ticks % cleanup_every == 0:
                        await self._cleanup_expired_rows()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Rate limiter sync error: %s", e, exc_info=True)

    def _collect_flush_rows(self, now: float) -> list[tuple]:
        """Build UPSERT rows for dirty buckets only."""
        rows: list[tuple] = []

        for client_id, timestamps in list(self._api_buckets.items()):
            if ("api", client_id) not in self._dirty_keys:
                continue
            clean = [t for t in timestamps if t > now - 60]
            if clean:
                window_start = datetime.fromtimestamp(
                    math.floor(clean[0] / 60) * 60, tz=timezone.utc
                )
                rows.append(("api", client_id, window_start, len(clean)))

        for client_id, timestamps in list(self._admin_write_buckets.items()):
            if ("admin_write", client_id) not in self._dirty_keys:
                continue
            clean = [t for t in timestamps if t > now - 60]
            if clean:
                window_start = datetime.fromtimestamp(
                    math.floor(clean[0] / 60) * 60, tz=timezone.utc
                )
                rows.append(("admin_write", client_id, window_start, len(clean)))

        for client_id, timestamps in list(self._optimize_buckets.items()):
            if ("optimize", client_id) not in self._dirty_keys:
                continue
            clean = [t for t in timestamps if t > now - 60]
            if clean:
                window_start = datetime.fromtimestamp(
                    math.floor(clean[0] / 60) * 60, tz=timezone.utc
                )
                rows.append(("optimize", client_id, window_start, len(clean)))

        for client_id, timestamps in list(self._agent_buckets.items()):
            if ("agent", client_id) not in self._dirty_keys:
                continue
            clean = [t for t in timestamps if t > now - 60]
            if clean:
                window_start = datetime.fromtimestamp(
                    math.floor(clean[0] / 60) * 60, tz=timezone.utc
                )
                rows.append(("agent", client_id, window_start, len(clean)))

        for depot_pair, timestamps in list(self._handoff_buckets.items()):
            key = f"{depot_pair[0]}:{depot_pair[1]}"
            if ("handoff", key) not in self._dirty_keys:
                continue
            clean = [t for t in timestamps if t > now - 3600]
            if clean:
                window_start = datetime.fromtimestamp(
                    math.floor(clean[0] / 3600) * 3600, tz=timezone.utc
                )
                rows.append(("handoff", key, window_start, len(clean)))

        rows.sort(key=lambda row: (row[0], row[1], row[2]))
        return rows

    async def _flush_to_db(self) -> None:
        """Write local bucket counts to the database.

        Uses UPSERT with GREATEST to conservatively merge counts
        across instances (may over-count, never under-count).
        """
        if self._pool is None:
            return

        rows = self._collect_flush_rows(time.time())
        if not rows:
            self._dirty_keys.clear()
            return

        sql = """
                    INSERT INTO rate_limit_state
                        (bucket_type, bucket_key, window_start, request_count, last_updated)
                    VALUES ($1, $2, $3, $4, NOW())
                    ON CONFLICT (bucket_type, bucket_key, window_start)
                    DO UPDATE SET
                        request_count = GREATEST(rate_limit_state.request_count, $4),
                        last_updated = NOW()
                    """
        try:
            async with self._pool.acquire() as conn:
                await conn.executemany(sql, rows)
                logger.debug("Flushed %d rate limit buckets to DB", len(rows))
            self._dirty_keys.clear()
        except asyncpg.DeadlockDetectedError:
            logger.warning("Rate limit flush deadlock; will retry next sync tick")
        except Exception as e:
            logger.error("Failed to flush rate limit state: %s", e)

    async def _merge_from_db(self) -> None:
        """Read remote counts and merge into local state.

        If the remote count exceeds the local count for a bucket,
        generate synthetic timestamps to pad the local bucket.
        """
        if self._pool is None:
            return

        now = time.time()
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT bucket_type, bucket_key, window_start, request_count
                    FROM rate_limit_state
                    WHERE last_updated > NOW() - INTERVAL '2 minutes'
                    """)
        except Exception as e:
            logger.error("Failed to read rate limit state: %s", e)
            return

        for row in rows:
            bucket_type = row["bucket_type"]
            bucket_key = row["bucket_key"]
            remote_count = row["request_count"]

            if bucket_type == "api":
                local = self._clean_bucket(self._api_buckets.get(bucket_key, []), 60)
                if remote_count > len(local):
                    self._api_buckets[bucket_key] = self._generate_synthetic_timestamps(
                        remote_count, 60, now
                    )
            elif bucket_type == "admin_write":
                local = self._clean_bucket(self._admin_write_buckets.get(bucket_key, []), 60)
                if remote_count > len(local):
                    self._admin_write_buckets[bucket_key] = self._generate_synthetic_timestamps(
                        remote_count, 60, now
                    )
            elif bucket_type == "optimize":
                local = self._clean_bucket(self._optimize_buckets.get(bucket_key, []), 60)
                if remote_count > len(local):
                    self._optimize_buckets[bucket_key] = self._generate_synthetic_timestamps(
                        remote_count, 60, now
                    )
            elif bucket_type == "agent":
                local = self._clean_bucket(self._agent_buckets.get(bucket_key, []), 60)
                if remote_count > len(local):
                    self._agent_buckets[bucket_key] = self._generate_synthetic_timestamps(
                        remote_count, 60, now
                    )
            elif bucket_type == "handoff":
                parts = bucket_key.split(":", 1)
                if len(parts) == 2:
                    depot_pair = tuple(parts)
                    local = self._clean_bucket(self._handoff_buckets.get(depot_pair, []), 3600)
                    if remote_count > len(local):
                        self._handoff_buckets[depot_pair] = self._generate_synthetic_timestamps(
                            remote_count, 3600, now
                        )

    @staticmethod
    def _generate_synthetic_timestamps(count: int, window_seconds: int, now: float) -> list[float]:
        """Generate evenly-spaced synthetic timestamps within a window.

        Used to populate in-memory buckets from DB aggregate counts.
        The timestamps are spread across the recent window so that
        the sliding window cleanup works correctly.
        """
        if count <= 0:
            return []
        spacing = window_seconds / (count + 1)
        return [now - window_seconds + spacing * (i + 1) for i in range(count)]

    async def _hydrate_from_db(self) -> None:
        """Populate in-memory buckets from DB on cold start."""
        if self._pool is None:
            return

        now = time.time()
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT bucket_type, bucket_key, request_count
                    FROM rate_limit_state
                    WHERE last_updated > NOW() - INTERVAL '2 minutes'
                    """)
        except Exception as e:
            logger.warning("Failed to hydrate rate limiter from DB: %s", e)
            return

        hydrated = 0
        for row in rows:
            bucket_type = row["bucket_type"]
            bucket_key = row["bucket_key"]
            count = row["request_count"]

            if bucket_type == "api":
                self._api_buckets[bucket_key] = self._generate_synthetic_timestamps(count, 60, now)
                hydrated += 1
            elif bucket_type == "admin_write":
                self._admin_write_buckets[bucket_key] = self._generate_synthetic_timestamps(
                    count, 60, now
                )
                hydrated += 1
            elif bucket_type == "optimize":
                self._optimize_buckets[bucket_key] = self._generate_synthetic_timestamps(
                    count, 60, now
                )
                hydrated += 1
            elif bucket_type == "agent":
                self._agent_buckets[bucket_key] = self._generate_synthetic_timestamps(
                    count, 60, now
                )
                hydrated += 1
            elif bucket_type == "handoff":
                parts = bucket_key.split(":", 1)
                if len(parts) == 2:
                    depot_pair = tuple(parts)
                    self._handoff_buckets[depot_pair] = self._generate_synthetic_timestamps(
                        count, 3600, now
                    )
                    hydrated += 1

        if hydrated:
            logger.info("Hydrated %d rate limit buckets from DB", hydrated)

    async def _cleanup_expired_rows(self) -> None:
        """Delete expired rate limit rows from the database."""
        if self._pool is None:
            return

        try:
            async with self._pool.acquire() as conn:
                deleted = await conn.execute("""
                    DELETE FROM rate_limit_state
                    WHERE last_updated < NOW() - INTERVAL '5 minutes'
                    """)
                if deleted and deleted != "DELETE 0":
                    logger.debug("Cleaned up expired rate limit rows: %s", deleted)
        except Exception as e:
            logger.error("Failed to cleanup rate limit state: %s", e)


# ============ Module-level Singleton ============

_rate_limiter: Optional[RateLimiter] = None

# Default in-memory instance for backward compatibility and tests
rate_limiter = RateLimiter()


def get_rate_limiter() -> RateLimiter:
    """Get the active rate limiter instance.

    Returns the DB-backed instance if set, otherwise the default in-memory one.
    """
    if _rate_limiter is not None:
        return _rate_limiter
    return rate_limiter


def set_rate_limiter(instance: RateLimiter) -> None:
    """Set the module-level rate limiter instance.

    Called during application startup after the DB pool is available.
    """
    global _rate_limiter
    _rate_limiter = instance
