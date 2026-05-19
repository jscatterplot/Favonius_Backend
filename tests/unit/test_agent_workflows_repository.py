"""Unit tests for ``src/api/agent_workflows/repository.py``.

Covers:

* Happy-path round-trip for every repository function against a mocked
  asyncpg pool (the ``mock_asyncpg_pool`` fixture in
  ``tests/conftest.py``).
* Branch coverage for nullable columns, JSON-encoding helpers, and
  the ``limit`` clamp in :func:`list_decisions`.
* The append-only guarantee for the ``decisions`` hypertable, run
  against a real TimescaleDB when ``TEST_DATABASE_URL`` is reachable
  (skipped otherwise). The trigger is non-negotiable per PRD §10.4, so
  the test attempts both ``UPDATE`` and ``DELETE`` and asserts each
  raises.
* :func:`is_depot_agent_enabled` env-flag parsing.
"""

from __future__ import annotations

import importlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

asyncpg = pytest.importorskip("asyncpg")

from src.api.agent_workflows import feature_flag  # noqa: E402
from src.api.agent_workflows.models import (
    Decision,
    Disposition,
    GraduationRule,
    PermissionTier,
    ToolCall,
    Workflow,
)
from src.api.agent_workflows.repository import (  # noqa: E402
    WorkflowNotFoundError,
    _decision_from_row,
    _loads,
    _tool_call_to_dict,
    _workflow_from_row,
    get_tier,
    get_workflow,
    insert_decision,
    list_decisions,
    set_tier,
    upsert_workflow,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _workflow_row(**overrides: object) -> dict:
    """Build a dict that quacks like an asyncpg Record for the workflows table."""
    row = {
        "id": uuid4(),
        "name": "daily_readiness",
        "version": "0.1.0",
        "description": "Daily readiness check",
        "prompt": "You are the depot agent.",
        "allowed_tools": ["get_scheduled_departures", "get_vehicle_state"],
        "parameters": {"window_minutes": 60},
        "created_at": datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 2, 9, 0, tzinfo=timezone.utc),
    }
    row.update(overrides)
    return row


def _decision_row(**overrides: object) -> dict:
    row = {
        "id": uuid4(),
        "workflow_id": uuid4(),
        "depot_id": uuid4(),
        "organization_id": uuid4(),
        "timestamp": datetime(2026, 5, 12, 6, 30, tzinfo=timezone.utc),
        "inputs_hash": "sha256:abcd",
        "tool_calls": [
            {"name": "get_vehicle_state", "arguments": {"id": "BUS-014"}, "result": {"soc": 0.71},
             "ok": True, "error": None}
        ],
        "output": {"status": "exceptions_present"},
        "rule_applied": "projected_soc_below_target",
        "disposition": "pending",
        "human_user_id": None,
        "diff_if_edited": None,
        "parent_decision_id": None,
    }
    row.update(overrides)
    return row


# ── feature_flag ─────────────────────────────────────────────────────────


class TestFeatureFlag:
    """``DEPOT_AGENT_ENABLED`` env-flag parsing."""

    def test_defaults_false(self, monkeypatch):
        monkeypatch.delenv("DEPOT_AGENT_ENABLED", raising=False)
        importlib.reload(feature_flag)
        assert feature_flag.is_depot_agent_enabled() is False

    def test_true_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("DEPOT_AGENT_ENABLED", "TrUe")
        importlib.reload(feature_flag)
        assert feature_flag.is_depot_agent_enabled() is True

    def test_other_values_false(self, monkeypatch):
        monkeypatch.setenv("DEPOT_AGENT_ENABLED", "1")
        importlib.reload(feature_flag)
        assert feature_flag.is_depot_agent_enabled() is False

    def test_whitespace_stripped(self, monkeypatch):
        monkeypatch.setenv("DEPOT_AGENT_ENABLED", "  true  ")
        importlib.reload(feature_flag)
        assert feature_flag.is_depot_agent_enabled() is True


