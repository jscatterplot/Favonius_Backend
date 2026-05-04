"""Comprehensive tests for the hybrid rate limiter.

Tests cover:
- RateLimitResult dataclass and __bool__ backward compatibility
- In-memory rate limiting (API, optimize, agent, handoff, trigger cooldown)
- Sliding window expiry
- Remaining count and reset time calculations
- DB sync lifecycle (start/stop)
- Flush to DB (UPSERT with GREATEST)
- Hydrate from DB (cold start)
- Merge from DB (cross-instance coordination)
- Graceful degradation without DB pool
- Module-level singleton management
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from src.security.rate_limiter import (
    RateLimitConfig,
    RateLimitResult,
    RateLimiter,
    get_rate_limiter,
    set_rate_limiter,
)

# ============ Tests: RateLimitResult ============


class TestRateLimitResult:
    """Test the RateLimitResult dataclass."""

    def test_allowed_is_truthy(self):
        """Allowed result is truthy in boolean context."""
        result = RateLimitResult(allowed=True, limit=100, remaining=99, reset_at=1.0)
        assert result
        assert bool(result) is True

    def test_denied_is_falsy(self):
        """Denied result is falsy in boolean context."""
        result = RateLimitResult(allowed=False, limit=100, remaining=0, reset_at=1.0)
        assert not result
        assert bool(result) is False

    def test_backward_compatible_if_not(self):
        """Works with existing 'if not result:' pattern."""
        allowed = RateLimitResult(allowed=True, limit=10, remaining=5, reset_at=1.0)
        denied = RateLimitResult(allowed=False, limit=10, remaining=0, reset_at=1.0)

        # This is how it's used in middleware
        if not allowed:
            pytest.fail("Should not enter this branch for allowed result")

        entered = False
        if not denied:
            entered = True
        assert entered

    def test_fields(self):
        """All fields are set correctly."""
        result = RateLimitResult(allowed=True, limit=100, remaining=42, reset_at=1234567890.0)
        assert result.limit == 100
        assert result.remaining == 42
        assert result.reset_at == 1234567890.0

    def test_defaults(self):
        """Default field values."""
        result = RateLimitResult(allowed=True)
        assert result.limit == 0
        assert result.remaining == 0
        assert result.reset_at == 0.0


# ============ Tests: In-Memory Rate Limiting ============


class TestInMemoryRateLimiting:
    """Test in-memory sliding window rate limiting (no DB)."""

    @pytest.fixture
    def limiter(self) -> RateLimiter:
        """Fresh rate limiter with small limits for testing."""
        config = RateLimitConfig(
            api_requests_per_minute=5,
            optimize_requests_per_minute=2,
            agent_requests_per_minute=2,
            handoff_messages_per_hour=3,
            trigger_optimization_cooldown_seconds=10,
        )
        return RateLimiter(config=config)

    def test_api_allows_under_threshold(self, limiter: RateLimiter):
        """Requests under the limit are allowed."""
        for i in range(5):
            result = limiter.check_api_limit("client_1")
            assert result.allowed is True
            assert result.remaining == 5 - (i + 1)

    def test_api_blocks_at_threshold(self, limiter: RateLimiter):
        """Request at the limit is blocked."""
        for _ in range(5):
            limiter.check_api_limit("client_1")

        result = limiter.check_api_limit("client_1")
        assert result.allowed is False
        assert result.remaining == 0
        assert result.limit == 5

    def test_api_separate_clients(self, limiter: RateLimiter):
        """Different clients have separate buckets."""
        for _ in range(5):
            limiter.check_api_limit("client_1")

        # client_2 should still be allowed
        result = limiter.check_api_limit("client_2")
        assert result.allowed is True

    def test_optimize_stricter_than_api(self, limiter: RateLimiter):
        """Optimize limit (2/min) is stricter than API (5/min)."""
        limiter.check_optimize_limit("client_1")
        limiter.check_optimize_limit("client_1")

        result = limiter.check_optimize_limit("client_1")
        assert result.allowed is False
        assert result.limit == 2

    def test_agent_bucket_independent_from_optimize(self, limiter: RateLimiter):
        """Agent and optimize use separate sliding windows for the same key."""
        for _ in range(2):
            limiter.check_optimize_limit("user:abc")
        assert limiter.check_optimize_limit("user:abc").allowed is False

        result = limiter.check_agent_limit("user:abc")
        assert result.allowed is True
        assert result.limit == 2

    def test_admin_write_uses_dedicated_bucket(self, limiter: RateLimiter):
        """Admin-write bucket is independent of the general API bucket.

        Sized for sequential xlsx imports on the fleet identity panel — a
        200-row file at the FE's serial cadence must not bleed into the
        100/min general bucket and lock the rest of the admin UI.
        """
        config = RateLimitConfig(
            api_requests_per_minute=5,
            admin_write_requests_per_minute=3,
            optimize_requests_per_minute=2,
            agent_requests_per_minute=2,
            handoff_messages_per_hour=3,
            trigger_optimization_cooldown_seconds=10,
        )
        scoped = RateLimiter(config=config)

        # Saturate the admin-write bucket for one user.
        for _ in range(3):
            scoped.check_admin_write_limit("user:abc")
        denied = scoped.check_admin_write_limit("user:abc")
        assert denied.allowed is False
        assert denied.limit == 3

        # Same user can still hit the general API bucket — buckets are independent.
        result = scoped.check_api_limit("user:abc")
        assert result.allowed is True
        assert result.limit == 5

    def test_admin_write_default_limit(self):
        """Default admin-write limit comfortably absorbs a 200-row import.

        Sequential xlsx import at any plausible network latency stays well
        under 1200 req/min (20 rps). If we ever lower the default, revisit
        the bulk-import flow.
        """
        config = RateLimitConfig()
        assert config.admin_write_requests_per_minute >= 600

    def test_handoff_sorted_key(self, limiter: RateLimiter):
        """Handoff uses sorted depot pair — a→b and b→a share same bucket."""
        limiter.check_handoff_limit("depot_a", "depot_b")
        limiter.check_handoff_limit("depot_b", "depot_a")
        limiter.check_handoff_limit("depot_a", "depot_b")

        # Both directions count, should be blocked now (limit=3)
        result = limiter.check_handoff_limit("depot_b", "depot_a")
        assert result.allowed is False
        assert result.limit == 3

    def test_handoff_different_pairs_independent(self, limiter: RateLimiter):
        """Different depot pairs have separate buckets."""
        for _ in range(3):
            limiter.check_handoff_limit("depot_a", "depot_b")

        # Different pair should be allowed
        result = limiter.check_handoff_limit("depot_a", "depot_c")
        assert result.allowed is True

    def test_trigger_cooldown_allows_first(self, limiter: RateLimiter):
        """First trigger optimization is always allowed."""
        depot_id = UUID("12345678-1234-1234-1234-123456789012")
        assert limiter.check_trigger_cooldown(depot_id) is True

    def test_trigger_cooldown_blocks_during_cooldown(self, limiter: RateLimiter):
        """Trigger blocked during cooldown period."""
        depot_id = UUID("12345678-1234-1234-1234-123456789012")
        limiter.record_trigger_optimization(depot_id)

        assert limiter.check_trigger_cooldown(depot_id) is False

    def test_trigger_cooldown_allows_after_expiry(self, limiter: RateLimiter):
        """Trigger allowed after cooldown expires."""
        depot_id = UUID("12345678-1234-1234-1234-123456789012")

        # Set trigger time to the past (beyond cooldown)
        limiter._last_trigger_optimization[depot_id] = time.time() - 15

        assert limiter.check_trigger_cooldown(depot_id) is True

    def test_window_expiry(self, limiter: RateLimiter):
        """Requests expire after the window passes."""
        # Fill the bucket
        for _ in range(5):
            limiter.check_api_limit("client_expire")

        assert not limiter.check_api_limit("client_expire")

        # Move all timestamps to the past (beyond 60-second window)
        limiter._api_buckets["client_expire"] = [time.time() - 61]

        # Should be allowed again
        result = limiter.check_api_limit("client_expire")
        assert result.allowed is True

    def test_remaining_decreases(self, limiter: RateLimiter):
        """Remaining count decreases with each request."""
        results = []
        for _ in range(5):
            results.append(limiter.check_api_limit("client_rem"))

        remaining_values = [r.remaining for r in results]
        assert remaining_values == [4, 3, 2, 1, 0]

    def test_reset_time_set(self, limiter: RateLimiter):
        """Reset time is set to first entry + window."""
        result = limiter.check_api_limit("client_reset")
        # reset_at should be roughly now + 60 seconds
        assert result.reset_at > time.time() + 55
        assert result.reset_at < time.time() + 65


# ============ Tests: DB Sync Lifecycle ============


class TestSyncLifecycle:
    """Test start/stop lifecycle with DB pool."""

    @pytest.mark.asyncio
    async def test_start_stop_without_db(self):
        """Start/stop works without a DB pool (pure in-memory mode)."""
        limiter = RateLimiter()
        await limiter.start()
        assert limiter._running is True
        assert limiter._task is not None

        await limiter.stop()
        assert limiter._running is False

    @pytest.mark.asyncio
    async def test_start_stop_with_db(self):
        """Start/stop works with a DB pool."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])

        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool.acquire.return_value = mock_cm

        limiter = RateLimiter(db_pool=mock_pool)
        await limiter.start()
        assert limiter._running is True

        await limiter.stop()
        assert limiter._running is False

    @pytest.mark.asyncio
    async def test_double_start_is_idempotent(self):
        """Calling start() twice doesn't create duplicate tasks."""
        limiter = RateLimiter()
        await limiter.start()
        task1 = limiter._task
        await limiter.start()
        assert limiter._task is task1  # Same task, not a new one
        await limiter.stop()


