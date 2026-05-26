"""End-to-end integration tests for the depot chat agent.

Exercises ``run_turn`` and the ``/agent/*`` HTTP surface against a real
DB (the ``agent_e2e`` schema bootstrapped in ``conftest.py``) with a
:class:`FakeLLMClient` so the suite is hermetic and free of network
dependencies. Skipped automatically when the test database is
unreachable — the unit tests in ``tests/unit/agent/`` still cover the
pure-Python paths.

Coverage targets per architecture doc §9.2:

- Happy path
- Disambiguation (cross-tenant John collision is *not* an issue because
  resolution is org-scoped; the in-org collision case lives in
  ``test_disambiguation``)
- Not-found
- Cross-org isolation
- SSE event ordering
- Error path
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.agent.controller import AgentReply, run_turn
from src.api.agent.router import (
    get_llm_client,
    get_static_pool,
    get_ts_pool,
)
from src.api.agent.router import router as agent_router
from src.api.agent.router import (
    verify_token_and_check_agent_limit,
)
from src.security.auth import verify_token
from src.security.rate_limiter import RateLimitConfig, RateLimiter
from tests.integration.agent.conftest import (
    FakeLLMClient,
    make_token_payload,
)

pytestmark = [pytest.mark.integration, pytest.mark.database]


# ── HTTP test client ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def http_client(seeded_db, fake_llm_client):
    """ASGI-backed httpx client wired to the agent router only.

    Builds a minimal FastAPI app that mounts only the agent router so
    we don't drag the lifespan handler (which would otherwise try to
    open production DB pools). Dependency overrides bypass JWT
    verification and the rate limiter for these tests.
    """
    app = FastAPI()
    app.include_router(agent_router)

    state: dict[str, Any] = {
        "token_payload": make_token_payload(seeded_db["user_a"], organization_id=seeded_db["org_a"])
    }

    def _verify_token_override():
        return state["token_payload"]

    app.dependency_overrides[verify_token] = _verify_token_override
    app.dependency_overrides[verify_token_and_check_agent_limit] = _verify_token_override
    app.dependency_overrides[get_static_pool] = lambda: seeded_db["static_pool"]
    app.dependency_overrides[get_ts_pool] = lambda: seeded_db["ts_pool"]
    app.dependency_overrides[get_llm_client] = lambda: fake_llm_client

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, state


# ── run_turn (orchestrator) tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_turn_happy_path(seeded_db, fake_llm_client):
    """A single-driver query writes one agent_runs row + one audit_log row."""
    token = make_token_payload(seeded_db["user_a"], organization_id=seeded_db["org_a"])
    reply = await run_turn(
        message="how much did Jane charge last month",
        token_payload=token,
        static_pool=seeded_db["static_pool"],
        ts_pool=seeded_db["ts_pool"],
        llm_client=fake_llm_client,
    )
    assert isinstance(reply, AgentReply)
    assert reply.status == "success"
    assert reply.intent == "consumption_by_user"
    assert reply.text == "Here is your answer."

    async with seeded_db["ts_pool"].acquire() as conn:
        runs = await conn.fetch("SELECT run_id, status, final_intent FROM agent_runs")
        audits = await conn.fetch(
            "SELECT action, target_id FROM audit_log WHERE action = 'agent.query'"
        )
    assert len(runs) == 1
    assert runs[0]["status"] == "success"
    assert runs[0]["final_intent"] == "consumption_by_user"
    assert len(audits) == 1
    assert audits[0]["target_id"] == str(reply.run_id)
    # The fake formatter was given non-zero rows (Jane has 25 sessions
    # spread over the seeded month, half of which fall in last_month).
    assert fake_llm_client.last_format_payload is not None
    assert fake_llm_client.last_format_payload["row_count"] >= 1


@pytest.mark.asyncio
async def test_run_turn_not_found(seeded_db, fake_llm_client):
    """An unknown name returns status='not_found' with a helpful suggestion."""
    token = make_token_payload(seeded_db["user_a"], organization_id=seeded_db["org_a"])
    reply = await run_turn(
        message="how much did Zorblax charge last month",
        token_payload=token,
        static_pool=seeded_db["static_pool"],
        ts_pool=seeded_db["ts_pool"],
        llm_client=fake_llm_client,
    )
    assert reply.status == "not_found"
    assert reply.intent == "consumption_by_user"
    assert "Zorblax" in reply.text
    assert reply.not_found == ["Zorblax"]

    async with seeded_db["ts_pool"].acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status FROM agent_runs WHERE run_id = $1::uuid", str(reply.run_id)
        )
    assert row is not None
    assert row["status"] == "not_found"


@pytest.mark.asyncio
async def test_run_turn_disambiguation(seeded_db, fake_llm_client):
    """Two Johns in the same org → reply.candidates lists both."""
    # Add a second John to Org A so the search collides in-org.
    async with seeded_db["static_pool"].acquire() as conn:
        await conn.execute(
            """
            INSERT INTO drivers (id, site_id, display_name, email)
            VALUES ($1::uuid, $2::uuid, 'John Roe', 'john.roe@a.test')
            """,
            "11111111-1111-4001-8000-000000000099",
            seeded_db["depot_a"],
        )

    try:
        token = make_token_payload(seeded_db["user_a"], organization_id=seeded_db["org_a"])
        reply = await run_turn(
            message="how much did John charge last month",
            token_payload=token,
            static_pool=seeded_db["static_pool"],
            ts_pool=seeded_db["ts_pool"],
            llm_client=fake_llm_client,
        )

        assert reply.status == "disambiguation"
        assert reply.intent == "consumption_by_user"
        displays = sorted(c.display for c in reply.candidates)
        assert any("John Smith" in d for d in displays)
        assert any("John Roe" in d for d in displays)

        async with seeded_db["ts_pool"].acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status FROM agent_runs WHERE run_id = $1::uuid",
                str(reply.run_id),
            )
        assert row is not None
        assert row["status"] == "disambiguation"
    finally:
        async with seeded_db["static_pool"].acquire() as conn:
            await conn.execute(
                "DELETE FROM drivers WHERE id = $1::uuid",
                "11111111-1111-4001-8000-000000000099",
            )


@pytest.mark.asyncio
async def test_run_turn_cross_org_isolation(seeded_db, fake_llm_client):
    """Org A user asking about an Org B driver gets 'not found'.

    Proves the ``visible_depot_ids`` filter holds: John Carter exists in
    Org B but the resolver only sees Org A's depots when building the
    AuthContext.
    """
    token = make_token_payload(seeded_db["user_a"], organization_id=seeded_db["org_a"])
    reply = await run_turn(
        message="how much did Carter charge last month",
        token_payload=token,
        static_pool=seeded_db["static_pool"],
        ts_pool=seeded_db["ts_pool"],
        llm_client=fake_llm_client,
    )
    assert reply.status == "not_found"
    assert "Carter" in reply.text


@pytest.mark.asyncio
async def test_run_turn_vehicle_fleet(seeded_db, fake_llm_client):
    """A fleet mention resolves to every matching vehicle (full expansion).

    "the renault vans" must expand to BOTH seeded Org A vans and sum
    across them — never a disambiguation prompt.
    """
    token = make_token_payload(seeded_db["user_a"], organization_id=seeded_db["org_a"])
    reply = await run_turn(
        message="how much did the renault vans charge last month",
        token_payload=token,
        static_pool=seeded_db["static_pool"],
        ts_pool=seeded_db["ts_pool"],
        llm_client=fake_llm_client,
    )
    assert reply.status == "success"
    assert reply.intent == "consumption_by_user"
    # Full expansion: both vans resolved, no disambiguation candidates.
    resolved = fake_llm_client.last_format_payload["resolved"]
    assert len(resolved) == 2
    assert all(r["kind"] == "vehicle" for r in resolved)
    assert all(not r["candidates"] for r in resolved)


@pytest.mark.asyncio
async def test_run_turn_depot_wide(seeded_db, fake_llm_client):
    """A no-subject depot-wide total sums sessions across the caller's chargers."""
    token = make_token_payload(seeded_db["user_a"], organization_id=seeded_db["org_a"])
    reply = await run_turn(
        message="what was the total consumption last month",
        token_payload=token,
        static_pool=seeded_db["static_pool"],
        ts_pool=seeded_db["ts_pool"],
        llm_client=fake_llm_client,
    )
    assert reply.status == "success"
    payload = fake_llm_client.last_format_payload
    assert payload["depot_wide"] is True
    assert payload["resolved"] == []  # no named subject
    # DEPOT_A's 'TEST_STATION' carries the seeded sessions; last_month has data.
    assert payload["result_summary"]["total_sessions"] >= 1


