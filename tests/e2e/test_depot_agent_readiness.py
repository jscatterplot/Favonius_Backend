"""AT-D1: Depot Agent — daily readiness workflow end-to-end acceptance.

Sprint 5's acceptance gate. Walks the full path the production system
follows on a new pilot's first morning:

  1. Call the sprint-5 startup hook so the workflow row appears in
     ``workflows`` and a default ``workflow_tiers`` row appears for
     the depot at tier=inform.
  2. Run the readiness workflow via the existing sprint-2 runtime
     (:class:`~src.api.agent_workflows.runtime.WorkflowAgent`) against
     a canned :class:`FakeAnthropicClient` driving the same agent
     behaviour the golden scenarios exercise.
  3. Persist the resulting :class:`Decision` via the canonical sprint-1
     :func:`~src.api.agent_workflows.repository.insert_decision` writer
     and assert the row landed exactly once at ``disposition=pending``.

Sprint-5 invariant: the tier seeded by registration MUST be ``inform``
(PRD §9.2). Tier graduation to ``draft_and_wait`` happens only after
the metrics-out criteria from PRD §11.1 — not as a startup side effect.

Marker: ``acceptance``. Skips cleanly if the test database is unreachable
(same pattern as the other acceptance tests under ``tests/e2e/``).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
import pytest_asyncio

# Pyomo stub so importing src.api.* doesn't drag in the solver.
if "pyomo" not in sys.modules:
    _pyomo_mock = MagicMock()
    sys.modules["pyomo"] = _pyomo_mock
    sys.modules["pyomo.environ"] = _pyomo_mock
    sys.modules["pyomo.core"] = _pyomo_mock
    sys.modules["pyomo.opt"] = _pyomo_mock

import asyncpg

from src.api.agent.auth_context import AuthContext
from src.api.agent_workflows.models import Disposition, PermissionTier
from src.api.agent_workflows.readiness_workflow import (
    READINESS_DEFAULT_TIER,
    READINESS_WORKFLOW_NAME,
    READINESS_WORKFLOW_VERSION,
    register_daily_readiness_workflow,
)
from src.api.agent_workflows.repo import AsyncpgDecisionRepo
from src.api.agent_workflows.repository import get_tier, get_workflow
from src.api.agent_workflows.runtime import WorkflowAgent
from src.api.agent_workflows.tools import ToolRegistry

pytestmark = [pytest.mark.acceptance, pytest.mark.asyncio]


_ORG_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_DEPOT_ID = UUID("11111111-1111-4111-8111-1111111100ad")
_USER_ID = UUID("aa000000-0000-4000-8000-000000000001")
_TEST_SCHEMA = "depot_agent_atd1"


def _test_db_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://favonius_test:test_password@localhost:5433/favonius_test",
    )


_DDL = f"""
CREATE EXTENSION IF NOT EXISTS pgcrypto;
DROP SCHEMA IF EXISTS {_TEST_SCHEMA} CASCADE;
CREATE SCHEMA {_TEST_SCHEMA};
SET search_path TO {_TEST_SCHEMA};

CREATE TABLE sites (
    id              UUID PRIMARY KEY,
    organization_id UUID,
    name            TEXT,
    timezone        TEXT DEFAULT 'Europe/Vilnius'
);

-- Match the production sprint-1 schema (migration 037) closely enough
-- that the upsert + tier seed paths exercise the same SQL the production
-- repository does.
CREATE TABLE workflows (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT UNIQUE NOT NULL,
    version         TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    prompt          TEXT NOT NULL,
    allowed_tools   TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    parameters      JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE workflow_tiers (
    workflow_id              UUID    NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    depot_id                 UUID    NOT NULL,
    tier                     TEXT    NOT NULL
        CHECK (tier IN ('inform', 'draft_and_wait', 'act_and_notify', 'autonomous')),
    min_decisions            INTEGER NOT NULL DEFAULT 100,
    max_override_rate        NUMERIC NOT NULL DEFAULT 0.05,
    max_edit_rate            NUMERIC NOT NULL DEFAULT 0.15,
    requires_human_signoff   BOOLEAN NOT NULL DEFAULT TRUE,
    next_tier                TEXT,
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workflow_id, depot_id)
);