# ── _loads / _tool_call_to_dict ──────────────────────────────────────────


class TestJsonHelpers:
    """JSONB decoding tolerates either parsed values or raw JSON text."""

    def test_loads_none_returns_default(self):
        assert _loads(None, default={"x": 1}) == {"x": 1}
        assert _loads(None, default=None) is None

    def test_loads_str(self):
        assert _loads('{"a": 2}', default={}) == {"a": 2}

    def test_loads_bytes(self):
        assert _loads(b'[1, 2]', default=[]) == [1, 2]

    def test_loads_passthrough(self):
        obj = {"already": "decoded"}
        assert _loads(obj, default={}) is obj

    def test_tool_call_to_dict_round_trip(self):
        tc = ToolCall(name="t", arguments={"k": 1}, result={"r": 2}, ok=False, error="boom")
        d = _tool_call_to_dict(tc)
        assert d == {"name": "t", "arguments": {"k": 1}, "result": {"r": 2},
                     "ok": False, "error": "boom"}


# ── Row → model helpers ──────────────────────────────────────────────────


class TestRowMappers:
    """Direct unit tests on the row→model helpers so we cover both branches
    (null vs populated nullable columns) without needing a DB."""

    def test_workflow_from_row_full(self):
        row = _workflow_row()
        wf = _workflow_from_row(row)
        assert isinstance(wf, Workflow)
        assert wf.name == "daily_readiness"
        assert wf.allowed_tools == ["get_scheduled_departures", "get_vehicle_state"]
        assert wf.parameters == {"window_minutes": 60}

    def test_workflow_from_row_handles_null_description_and_str_parameters(self):
        row = _workflow_row(description=None, allowed_tools=None, parameters='{"k": "v"}')
        wf = _workflow_from_row(row)
        assert wf.description == ""
        assert wf.allowed_tools == []
        assert wf.parameters == {"k": "v"}

    def test_decision_from_row_full(self):
        row = _decision_row(
            diff_if_edited={"output": {"before": 1, "after": 2}},
            parent_decision_id=uuid4(),
            disposition="edited",
        )
        decision = _decision_from_row(row)
        assert decision.disposition is Disposition.EDITED
        assert decision.diff_if_edited == {"output": {"before": 1, "after": 2}}
        assert decision.parent_decision_id is not None
        assert decision.tool_calls[0].name == "get_vehicle_state"

    def test_decision_from_row_handles_string_jsonb(self):
        row = _decision_row(
            tool_calls=json.dumps([{"name": "x", "arguments": {}, "result": None,
                                    "ok": True, "error": None}]),
            output=json.dumps({"a": 1}),
            diff_if_edited=json.dumps({"d": 1}),
        )
        decision = _decision_from_row(row)
        assert decision.output == {"a": 1}
        assert decision.tool_calls[0].name == "x"
        assert decision.diff_if_edited == {"d": 1}


# ── get_workflow ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestGetWorkflow:
    async def test_returns_workflow_on_match(self, mock_asyncpg_pool):
        pool, conn = mock_asyncpg_pool
        conn.fetchrow.return_value = _workflow_row()
        wf = await get_workflow(pool, "daily_readiness")
        assert wf.name == "daily_readiness"
        sql, name = conn.fetchrow.call_args[0]
        assert "FROM workflows" in sql
        assert "WHERE name = $1" in sql
        assert name == "daily_readiness"

    async def test_raises_on_missing(self, mock_asyncpg_pool):
        pool, conn = mock_asyncpg_pool
        conn.fetchrow.return_value = None
        with pytest.raises(WorkflowNotFoundError):
            await get_workflow(pool, "ghost")