@pytest.mark.asyncio
async def test_run_turn_depot_wide_scoped_to_visible_depots(seeded_db, fake_llm_client):
    """Org B's depot-wide total only sees Org B chargers.

    All seeded sessions live on DEPOT_A's 'TEST_STATION'; Org B's only
    charger ('TEST_STATION_B') has none, so the honest answer is
    'no_sessions' — NOT an error and NOT Org A's data.
    """
    token = make_token_payload(seeded_db["user_b"], organization_id=seeded_db["org_b"])
    reply = await run_turn(
        message="what was the total consumption last month",
        token_payload=token,
        static_pool=seeded_db["static_pool"],
        ts_pool=seeded_db["ts_pool"],
        llm_client=fake_llm_client,
    )
    assert reply.status == "success"
    assert fake_llm_client.last_format_payload["result_summary"]["disposition"] == "no_sessions"


@pytest.mark.asyncio
async def test_run_turn_depot_scoped_subject(seeded_db, fake_llm_client):
    """A named depot subject ("at the Vilnius depot") sums that depot's total.

    The compiler raises on a depot subject; the orchestrator must route it
    into the depot-scoped aggregation instead of erroring. DEPOT_A's
    'TEST_STATION' carries the seeded sessions, so last_month has data.
    """
    token = make_token_payload(seeded_db["user_a"], organization_id=seeded_db["org_a"])
    reply = await run_turn(
        message="how much power did the vilnius depot use last month",
        token_payload=token,
        static_pool=seeded_db["static_pool"],
        ts_pool=seeded_db["ts_pool"],
        llm_client=fake_llm_client,
    )
    assert reply.status == "success"
    assert reply.intent == "consumption_by_user"
    payload = fake_llm_client.last_format_payload
    assert payload["result_summary"]["total_sessions"] >= 1