# ============ Tests: DB Flush ============


class TestFlushToDB:
    """Test flushing local state to PostgreSQL."""

    @pytest.mark.asyncio
    async def test_flush_upserts(self):
        """Flush writes bucket counts to the database."""
        mock_conn = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_cm

        limiter = RateLimiter(db_pool=mock_pool)
        limiter.check_api_limit("client_a")
        limiter.check_api_limit("client_a")
        limiter.check_admin_write_limit("client_a")
        limiter.check_optimize_limit("client_b")
        limiter.check_agent_limit("client_c")

        await limiter._flush_to_db()

        mock_conn.executemany.assert_called_once()
        call_args = mock_conn.executemany.call_args
        sql = call_args[0][0]
        rows = call_args[0][1]

        assert "INSERT INTO rate_limit_state" in sql
        assert "GREATEST" in sql
        assert len(rows) == 4  # api, admin_write, optimize, agent

    @pytest.mark.asyncio
    async def test_flush_with_no_data(self):
        """Flush with empty buckets does nothing."""
        mock_pool = MagicMock()
        limiter = RateLimiter(db_pool=mock_pool)

        await limiter._flush_to_db()

        mock_pool.acquire.assert_not_called()

    @pytest.mark.asyncio
    async def test_flush_without_pool(self):
        """Flush without DB pool is a no-op."""
        limiter = RateLimiter(db_pool=None)
        limiter.check_api_limit("client_a")

        # Should not raise
        await limiter._flush_to_db()

    @pytest.mark.asyncio
    async def test_flush_db_error_does_not_crash(self):
        """DB error during flush is handled gracefully."""
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(side_effect=RuntimeError("DB down"))
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_cm

        limiter = RateLimiter(db_pool=mock_pool)
        limiter.check_api_limit("client_a")

        # Should not raise
        await limiter._flush_to_db()