@pytest.mark.asyncio
class TestUpsertWorkflow:
    async def test_returns_workflow_with_upsert_sql(self, mock_asyncpg_pool):
        pool, conn = mock_asyncpg_pool
        # The upsert RETURNING clause echoes the same columns get_workflow reads.
        conn.fetchrow.return_value = _workflow_row(
            name="daily_readiness_check",
            version="1.0.0",
            allowed_tools=[
                "get_scheduled_departures",
                "get_vehicle_state",
                "get_charger_state",
                "get_charging_plan",
                "get_driver_assignment",
            ],
            parameters={"lead_time_min": 60},
        )
        wf = await upsert_workflow(
            pool,
            name="daily_readiness_check",
            version="1.0.0",
            description="Daily readiness check",
            prompt="You are the readiness agent.",
            allowed_tools=["get_scheduled_departures", "get_vehicle_state"],
            parameters={"lead_time_min": 60},
        )
        assert wf.name == "daily_readiness_check"
        assert wf.version == "1.0.0"

        sql, *params = conn.fetchrow.call_args[0]
        assert "INSERT INTO workflows" in sql
        assert "ON CONFLICT (name) DO UPDATE" in sql
        assert "RETURNING" in sql
        assert params[0] == "daily_readiness_check"
        assert params[1] == "1.0.0"
        # allowed_tools is positional arg #4 (index 4) — passed as TEXT[].
        assert params[4] == ["get_scheduled_departures", "get_vehicle_state"]


# ── get_tier / set_tier ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestTierRepository:
    async def test_get_tier_returns_none_when_missing(self, mock_asyncpg_pool):
        pool, conn = mock_asyncpg_pool
        conn.fetchrow.return_value = None
        result = await get_tier(pool, uuid4(), uuid4())
        assert result is None

    async def test_get_tier_round_trips_full_row(self, mock_asyncpg_pool):
        pool, conn = mock_asyncpg_pool
        conn.fetchrow.return_value = {
            "tier": "draft_and_wait",
            "min_decisions": 100,
            "max_override_rate": 0.05,
            "max_edit_rate": 0.15,
            "requires_human_signoff": True,
            "next_tier": "act_and_notify",
        }
        result = await get_tier(pool, uuid4(), uuid4())
        assert result is not None
        tier, rule = result
        assert tier is PermissionTier.DRAFT_AND_WAIT
        assert rule.next_tier is PermissionTier.ACT_AND_NOTIFY
        assert rule.min_decisions == 100

    async def test_get_tier_handles_null_next_tier(self, mock_asyncpg_pool):
        pool, conn = mock_asyncpg_pool
        conn.fetchrow.return_value = {
            "tier": "autonomous",
            "min_decisions": 0,
            "max_override_rate": 0.0,
            "max_edit_rate": 0.0,
            "requires_human_signoff": False,
            "next_tier": None,
        }
        result = await get_tier(pool, uuid4(), uuid4())
        assert result is not None
        tier, rule = result
        assert tier is PermissionTier.AUTONOMOUS
        assert rule.next_tier is None

    async def test_set_tier_with_next_tier(self, mock_asyncpg_pool):
        pool, conn = mock_asyncpg_pool
        workflow_id = uuid4()
        depot_id = uuid4()
        rule = GraduationRule(
            min_decisions=100,
            max_override_rate=0.05,
            max_edit_rate=0.15,
            requires_human_signoff=True,
            next_tier=PermissionTier.ACT_AND_NOTIFY,
        )
        await set_tier(pool, workflow_id, depot_id, PermissionTier.DRAFT_AND_WAIT, rule)

        sql, *params = conn.execute.call_args[0]
        assert "INSERT INTO workflow_tiers" in sql
        assert "ON CONFLICT (workflow_id, depot_id) DO UPDATE" in sql
        assert params[0] == workflow_id
        assert params[1] == depot_id
        assert params[2] == "draft_and_wait"
        assert params[3] == 100
        assert params[7] == "act_and_notify"

    async def test_set_tier_with_null_next_tier(self, mock_asyncpg_pool):
        pool, conn = mock_asyncpg_pool
        rule = GraduationRule(
            min_decisions=0,
            max_override_rate=0.0,
            max_edit_rate=0.0,
            requires_human_signoff=False,
            next_tier=None,
        )
        await set_tier(pool, uuid4(), uuid4(), PermissionTier.AUTONOMOUS, rule)
        _, *params = conn.execute.call_args[0]
        assert params[7] is None