@pytest.mark.asyncio
async def test_run_turn_depot_scoped_cross_org_isolated(seeded_db, fake_llm_client):
    """Org B asking for the Vilnius depot (Org A) resolves nothing → not_found.

    The depot resolver scopes by visible_depot_ids, so a depot in another
    tenant never resolves — the caller can't pull Org A's total by name.
    """
    token = make_token_payload(seeded_db["user_b"], organization_id=seeded_db["org_b"])
    reply = await run_turn(
        message="how much power did the vilnius depot use last month",
        token_payload=token,
        static_pool=seeded_db["static_pool"],
        ts_pool=seeded_db["ts_pool"],
        llm_client=fake_llm_client,
    )
    assert reply.status == "not_found"


@pytest.mark.asyncio
async def test_run_turn_error_path_marks_run(seeded_db):
    """LLM client failure → agent_runs.status='error' and the call re-raises."""
    bad_llm = FakeLLMClient(raise_on_extract=True)
    token = make_token_payload(seeded_db["user_a"], organization_id=seeded_db["org_a"])

    with pytest.raises(RuntimeError, match="simulated LLM upstream failure"):
        await run_turn(
            message="how much did Jane charge last month",
            token_payload=token,
            static_pool=seeded_db["static_pool"],
            ts_pool=seeded_db["ts_pool"],
            llm_client=bad_llm,
        )

    async with seeded_db["ts_pool"].acquire() as conn:
        rows = await conn.fetch("SELECT status FROM agent_runs ORDER BY created_at DESC")
    assert rows
    assert rows[0]["status"] == "error"


