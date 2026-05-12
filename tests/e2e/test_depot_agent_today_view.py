"""End-to-end test for the depot-agent today-view streaming endpoint.

Boots a FastAPI app that mounts ``agent_workflows.router`` with the
feature flag on and a :class:`StaticToolBundle` loaded from scenario
02. The test:

1. Calls ``POST /agent-workflows/today/{depot_id}/stream`` with a JWT.
2. Parses the SSE event stream.
3. Asserts at least one ``step`` event preceded the final ``result``
   event.
4. Asserts the ``result`` payload matches the §6.1 contract for
   scenario 02 (one ``swap_charger`` exception).
5. Calls ``GET /agent-workflows/decisions/{id}`` and asserts the
   returned envelope carries the full tool-call trace.

The whole flow runs in-process — no DB needed; pool fakes and tool
bundle fakes do the work.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.agent_workflows.router import (
    get_static_pool,
    get_ts_pool,
    get_tool_bundle,
    require_feature_flag,
)
from src.api.agent_workflows.router import router as workflows_router
from src.security.auth import verify_token
from tests.golden.depot_agent.loader import load_scenario


USER_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


class _DecisionStore:
    """In-memory backing for ``workflow_decisions`` writes/reads."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def insert(self, args: tuple) -> None:
        (
            decision_id, workflow_name, workflow_version, depot_id,
            organization_id, triggered_by, triggered_by_user_id,
            inputs_hash, tool_calls_json, output_json, permission_tier,
            status, duration_ms, created_at,
        ) = args
        self.rows[str(decision_id)] = {
            "decision_id": str(decision_id),
            "workflow_name": workflow_name,
            "workflow_version": workflow_version,
            "depot_id": str(depot_id),
            "organization_id": str(organization_id) if organization_id else None,
            "triggered_by": triggered_by,
            "triggered_by_user_id": str(triggered_by_user_id) if triggered_by_user_id else None,
            "inputs_hash": inputs_hash,
            "tool_calls": tool_calls_json,
            "output": output_json,
            "permission_tier": permission_tier,
            "status": status,
            "duration_ms": duration_ms,
            "created_at": created_at,
        }


class _Conn:
    def __init__(self, depot_id: UUID, org_id: UUID, store: _DecisionStore) -> None:
        self.depot_id = depot_id
        self.org_id = org_id
        self.store = store

    async def fetchrow(self, query: str, *args: Any) -> Any:
        if "FROM sites" in query and "EXISTS" in query:
            return None  # not called via this path
        if "FROM sites" in query and "id = $1::uuid" in query:
            return {
                "depot_id": str(self.depot_id),
                "organization_id": str(self.org_id),
                "name": "Test Depot",
                "latitude": 0.0,
                "longitude": 0.0,
                "timezone": "Europe/Vilnius",
                "currency": "EUR",
                "utility_id": None,
                "max_grid_kw": 100.0,
                "demand_charge_rate_kw": 0.0,
                "demand_charge_billing_period": "month",
                "address": None,
                "billing_metadata": None,
                "building_load_source": None,
            }
        if "FROM workflow_decisions" in query and "created_at > NOW()" in query:
            # Return the most-recently stored row for this depot if any.
            depot_arg = args[1]
            recent = [
                r
                for r in self.store.rows.values()
                if str(r["depot_id"]) == str(depot_arg)
            ]
            if not recent:
                return None
            recent.sort(key=lambda r: r["created_at"], reverse=True)
            return recent[0]
        if "FROM workflow_decisions" in query and "decision_id = $1::uuid" in query:
            decision_id = args[0]
            return self.store.rows.get(str(decision_id))
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "EXISTS" in query and "sites" in query:
            depot_arg, org_arg = args
            return str(depot_arg) == str(self.depot_id) and str(org_arg) == str(self.org_id)
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        if "FROM workflow_decisions" in query:
            return list(self.store.rows.values())
        return []

    async def execute(self, query: str, *args: Any) -> str:
        if "INSERT INTO workflow_decisions" in query:
            await self.store.insert(args)
        return "OK"


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

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


def _parse_sse(body: str) -> list[tuple[str, Any]]:
    """Parse an SSE response body into a list of (event_name, json_payload)."""
    events: list[tuple[str, Any]] = []
    current_event: str | None = None
    current_data: list[str] = []
    for line in body.split("\n"):
        if line.startswith("event:"):
            current_event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            current_data.append(line[len("data:"):].strip())
        elif line == "":
            if current_event is not None and current_data:
                payload = json.loads("\n".join(current_data))
                events.append((current_event, payload))
            current_event = None
            current_data = []
    return events


