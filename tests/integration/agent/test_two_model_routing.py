"""End-to-end check that the two-model split routes per the per-org flag.

PLAN.md §S5b (build-only spike). Drives the real consumption-path
``run_turn`` against the integration test DB with a synthetic pilot org whose
``organizations.agent_two_model_enabled`` column is flipped on, and asserts the
routing decision landed in ``agent_runs.steps_json`` (the ``model_route`` step)
— the spike's verification anchor (Definition of Done).

A control case proves the gate actually gates: an org with the column at its
default (false) keeps the single-model behavior (explore = ``CONFIG.model``).

Skips automatically when the integration test database is unreachable; CI
provides it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.api.agent.controller import run_turn
from tests.integration.agent.conftest import make_token_payload


def _model_route_payload(steps_json: Any) -> dict[str, Any]:
    """Pull the ``model_route`` step's payload out of an ``agent_runs`` row."""
    steps = steps_json if isinstance(steps_json, list) else json.loads(steps_json)
    for step in steps:
        if step.get("name") == "model_route":
            return step["payload"]
    raise AssertionError(f"no 'model_route' step recorded in steps_json: {steps!r}")


async def _route_for_turn(
    seeded_db: dict[str, Any], llm_client: Any, *, org_key: str, user_key: str
) -> dict[str, Any]:
    """Run one consumption turn and return its recorded ``model_route`` payload.

    The ``model_route`` step is written before extraction, so the routing is
    captured regardless of the turn's final status (success / not_found).
    """
    ts_pool = seeded_db["ts_pool"]
    reply = await run_turn(
        message="How much did John charge last month?",
        token_payload=make_token_payload(seeded_db[user_key], organization_id=seeded_db[org_key]),
        static_pool=seeded_db["static_pool"],
        ts_pool=ts_pool,
        llm_client=llm_client,
    )
    row = await ts_pool.fetchrow(
        "SELECT steps_json FROM agent_runs WHERE run_id = $1::uuid", str(reply.run_id)
    )
    assert row is not None, "run_turn did not persist an agent_runs row"
    return _model_route_payload(row["steps_json"])


@pytest.mark.asyncio
async def test_pilot_org_routes_explore_to_haiku(seeded_db, fake_llm_client) -> None:
    """Flag ON for a synthetic pilot org → explore=Haiku, format=Sonnet, in steps_json."""
    async with seeded_db["static_pool"].acquire() as conn:
        await conn.execute(
            "UPDATE organizations SET agent_two_model_enabled = true WHERE id = $1::uuid",
            str(seeded_db["org_a"]),
        )

    route = await _route_for_turn(seeded_db, fake_llm_client, org_key="org_a", user_key="user_a")
    assert route["two_model_enabled"] is True
    assert route["explore_model"] == "claude-haiku-4-5"
    assert route["format_model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_default_org_keeps_single_model(seeded_db, fake_llm_client) -> None:
    """Flag at its default (false) → BOTH phases stay on the configured default.

    Proves the gate gates *and* that the off path never silently forces Sonnet
    for the format phase (the Codex P2 regression).
    """
    from src.api.agent.llm_router import configured_default_model

    default_model = configured_default_model()
    route = await _route_for_turn(seeded_db, fake_llm_client, org_key="org_b", user_key="user_b")
    assert route["two_model_enabled"] is False
    assert route["explore_model"] == default_model
    assert route["format_model"] == default_model
