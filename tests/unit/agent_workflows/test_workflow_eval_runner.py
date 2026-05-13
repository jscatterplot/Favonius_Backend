"""Unit tests for the workflow eval harness.

Two halves:

1. Pure-logic tests (validation, time resolution, fake client, expected-
   block assertion, diff rendering). No DB, no event loop required.
2. End-to-end runner tests that exercise :func:`run_scenario` against a
   :class:`_FakePool` whose connection stubs match every SQL call the
   runner makes. This keeps the suite fast while still covering every
   branch of the runner.

The real-DB tests live in ``tests/golden/workflows/test_workflow_golden.py``
and gate on CI with a TimescaleDB service container.

Coverage target: ≥ 90% on the runner (PRD §11.2's optimiser bar applies
to the harness too).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.api.agent_workflows.eval.runner import (
    EvalResult,
    FakeAnthropicClient,
    ScenarioLoadError,
    _AcquireContext,
    _TxPool,
    _build_auth_context,
    _build_constraints,
    _build_workflow,
    _parse_scenario_now,
    _resolve_time,
    _stable_json,
    diff_actual_vs_expected,
    load_scenario,
    load_snapshot,
    run_scenario,
)
from src.api.agent_workflows.models import Decision, Disposition, PermissionTier
from src.api.agent_workflows.repo import InMemoryDecisionRepo

# ── Test doubles ──────────────────────────────────────────────────────────


class _FakeTransaction:
    def __init__(self) -> None:
        self.started = False
        self.rolled_back = False

    async def start(self) -> None:
        self.started = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeConnection:
    """asyncpg.Connection-shaped fake.

    Records every execute, replays canned rows on fetch/fetchrow. The
    runner's snapshot loader fires INSERTs; the default workflow's
    tools fire SELECTs. Both go through this fake — no real DB.
    """

    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple[Any, ...]]] = []
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []
        self.fetch_responses: dict[str, list[Any]] = {}
        self.fetchrow_responses: dict[str, list[Any]] = {}
        self.existing_tables: set[str] = {
            "organizations",
            "depots",
            "vehicles",
            "chargers",
            "drivers",
            "schedules",
            "telemetry",
            "prices",
            "building_load",
        }
        self.transactions: list[_FakeTransaction] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executes.append((sql, args))
        return "EXECUTE"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.fetches.append((sql, args))
        for key, rows in self.fetch_responses.items():
            if key in sql:
                return rows
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.fetches.append((sql, args))
        for key, rows in self.fetchrow_responses.items():
            if key in sql and rows:
                return rows[0]
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.fetches.append((sql, args))
        if "to_regclass" in sql and args:
            return args[0] if args[0] in self.existing_tables else None
        return None

    def transaction(self) -> _FakeTransaction:
        tx = _FakeTransaction()
        self.transactions.append(tx)
        return tx


class _FakePool:
    def __init__(self, conn: _FakeConnection | None = None) -> None:
        self.conn = conn or _FakeConnection()
        self.acquire_calls = 0

    def acquire(self) -> "_FakePool._AcquireCM":
        self.acquire_calls += 1
        return _FakePool._AcquireCM(self.conn)

    class _AcquireCM:
        def __init__(self, conn: _FakeConnection) -> None:
            self._conn = conn

        async def __aenter__(self) -> _FakeConnection:
            return self._conn

        async def __aexit__(self, *_exc: Any) -> None:
            return None


# ── Minimal scenario factory ──────────────────────────────────────────────


_SCENARIO_NOW = "2026-05-13T05:00:00+00:00"
_DEPOT_ID = "44444444-4444-4444-8444-444444444444"
_WORKFLOW_ID = "55555555-5555-4555-8555-555555555555"
_VEHICLE_ID = "66666666-6666-4666-8666-666666666666"


def _minimal_scenario(**overrides: Any) -> dict:
    """Schema-valid scenario with a one-turn LLM trace that emits an empty decision."""
    scenario: dict[str, Any] = {
        "id": "test-min",
        "description": "minimal scenario",
        "scenario_now": _SCENARIO_NOW,
        "severity": "blocking",
        "workflow": {
            "id": _WORKFLOW_ID,
            "name": "test_workflow",
            "version": "v1",
            "prompt": "Do the thing.",
            "allowed_tools": [],
            "parameters": {},
        },
        "graph_snapshot": {
            "depot": {"depot_id": _DEPOT_ID, "name": "Test depot"},
        },
        "llm_trace": [
            {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_emit",
                        "name": "emit_decision",
                        "input": {
                            "summary": "All good.",
                            "proposed_actions": [],
                        },
                    }
                ],
            }
        ],
        "expected": {
            "disposition": "pending",
            "output": {"summary": "All good.", "proposed_action_count_max": 0},
        },
    }
    scenario.update(overrides)
    return scenario


# ── Validation ────────────────────────────────────────────────────────────


def test_load_scenario_round_trips_yaml() -> None:
    import yaml

    text = yaml.safe_dump(_minimal_scenario())
    scenario = load_scenario(text)
    assert scenario["id"] == "test-min"
    assert scenario["workflow"]["name"] == "test_workflow"


def test_load_scenario_rejects_non_mapping() -> None:
    with pytest.raises(ScenarioLoadError, match="mapping at the top level"):
        load_scenario("- not a mapping\n- still not\n")


def test_load_scenario_rejects_missing_keys() -> None:
    with pytest.raises(ScenarioLoadError, match="missing keys"):
        load_scenario("id: incomplete\n")


def test_load_scenario_rejects_bad_severity() -> None:
    s = _minimal_scenario()
    s["severity"] = "critical"
    import yaml

    with pytest.raises(ScenarioLoadError, match="severity must be one of"):
        load_scenario(yaml.safe_dump(s))


def test_load_scenario_rejects_bad_tier() -> None:
    s = _minimal_scenario()
    s["permission_tier"] = "supreme_overlord"
    import yaml

    with pytest.raises(ScenarioLoadError, match="permission_tier must be one of"):
        load_scenario(yaml.safe_dump(s))


def test_load_scenario_rejects_non_mapping_snapshot() -> None:
    s = _minimal_scenario()
    s["graph_snapshot"] = "not a dict"
    import yaml

    with pytest.raises(ScenarioLoadError, match="graph_snapshot must be a mapping"):
        load_scenario(yaml.safe_dump(s))


def test_load_scenario_rejects_non_mapping_workflow() -> None:
    s = _minimal_scenario()
    s["workflow"] = ["name", "version"]
    import yaml

    with pytest.raises(ScenarioLoadError, match="workflow must be a mapping"):
        load_scenario(yaml.safe_dump(s))


def test_load_scenario_rejects_missing_workflow_keys() -> None:
    s = _minimal_scenario()
    s["workflow"].pop("prompt")
    import yaml

    with pytest.raises(ScenarioLoadError, match="workflow.prompt is required"):
        load_scenario(yaml.safe_dump(s))


def test_load_scenario_rejects_empty_llm_trace() -> None:
    s = _minimal_scenario()
    s["llm_trace"] = []
    import yaml

    with pytest.raises(ScenarioLoadError, match="llm_trace must be a non-empty list"):
        load_scenario(yaml.safe_dump(s))


def test_load_scenario_rejects_unknown_top_level_field() -> None:
    s = _minimal_scenario()
    s["typo"] = True

    import yaml

    with pytest.raises(ScenarioLoadError, match="schema validation failed"):
        load_scenario(yaml.safe_dump(s))


def test_load_scenario_rejects_trace_entry_missing_content() -> None:
    s = _minimal_scenario()
    s["llm_trace"] = [{"stop_reason": "tool_use"}]
    import yaml

    with pytest.raises(ScenarioLoadError, match=r"llm_trace\[0\] missing 'content'"):
        load_scenario(yaml.safe_dump(s))


# ── Time resolution ───────────────────────────────────────────────────────


def test_parse_scenario_now_accepts_iso_offset() -> None:
    dt = _parse_scenario_now("2026-05-13T05:00:00+00:00")
    assert dt == datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)


def test_parse_scenario_now_accepts_z_suffix() -> None:
    assert _parse_scenario_now("2026-05-13T05:00:00Z").tzinfo == timezone.utc


def test_parse_scenario_now_assumes_utc_for_naive() -> None:
    assert _parse_scenario_now("2026-05-13T05:00:00").tzinfo == timezone.utc


def test_parse_scenario_now_passes_through_datetime() -> None:
    src = datetime(2026, 1, 1, 12, 0, 0)
    assert _parse_scenario_now(src).tzinfo == timezone.utc


def test_parse_scenario_now_rejects_non_string() -> None:
    with pytest.raises(ScenarioLoadError, match="ISO-8601"):
        _parse_scenario_now(12345)


def test_parse_scenario_now_rejects_invalid() -> None:
    with pytest.raises(ScenarioLoadError, match="invalid scenario_now"):
        _parse_scenario_now("not-a-date")


@pytest.mark.parametrize(
    "value,want_seconds",
    [("+2h", 7200), ("-15m", -900), ("+45s", 45), ("-3h", -10800)],
)
def test_resolve_time_duration_shorthand(value: str, want_seconds: int) -> None:
    base = datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)
    assert int((_resolve_time(value, base) - base).total_seconds()) == want_seconds


def test_resolve_time_int_seconds() -> None:
    base = datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)
    assert _resolve_time(3600, base).hour == 6


def test_resolve_time_signed_string() -> None:
    base = datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)
    assert _resolve_time("-1800", base).hour == 4


def test_resolve_time_iso_string() -> None:
    base = datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)
    assert _resolve_time("2026-05-13T07:00:00Z", base).hour == 7


def test_resolve_time_passes_through_datetime() -> None:
    base = datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)
    src = datetime(2026, 6, 1, 0, 0, 0)
    assert _resolve_time(src, base).tzinfo == timezone.utc


def test_resolve_time_rejects_null() -> None:
    with pytest.raises(ScenarioLoadError, match="null timestamps"):
        _resolve_time(None, datetime.now(timezone.utc))


def test_resolve_time_rejects_unparseable_string() -> None:
    with pytest.raises(ScenarioLoadError, match="unparseable time value"):
        _resolve_time("not-iso", datetime.now(timezone.utc))


def test_resolve_time_rejects_unsupported_type() -> None:
    with pytest.raises(ScenarioLoadError, match="unsupported time value type"):
        _resolve_time(object(), datetime.now(timezone.utc))


# ── FakeAnthropicClient ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fake_client_replays_in_order() -> None:
    trace = [
        {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "a", "name": "t1", "input": {"x": 1}}],
        },
        {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"}],
        },
    ]
    client = FakeAnthropicClient(trace)

    r1 = await client.messages.create(model="m", messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    assert r1.content[0].type == "tool_use"
    assert r1.content[0].name == "t1"

    r2 = await client.messages.create(
        model="m",
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "t1", "input": {"x": 1}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]},
        ],
    )
    assert r2.content[0].type == "text"
    assert r2.stop_reason == "end_turn"


@pytest.mark.asyncio
async def test_fake_client_records_calls() -> None:
    client = FakeAnthropicClient(
        [{"stop_reason": "tool_use", "content": [{"type": "text", "text": ""}]}]
    )
    await client.messages.create(model="m", system=[{"text": "hi"}], tools=[], messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    assert len(client.messages.calls) == 1
    assert client.messages.calls[0]["model"] == "m"


@pytest.mark.asyncio
async def test_fake_client_carries_usage_when_present() -> None:
    client = FakeAnthropicClient(
        [
            {
                "stop_reason": "tool_use",
                "content": [{"type": "text", "text": ""}],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            }
        ]
    )
    r = await client.messages.create(messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    assert r.usage.input_tokens == 10
    assert r.usage.output_tokens == 20


@pytest.mark.asyncio
async def test_fake_client_raises_when_exhausted() -> None:
    client = FakeAnthropicClient(
        [{"stop_reason": "tool_use", "content": [{"type": "text", "text": "ok"}]}]
    )
    await client.messages.create(messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    with pytest.raises(AssertionError, match="exhausted"):
        await client.messages.create(messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])


@pytest.mark.asyncio
async def test_fake_client_requires_tool_result_after_tool_use() -> None:
    trace = [
        {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "a", "name": "t1", "input": {}}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]},
    ]
    client = FakeAnthropicClient(trace)
    await client.messages.create(messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}])
    with pytest.raises(AssertionError, match="tool_result context"):
        await client.messages.create(messages=[{"role": "user", "content": [{"type": "text", "text": "next"}]}])


def test_fake_client_rejects_unknown_block_type() -> None:
    with pytest.raises(ScenarioLoadError, match="unsupported type"):
        FakeAnthropicClient([{"stop_reason": "tool_use", "content": [{"type": "wat"}]}])


# ── Snapshot loader ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_snapshot_inserts_depot_only() -> None:
    conn = _FakeConnection()
    depot_id = await load_snapshot(conn, _minimal_scenario())
    assert depot_id == UUID(_DEPOT_ID)
    assert sum("INSERT INTO depots" in sql for sql, _ in conn.executes) == 1


@pytest.mark.asyncio
async def test_load_snapshot_inserts_full_graph() -> None:
    conn = _FakeConnection()
    s = _minimal_scenario()
    snap = s["graph_snapshot"]
    snap["vehicles"] = [
        {
            "vehicle_id": _VEHICLE_ID,
            "external_id": "BUS-1",
            "vehicle_type": "bus_large",
            "battery_kwh": 324.0,
            "max_charge_kw": 80.0,
            "id_tag": "TAG-1",
        }
    ]
    snap["chargers"] = [
        {
            "charger_id": "77777777-7777-4777-8777-777777777777",
            "ocpp_id": "CP-1",
            "rated_kw": 80.0,
            "efficiency": 0.95,
            "connector_type": "CCS",
            "status": "Available",
        }
    ]
    snap["drivers"] = [
        {
            "driver_id": "88888888-8888-4888-8888-888888888888",
            "external_driver_id": "EMP-001",
            "display_name": "Test driver",
            "status": "active",
        }
    ]
    snap["schedules"] = [
        {
            "vehicle_id": _VEHICLE_ID,
            "route_id": "R-1",
            "departure_time": "+2h",
            "return_time": "+10h",
            "energy_kwh": 240.0,
            "required_soc": 0.99,
        }
    ]
    snap["telemetry"] = [
        {
            "vehicle_id": _VEHICLE_ID,
            "charger_id": "77777777-7777-4777-8777-777777777777",
            "time": "-5m",
            "soc": 0.95,
            "is_plugged": True,
            "charging_kw": 60.0,
        }
    ]
    snap["prices"] = [{"time": "+1h", "energy_kwh": 0.12, "source": "test"}]
    snap["building_load"] = [{"time": "+30m", "power_kw": 50.0, "source": "test"}]

    await load_snapshot(conn, s)

    tables = {sql.split("INSERT INTO ", 1)[1].split()[0] for sql, _ in conn.executes}
    assert tables == {
        "organizations",
        "depots",
        "vehicles",
        "chargers",
        "drivers",
        "schedules",
        "telemetry",
        "prices",
        "building_load",
    }


@pytest.mark.asyncio
async def test_load_snapshot_default_external_id_for_vehicle() -> None:
    conn = _FakeConnection()
    s = _minimal_scenario()
    s["graph_snapshot"]["vehicles"] = [{"vehicle_id": _VEHICLE_ID}]
    await load_snapshot(conn, s)
    veh = next(call for call in conn.executes if "INSERT INTO vehicles" in call[0])
    # vehicle_id, depot_id, external_id, ...
    assert veh[1][2].startswith("EXT-")


@pytest.mark.asyncio
async def test_load_snapshot_requires_depot() -> None:
    conn = _FakeConnection()
    s = _minimal_scenario()
    s["graph_snapshot"].pop("depot")
    with pytest.raises(ScenarioLoadError, match="graph_snapshot.depot is required"):
        await load_snapshot(conn, s)


@pytest.mark.asyncio
async def test_load_snapshot_rejects_bad_uuid() -> None:
    conn = _FakeConnection()
    s = _minimal_scenario()
    s["graph_snapshot"]["depot"]["depot_id"] = "not-a-uuid"
    with pytest.raises(ScenarioLoadError, match="invalid UUID"):
        await load_snapshot(conn, s)


@pytest.mark.asyncio
async def test_load_snapshot_rejects_non_uuid_type() -> None:
    conn = _FakeConnection()
    s = _minimal_scenario()
    s["graph_snapshot"]["depot"]["depot_id"] = 12345
    with pytest.raises(ScenarioLoadError, match="expected UUID"):
        await load_snapshot(conn, s)


# ── Helper builders ───────────────────────────────────────────────────────


def test_build_workflow_uses_scenario_anchor_for_timestamps() -> None:
    s = _minimal_scenario()
    scenario_now = _parse_scenario_now(s["scenario_now"])
    wf = _build_workflow(s["workflow"], scenario_now)
    assert wf.created_at == scenario_now
    assert wf.updated_at == scenario_now
    assert wf.name == "test_workflow"
    assert wf.allowed_tools == []


def test_build_constraints_defaults() -> None:
    c = _build_constraints({})
    assert c.min_departure_soc == 0.99
    assert c.max_grid_kw is None


def test_build_constraints_overrides() -> None:
    c = _build_constraints({"depot_constraints": {"min_departure_soc": 0.8, "max_grid_kw": 400.0}})
    assert c.min_departure_soc == 0.8
    assert c.max_grid_kw == 400.0


def test_build_auth_context_defaults() -> None:
    depot_id = uuid4()
    auth = _build_auth_context({}, depot_id)
    assert auth.visible_depot_ids == [depot_id]
    assert auth.role == "customer_operator"
    assert auth.organization_id is not None


def test_build_auth_context_explicit_values() -> None:
    user_id = uuid4()
    org_id = uuid4()
    depot_id = uuid4()
    auth = _build_auth_context(
        {
            "auth": {
                "user_id": str(user_id),
                "organization_id": str(org_id),
                "role": "favonius_admin",
            }
        },
        depot_id,
    )
    assert auth.user_id == user_id
    assert auth.organization_id == org_id
    assert auth.role == "favonius_admin"


# ── Diff rendering ────────────────────────────────────────────────────────


def test_diff_actual_vs_expected_renders_unified_diff() -> None:
    diff = diff_actual_vs_expected({"a": 1}, {"a": 2})
    assert "--- expected" in diff
    assert "+++ actual" in diff


def test_diff_empty_when_equal() -> None:
    assert diff_actual_vs_expected({"a": 1}, {"a": 1}) == ""


def test_stable_json_is_sorted() -> None:
    payload = _stable_json({"b": 2, "a": 1})
    assert payload.index('"a"') < payload.index('"b"')


def test_eval_result_diff_helper() -> None:
    result = EvalResult(
        scenario_id="t",
        workflow="w",
        passed=False,
        severity="blocking",
        decision=None,
        expected={"x": 1},
        actual={"x": 2},
    )
    assert "x" in result.diff()


# ── _TxPool adapter ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tx_pool_delegates_methods() -> None:
    conn = _FakeConnection()
    conn.fetch_responses["SELECT a"] = [{"a": 1}]
    conn.fetchrow_responses["SELECT b"] = [{"b": 2}]
    pool = _TxPool(conn)
    assert await pool.fetch("SELECT a FROM t") == [{"a": 1}]
    assert await pool.fetchrow("SELECT b FROM t") == {"b": 2}
    await pool.fetchval("SELECT 3")
    await pool.execute("INSERT INTO t", 1)
    async with pool.acquire() as got:
        assert got is conn


@pytest.mark.asyncio
async def test_acquire_context_aexit_returns_none() -> None:
    conn = _FakeConnection()
    ctx = _AcquireContext(conn)
    async with ctx as got:
        assert got is conn


# ── End-to-end runner ─────────────────────────────────────────────────────


def _emit_decision_trace(**emit_input: Any) -> list[dict[str, Any]]:
    """One-turn trace whose emit_decision input is fully under test control."""
    return [
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_emit",
                    "name": "emit_decision",
                    "input": emit_input,
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_run_scenario_happy_path() -> None:
    """The runner threads a one-turn trace through WorkflowAgent.run_turn,
    rolls back the transaction, and returns a passing EvalResult."""
    scenario = _minimal_scenario()
    pool = _FakePool()
    result = await run_scenario(scenario, pool=pool, decision_repo=InMemoryDecisionRepo())
    assert result.passed, result.failures
    assert result.decision is not None
    assert result.decision.disposition is Disposition.PENDING
    assert pool.conn.transactions[0].started
    assert pool.conn.transactions[0].rolled_back


@pytest.mark.asyncio
async def test_run_scenario_failure_on_summary_mismatch() -> None:
    scenario = _minimal_scenario()
    scenario["expected"]["output"]["summary"] = "totally different"
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("output.summary" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_summary_contains_substring() -> None:
    scenario = _minimal_scenario()
    scenario["expected"] = {"output": {"summary_contains": "good"}}
    result = await run_scenario(scenario, pool=_FakePool())
    assert result.passed, result.failures


@pytest.mark.asyncio
async def test_run_scenario_summary_contains_miss() -> None:
    scenario = _minimal_scenario()
    scenario["expected"] = {"output": {"summary_contains": "absent"}}
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("summary_contains" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_proposed_action_types_match() -> None:
    scenario = _minimal_scenario()
    scenario["llm_trace"] = _emit_decision_trace(
        summary="s",
        proposed_actions=[
            {"vehicle_id": _VEHICLE_ID, "type": "swap_charger"},
            {"vehicle_id": _VEHICLE_ID, "type": "reassign_to_route"},
        ],
    )
    scenario["expected"] = {
        "output": {"proposed_action_types": ["reassign_to_route", "swap_charger"]}
    }
    result = await run_scenario(scenario, pool=_FakePool())
    assert result.passed, result.failures


@pytest.mark.asyncio
async def test_run_scenario_proposed_action_types_mismatch() -> None:
    scenario = _minimal_scenario()
    scenario["llm_trace"] = _emit_decision_trace(
        summary="s",
        proposed_actions=[{"vehicle_id": _VEHICLE_ID, "type": "swap_charger"}],
    )
    scenario["expected"] = {"output": {"proposed_action_types": ["reassign_to_route"]}}
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("proposed_action_types" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_proposed_action_count_bounds() -> None:
    scenario = _minimal_scenario()
    scenario["llm_trace"] = _emit_decision_trace(
        summary="s",
        proposed_actions=[{"vehicle_id": _VEHICLE_ID, "type": "swap_charger"} for _ in range(3)],
    )
    scenario["expected"] = {
        "output": {"proposed_action_count_min": 1, "proposed_action_count_max": 2}
    }
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("> max" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_proposed_action_count_too_few() -> None:
    scenario = _minimal_scenario()
    scenario["llm_trace"] = _emit_decision_trace(summary="s", proposed_actions=[])
    scenario["expected"] = {"output": {"proposed_action_count_min": 1}}
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("< min" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_must_propose_for_vehicle() -> None:
    scenario = _minimal_scenario()
    scenario["llm_trace"] = _emit_decision_trace(
        summary="s",
        proposed_actions=[{"vehicle_id": _VEHICLE_ID, "type": "swap_charger"}],
    )
    scenario["expected"] = {
        "output": {
            "must_propose_for_vehicle": [
                {"vehicle_id": _VEHICLE_ID, "action_type_contains": "swap"}
            ]
        }
    }
    result = await run_scenario(scenario, pool=_FakePool())
    assert result.passed, result.failures


@pytest.mark.asyncio
async def test_run_scenario_must_propose_miss() -> None:
    scenario = _minimal_scenario()
    scenario["llm_trace"] = _emit_decision_trace(summary="s", proposed_actions=[])
    scenario["expected"] = {
        "output": {
            "must_propose_for_vehicle": [
                {"vehicle_id": _VEHICLE_ID, "action_type_contains": "swap"}
            ]
        }
    }
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("must_propose_for_vehicle" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_must_not_propose_violation() -> None:
    scenario = _minimal_scenario()
    scenario["llm_trace"] = _emit_decision_trace(
        summary="s",
        proposed_actions=[{"vehicle_id": _VEHICLE_ID, "type": "swap_charger"}],
    )
    scenario["expected"] = {
        "output": {"must_not_propose_for_vehicle": [{"vehicle_id": _VEHICLE_ID}]}
    }
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("must_not_propose_for_vehicle" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_filters_constraint_violation() -> None:
    """A proposed_action with departure_soc below the guard's threshold
    must be filtered out and recorded under filtered_violations."""
    scenario = _minimal_scenario()
    scenario["depot_constraints"] = {"min_departure_soc": 0.99, "max_grid_kw": 800.0}
    scenario["llm_trace"] = _emit_decision_trace(
        summary="s",
        proposed_actions=[{"vehicle_id": _VEHICLE_ID, "type": "x", "departure_soc": 0.5}],
    )
    scenario["expected"] = {
        "output": {
            "proposed_action_count_max": 0,
            "filtered_violations_min": 1,
            "filtered_violations_max": 1,
        }
    }
    result = await run_scenario(scenario, pool=_FakePool())
    assert result.passed, result.failures


@pytest.mark.asyncio
async def test_run_scenario_disposition_mismatch() -> None:
    scenario = _minimal_scenario()
    scenario["expected"]["disposition"] = "approved"
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("disposition" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_rule_applied_match() -> None:
    scenario = _minimal_scenario()
    scenario["llm_trace"] = _emit_decision_trace(
        summary="s", proposed_actions=[], rule_applied="my_rule"
    )
    scenario["expected"] = {"rule_applied": "my_rule"}
    result = await run_scenario(scenario, pool=_FakePool())
    assert result.passed, result.failures


@pytest.mark.asyncio
async def test_run_scenario_rule_applied_mismatch() -> None:
    scenario = _minimal_scenario()
    scenario["expected"] = {"rule_applied": "missing_rule"}
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("rule_applied" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_tool_calls_count_min() -> None:
    """The default registry exposes get_scheduled_departures — call it
    once before emitting, then assert tool_calls.count_min."""
    scenario = _minimal_scenario()
    scenario["workflow"]["allowed_tools"] = ["get_scheduled_departures"]
    scenario["llm_trace"] = [
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_t1",
                    "name": "get_scheduled_departures",
                    "input": {},
                }
            ],
        },
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_emit",
                    "name": "emit_decision",
                    "input": {"summary": "s", "proposed_actions": []},
                }
            ],
        },
    ]
    scenario["expected"] = {
        "tool_calls": {
            "count_min": 1,
            "names_in_order": ["get_scheduled_departures"],
            "all_ok": True,
        }
    }
    result = await run_scenario(scenario, pool=_FakePool())
    assert result.passed, result.failures


@pytest.mark.asyncio
async def test_run_scenario_tool_calls_count_max_failure() -> None:
    scenario = _minimal_scenario()
    scenario["workflow"]["allowed_tools"] = ["get_scheduled_departures"]
    scenario["llm_trace"] = [
        {
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": "a", "name": "get_scheduled_departures", "input": {}}
            ],
        },
        {
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": "b", "name": "get_scheduled_departures", "input": {}}
            ],
        },
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "emit",
                    "name": "emit_decision",
                    "input": {"summary": "s", "proposed_actions": []},
                }
            ],
        },
    ]
    scenario["expected"] = {"tool_calls": {"count_max": 1}}
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("count" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_tool_names_in_order_miss() -> None:
    scenario = _minimal_scenario()
    scenario["expected"] = {"tool_calls": {"names_in_order": ["never_called"]}}
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("names_in_order" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_tool_all_ok_false_assertion() -> None:
    scenario = _minimal_scenario()
    # No tool calls at all → all_ok over empty list is True.
    scenario["expected"] = {"tool_calls": {"all_ok": False}}
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("all_ok=false" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_validates_before_acquire() -> None:
    bad = _minimal_scenario()
    bad.pop("severity")
    pool = _FakePool()
    with pytest.raises(ScenarioLoadError):
        await run_scenario(bad, pool=pool)
    assert pool.acquire_calls == 0


@pytest.mark.asyncio
async def test_run_scenario_rolls_back_on_exception() -> None:
    """If the runtime raises, the harness still rolls back."""
    scenario = _minimal_scenario()
    # Empty trace makes FakeAnthropicClient raise on the first .create().
    scenario["llm_trace"] = [{"stop_reason": "tool_use", "content": [{"type": "text", "text": ""}]}]
    # Force the runtime to take more than one turn so the fake client
    # exhausts. Easiest path: use end_turn so the runtime exits the
    # iteration normally with a no_terminator status (no rollback
    # surprise needed). To exercise the actual exception-rollback
    # branch, point the trace at a tool name that's not allow-listed.
    scenario["workflow"]["allowed_tools"] = []
    scenario["llm_trace"] = [
        {
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": "bad", "name": "not_in_allow_list", "input": {}}
            ],
        }
    ]
    pool = _FakePool()
    with pytest.raises(Exception):
        await run_scenario(scenario, pool=pool)
    assert pool.conn.transactions[0].rolled_back


@pytest.mark.asyncio
async def test_run_scenario_custom_tool_registry_builder() -> None:
    """A scenario can ship its own ToolRegistry builder for workflow-
    specific tools."""
    from src.api.agent_workflows.tools import ToolRegistry

    captured: dict[str, Any] = {}

    async def my_tool() -> dict[str, Any]:
        captured["called"] = True
        return {"ok": True}

    def builder(conn: Any, depot_id: Any) -> ToolRegistry:
        captured["depot_id"] = depot_id
        reg = ToolRegistry()
        reg.register(
            "my_tool",
            description="custom",
            input_schema={"type": "object", "properties": {}},
            fn=my_tool,
        )
        return reg

    scenario = _minimal_scenario()
    scenario["workflow"]["allowed_tools"] = ["my_tool"]
    scenario["llm_trace"] = [
        {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "t1", "name": "my_tool", "input": {}}],
        },
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "emit",
                    "name": "emit_decision",
                    "input": {"summary": "s", "proposed_actions": []},
                }
            ],
        },
    ]
    scenario["expected"] = {"tool_calls": {"names_in_order": ["my_tool"]}}

    result = await run_scenario(scenario, pool=_FakePool(), tool_registry_builder=builder)
    assert result.passed, result.failures
    assert captured["called"] is True


@pytest.mark.asyncio
async def test_run_scenario_filtered_violations_below_min() -> None:
    """expected.output.filtered_violations_min raises when too few were filtered."""
    scenario = _minimal_scenario()
    scenario["expected"] = {"output": {"filtered_violations_min": 2}}
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("filtered_violations" in f and "< min" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_coverage_mismatch() -> None:
    """expected.output.coverage checks vehicles/chargers/routes counts."""
    scenario = _minimal_scenario()
    scenario["llm_trace"] = _emit_decision_trace(
        summary="s",
        proposed_actions=[],
        coverage={"vehicles_checked": 5, "chargers_checked": 0, "routes_checked": 0},
    )
    scenario["expected"] = {
        "output": {"coverage": {"vehicles_checked": 2, "chargers_checked": 0, "routes_checked": 0}}
    }
    result = await run_scenario(scenario, pool=_FakePool())
    assert not result.passed
    assert any("coverage.vehicles_checked" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_all_ok_true_with_failed_call() -> None:
    """A tool whose dispatch errors must trip all_ok=true."""
    from src.api.agent_workflows.tools import ToolRegistry

    async def boom() -> dict[str, Any]:
        raise RuntimeError("tool failed")

    def builder(conn: Any, depot_id: Any) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register(
            "boom",
            description="always fails",
            input_schema={"type": "object", "properties": {}},
            fn=boom,
        )
        return reg

    scenario = _minimal_scenario()
    scenario["workflow"]["allowed_tools"] = ["boom"]
    scenario["llm_trace"] = [
        {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "b1", "name": "boom", "input": {}}],
        },
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "emit",
                    "name": "emit_decision",
                    "input": {"summary": "s", "proposed_actions": []},
                }
            ],
        },
    ]
    scenario["expected"] = {"tool_calls": {"all_ok": True}}
    result = await run_scenario(scenario, pool=_FakePool(), tool_registry_builder=builder)
    assert not result.passed
    assert any("all_ok=true" in f for f in result.failures)


@pytest.mark.asyncio
async def test_default_tools_get_vehicle_state_round_trip() -> None:
    """Exercise get_vehicle_state: a scenario that calls it and asserts
    on the resulting tool_calls list."""
    scenario = _minimal_scenario()
    scenario["workflow"]["allowed_tools"] = ["get_vehicle_state"]
    scenario["llm_trace"] = [
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "v1",
                    "name": "get_vehicle_state",
                    "input": {"vehicle_id": _VEHICLE_ID},
                }
            ],
        },
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "emit",
                    "name": "emit_decision",
                    "input": {"summary": "s", "proposed_actions": []},
                }
            ],
        },
    ]
    scenario["expected"] = {"tool_calls": {"names_in_order": ["get_vehicle_state"], "all_ok": True}}
    # Fake fetchrow returns None → tool returns {soc: None}.
    pool = _FakePool()
    result = await run_scenario(scenario, pool=pool)
    assert result.passed, result.failures


@pytest.mark.asyncio
async def test_default_tools_get_charger_state_with_row() -> None:
    """get_charger_state with a row payload threads through cleanly."""
    cid = "77777777-7777-4777-8777-777777777777"
    scenario = _minimal_scenario()
    scenario["workflow"]["allowed_tools"] = ["get_charger_state"]
    scenario["llm_trace"] = [
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "c1",
                    "name": "get_charger_state",
                    "input": {"charger_id": cid},
                }
            ],
        },
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "emit",
                    "name": "emit_decision",
                    "input": {"summary": "s", "proposed_actions": []},
                }
            ],
        },
    ]
    scenario["expected"] = {"tool_calls": {"all_ok": True}}
    # Stage a fake row keyed on the SQL the tool issues.
    pool = _FakePool()
    pool.conn.fetchrow_responses["FROM telemetry"] = [
        {"ocpp_id": "CP-001", "status": "Available", "rated_kw": 80.0}
    ]
    result = await run_scenario(scenario, pool=pool)
    assert result.passed, result.failures


@pytest.mark.asyncio
async def test_default_tools_get_vehicle_state_with_row() -> None:
    """get_vehicle_state returns the row dict when fetchrow has a hit."""
    scenario = _minimal_scenario()
    scenario["workflow"]["allowed_tools"] = ["get_vehicle_state"]
    scenario["llm_trace"] = [
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "v1",
                    "name": "get_vehicle_state",
                    "input": {"vehicle_id": _VEHICLE_ID},
                }
            ],
        },
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": "emit",
                    "name": "emit_decision",
                    "input": {"summary": "s", "proposed_actions": []},
                }
            ],
        },
    ]
    scenario["expected"] = {"tool_calls": {"all_ok": True}}
    pool = _FakePool()
    pool.conn.fetchrow_responses["FROM telemetry"] = [
        {
            "soc": 0.95,
            "charger_id": "77777777-7777-4777-8777-777777777777",
            "is_plugged": True,
            "charging_kw": 60.0,
        }
    ]
    result = await run_scenario(scenario, pool=pool)
    assert result.passed, result.failures


# ── Example scenarios round-trip ──────────────────────────────────────────


def test_example_scenarios_load_cleanly() -> None:
    """Every YAML under tests/golden/workflows/_examples/ must parse +
    validate. No DB required."""
    from pathlib import Path

    examples_dir = Path(__file__).parent.parent.parent / "golden" / "workflows" / "_examples"
    paths = sorted(examples_dir.glob("*.yaml"))
    assert paths, "expected at least one example scenario"
    for path in paths:
        scenario = load_scenario(path.read_text(encoding="utf-8"))
        assert scenario["id"] == path.stem
        # Severity is validated by load_scenario already; assert here
        # for self-documentation.
        assert scenario["severity"] in {"blocking", "warning", "info"}
        # Sanity-check that the workflow block points at the readiness
        # workflow our default tool registry knows how to support.
        assert scenario["workflow"]["name"] == "readiness_check"