@pytest.mark.asyncio
async def test_today_stream_happy_path_with_scenario_02():
    scenario = load_scenario("02_undercharge")
    store = _DecisionStore()
    conn = _Conn(scenario.depot_id, scenario.organization_id, store)
    pool = _Pool(conn)

    app = FastAPI()
    app.include_router(workflows_router)

    user = {
        "sub": str(USER_ID),
        "email": "u@example.com",
        "role": "authenticated",
        "app_metadata": {
            "favonius_role": "customer_admin",
            "organization_id": str(scenario.organization_id),
        },
    }
    app.dependency_overrides[require_feature_flag] = lambda: None
    app.dependency_overrides[verify_token] = lambda: user
    app.dependency_overrides[get_static_pool] = lambda: pool
    app.dependency_overrides[get_ts_pool] = lambda: pool
    app.dependency_overrides[get_tool_bundle] = lambda: scenario.tools

    # Reset the workflow rate-limit bucket so unrelated tests don't interfere.
    from src.security import rate_limiter as rl_mod

    rl_mod.rate_limiter.reset_in_memory_buckets_for_tests()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. SSE stream.
        resp = await client.post(
            f"/agent-workflows/today/{scenario.depot_id}/stream",
            headers={"Accept": "text/event-stream"},
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse(resp.text)
        names = [name for name, _ in events]
        assert "step" in names, f"expected step events, got {names}"
        assert names[-1] == "result", f"final event must be 'result', got {names[-1]}"

        result_payload = events[-1][1]
        assert result_payload["status"] == scenario.expected["status"]
        assert result_payload["coverage"] == scenario.expected["coverage"]
        assert len(result_payload["exceptions"]) == scenario.expected["exception_count"]
        exc = result_payload["exceptions"][0]
        assert exc["vehicle_id"] == scenario.expected["exception_vehicle_id"]
        assert exc["proposed_action"]["type"] == scenario.expected["exception_action_type"]
        decision_id = result_payload["decision_id"]

        # 2. GET /decisions/{id} — same row, plus full tool-call trace.
        get_resp = await client.get(f"/agent-workflows/decisions/{decision_id}")
        assert get_resp.status_code == 200
        envelope = get_resp.json()
        assert envelope["decision_id"] == decision_id
        assert envelope["depot_id"] == str(scenario.depot_id)
        assert isinstance(envelope["tool_calls"], list)
        assert len(envelope["tool_calls"]) >= 4  # at least the 4 main getters
        names_in_trace = [tc["name"] for tc in envelope["tool_calls"]]
        assert "get_scheduled_departures" in names_in_trace

        # 3. POST /today (non-stream) should serve the cached row within
        #    the 60-second idempotency window.
        cached_resp = await client.post(f"/agent-workflows/today/{scenario.depot_id}")
        assert cached_resp.status_code == 200
        cached_body = cached_resp.json()
        assert cached_body["cached"] is True
        assert cached_body["decision_id"] == decision_id


@pytest.mark.asyncio
async def test_list_decisions_returns_recent_audit_rows():
    """GET /workflows/{name}/decisions surfaces the row we just wrote."""
    scenario = load_scenario("02_undercharge")
    store = _DecisionStore()
    conn = _Conn(scenario.depot_id, scenario.organization_id, store)
    pool = _Pool(conn)

    app = FastAPI()
    app.include_router(workflows_router)
    user = {
        "sub": str(USER_ID),
        "email": "u@example.com",
        "role": "authenticated",
        "app_metadata": {
            "favonius_role": "customer_admin",
            "organization_id": str(scenario.organization_id),
        },
    }
    app.dependency_overrides[require_feature_flag] = lambda: None
    app.dependency_overrides[verify_token] = lambda: user
    app.dependency_overrides[get_static_pool] = lambda: pool
    app.dependency_overrides[get_ts_pool] = lambda: pool
    app.dependency_overrides[get_tool_bundle] = lambda: scenario.tools

    # Pre-populate decisions table with one row so the LIST endpoint has
    # something to return.
    store.rows[str(uuid4())] = {
        "decision_id": str(uuid4()),
        "workflow_name": "daily_readiness_check",
        "workflow_version": "v1",
        "depot_id": str(scenario.depot_id),
        "organization_id": str(scenario.organization_id),
        "triggered_by": "manual",
        "triggered_by_user_id": str(USER_ID),
        "inputs_hash": "00" * 32,
        "tool_calls": "[]",
        "output": json.dumps(
            {
                "depot_id": str(scenario.depot_id),
                "window": "2026-05-13T05:00 → 09:00",
                "coverage": {"vehicles_checked": 2, "chargers_checked": 2, "routes_checked": 2},
                "status": "exceptions_present",
                "exceptions": [{"vehicle_id": "v", "issue": "x", "evidence": {}, "permission_required": "inform"}],
            }
        ),
        "permission_tier": "inform",
        "status": "success",
        "duration_ms": 50,
        "created_at": datetime.now(timezone.utc),
    }

    # `_Conn.fetch` already returns `store.rows` for the workflow_decisions
    # query so list_decisions sees the seeded row. But our seeded depot
    # must match the caller's visible depots; configure the auth context
    # by patching :func:`build_auth_context`.
    from src.api.agent.auth_context import AuthContext
    import src.api.agent_workflows.router as router_mod

    async def fake_build_auth(*args: Any, **kwargs: Any) -> AuthContext:
        return AuthContext(
            user_id=USER_ID,
            organization_id=scenario.organization_id,
            role="customer_admin",
            visible_depot_ids=[scenario.depot_id],
        )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Patch within the request's event loop scope so the dependency picks it up.
        import contextlib

        with contextlib.ExitStack() as _:
            orig = router_mod.build_auth_context
            router_mod.build_auth_context = fake_build_auth
            try:
                resp = await client.get(
                    "/agent-workflows/workflows/daily_readiness_check/decisions",
                    params={"depot_id": str(scenario.depot_id)},
                )
            finally:
                router_mod.build_auth_context = orig

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["items"], list)
    assert body["items"], "expected at least one decision in audit list"
    assert body["items"][0]["depot_id"] == str(scenario.depot_id)