# ── insert_decision ──────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestInsertDecision:
    def _decision(self, **overrides: object) -> Decision:
        defaults = dict(
            id=uuid4(),
            workflow_id=uuid4(),
            depot_id=uuid4(),
            organization_id=uuid4(),
            timestamp=datetime(2026, 5, 12, 6, 30, tzinfo=timezone.utc),
            inputs_hash="sha256:abcd",
            tool_calls=[ToolCall(name="get_vehicle_state", arguments={"id": "BUS-014"},
                                 result={"soc": 0.71})],
            output={"status": "exceptions_present"},
            rule_applied="projected_soc_below_target",
            disposition=Disposition.PENDING,
        )
        defaults.update(overrides)
        return Decision(**defaults)

    async def test_inserts_with_json_encoded_payloads(self, mock_asyncpg_pool):
        pool, conn = mock_asyncpg_pool
        decision = self._decision()
        await insert_decision(pool, decision)

        sql, *params = conn.execute.call_args[0]
        assert "INSERT INTO decisions" in sql
        # tool_calls JSON-encoded
        tool_calls_json = params[6]
        assert json.loads(tool_calls_json)[0]["name"] == "get_vehicle_state"
        # output JSON-encoded
        assert json.loads(params[7]) == {"status": "exceptions_present"}
        # diff_if_edited is None when not set
        assert params[11] is None
        # disposition is the enum value (string)
        assert params[9] == "pending"

    async def test_inserts_edit_with_diff_and_parent(self, mock_asyncpg_pool):
        pool, conn = mock_asyncpg_pool
        parent_id = uuid4()
        decision = self._decision(
            disposition=Disposition.EDITED,
            diff_if_edited={"output": {"before": 1, "after": 2}},
            parent_decision_id=parent_id,
            human_user_id=uuid4(),
        )
        await insert_decision(pool, decision)

        _, *params = conn.execute.call_args[0]
        assert json.loads(params[11]) == {"output": {"before": 1, "after": 2}}
        assert params[12] == parent_id


# ── list_decisions ───────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestListDecisions:
    async def test_returns_decisions(self, mock_asyncpg_pool):
        pool, conn = mock_asyncpg_pool
        rows = [_decision_row(), _decision_row(disposition="approved")]
        conn.fetch.return_value = rows
        out = await list_decisions(
            pool,
            workflow_id=uuid4(),
            depot_id=uuid4(),
            since=datetime(2026, 5, 1, tzinfo=timezone.utc),
            limit=10,
        )
        assert len(out) == 2
        assert {d.disposition for d in out} == {Disposition.PENDING, Disposition.APPROVED}

    async def test_clamps_low_limit(self, mock_asyncpg_pool):
        pool, conn = mock_asyncpg_pool
        conn.fetch.return_value = []
        await list_decisions(pool, uuid4(), uuid4(),
                             datetime(2026, 5, 1, tzinfo=timezone.utc), limit=0)
        _, *params = conn.fetch.call_args[0]
        assert params[-1] == 1

    async def test_clamps_high_limit(self, mock_asyncpg_pool):
        pool, conn = mock_asyncpg_pool
        conn.fetch.return_value = []
        await list_decisions(pool, uuid4(), uuid4(),
                             datetime(2026, 5, 1, tzinfo=timezone.utc), limit=5000)
        _, *params = conn.fetch.call_args[0]
        assert params[-1] == 1000


# ── Append-only enforcement (real DB) ────────────────────────────────────
#
# Non-negotiable per PRD §10.4: UPDATE and DELETE on `decisions` must
# raise. This requires the trigger from migration 037 to actually exist,
# which means we need a real TimescaleDB. Skipped when TEST_DATABASE_URL
# is unreachable so the rest of the suite still runs unattended.


