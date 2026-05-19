"""Unit tests for the sprint-5 readiness workflow registration module.

Covers:

* The constants the production seed exposes (name, version, prompt
  hash invariants, the 5 sprint-4 allowed tools, parameter defaults).
* The system prompt encodes every PRD §10.3 / §6.1 guardrail the
  acceptance gate cares about (regex over the prompt text).
* The startup seed calls the repository's upsert + tier writers with
  the right arguments and is idempotent on re-run.

The acceptance-level end-to-end check runs against a real DB in
``tests/e2e/test_depot_agent_readiness.py``; everything here is
offline.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest

from src.api.agent_workflows.models import (
    GraduationRule,
    PermissionTier,
    Workflow,
)
from src.api.agent_workflows.readiness_workflow import (
    READINESS_ALLOWED_TOOLS,
    READINESS_DEFAULT_GRADUATION_RULE,
    READINESS_DEFAULT_TIER,
    READINESS_PARAMETER_DEFAULTS,
    READINESS_SYSTEM_PROMPT,
    READINESS_WORKFLOW_NAME,
    READINESS_WORKFLOW_VERSION,
    build_readiness_workflow,
    register_daily_readiness_workflow,
    seed_default_tiers,
    upsert_workflow_row,
)

# Sync tests in this module get no marker; async tests below carry the
# decorator individually so pytest-asyncio doesn't warn on sync defs.


# ── Constants & shape invariants ──────────────────────────────────────────


def test_workflow_identity_constants():
    assert READINESS_WORKFLOW_NAME == "daily_readiness_check"
    assert READINESS_WORKFLOW_VERSION == "1.0.0"


def test_allowed_tools_match_sprint_4_set():
    """The 5 tool names the sprint-4 ``readiness_tools`` module ships."""
    from src.api.agent_workflows.readiness_tools import (
        build_readiness_tool_registry,  # noqa: F401 — proves the module exists
    )

    assert READINESS_ALLOWED_TOOLS == (
        "get_scheduled_departures",
        "get_vehicle_state",
        "get_charger_state",
        "get_charging_plan",
        "get_driver_assignment",
    )
    assert len(READINESS_ALLOWED_TOOLS) == 5


def test_parameter_defaults_cover_three_tier_one_knobs():
    """PRD §7.4 calls out: lead time, SoC tolerance, escalation threshold."""
    assert set(READINESS_PARAMETER_DEFAULTS) == {
        "lead_time_min",
        "soc_tolerance_pct",
        "escalation_threshold",
    }
    assert READINESS_PARAMETER_DEFAULTS["lead_time_min"] == 60
    assert READINESS_PARAMETER_DEFAULTS["soc_tolerance_pct"] == 1.0
    assert READINESS_PARAMETER_DEFAULTS["escalation_threshold"] == 3


def test_default_tier_is_inform():
    """Sprint-5 invariant per PRD §9.2 — graduation gated by metrics."""
    assert READINESS_DEFAULT_TIER is PermissionTier.INFORM


def test_graduation_rule_targets_draft_and_wait():
    rule = READINESS_DEFAULT_GRADUATION_RULE
    assert isinstance(rule, GraduationRule)
    assert rule.min_decisions == 100
    assert rule.max_override_rate == 0.05
    assert rule.max_edit_rate == 0.15
    assert rule.requires_human_signoff is True
    assert rule.next_tier is PermissionTier.DRAFT_AND_WAIT


# ── System prompt invariants ──────────────────────────────────────────────


def test_system_prompt_mentions_each_hard_constraint():
    """Each PRD §10.3 hard constraint must be named in the prompt."""
    prompt = READINESS_SYSTEM_PROMPT.lower()
    # Departure SoC floor (99%).
    assert "99%" in READINESS_SYSTEM_PROMPT
    # Site grid power cap.
    assert "max_grid_kw" in prompt
    # Driver hours.
    assert "shift_end" in prompt or "driver-hours" in prompt
    # Market signal override.
    assert "market" in prompt
    # Telemetry freshness gate.
    assert "stale" in prompt or "freshness" in prompt
    # Cascading-fault dedup (§6.3 edge case used in readiness §6.1 too).
    assert "circuit" in prompt or "cascad" in prompt


def test_system_prompt_lists_the_five_tools():
    for name in READINESS_ALLOWED_TOOLS:
        assert name in READINESS_SYSTEM_PROMPT


def test_system_prompt_describes_requires_manager_escape_hatch():
    """The §10.3 escape hatch is the prompt's only legal way out of a
    constraint conflict — every reviewer should see it without scrolling."""
    assert "requires_manager" in READINESS_SYSTEM_PROMPT


# ── Workflow object builder ───────────────────────────────────────────────


def test_build_readiness_workflow_returns_pydantic_model():
    wf_id = uuid4()
    wf = build_readiness_workflow(wf_id)
    assert isinstance(wf, Workflow)
    assert wf.id == wf_id
    assert wf.name == READINESS_WORKFLOW_NAME
    assert wf.version == READINESS_WORKFLOW_VERSION
    assert wf.prompt == READINESS_SYSTEM_PROMPT
    assert wf.allowed_tools == list(READINESS_ALLOWED_TOOLS)
    assert wf.parameters == dict(READINESS_PARAMETER_DEFAULTS)


# ── Startup hook ──────────────────────────────────────────────────────────


def _row(**values: Any) -> dict[str, Any]:
    return values


def _fake_ts_pool_returning(
    workflow_row: dict[str, Any],
    existing_tiers: dict[tuple[UUID, UUID], Any] | None = None,
) -> tuple[Any, list[tuple[str, tuple]]]:
    """Build an asyncpg-pool-shaped fake.

    Captures every fetchrow / execute call in a log so tests can assert
    the right SQL ran with the right arguments. ``existing_tiers``
    keys a (workflow_id, depot_id) → row dict so we can simulate
    "already seeded" rows without re-running execute.
    """
    pool = MagicMock()
    log: list[tuple[str, tuple]] = []
    existing = dict(existing_tiers or {})

    conn = AsyncMock()

    async def _fetchrow(sql: str, *args: Any) -> Any:
        log.append(("fetchrow", (sql,) + args))
        # `upsert_workflow` calls fetchrow with INSERT…RETURNING; everything
        # else (get_tier) is also fetchrow but reads from workflow_tiers.
        if "FROM workflow_tiers" in sql:
            key = (args[0], args[1])
            return existing.get(key)
        return workflow_row

    async def _execute(sql: str, *args: Any) -> Any:
        log.append(("execute", (sql,) + args))
        if "INSERT INTO workflow_tiers" in sql:
            existing[(args[0], args[1])] = {
                "tier": args[2],
                "min_decisions": args[3],
                "max_override_rate": args[4],
                "max_edit_rate": args[5],
                "requires_human_signoff": args[6],
                "next_tier": args[7],
            }
        return "INSERT 0 1"

    conn.fetchrow = _fetchrow
    conn.execute = _execute

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool, log


def _fake_static_pool_returning(depot_ids: list[UUID]) -> Any:
    pool = MagicMock()
    conn = AsyncMock()

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        return [{"depot_id": d} for d in depot_ids]

    conn.fetch = _fetch

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool


@pytest.mark.asyncio
async def test_upsert_workflow_row_carries_sprint_5_metadata():
    """The upsert hits ``workflows`` with the registered name/version
    and the five sprint-4 tools."""
    expected_id = uuid4()
    workflow_row = {
        "id": expected_id,
        "name": READINESS_WORKFLOW_NAME,
        "version": READINESS_WORKFLOW_VERSION,
        "description": "d",
        "prompt": READINESS_SYSTEM_PROMPT,
        "allowed_tools": list(READINESS_ALLOWED_TOOLS),
        "parameters": dict(READINESS_PARAMETER_DEFAULTS),
        "created_at": datetime(2026, 5, 13, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 13, tzinfo=timezone.utc),
    }
    ts_pool, log = _fake_ts_pool_returning(workflow_row)

    wf = await upsert_workflow_row(ts_pool)
    assert wf.id == expected_id
    assert wf.name == READINESS_WORKFLOW_NAME

    # Inspect the SQL: must be upsert with allowed_tools as TEXT[].
    fetchrows = [args for kind, args in log if kind == "fetchrow"]
    assert fetchrows, "no fetchrow recorded"
    sql, *params = fetchrows[0]
    assert "INSERT INTO workflows" in sql
    assert "ON CONFLICT (name)" in sql
    # arg positions: name, version, description, prompt, allowed_tools, parameters (JSON)
    assert params[0] == READINESS_WORKFLOW_NAME
    assert params[1] == READINESS_WORKFLOW_VERSION
    assert params[4] == list(READINESS_ALLOWED_TOOLS)


@pytest.mark.asyncio
async def test_seed_default_tiers_inserts_inform_for_new_depots():
    workflow_row = {
        "id": uuid4(), "name": "x", "version": "1", "description": "",
        "prompt": "p", "allowed_tools": [], "parameters": {},
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    ts_pool, log = _fake_ts_pool_returning(workflow_row, existing_tiers={})

    depot_a, depot_b = uuid4(), uuid4()
    inserted = await seed_default_tiers(
        ts_pool, workflow_row["id"], [depot_a, depot_b]
    )
    assert inserted == 2

    inserts = [
        (kind, args) for kind, args in log
        if kind == "execute" and "INSERT INTO workflow_tiers" in args[0]
    ]
    assert len(inserts) == 2
    # Every insert must be tier='inform'.
    for _, args in inserts:
        assert args[3] == "inform"  # tier column
        assert args[8] == "draft_and_wait"  # next_tier


@pytest.mark.asyncio
async def test_seed_default_tiers_skips_existing_rows():
    workflow_row = {
        "id": uuid4(), "name": "x", "version": "1", "description": "",
        "prompt": "p", "allowed_tools": [], "parameters": {},
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    depot_existing, depot_new = uuid4(), uuid4()
    existing = {
        (workflow_row["id"], depot_existing): {
            "tier": "draft_and_wait",
            "min_decisions": 100,
            "max_override_rate": 0.05,
            "max_edit_rate": 0.15,
            "requires_human_signoff": True,
            "next_tier": None,
        }
    }
    ts_pool, log = _fake_ts_pool_returning(workflow_row, existing_tiers=existing)

    inserted = await seed_default_tiers(
        ts_pool, workflow_row["id"], [depot_existing, depot_new]
    )
    assert inserted == 1, (
        "Existing tier rows MUST be left alone — graduation is an "
        "explicit, audited action."
    )


@pytest.mark.asyncio
async def test_register_daily_readiness_workflow_full_flow():
    workflow_id = uuid4()
    workflow_row = {
        "id": workflow_id,
        "name": READINESS_WORKFLOW_NAME,
        "version": READINESS_WORKFLOW_VERSION,
        "description": "d",
        "prompt": READINESS_SYSTEM_PROMPT,
        "allowed_tools": list(READINESS_ALLOWED_TOOLS),
        "parameters": dict(READINESS_PARAMETER_DEFAULTS),
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    ts_pool, _ = _fake_ts_pool_returning(workflow_row)

    depots = [uuid4(), uuid4(), uuid4()]
    static_pool = _fake_static_pool_returning(depots)

    result = await register_daily_readiness_workflow(
        static_pool=static_pool, ts_pool=ts_pool, organization_id=None
    )
    assert result["workflow_id"] == workflow_id
    assert result["depots_seen"] == 3
    assert result["tiers_seeded"] == 3


@pytest.mark.asyncio
async def test_register_org_scoped_query_uses_org_filter():
    """When organization_id is provided, the depot enumeration filters by it."""
    workflow_id = uuid4()
    workflow_row = {
        "id": workflow_id,
        "name": READINESS_WORKFLOW_NAME,
        "version": READINESS_WORKFLOW_VERSION,
        "description": "d",
        "prompt": "p",
        "allowed_tools": [],
        "parameters": {},
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    ts_pool, _ = _fake_ts_pool_returning(workflow_row)

    pool = MagicMock()
    conn = AsyncMock()
    captured: list[tuple[str, tuple]] = []

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        captured.append((sql, args))
        return []

    conn.fetch = _fetch

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    org_id = uuid4()

    await register_daily_readiness_workflow(
        static_pool=pool, ts_pool=ts_pool, organization_id=org_id
    )
    assert any("organization_id" in s for s, _ in captured), (
        f"Expected org-scoped fetch; saw: {captured!r}"
    )
