"""Additional branch-coverage tests for :mod:`src.api.agent_workflows.router`.

Targets:
  - get_static_pool / get_ts_pool / get_db_pools / get_tool_bundle raise
    503 when ``main.db_pools`` is None.
  - require_feature_flag raises 404 when the env var is off.
  - POST /today on a depot that is owned by the caller but doesn't
    exist in `sites` → 404 "Depot not found".
  - GET /workflows/{name}/decisions with a depot_id outside the
    caller's visible set → 404.
  - GET /agent-workflows/decisions/{id} with a decision row whose
    organization_id is None → still visible to non-admin owners.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from src.api.agent_workflows.router import (
    get_db_pools,
    get_static_pool,
    get_tool_bundle,
    get_ts_pool,
    require_feature_flag,
)
from src.api.agent_workflows.router import router as workflows_router
from src.core.workflows.tools import StaticToolBundle
from src.security.auth import verify_token


USER_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
ORG_ID = UUID("44444444-4444-4444-8444-444444444444")
DEPOT_ID = UUID("33333333-3333-4333-8333-333333333333")
OTHER_DEPOT = UUID("99999999-9999-4999-8999-999999999999")


def test_get_static_pool_raises_503_when_db_pools_none(monkeypatch):
    import src.api.main as api_main

    monkeypatch.setattr(api_main, "db_pools", None)
    with pytest.raises(HTTPException) as exc:
        get_static_pool()
    assert exc.value.status_code == 503


def test_get_ts_pool_raises_503_when_db_pools_none(monkeypatch):
    import src.api.main as api_main

    monkeypatch.setattr(api_main, "db_pools", None)
    with pytest.raises(HTTPException) as exc:
        get_ts_pool()
    assert exc.value.status_code == 503


def test_get_db_pools_raises_503_when_db_pools_none(monkeypatch):
    import src.api.main as api_main

    monkeypatch.setattr(api_main, "db_pools", None)
    with pytest.raises(HTTPException) as exc:
        get_db_pools()
    assert exc.value.status_code == 503


def test_get_tool_bundle_propagates_503(monkeypatch):
    import src.api.main as api_main

    monkeypatch.setattr(api_main, "db_pools", None)
    with pytest.raises(HTTPException) as exc:
        get_tool_bundle()
    assert exc.value.status_code == 503


def test_require_feature_flag_off_raises_404(monkeypatch):
    monkeypatch.setenv("DEPOT_AGENT_ENABLED", "false")
    with pytest.raises(HTTPException) as exc:
        require_feature_flag()
    assert exc.value.status_code == 404


def test_require_feature_flag_on_returns_none(monkeypatch):
    monkeypatch.setenv("DEPOT_AGENT_ENABLED", "true")
    assert require_feature_flag() is None


# ── Integration-style branch tests for the router ──────────────────────────


class _Conn:
    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan = plan

    async def fetchrow(self, query: str, *args: Any) -> Any:
        if "FROM sites" in query and "id = $1::uuid" in query:
            return self.plan.get("site_row")
        if "FROM workflow_decisions" in query and "created_at > NOW()" in query:
            return None
        if "FROM workflow_decisions" in query and "decision_id = $1::uuid" in query:
            return self.plan.get("decision_row")
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "EXISTS" in query and "sites" in query:
            return self.plan.get("depot_access", False)
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        if "FROM workflow_decisions" in query:
            return self.plan.get("decision_list", [])
        return []

    async def execute(self, *args: Any) -> str:
        return "OK"


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Pool:
    def __init__(self, plan: dict[str, Any]) -> None:
        self._conn = _Conn(plan)

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)

    async def fetchrow(self, q: str, *a: Any) -> Any:
        return await self._conn.fetchrow(q, *a)

    async def fetchval(self, q: str, *a: Any) -> Any:
        return await self._conn.fetchval(q, *a)

    async def fetch(self, q: str, *a: Any) -> list[Any]:
        return await self._conn.fetch(q, *a)

    async def execute(self, q: str, *a: Any) -> str:
        return await self._conn.execute(q, *a)


def _user() -> dict:
    return {
        "sub": str(USER_ID),
        "email": "u@example.com",
        "role": "authenticated",
        "app_metadata": {
            "favonius_role": "customer_admin",
            "organization_id": str(ORG_ID),
        },
    }


def _app(rows: dict[str, Any]) -> FastAPI:
    app = FastAPI()
    app.include_router(workflows_router)
    pool = _Pool(rows)
    app.dependency_overrides[require_feature_flag] = lambda: None
    app.dependency_overrides[verify_token] = lambda: _user()
    app.dependency_overrides[get_static_pool] = lambda: pool
    app.dependency_overrides[get_ts_pool] = lambda: pool
    app.dependency_overrides[get_tool_bundle] = lambda: StaticToolBundle()
    return app


@pytest.mark.asyncio
async def test_post_today_depot_not_found_returns_404(monkeypatch):
    """Caller has access to the depot but `sites` lookup returns None."""
    from src.security import rate_limiter as rl_mod

    rl_mod.rate_limiter.reset_in_memory_buckets_for_tests()
    rows = {
        "depot_access": True,  # verify_depot_access passes
        "site_row": None,  # but get_depot_by_id returns None
    }
    app = _app(rows)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(f"/agent-workflows/today/{DEPOT_ID}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_decisions_unknown_depot_returns_404(monkeypatch):
    """``depot_id`` outside the caller's visible set → 404 (no enumeration)."""

    import src.api.agent_workflows.router as router_mod
    from src.api.agent.auth_context import AuthContext

    async def fake_build_auth(*args: Any, **kwargs: Any) -> AuthContext:
        return AuthContext(
            user_id=USER_ID,
            organization_id=ORG_ID,
            role="customer_admin",
            visible_depot_ids=[DEPOT_ID],  # caller can see only DEPOT_ID
        )

    monkeypatch.setattr(router_mod, "build_auth_context", fake_build_auth)
    app = _app({})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(
            "/agent-workflows/workflows/daily_readiness_check/decisions",
            params={"depot_id": str(OTHER_DEPOT)},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_decisions_no_depot_id_scopes_all_visible(monkeypatch):
    """When ``depot_id`` is omitted the response scopes to ``visible_depot_ids``."""
    import src.api.agent_workflows.router as router_mod
    from src.api.agent.auth_context import AuthContext

    async def fake_build_auth(*args: Any, **kwargs: Any) -> AuthContext:
        return AuthContext(
            user_id=USER_ID,
            organization_id=ORG_ID,
            role="customer_admin",
            visible_depot_ids=[DEPOT_ID],
        )

    monkeypatch.setattr(router_mod, "build_auth_context", fake_build_auth)

    rows = {
        "decision_list": [
            {
                "decision_id": str(uuid4()),
                "workflow_name": "daily_readiness_check",
                "workflow_version": "v1",
                "depot_id": str(DEPOT_ID),
                "organization_id": str(ORG_ID),
                "triggered_by": "manual",
                "triggered_by_user_id": str(USER_ID),
                "inputs_hash": "00" * 32,
                "tool_calls": "[]",
                "output": json.dumps(
                    {
                        "depot_id": str(DEPOT_ID),
                        "window": "2026-05-13T05:00 → 09:00",
                        "coverage": {
                            "vehicles_checked": 0,
                            "chargers_checked": 0,
                            "routes_checked": 0,
                        },
                        "status": "all_clear",
                        "exceptions": [],
                    }
                ),
                "permission_tier": "inform",
                "status": "success",
                "duration_ms": 50,
                "created_at": datetime.now(timezone.utc),
            }
        ]
    }
    app = _app(rows)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/agent-workflows/workflows/daily_readiness_check/decisions")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["items"], list)
    assert len(body["items"]) == 1


@pytest.mark.asyncio
async def test_get_decision_admin_can_read_any_org(monkeypatch):
    """favonius_admin bypasses the org check on the decisions endpoint."""
    decision_id = uuid4()
    rows = {
        "decision_row": {
            "decision_id": str(decision_id),
            "workflow_name": "daily_readiness_check",
            "workflow_version": "v1",
            "depot_id": str(DEPOT_ID),
            "organization_id": str(UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")),
            "triggered_by": "scheduler",
            "triggered_by_user_id": None,
            "inputs_hash": "00" * 32,
            "tool_calls": "[]",
            "output": json.dumps(
                {
                    "depot_id": str(DEPOT_ID),
                    "window": "2026-05-13T05:00 → 09:00",
                    "coverage": {
                        "vehicles_checked": 0,
                        "chargers_checked": 0,
                        "routes_checked": 0,
                    },
                    "status": "all_clear",
                    "exceptions": [],
                }
            ),
            "permission_tier": "inform",
            "status": "success",
            "duration_ms": 12,
            "created_at": datetime.now(timezone.utc),
        }
    }
    app = _app(rows)
    # Override the user with a favonius_admin staff JWT.
    app.dependency_overrides[verify_token] = lambda: {
        "sub": str(USER_ID),
        "email": "u@favoniusenergy.com",
        "email_verified": True,
        "role": "authenticated",
        "app_metadata": {"favonius_role": "favonius_admin"},
        "user_metadata": {"email_verified": True},
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(f"/agent-workflows/decisions/{decision_id}")
    assert resp.status_code == 200
    assert resp.json()["decision_id"] == str(decision_id)


@pytest.mark.asyncio
async def test_get_decision_returns_404_for_unknown_id(monkeypatch):
    rows = {"decision_row": None}
    app = _app(rows)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(f"/agent-workflows/decisions/{uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_stream_emits_error_event_when_workflow_raises(monkeypatch):
    """SSE error path is exercised when execute_readiness_workflow raises."""
    from src.security import rate_limiter as rl_mod

    rl_mod.rate_limiter.reset_in_memory_buckets_for_tests()
    rows = {
        "depot_access": True,
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
    }
    app = _app(rows)

    import src.api.agent_workflows.router as router_mod

    async def boom(**kwargs: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(router_mod, "execute_readiness_workflow", boom)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(f"/agent-workflows/today/{DEPOT_ID}/stream")
    assert resp.status_code == 200
    body = resp.text
    assert "event: error" in body
