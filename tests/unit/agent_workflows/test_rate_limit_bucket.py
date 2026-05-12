"""Unit tests for the new ``check_agent_workflow_limit`` bucket."""

from __future__ import annotations

from src.security.rate_limiter import RateLimitConfig, RateLimiter


def test_agent_workflow_bucket_is_distinct_from_other_buckets():
    """Exhausting one bucket must not consume budget from another."""
    cfg = RateLimitConfig(
        agent_requests_per_minute=1,
        agent_workflow_requests_per_minute=1,
        optimize_requests_per_minute=1,
    )
    limiter = RateLimiter(config=cfg)

    # Exhaust the workflow bucket.
    assert limiter.check_agent_workflow_limit("u").allowed is True
    assert limiter.check_agent_workflow_limit("u").allowed is False

    # Other buckets still have budget.
    assert limiter.check_agent_limit("u").allowed is True
    assert limiter.check_optimize_limit("u").allowed is True


def test_agent_workflow_bucket_resets_after_window(monkeypatch):
    """Sliding window cleanup releases capacity once entries expire."""
    cfg = RateLimitConfig(agent_workflow_requests_per_minute=1)
    limiter = RateLimiter(config=cfg)

    import time as real_time

    fake_t = [1000.0]

    monkeypatch.setattr(
        "src.security.rate_limiter.time.time", lambda: fake_t[0]
    )

    assert limiter.check_agent_workflow_limit("u").allowed is True
    assert limiter.check_agent_workflow_limit("u").allowed is False

    # Advance past the 60s window.
    fake_t[0] += 61
    assert limiter.check_agent_workflow_limit("u").allowed is True


def test_agent_workflow_bucket_cleared_for_tests():
    cfg = RateLimitConfig(agent_workflow_requests_per_minute=1)
    limiter = RateLimiter(config=cfg)
    limiter.check_agent_workflow_limit("u")
    assert limiter.check_agent_workflow_limit("u").allowed is False
    limiter.reset_in_memory_buckets_for_tests()
    assert limiter.check_agent_workflow_limit("u").allowed is True