# ============ Tests: DB Hydrate ============


class TestHydrateFromDB:
    """Test cold-start hydration from PostgreSQL."""

    @pytest.mark.asyncio
    async def test_hydrate_populates_buckets(self):
        """Hydration from DB populates in-memory buckets."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                {"bucket_type": "api", "bucket_key": "client_x", "request_count": 10},
                {"bucket_type": "optimize", "bucket_key": "client_y", "request_count": 3},
                {"bucket_type": "agent", "bucket_key": "client_z", "request_count": 2},
                {"bucket_type": "handoff", "bucket_key": "depot_a:depot_b", "request_count": 5},
            ]
        )
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_cm

        limiter = RateLimiter(db_pool=mock_pool)
        await limiter._hydrate_from_db()

        assert len(limiter._api_buckets["client_x"]) == 10
        assert len(limiter._optimize_buckets["client_y"]) == 3
        assert len(limiter._agent_buckets["client_z"]) == 2
        assert len(limiter._handoff_buckets[("depot_a", "depot_b")]) == 5

    @pytest.mark.asyncio
    async def test_hydrate_without_pool(self):
        """Hydration without DB pool is a no-op."""
        limiter = RateLimiter(db_pool=None)
        await limiter._hydrate_from_db()
        assert len(limiter._api_buckets) == 0

    @pytest.mark.asyncio
    async def test_hydrate_db_error_continues(self):
        """DB error during hydration logs warning and continues."""
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(side_effect=RuntimeError("DB unreachable"))
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_cm

        limiter = RateLimiter(db_pool=mock_pool)
        # Should not raise
        await limiter._hydrate_from_db()


# ============ Tests: DB Merge ============


class TestMergeFromDB:
    """Test merging remote counts into local state."""

    @pytest.mark.asyncio
    async def test_merge_takes_max(self):
        """Merge uses GREATEST — remote count > local overwrites."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "bucket_type": "api",
                    "bucket_key": "client_a",
                    "window_start": None,
                    "request_count": 20,
                },
            ]
        )
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_cm

        limiter = RateLimiter(db_pool=mock_pool)
        # Local has 5 entries
        for _ in range(5):
            limiter.check_api_limit("client_a")
        assert len(limiter._api_buckets["client_a"]) == 5

        await limiter._merge_from_db()

        # Remote has 20 — should now have 20 synthetic timestamps
        assert len(limiter._api_buckets["client_a"]) == 20

    @pytest.mark.asyncio
    async def test_merge_keeps_local_if_higher(self):
        """Merge keeps local count when it's higher than remote."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "bucket_type": "api",
                    "bucket_key": "client_a",
                    "window_start": None,
                    "request_count": 2,
                },
            ]
        )
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_cm

        limiter = RateLimiter(db_pool=mock_pool)
        for _ in range(5):
            limiter.check_api_limit("client_a")

        await limiter._merge_from_db()

        # Local had 5, remote had 2 — keep 5
        assert len(limiter._api_buckets["client_a"]) == 5

    @pytest.mark.asyncio
    async def test_merge_agent_remote_exceeds_local(self):
        """Merge pads agent bucket when remote count exceeds local."""
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(
            return_value=[
                {
                    "bucket_type": "agent",
                    "bucket_key": "user:u1",
                    "window_start": None,
                    "request_count": 4,
                },
            ]
        )
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_cm

        limiter = RateLimiter(db_pool=mock_pool)
        limiter.check_agent_limit("user:u1")
        assert len(limiter._agent_buckets["user:u1"]) == 1

        await limiter._merge_from_db()

        assert len(limiter._agent_buckets["user:u1"]) == 4


# ============ Tests: Synthetic Timestamps ============


class TestSyntheticTimestamps:
    """Test synthetic timestamp generation."""

    def test_generates_correct_count(self):
        """Generates exactly the requested number of timestamps."""
        now = time.time()
        timestamps = RateLimiter._generate_synthetic_timestamps(10, 60, now)
        assert len(timestamps) == 10

    def test_timestamps_within_window(self):
        """All timestamps fall within the window."""
        now = time.time()
        timestamps = RateLimiter._generate_synthetic_timestamps(5, 60, now)
        for ts in timestamps:
            assert ts > now - 60
            assert ts <= now

    def test_timestamps_evenly_spaced(self):
        """Timestamps are evenly spaced."""
        now = time.time()
        timestamps = RateLimiter._generate_synthetic_timestamps(3, 60, now)
        gaps = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
        assert all(abs(g - gaps[0]) < 0.001 for g in gaps)

    def test_zero_count_returns_empty(self):
        """Zero count returns empty list."""
        assert RateLimiter._generate_synthetic_timestamps(0, 60, time.time()) == []

    def test_negative_count_returns_empty(self):
        """Negative count returns empty list."""
        assert RateLimiter._generate_synthetic_timestamps(-1, 60, time.time()) == []


# ============ Tests: Module Singleton ============


class TestModuleSingleton:
    """Test module-level singleton management."""

    def test_get_returns_default_when_not_set(self):
        """get_rate_limiter returns default instance when singleton not set."""
        import src.security.rate_limiter as mod

        original = mod._rate_limiter
        mod._rate_limiter = None
        try:
            limiter = get_rate_limiter()
            assert limiter is mod.rate_limiter  # The default instance
        finally:
            mod._rate_limiter = original

    def test_set_and_get(self):
        """set_rate_limiter makes get_rate_limiter return the set instance."""
        import src.security.rate_limiter as mod

        original = mod._rate_limiter
        try:
            custom = RateLimiter()
            set_rate_limiter(custom)
            assert get_rate_limiter() is custom
        finally:
            mod._rate_limiter = original

    def test_set_overrides_default(self):
        """Set instance takes priority over default."""
        import src.security.rate_limiter as mod

        original = mod._rate_limiter
        try:
            custom = RateLimiter()
            set_rate_limiter(custom)
            assert get_rate_limiter() is not mod.rate_limiter
            assert get_rate_limiter() is custom
        finally:
            mod._rate_limiter = original


# ============ Tests: Cleanup ============


class TestCleanup:
    """Test expired row cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_deletes_expired(self):
        """Cleanup deletes rows older than 5 minutes."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value="DELETE 3")
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_pool = MagicMock()
        mock_pool.acquire.return_value = mock_cm

        limiter = RateLimiter(db_pool=mock_pool)
        await limiter._cleanup_expired_rows()

        mock_conn.execute.assert_called_once()
        sql = mock_conn.execute.call_args[0][0]
        assert "DELETE FROM rate_limit_state" in sql

    @pytest.mark.asyncio
    async def test_cleanup_without_pool(self):
        """Cleanup without DB pool is a no-op."""
        limiter = RateLimiter(db_pool=None)
        await limiter._cleanup_expired_rows()  # Should not raise
