"""Router-level rate-limit regression tests for the depot chat agent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.agent.router import get_ts_pool
from src.api.agent.router import router as agent_router
from src.security.auth import verify_token
from src.security.rate_limiter import RateLimitConfig, RateLimiter


class _FakeConnection:
    """Tiny asyncpg-shaped connection that serves one agent_runs row."""

    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row

    async def fetchrow(self, _sql: str, run_id: str) -> dict[str, Any] | None:
        if run_id == str(self._row["run_id"]):
            return self._row
        return None


class _AcquireContext:
    """Async context manager returned by asyncpg pool.acquire()."""

    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConnection:
        return self._conn

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakePool:
    """Tiny asyncpg pool stub for GET /agent/runs/{id}."""

    def __init__(self, row: dict[str, Any]) -> None:
        self._conn = _FakeConnection(row)

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self._conn)


@pytest.mark.asyncio
async def test_get_run_does_not_use_agent_turn_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run trace reads still work after the LLM-turn bucket is exhausted."""
    user_id = UUID("aa000000-0000-4000-8000-000000000001")
    org_id = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    run_id = uuid4()
    row = {
        "run_id": run_id,
        "user_id": user_id,
        "organization_id": org_id,
        "depot_id": None,
        "user_message": "show me the trace",
        "final_intent": "consumption_by_user",
        "steps_json": [],
        "status": "success",
        "duration_ms": 12,
        "created_at": datetime.now(timezone.utc),
    }

    app = FastAPI()
    app.include_router(agent_router)
    app.dependency_overrides[verify_token] = lambda: {
        "sub": str(user_id),
        "role": "authenticated",
        "app_metadata": {
            "organization_id": str(org_id),
            "favonius_role": "customer_admin",
        },
    }
    app.dependency_overrides[get_ts_pool] = lambda: _FakePool(row)

    limiter = RateLimiter(config=RateLimitConfig(agent_requests_per_minute=1))
    assert limiter.check_agent_limit(f"user:{user_id}").allowed is True
    assert limiter.check_agent_limit(f"user:{user_id}").allowed is False
    monkeypatch.setattr("src.api.agent.router.get_rate_limiter", lambda: limiter)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/agent/runs/{run_id}")

    assert resp.status_code == 200
    assert resp.json()["run_id"] == str(run_id)