MIGRATION_PATH = Path(__file__).resolve().parents[2] / "migrations" / "037_depot_agent_workflows.sql"


@pytest_asyncio.fixture(scope="module")
async def real_pool():
    db_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://favonius_test:test_password@localhost:5433/favonius_test",
    )
    try:
        pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
    except Exception as exc:
        pytest.skip(f"TEST_DATABASE_URL unreachable: {exc}")

    # Apply the migration on top of whatever schema already exists.
    sql = MIGRATION_PATH.read_text()
    try:
        async with pool.acquire() as conn:
            await conn.execute(sql)
    except Exception as exc:  # pragma: no cover - skipped when TimescaleDB missing
        await pool.close()
        pytest.skip(f"Migration 037 could not be applied (TimescaleDB required): {exc}")

    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def seeded_decision(real_pool):
    """Seed one workflow + one decision row; return its (id, timestamp)."""
    workflow_id = uuid4()
    depot_id = uuid4()
    org_id = uuid4()
    decision_id = uuid4()
    ts = datetime.now(tz=timezone.utc) - timedelta(minutes=1)

    async with real_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO workflows (id, name, version, prompt)
            VALUES ($1, $2, '0.1.0', 'system')
            """,
            workflow_id,
            f"test_wf_{decision_id.hex[:8]}",
        )
        await conn.execute(
            """
            INSERT INTO decisions (
                id, workflow_id, depot_id, organization_id, timestamp,
                inputs_hash, tool_calls, output, disposition
            )
            VALUES ($1, $2, $3, $4, $5, 'h', '[]'::jsonb, '{}'::jsonb, 'pending')
            """,
            decision_id,
            workflow_id,
            depot_id,
            org_id,
            ts,
        )

    # No teardown delete: decisions.workflow_id references workflows(id) without
    # ON DELETE CASCADE and decisions are append-only, so deleting the parent
    # workflow would fail with FK violations in integration runs.
    yield decision_id, ts, workflow_id


@pytest.mark.database
@pytest.mark.integration
class TestAppendOnlyTrigger:
    """PRD §10.4: ``decisions`` rejects UPDATE and DELETE at the DB layer."""

    async def test_update_raises(self, real_pool, seeded_decision):
        decision_id, ts, _ = seeded_decision
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as excinfo:
            async with real_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE decisions SET rule_applied = 'tampered' WHERE id = $1",
                    decision_id,
                )
        assert "append-only" in str(excinfo.value)

    async def test_delete_raises(self, real_pool, seeded_decision):
        decision_id, _, _ = seeded_decision
        with pytest.raises(asyncpg.exceptions.CheckViolationError) as excinfo:
            async with real_pool.acquire() as conn:
                await conn.execute("DELETE FROM decisions WHERE id = $1", decision_id)
        assert "append-only" in str(excinfo.value)

    async def test_insert_then_round_trip_through_repository(
        self, real_pool, seeded_decision
    ):
        _, _, workflow_id = seeded_decision
        new_decision = Decision(
            id=uuid4(),
            workflow_id=workflow_id,
            depot_id=uuid4(),
            organization_id=uuid4(),
            timestamp=datetime.now(tz=timezone.utc),
            inputs_hash="sha256:test",
            tool_calls=[ToolCall(name="probe", arguments={}, result={"ok": True})],
            output={"checked": 1},
            disposition=Disposition.AUTO_EXECUTED,
        )
        await insert_decision(real_pool, new_decision)
        rows = await list_decisions(
            real_pool,
            workflow_id=workflow_id,
            depot_id=new_decision.depot_id,
            since=new_decision.timestamp - timedelta(minutes=1),
            limit=10,
        )
        assert len(rows) == 1
        assert rows[0].id == new_decision.id
        assert rows[0].tool_calls[0].name == "probe"
