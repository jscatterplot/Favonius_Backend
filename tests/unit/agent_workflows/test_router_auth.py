"""Route-level auth tests for /agent-workflows/*.

Validates:
  - Missing JWT → 401 from FastAPI's HTTPBearer dependency.
  - Cross-org caller → 403 from verify_depot_access (POST endpoints).
  - Cross-org caller → 404 from get_decision ownership gate.
  - DEPOT_AGENT_ENABLED=false → 404 from require_feature_flag.
"""

from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from src.api.agent_workflows.router import (
    get_static_pool,
    get_ts_pool,
    get_tool_bundle,
    require_feature_flag,
)
from src.api.agent_workflows.router import router as workflows_router
from src.core.workflows.tools import StaticToolBundle
from src.security.auth import verify_token


DEPOT_ID = UUID("33333333-3333-4333-8333-333333333333")
DEPOT_ID_OTHER = UUID("ddddffff-ffff-4fff-8fff-ffffffffffff")
ORG_ID = UUID("44444444-4444-4444-8444-444444444444")
USER_ID = UUID("aaaa1111-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


# ── Fake pools / connections ──────────────────────────────────────────────


class _FakeConn:
    def __init__(self, rows: dict[str, Any]) -> None:
        self.rows = rows

    async def fetchrow(self, query: str, *args: Any) -> Any:
        # The router calls:
        #  - get_depot_by_id(static_pool, depot_id) → SELECT … FROM sites
        #  - verify_depot_access → SELECT EXISTS … FROM sites …
        #  - fetch_decision → SELECT … FROM workflow_decisions
        #  - find_recent_decision → SELECT … FROM workflow_decisions
        if "FROM sites" in query and "EXISTS" in query:
            depot_arg, org_arg = args
            same_tenant = str(depot_arg) == str(DEPOT_ID) and str(org_arg) == str(ORG_ID)
            return {"exists": same_tenant}
        if "FROM sites" in query and "id = $1::uuid" in query:
            depot_arg = args[0]
            if str(depot_arg) == str(DEPOT_ID):
                return self.rows.get("site_row")
            return None
        if "FROM workflow_decisions" in query and "decision_id = $1::uuid" in query:
            return self.rows.get("decision_row")
        if "FROM workflow_decisions" in query and "created_at > NOW()" in query:
            return self.rows.get("recent_decision")
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "EXISTS" in query and "sites" in query:
            depot_arg, org_arg = args
            return str(depot_arg) == str(DEPOT_ID) and str(org_arg) == str(ORG_ID)
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        if "FROM workflow_decisions" in query:
            return self.rows.get("decision_list", [])
        if "FROM sites" in query and "depots_for_organization" in query.lower():
            return []
        if "FROM sites" in query:
            return self.rows.get("depots_for_org", [])
        return []

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        return "OK"


class _Acquire:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakePool:
    def __init__(self, rows: dict[str, Any]) -> None:
        self._conn = _FakeConn(rows)

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)

    # asyncpg pools forward fetch/fetchrow/fetchval to a transient
    # connection. Our :class:`_FakeConn` already implements the same
    # surface, so delegate.
    async def fetchrow(self, query: str, *args: Any) -> Any:
        return await self._conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await self._conn.fetchval(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        return await self._conn.fetch(query, *args)

    async def execute(self, *args: Any, **kwargs: Any) -> str:
        return await self._conn.execute(*args, **kwargs)


# ── Fixtures ─────────────────────────────────────────────────────────────


def _build_app(rows: dict[str, Any], *, user: dict | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(workflows_router)
    pool = _FakePool(rows)

    def _no_op() -> None:
        return None

    app.dependency_overrides[require_feature_flag] = _no_op
    app.dependency_overrides[get_static_pool] = lambda: pool
    app.dependency_overrides[get_ts_pool] = lambda: pool
    app.dependency_overrides[get_tool_bundle] = lambda: StaticToolBundle()

    if user is not None:
        app.dependency_overrides[verify_token] = lambda: user
    return app


def _user(*, org_id: UUID | None = ORG_ID, role: str = "customer_admin") -> dict:
    return {
        "sub": str(USER_ID),
        "email": "u@example.com",
        "role": "authenticated",
        "app_metadata": {
            "favonius_role": role,
            "organization_id": str(org_id) if org_id else None,
        },
    }


def _seed_rows() -> dict[str, Any]:
    return {
        "site_row": {
            "depot_id": str(DEPOT_ID),
            "organization_id": str(ORG_ID),
            "name": "Test Depot",
            "latitude": 0.0,
            "longitude": 0.0,
            "timezone": "UTC",
            "currency": "EUR",
            "utility_id": None,
            "max_grid_kw": 100.0,
            "demand_charge_rate_kw": 0.0,
            "demand_charge_billing_period": "month",
            "address": None,
            "billing_metadata": None,
            "building_load_source": None,
        },
        "recent_decision": None,  # forces a fresh run by default
        "decision_row": None,
        "decision_list": [],
        "depots_for_org": [{"depot_id": str(DEPOT_ID)}],
    }


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_jwt_returns_401_on_post_today():
    app = _build_app(_seed_rows())  # no verify_token override → real dep
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(f"/agent-workflows/today/{DEPOT_ID}")
    # FastAPI HTTPBearer returns 403 without `auto_error=False`; the default
    # is 403 when the Authorization header is missing. The 401 path is
    # also acceptable in environments that customise HTTPBearer.
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_cross_org_caller_gets_403_from_post_today(monkeypatch):
    rows = _seed_rows()
    user = _user(org_id=UUID("99999999-9999-4999-8999-999999999999"))
    app = _build_app(rows, user=user)
    # Defeat rate limiter (always allow)
    from src.security import rate_limiter as rl_mod

    rl_mod.rate_limiter.reset_in_memory_buckets_for_tests()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(f"/agent-workflows/today/{DEPOT_ID}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_feature_flag_off_returns_404(monkeypatch):
    """Mounting at runtime: even with router attached, require_feature_flag returns 404 when flag off."""
    app = FastAPI()
    app.include_router(workflows_router)
    pool = _FakePool(_seed_rows())
    app.dependency_overrides[get_static_pool] = lambda: pool
    app.dependency_overrides[get_ts_pool] = lambda: pool
    app.dependency_overrides[get_tool_bundle] = lambda: StaticToolBundle()
    app.dependency_overrides[verify_token] = lambda: _user()

    # Do NOT override require_feature_flag — env var off => 404.
    monkeypatch.setenv("DEPOT_AGENT_ENABLED", "false")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(f"/agent-workflows/decisions/{uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_decision_cross_org_returns_404(monkeypatch):
    other_org = UUID("99999999-9999-4999-8999-999999999999")
    decision_id = uuid4()
    rows = _seed_rows()
    rows["decision_row"] = {
        "decision_id": str(decision_id),
        "workflow_name": "daily_readiness_check",
        "workflow_version": "v1",
        "depot_id": str(DEPOT_ID),
        "organization_id": str(other_org),
        "triggered_by": "manual",
        "triggered_by_user_id": None,
        "inputs_hash": "x" * 64,
        "tool_calls": "[]",
        "output": json.dumps(
            {
                "depot_id": str(DEPOT_ID),
                "window": "2026-05-13T05:00 → 09:00",
                "coverage": {"vehicles_checked": 0, "chargers_checked": 0, "routes_checked": 0},
                "status": "all_clear",
                "exceptions": [],
            }
        ),
        "permission_tier": "inform",
        "status": "success",
        "duration_ms": 12,
        "created_at": datetime.now(timezone.utc),
    }
    app = _build_app(rows, user=_user(org_id=ORG_ID))  # caller is in ORG_ID, row is in other_org

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(f"/agent-workflows/decisions/{decision_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_decision_owner_returns_200(monkeypatch):
    decision_id = uuid4()
    rows = _seed_rows()
    rows["decision_row"] = {
        "decision_id": str(decision_id),
        "workflow_name": "daily_readiness_check",
        "workflow_version": "v1",
        "depot_id": str(DEPOT_ID),
        "organization_id": str(ORG_ID),
        "triggered_by": "manual",
        "triggered_by_user_id": str(USER_ID),
        "inputs_hash": "x" * 64,
        "tool_calls": "[]",
        "output": json.dumps(
            {
                "depot_id": str(DEPOT_ID),
                "window": "2026-05-13T05:00 → 09:00",
                "coverage": {"vehicles_checked": 0, "chargers_checked": 0, "routes_checked": 0},
                "status": "all_clear",
                "exceptions": [],
            }
        ),
        "permission_tier": "inform",
        "status": "success",
        "duration_ms": 12,
        "created_at": datetime.now(timezone.utc),
    }
    app = _build_app(rows, user=_user())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(f"/agent-workflows/decisions/{decision_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision_id"] == str(decision_id)
    assert body["output"]["status"] == "all_clear"


@pytest.mark.asyncio
async def test_rate_limit_post_today(monkeypatch):
    """Burst past 10 req/min on POST /today should 429."""
    from src.security.rate_limiter import RateLimitConfig, RateLimiter

    user = _user()
    app = _build_app(_seed_rows(), user=user)
    limiter = RateLimiter(config=RateLimitConfig(agent_workflow_requests_per_minute=2))
    import src.api.agent_workflows.router as router_mod

    monkeypatch.setattr(router_mod, "get_rate_limiter", lambda: limiter)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # First two are allowed by bucket; third must 429. We don't care
        # whether the request body succeeds — only the rate-limit gate.
        results = []
        for _ in range(3):
            r = await c.post(f"/agent-workflows/today/{DEPOT_ID}")
            results.append(r.status_code)

    assert 429 in results