CREATE TABLE decisions (
    id                  UUID         NOT NULL DEFAULT gen_random_uuid(),
    workflow_id         UUID         NOT NULL REFERENCES workflows(id),
    depot_id            UUID         NOT NULL,
    organization_id     UUID         NOT NULL,
    timestamp           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    inputs_hash         TEXT         NOT NULL,
    tool_calls          JSONB        NOT NULL DEFAULT '[]'::jsonb,
    output              JSONB        NOT NULL DEFAULT '{{}}'::jsonb,
    rule_applied        TEXT,
    disposition         TEXT         NOT NULL
        CHECK (disposition IN ('pending', 'approved', 'edited', 'rejected', 'auto_executed')),
    human_user_id       UUID,
    diff_if_edited      JSONB,
    parent_decision_id  UUID,
    PRIMARY KEY (id, timestamp)
);
"""

_SEED_SQL = f"""
SET search_path TO {_TEST_SCHEMA};
INSERT INTO sites VALUES ('{_DEPOT_ID}', '{_ORG_ID}', 'AT-D1 depot', 'Europe/Vilnius');
"""


@pytest_asyncio.fixture
async def atd1_pools():
    """Bootstrap the AT-D1 schema and yield (static_pool, ts_pool)."""
    try:
        bootstrap = await asyncpg.connect(_test_db_url())
        await bootstrap.execute(_DDL)
        await bootstrap.execute(_SEED_SQL)
        await bootstrap.close()
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"AT-D1: test database unavailable: {exc}")

    search = {"search_path": f"{_TEST_SCHEMA}, public"}
    static_pool = await asyncpg.create_pool(
        _test_db_url(), min_size=1, max_size=2,
        command_timeout=15, server_settings=search,
    )
    ts_pool = await asyncpg.create_pool(
        _test_db_url(), min_size=1, max_size=2,
        command_timeout=15, server_settings=search,
    )

    yield static_pool, ts_pool

    await static_pool.close()
    await ts_pool.close()


# ── Tiny fake Anthropic client (same shape as eval/runner.FakeAnthropicClient) ─


class _Block:
    def __init__(self, *, type: str, text: str = "", id: str = "",
                 name: str = "", input: dict[str, Any] | None = None) -> None:
        self.type = type
        self.text = text
        self.id = id
        self.name = name
        self.input = dict(input or {})


class _Usage:
    def __init__(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    def __init__(self, *, content: list[_Block], stop_reason: str = "tool_use",
                 usage: _Usage | None = None) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


class _MessagesFake:
    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)

    async def create(self, **kwargs: Any) -> _Response:
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, responses: list[_Response]) -> None:
        self.messages = _MessagesFake(responses)


# ── Test ──────────────────────────────────────────────────────────────────


async def test_at_d1_full_flow(atd1_pools: tuple[Any, Any]) -> None:
    """AT-D1: workflow upsert → agent run → Decision row persisted at pending.

    Validates:
      - The startup hook writes a row to ``workflows`` with the
        registered name/version/allowed_tools.
      - The startup hook seeds a ``workflow_tiers`` row at
        tier='inform' for every visible depot.
      - The sprint-2 runtime produces a Decision against the Sprint-5
        workflow, with disposition='pending'.
      - The canonical insert_decision writer (via AsyncpgDecisionRepo)
        persists the row in ``decisions`` exactly once.
      - Re-running the startup hook is idempotent (zero new tier rows).
    """
    static_pool, ts_pool = atd1_pools

    # 1. Startup hook — same path the FastAPI lifespan invokes.
    result = await register_daily_readiness_workflow(
        static_pool=static_pool,
        ts_pool=ts_pool,
        organization_id=_ORG_ID,
    )
    assert result["depots_seen"] == 1
    assert result["tiers_seeded"] == 1, (
        f"Expected one new tier row on first registration; got {result!r}"
    )

    # 2. Workflow row is registered with the sprint-5 metadata.
    workflow = await get_workflow(ts_pool, READINESS_WORKFLOW_NAME)
    assert workflow.name == READINESS_WORKFLOW_NAME
    assert workflow.version == READINESS_WORKFLOW_VERSION
    assert "get_scheduled_departures" in workflow.allowed_tools
    assert len(workflow.allowed_tools) == 5
    assert workflow.parameters.get("lead_time_min") == 60
    assert workflow.parameters.get("soc_tolerance_pct") == 1.0
    assert workflow.parameters.get("escalation_threshold") == 3

    # 3. Tier row landed at the launch default (PRD §9.2).
    tier_row = await get_tier(ts_pool, workflow.id, _DEPOT_ID)
    assert tier_row is not None, "workflow_tiers row not seeded"
    tier, rule = tier_row
    assert tier == READINESS_DEFAULT_TIER == PermissionTier.INFORM, (
        "Tier MUST remain 'inform' at end of sprint 5; never silently "
        "graduated by startup."
    )
    assert rule.min_decisions == 100
    assert rule.next_tier == PermissionTier.DRAFT_AND_WAIT

    # 4. Re-running registration is idempotent on tiers.
    second = await register_daily_readiness_workflow(
        static_pool=static_pool, ts_pool=ts_pool, organization_id=_ORG_ID
    )
    assert second["tiers_seeded"] == 0, (
        f"Second registration should insert zero new tier rows; got {second!r}"
    )

    # 5. Run a single agent turn through the sprint-2 runtime.
    auth = AuthContext(
        user_id=_USER_ID,
        organization_id=_ORG_ID,
        role="customer_operator",
        visible_depot_ids=[_DEPOT_ID],
    )

    # Trim allowed_tools to those the empty registry knows for AT-D1
    # (the harness's default registry is shared with this test path —
    # see tests/golden/workflows/conftest.py + runner._build_default…).
    workflow_for_run = workflow.model_copy(
        update={"allowed_tools": ["get_scheduled_departures"]}
    )

    registry = ToolRegistry()

    async def get_scheduled_departures() -> dict[str, Any]:
        return {"departures": []}

    registry.register(
        "get_scheduled_departures",
        description="Stub",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        fn=get_scheduled_departures,
    )

    fake = _FakeClient(
        [
            _Response(content=[
                _Block(type="tool_use", id="toolu_atd1_sched",
                       name="get_scheduled_departures", input={}),
            ]),
            _Response(content=[
                _Block(type="tool_use", id="toolu_atd1_emit",
                       name="emit_decision", input={
                           "summary": "AT-D1: pilot morning all_clear with 1 vehicle on plan.",
                           "rule_applied": "all_clear",
                           "proposed_actions": [],
                           "coverage": {
                               "vehicles_checked": 1,
                               "chargers_checked": 1,
                               "routes_checked": 1,
                           },
                       }),
            ]),
        ]
    )

    repo = AsyncpgDecisionRepo(ts_pool)
    agent = WorkflowAgent(anthropic_client=fake, decision_repo=repo)

    decision = await agent.run_turn(
        workflow_for_run,
        _DEPOT_ID,
        auth,
        registry,
        permission_tier=tier,
    )

    assert decision.disposition == Disposition.PENDING, (
        "WorkflowAgent must NEVER write auto_executed at tier=inform "
        "(PRD §9.2)."
    )
    assert decision.workflow_id == workflow.id
    assert decision.depot_id == _DEPOT_ID
    assert decision.organization_id == _ORG_ID
    assert decision.output["summary"].startswith("AT-D1")
    assert decision.output["coverage"]["vehicles_checked"] == 1

    # 6. Decision row landed in the audit hypertable.
    async with ts_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT disposition, workflow_id, depot_id, organization_id "
            "FROM decisions WHERE id = $1",
            decision.id,
        )
    assert row is not None, "decisions row was not persisted"
    assert row["disposition"] == "pending"
    assert row["workflow_id"] == workflow.id
    assert row["depot_id"] == _DEPOT_ID
    assert row["organization_id"] == _ORG_ID