# ── HTTP / SSE tests ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_turn_returns_json(http_client):
    """POST /agent/turn returns the AgentReply payload."""
    client, _state = http_client
    resp = await client.post("/agent/turn", json={"message": "how much did Jane charge last month"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["intent"] == "consumption_by_user"
    assert body["text"]


@pytest.mark.asyncio
async def test_post_turn_error_returns_502(seeded_db):
    """An LLM failure surfaces as 502 with a generic message — no leak."""
    app = FastAPI()
    app.include_router(agent_router)

    bad_llm = FakeLLMClient(raise_on_extract=True)
    app.dependency_overrides[verify_token_and_check_agent_limit] = lambda: make_token_payload(
        seeded_db["user_a"], organization_id=seeded_db["org_a"]
    )
    app.dependency_overrides[get_static_pool] = lambda: seeded_db["static_pool"]
    app.dependency_overrides[get_ts_pool] = lambda: seeded_db["ts_pool"]
    app.dependency_overrides[get_llm_client] = lambda: bad_llm

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/agent/turn", json={"message": "how much did Jane charge last month"}
        )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "Agent turn failed" in detail
    # Ensure the upstream simulated message did NOT leak.
    assert "simulated LLM upstream failure" not in detail


@pytest.mark.asyncio
async def test_post_turn_stream_event_order(http_client):
    """SSE stream emits step events in order, then a single answer event."""
    client, _state = http_client
    async with client.stream(
        "POST", "/agent/turn/stream", json={"message": "how much did Jane charge last month"}
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        assert resp.headers["x-accel-buffering"] == "no"

        events: list[tuple[str, dict]] = []
        current_event: str | None = None
        async for line in resp.aiter_lines():
            if line.startswith("event: "):
                current_event = line[len("event: ") :].strip()
            elif line.startswith("data: ") and current_event is not None:
                payload = json.loads(line[len("data: ") :])
                events.append((current_event, payload))
                current_event = None

    names = [e[0] for e in events]
    assert names[-1] == "answer"
    step_names = [
        e[1]["name"] for e in events if e[0] == "step" and isinstance(e[1].get("name"), str)
    ]
    # Order must match the orchestrator phases.
    assert step_names == ["extract_plan", "resolve_entities", "compile", "execute"]
    answer_payload = events[-1][1]
    assert answer_payload["status"] == "success"


@pytest.mark.asyncio
async def test_get_run_returns_stored_trace(http_client, seeded_db):
    """GET /agent/runs/{id} returns the stored row for the caller."""
    client, _state = http_client
    resp = await client.post("/agent/turn", json={"message": "how much did Jane charge last month"})
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    resp = await client.get(f"/agent/runs/{run_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == run_id
    assert body["status"] == "success"
    assert body["final_intent"] == "consumption_by_user"
    assert isinstance(body["steps_json"], list)
    assert len(body["steps_json"]) >= 4  # extract, resolve, compile, execute


@pytest.mark.asyncio
async def test_get_run_does_not_consume_agent_turn_rate_limit(seeded_db, monkeypatch):
    """GET /agent/runs/{id} is a cheap DB read and must not use the LLM-turn bucket."""
    app = FastAPI()
    app.include_router(agent_router)

    token = make_token_payload(seeded_db["user_a"], organization_id=seeded_db["org_a"])
    app.dependency_overrides[verify_token] = lambda: token
    app.dependency_overrides[get_ts_pool] = lambda: seeded_db["ts_pool"]

    limiter = RateLimiter(config=RateLimitConfig(agent_requests_per_minute=1))
    assert limiter.check_agent_limit(f"user:{seeded_db['user_a']}").allowed is True
    assert limiter.check_agent_limit(f"user:{seeded_db['user_a']}").allowed is False
    monkeypatch.setattr("src.api.agent.router.get_rate_limiter", lambda: limiter)

    async with seeded_db["ts_pool"].acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO agent_runs (
                user_id, organization_id, user_message, final_intent, steps_json, status
            )
            VALUES ($1::uuid, $2::uuid, 'show me the trace', 'consumption_by_user', '[]', 'success')
            RETURNING run_id
            """,
            str(seeded_db["user_a"]),
            str(seeded_db["org_a"]),
        )
    assert row is not None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/agent/runs/{row['run_id']}")

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_run_other_user_returns_404(http_client, seeded_db):
    """A different (non-admin) user accessing someone else's run gets 404."""
    client, state = http_client
    resp = await client.post("/agent/turn", json={"message": "how much did Jane charge last month"})
    run_id = resp.json()["run_id"]

    # Switch the JWT subject to user_b mid-test.
    state["token_payload"] = make_token_payload(
        seeded_db["user_b"], organization_id=seeded_db["org_b"]
    )
    resp = await client.get(f"/agent/runs/{run_id}")
    assert resp.status_code == 404
