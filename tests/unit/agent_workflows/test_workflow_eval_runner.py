"""Unit tests for ``src.api.agent_workflows.eval.runner``.

Coverage target: ≥ 90% on the runner module (PRD §11.2 carries 90% for
optimiser/surrogate; the harness inherits the same bar because it is
itself part of the critical path that gates every workflow ship).

All DB calls are stubbed by :class:`_FakeConnection` and :class:`_FakePool`;
the integration tests under ``tests/golden/workflows/`` cover the
real-asyncpg path. Splitting the suites this way means the runner's
pure logic (snapshot validation, time resolution, expected-block
assertion, transaction handling) is fast to run, while the schema-drift
guard still lives in CI.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.api.agent_workflows.runtime import (
    Decision,
    WorkflowAgent,
    WorkflowContext,
    WorkflowNotRegisteredError,
    register_workflow,
    workflow_registry,
)
from src.api.agent_workflows.eval.runner import (
    EvalResult,
    ScenarioLoadError,
    _AcquireContext,
    _TxPool,
    _maybe_resolve_time,
    _parse_scenario_now,
    _resolve_time,
    _stable_json,
    diff_actual_vs_expected,
    load_scenario,
    load_snapshot,
    run_scenario,
)


# ── Test doubles ──────────────────────────────────────────────────────────


class _FakeTransaction:
    """Mimic :class:`asyncpg.Connection.transaction()`'s start/rollback."""

    def __init__(self) -> None:
        self.started = False
        self.rolled_back = False
        self.committed = False

    async def start(self) -> None:
        self.started = True

    async def rollback(self) -> None:
        self.rolled_back = True


class _FakeConnection:
    """Records every exec/fetch the runner makes so tests can introspect."""

    def __init__(self) -> None:
        self.executes: list[tuple[str, tuple[Any, ...]]] = []
        self.fetches: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_responses: list[Any] = []
        self.fetch_responses: list[list[Any]] = []
        self.transactions: list[_FakeTransaction] = []
        # If set, raises this exception on the next `execute` call.
        self.execute_error: Exception | None = None

    async def execute(self, sql: str, *args: Any) -> str:
        if self.execute_error is not None:
            err = self.execute_error
            self.execute_error = None
            raise err
        self.executes.append((sql, args))
        return "EXECUTE"

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.fetches.append((sql, args))
        if self.fetch_responses:
            return self.fetch_responses.pop(0)
        return []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.fetches.append((sql, args))
        if self.fetchrow_responses:
            return self.fetchrow_responses.pop(0)
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.fetches.append((sql, args))
        return None

    def transaction(self) -> _FakeTransaction:
        tx = _FakeTransaction()
        self.transactions.append(tx)
        return tx


class _FakePool:
    """asyncpg.Pool-shaped fake whose ``acquire()`` always yields a stored conn."""

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
_VEHICLE_ID = "55555555-5555-4555-8555-555555555555"
_CHARGER_ID = "66666666-6666-4666-8666-666666666666"


def _minimal_scenario(**overrides: Any) -> dict:
    """A schema-valid scenario; tests override individual blocks."""
    scenario: dict[str, Any] = {
        "id": "test-min",
        "workflow": "test_workflow",
        "description": "minimal test scenario",
        "scenario_now": _SCENARIO_NOW,
        "severity": "blocking",
        "graph_snapshot": {
            "depot": {
                "depot_id": _DEPOT_ID,
                "name": "Test depot",
            },
            "vehicles": [],
            "chargers": [],
            "schedules": [],
            "telemetry": [],
            "prices": [],
            "building_load": [],
            "drivers": [],
        },
        "expected": {
            "status": "all_clear",
            "coverage": {"vehicles_checked": 0, "chargers_checked": 0, "routes_checked": 0},
            "exception_count_max": 0,
        },
    }
    scenario.update(overrides)
    return scenario


# ── Scenario validation ───────────────────────────────────────────────────


def test_load_scenario_parses_yaml() -> None:
    yaml_text = (
        "id: t1\n"
        "workflow: w\n"
        "description: d\n"
        "scenario_now: '2026-01-01T00:00:00Z'\n"
        "severity: info\n"
        "graph_snapshot:\n"
        "  depot:\n"
        f"    depot_id: '{_DEPOT_ID}'\n"
        "expected:\n"
        "  status: all_clear\n"
    )
    scenario = load_scenario(yaml_text)
    assert scenario["id"] == "t1"
    assert scenario["graph_snapshot"]["depot"]["depot_id"] == _DEPOT_ID


def test_load_scenario_rejects_non_mapping() -> None:
    with pytest.raises(ScenarioLoadError, match="mapping at the top level"):
        load_scenario("- not a mapping\n- still not\n")


def test_load_scenario_rejects_missing_keys() -> None:
    with pytest.raises(ScenarioLoadError, match="missing keys"):
        load_scenario("id: incomplete\n")


def test_load_scenario_rejects_bad_status() -> None:
    yaml_text = (
        "id: t\nworkflow: w\ndescription: d\n"
        "scenario_now: '2026-01-01T00:00:00Z'\nseverity: info\n"
        "graph_snapshot:\n  depot:\n    depot_id: '%s'\n"
        "expected:\n  status: not_a_real_status\n" % _DEPOT_ID
    )
    with pytest.raises(ScenarioLoadError, match="expected.status must be one of"):
        load_scenario(yaml_text)


def test_load_scenario_rejects_bad_severity() -> None:
    yaml_text = (
        "id: t\nworkflow: w\ndescription: d\n"
        "scenario_now: '2026-01-01T00:00:00Z'\nseverity: critical\n"
        "graph_snapshot:\n  depot:\n    depot_id: '%s'\n"
        "expected:\n  status: all_clear\n" % _DEPOT_ID
    )
    with pytest.raises(ScenarioLoadError, match="severity must be one of"):
        load_scenario(yaml_text)


def test_load_scenario_rejects_non_mapping_snapshot() -> None:
    yaml_text = (
        "id: t\nworkflow: w\ndescription: d\n"
        "scenario_now: '2026-01-01T00:00:00Z'\nseverity: info\n"
        "graph_snapshot: not a dict\n"
        "expected:\n  status: all_clear\n"
    )
    with pytest.raises(ScenarioLoadError, match="graph_snapshot must be a mapping"):
        load_scenario(yaml_text)


# ── Time resolution ───────────────────────────────────────────────────────


def test_parse_scenario_now_accepts_iso_with_offset() -> None:
    dt = _parse_scenario_now("2026-05-13T05:00:00+00:00")
    assert dt == datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)


def test_parse_scenario_now_accepts_iso_with_z() -> None:
    dt = _parse_scenario_now("2026-05-13T05:00:00Z")
    assert dt.tzinfo == timezone.utc


def test_parse_scenario_now_assumes_utc_for_naive() -> None:
    dt = _parse_scenario_now("2026-05-13T05:00:00")
    assert dt.tzinfo == timezone.utc


def test_parse_scenario_now_accepts_datetime() -> None:
    src = datetime(2026, 1, 1, 12, 0, 0)
    dt = _parse_scenario_now(src)
    assert dt.tzinfo == timezone.utc


def test_parse_scenario_now_rejects_non_string() -> None:
    with pytest.raises(ScenarioLoadError, match="ISO-8601"):
        _parse_scenario_now(12345)


def test_parse_scenario_now_rejects_invalid_string() -> None:
    with pytest.raises(ScenarioLoadError, match="invalid scenario_now"):
        _parse_scenario_now("not-a-date")


def test_resolve_time_iso() -> None:
    base = datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)
    assert _resolve_time("2026-05-13T07:00:00Z", base).hour == 7


def test_resolve_time_seconds_int() -> None:
    base = datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)
    assert _resolve_time(3600, base).hour == 6


def test_resolve_time_seconds_str() -> None:
    base = datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)
    assert _resolve_time("+3600", base).hour == 6
    assert _resolve_time("-1800", base).minute == 30
    assert _resolve_time("-1800", base).hour == 4


@pytest.mark.parametrize(
    "shorthand,expected_delta_seconds",
    [
        ("+2h", 7200),
        ("-15m", -900),
        ("+45s", 45),
        ("-3h", -10800),
    ],
)
def test_resolve_time_duration_suffix(shorthand: str, expected_delta_seconds: int) -> None:
    base = datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)
    resolved = _resolve_time(shorthand, base)
    assert int((resolved - base).total_seconds()) == expected_delta_seconds


def test_resolve_time_passes_through_datetime() -> None:
    base = datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)
    src = datetime(2026, 6, 1, 0, 0, 0)
    assert _resolve_time(src, base).tzinfo == timezone.utc


def test_resolve_time_rejects_null() -> None:
    with pytest.raises(ScenarioLoadError, match="null timestamps"):
        _resolve_time(None, datetime.now(timezone.utc))


def test_resolve_time_rejects_unparseable_string() -> None:
    with pytest.raises(ScenarioLoadError, match="unparseable time value"):
        _resolve_time("definitely-not-iso", datetime.now(timezone.utc))


def test_resolve_time_rejects_unsupported_type() -> None:
    with pytest.raises(ScenarioLoadError, match="unsupported time value type"):
        _resolve_time(object(), datetime.now(timezone.utc))


def test_maybe_resolve_time_returns_none_for_none() -> None:
    assert _maybe_resolve_time(None, datetime.now(timezone.utc)) is None


def test_maybe_resolve_time_resolves_string() -> None:
    base = datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)
    assert _maybe_resolve_time("+1h", base).hour == 6


# ── Snapshot loading ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_snapshot_writes_depot_only() -> None:
    conn = _FakeConnection()
    scenario = _minimal_scenario()
    depot_id = await load_snapshot(conn, scenario)
    assert depot_id == UUID(_DEPOT_ID)
    # Exactly one INSERT (for depots) since every other list is empty.
    insert_sqls = [sql for sql, _ in conn.executes]
    assert len(insert_sqls) == 1
    assert "INSERT INTO depots" in insert_sqls[0]


@pytest.mark.asyncio
async def test_load_snapshot_writes_full_graph() -> None:
    conn = _FakeConnection()
    scenario = _minimal_scenario()
    snapshot = scenario["graph_snapshot"]
    snapshot["vehicles"] = [
        {
            "vehicle_id": _VEHICLE_ID,
            "external_id": "BUS-001",
            "vehicle_type": "bus_large",
            "battery_kwh": 324.0,
            "max_charge_kw": 80.0,
            "id_tag": "TAG-1",
        }
    ]
    snapshot["chargers"] = [
        {
            "charger_id": _CHARGER_ID,
            "ocpp_id": "CP-001",
            "rated_kw": 80.0,
            "efficiency": 0.95,
            "connector_type": "CCS",
            "status": "Available",
        }
    ]
    snapshot["drivers"] = [
        {
            "driver_id": "77777777-7777-4777-8777-777777777777",
            "external_driver_id": "EMP-001",
            "display_name": "Test driver",
            "status": "active",
        }
    ]
    snapshot["schedules"] = [
        {
            "vehicle_id": _VEHICLE_ID,
            "route_id": "R-1",
            "departure_time": "+2h",
            "return_time": "+10h",
            "energy_kwh": 240.0,
            "required_soc": 0.85,
        }
    ]
    snapshot["telemetry"] = [
        {
            "vehicle_id": _VEHICLE_ID,
            "charger_id": _CHARGER_ID,
            "time": "-5m",
            "soc": 0.95,
            "is_plugged": True,
            "charging_kw": 60.0,
        }
    ]
    snapshot["prices"] = [
        {"time": "+1h", "energy_kwh": 0.12, "source": "test"}
    ]
    snapshot["building_load"] = [
        {"time": "+30m", "power_kw": 50.0, "source": "test"}
    ]

    await load_snapshot(conn, scenario)

    tables_seen = {sql.split("INSERT INTO ", 1)[1].split()[0] for sql, _ in conn.executes}
    assert tables_seen == {
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
async def test_load_snapshot_requires_depot() -> None:
    conn = _FakeConnection()
    scenario = _minimal_scenario()
    scenario["graph_snapshot"].pop("depot")
    with pytest.raises(ScenarioLoadError, match="graph_snapshot.depot is required"):
        await load_snapshot(conn, scenario)


@pytest.mark.asyncio
async def test_load_snapshot_uses_defaults_for_optional_fields() -> None:
    """Vehicles with only the required vehicle_id should still load —
    the runner fills external_id with a uuid-derived placeholder."""
    conn = _FakeConnection()
    scenario = _minimal_scenario()
    scenario["graph_snapshot"]["vehicles"] = [{"vehicle_id": _VEHICLE_ID}]
    await load_snapshot(conn, scenario)
    # Find the vehicles insert, confirm a non-empty external_id was passed.
    veh_insert = next(call for call in conn.executes if "INSERT INTO vehicles" in call[0])
    args = veh_insert[1]
    # args order matches columns: vehicle_id, depot_id, external_id, ...
    assert args[2].startswith("EXT-")


@pytest.mark.asyncio
async def test_load_snapshot_rejects_bad_uuid() -> None:
    conn = _FakeConnection()
    scenario = _minimal_scenario()
    scenario["graph_snapshot"]["depot"]["depot_id"] = "not-a-uuid"
    with pytest.raises(ScenarioLoadError, match="invalid UUID"):
        await load_snapshot(conn, scenario)


@pytest.mark.asyncio
async def test_load_snapshot_rejects_non_uuid_type() -> None:
    conn = _FakeConnection()
    scenario = _minimal_scenario()
    scenario["graph_snapshot"]["depot"]["depot_id"] = 12345
    with pytest.raises(ScenarioLoadError, match="expected UUID"):
        await load_snapshot(conn, scenario)


# ── Expected-block assertion ──────────────────────────────────────────────


def _result_actual(output: dict) -> EvalResult:
    """Convenience builder used by the assertion tests below."""
    return EvalResult(
        scenario_id="t",
        workflow="w",
        passed=True,
        severity="info",
        decision=None,
        expected={},
        actual=output,
    )


@pytest.mark.asyncio
async def test_run_scenario_pass_all_clear() -> None:
    """A workflow that returns an all-clear output passes the scenario."""

    async def handler(_ctx: WorkflowContext) -> dict[str, Any]:
        return {
            "status": "all_clear",
            "coverage": {"vehicles_checked": 0, "chargers_checked": 0, "routes_checked": 0},
            "exceptions": [],
        }

    registry = workflow_registry()
    register_workflow("__test_all_clear", handler, registry=registry)
    try:
        agent = WorkflowAgent(registry=registry)
        pool = _FakePool()
        scenario = _minimal_scenario(workflow="__test_all_clear")
        result = await run_scenario(scenario, pool=pool, agent=agent)
    finally:
        registry.unregister("__test_all_clear")

    assert result.passed
    assert result.failures == []
    assert pool.conn.transactions[0].started
    assert pool.conn.transactions[0].rolled_back


@pytest.mark.asyncio
async def test_run_scenario_fail_on_status_mismatch() -> None:
    async def handler(_ctx: WorkflowContext) -> dict[str, Any]:
        return {"status": "exceptions_present", "exceptions": []}

    registry = workflow_registry()
    register_workflow("__test_status_mismatch", handler, registry=registry)
    try:
        result = await run_scenario(
            _minimal_scenario(workflow="__test_status_mismatch"),
            pool=_FakePool(),
            agent=WorkflowAgent(registry=registry),
        )
    finally:
        registry.unregister("__test_status_mismatch")

    assert not result.passed
    assert any("status:" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_fail_on_coverage_mismatch() -> None:
    async def handler(_ctx: WorkflowContext) -> dict[str, Any]:
        return {
            "status": "all_clear",
            "coverage": {"vehicles_checked": 5, "chargers_checked": 0, "routes_checked": 0},
            "exceptions": [],
        }

    registry = workflow_registry()
    register_workflow("__test_coverage_mismatch", handler, registry=registry)
    try:
        result = await run_scenario(
            _minimal_scenario(workflow="__test_coverage_mismatch"),
            pool=_FakePool(),
            agent=WorkflowAgent(registry=registry),
        )
    finally:
        registry.unregister("__test_coverage_mismatch")

    assert not result.passed
    assert any("coverage.vehicles_checked" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_fail_on_too_many_exceptions() -> None:
    async def handler(_ctx: WorkflowContext) -> dict[str, Any]:
        return {
            "status": "exceptions_present",
            "coverage": {"vehicles_checked": 0, "chargers_checked": 0, "routes_checked": 0},
            "exceptions": [
                {"vehicle_id": str(uuid4()), "issue": "x"} for _ in range(3)
            ],
        }

    scenario = _minimal_scenario()
    scenario["expected"]["status"] = "exceptions_present"
    scenario["expected"]["exception_count_max"] = 1

    registry = workflow_registry()
    register_workflow("__test_too_many", handler, registry=registry)
    try:
        result = await run_scenario(
            {**scenario, "workflow": "__test_too_many"},
            pool=_FakePool(),
            agent=WorkflowAgent(registry=registry),
        )
    finally:
        registry.unregister("__test_too_many")

    assert not result.passed
    assert any("> max" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_fail_on_too_few_exceptions() -> None:
    async def handler(_ctx: WorkflowContext) -> dict[str, Any]:
        return {
            "status": "exceptions_present",
            "coverage": {"vehicles_checked": 0, "chargers_checked": 0, "routes_checked": 0},
            "exceptions": [],
        }

    scenario = _minimal_scenario()
    scenario["expected"]["exception_count_min"] = 1

    registry = workflow_registry()
    register_workflow("__test_too_few", handler, registry=registry)
    try:
        result = await run_scenario(
            {**scenario, "workflow": "__test_too_few"},
            pool=_FakePool(),
            agent=WorkflowAgent(registry=registry),
        )
    finally:
        registry.unregister("__test_too_few")

    assert not result.passed
    assert any("< min" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_must_include_match() -> None:
    vid = str(uuid4())

    async def handler(_ctx: WorkflowContext) -> dict[str, Any]:
        return {
            "status": "exceptions_present",
            "coverage": {"vehicles_checked": 0, "chargers_checked": 0, "routes_checked": 0},
            "exceptions": [{"vehicle_id": vid, "issue": "Faulted charger detected"}],
        }

    scenario = _minimal_scenario()
    scenario["expected"] = {
        "status": "exceptions_present",
        "exception_count_min": 1,
        "must_include_exceptions": [
            {"vehicle_id": vid, "issue_contains": "faulted"}  # case-insensitive
        ],
    }

    registry = workflow_registry()
    register_workflow("__test_must_include", handler, registry=registry)
    try:
        result = await run_scenario(
            {**scenario, "workflow": "__test_must_include"},
            pool=_FakePool(),
            agent=WorkflowAgent(registry=registry),
        )
    finally:
        registry.unregister("__test_must_include")

    assert result.passed


@pytest.mark.asyncio
async def test_run_scenario_must_include_miss() -> None:
    async def handler(_ctx: WorkflowContext) -> dict[str, Any]:
        return {
            "status": "exceptions_present",
            "coverage": {"vehicles_checked": 0, "chargers_checked": 0, "routes_checked": 0},
            "exceptions": [{"vehicle_id": str(uuid4()), "issue": "something else"}],
        }

    scenario = _minimal_scenario()
    scenario["expected"]["status"] = "exceptions_present"
    scenario["expected"]["exception_count_min"] = 1
    scenario["expected"]["must_include_exceptions"] = [
        {"vehicle_id": str(uuid4()), "issue_contains": "faulted"}
    ]

    registry = workflow_registry()
    register_workflow("__test_must_include_miss", handler, registry=registry)
    try:
        result = await run_scenario(
            {**scenario, "workflow": "__test_must_include_miss"},
            pool=_FakePool(),
            agent=WorkflowAgent(registry=registry),
        )
    finally:
        registry.unregister("__test_must_include_miss")

    assert not result.passed
    assert any("must_include_exceptions" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_must_not_include_violation() -> None:
    vid = str(uuid4())

    async def handler(_ctx: WorkflowContext) -> dict[str, Any]:
        return {
            "status": "exceptions_present",
            "coverage": {"vehicles_checked": 0, "chargers_checked": 0, "routes_checked": 0},
            "exceptions": [{"vehicle_id": vid, "issue": "any"}],
        }

    scenario = _minimal_scenario()
    scenario["expected"]["status"] = "exceptions_present"
    scenario["expected"]["exception_count_min"] = 1
    scenario["expected"]["must_not_include"] = [{"vehicle_id": vid}]

    registry = workflow_registry()
    register_workflow("__test_must_not_include", handler, registry=registry)
    try:
        result = await run_scenario(
            {**scenario, "workflow": "__test_must_not_include"},
            pool=_FakePool(),
            agent=WorkflowAgent(registry=registry),
        )
    finally:
        registry.unregister("__test_must_not_include")

    assert not result.passed
    assert any("must_not_include" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_action_allow_list_violation() -> None:
    async def handler(_ctx: WorkflowContext) -> dict[str, Any]:
        return {
            "status": "exceptions_present",
            "coverage": {"vehicles_checked": 0, "chargers_checked": 0, "routes_checked": 0},
            "exceptions": [
                {
                    "vehicle_id": str(uuid4()),
                    "issue": "any",
                    "proposed_action": {"type": "do_a_barrel_roll"},
                }
            ],
        }

    scenario = _minimal_scenario()
    scenario["expected"]["status"] = "exceptions_present"
    scenario["expected"]["exception_count_min"] = 1
    scenario["expected"]["action_type_allow_list"] = ["swap_charger", "reassign_to_route"]

    registry = workflow_registry()
    register_workflow("__test_action_allow", handler, registry=registry)
    try:
        result = await run_scenario(
            {**scenario, "workflow": "__test_action_allow"},
            pool=_FakePool(),
            agent=WorkflowAgent(registry=registry),
        )
    finally:
        registry.unregister("__test_action_allow")

    assert not result.passed
    assert any("action_type_allow_list" in f for f in result.failures)


@pytest.mark.asyncio
async def test_run_scenario_action_allow_list_passes_when_within_list() -> None:
    async def handler(_ctx: WorkflowContext) -> dict[str, Any]:
        return {
            "status": "exceptions_present",
            "coverage": {"vehicles_checked": 0, "chargers_checked": 0, "routes_checked": 0},
            "exceptions": [
                {
                    "vehicle_id": str(uuid4()),
                    "issue": "any",
                    "proposed_action": {"type": "swap_charger"},
                }
            ],
        }

    scenario = _minimal_scenario()
    scenario["expected"] = {
        "status": "exceptions_present",
        "exception_count_min": 1,
        "action_type_allow_list": ["swap_charger"],
    }

    registry = workflow_registry()
    register_workflow("__test_action_allow_ok", handler, registry=registry)
    try:
        result = await run_scenario(
            {**scenario, "workflow": "__test_action_allow_ok"},
            pool=_FakePool(),
            agent=WorkflowAgent(registry=registry),
        )
    finally:
        registry.unregister("__test_action_allow_ok")

    assert result.passed


# ── Transaction handling ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_scenario_rolls_back_on_failure() -> None:
    """Even when the assertion fails the transaction must roll back."""

    async def handler(_ctx: WorkflowContext) -> dict[str, Any]:
        return {"status": "exceptions_present", "exceptions": []}

    registry = workflow_registry()
    register_workflow("__test_rollback_on_fail", handler, registry=registry)
    try:
        pool = _FakePool()
        await run_scenario(
            _minimal_scenario(workflow="__test_rollback_on_fail"),
            pool=pool,
            agent=WorkflowAgent(registry=registry),
        )
    finally:
        registry.unregister("__test_rollback_on_fail")

    assert pool.conn.transactions[0].rolled_back


@pytest.mark.asyncio
async def test_run_scenario_rolls_back_on_exception() -> None:
    """If the workflow raises, the transaction still rolls back."""

    async def handler(_ctx: WorkflowContext) -> dict[str, Any]:
        raise RuntimeError("workflow blew up")

    registry = workflow_registry()
    register_workflow("__test_rollback_on_raise", handler, registry=registry)
    pool = _FakePool()
    try:
        with pytest.raises(RuntimeError, match="workflow blew up"):
            await run_scenario(
                _minimal_scenario(workflow="__test_rollback_on_raise"),
                pool=pool,
                agent=WorkflowAgent(registry=registry),
            )
    finally:
        registry.unregister("__test_rollback_on_raise")

    assert pool.conn.transactions[0].rolled_back


@pytest.mark.asyncio
async def test_run_scenario_uses_scenario_now_for_ctx() -> None:
    captured: dict[str, Any] = {}

    async def handler(ctx: WorkflowContext) -> dict[str, Any]:
        captured["now"] = ctx.now
        captured["depot_id"] = ctx.depot_id
        captured["visible_depot_ids"] = ctx.visible_depot_ids
        captured["parameters"] = ctx.parameters
        return {
            "status": "all_clear",
            "coverage": {"vehicles_checked": 0, "chargers_checked": 0, "routes_checked": 0},
            "exceptions": [],
        }

    registry = workflow_registry()
    register_workflow("__test_ctx_now", handler, registry=registry)
    try:
        await run_scenario(
            _minimal_scenario(workflow="__test_ctx_now"),
            pool=_FakePool(),
            agent=WorkflowAgent(registry=registry),
            parameters={"lead_time_minutes": 60},
        )
    finally:
        registry.unregister("__test_ctx_now")

    assert captured["now"] == datetime(2026, 5, 13, 5, 0, 0, tzinfo=timezone.utc)
    assert captured["depot_id"] == UUID(_DEPOT_ID)
    assert captured["visible_depot_ids"] == [UUID(_DEPOT_ID)]
    assert captured["parameters"] == {"lead_time_minutes": 60}


@pytest.mark.asyncio
async def test_run_scenario_unknown_workflow_raises() -> None:
    with pytest.raises(WorkflowNotRegisteredError):
        await run_scenario(
            _minimal_scenario(workflow="not_registered"),
            pool=_FakePool(),
        )


@pytest.mark.asyncio
async def test_run_scenario_validates_before_acquiring_pool() -> None:
    """Bad scenarios should not even touch the pool."""
    pool = _FakePool()
    bad = _minimal_scenario()
    bad.pop("severity")
    with pytest.raises(ScenarioLoadError):
        await run_scenario(bad, pool=pool)
    assert pool.acquire_calls == 0


# ── Diff rendering ────────────────────────────────────────────────────────


def test_diff_actual_vs_expected_renders_unified_diff() -> None:
    diff = diff_actual_vs_expected(
        {"status": "all_clear"},
        {"status": "exceptions_present"},
    )
    assert "--- expected" in diff
    assert "+++ actual" in diff
    assert "-" in diff and "+" in diff
    assert "all_clear" in diff
    assert "exceptions_present" in diff


def test_diff_empty_when_equal() -> None:
    assert diff_actual_vs_expected({"a": 1}, {"a": 1}) == ""


def test_eval_result_diff_helper_delegates() -> None:
    result = EvalResult(
        scenario_id="t",
        workflow="w",
        passed=False,
        severity="blocking",
        decision=None,
        expected={"status": "all_clear"},
        actual={"status": "exceptions_present"},
    )
    diff = result.diff()
    assert "all_clear" in diff and "exceptions_present" in diff


def test_stable_json_is_sorted() -> None:
    payload = _stable_json({"b": 2, "a": 1})
    # Keys appear in sorted order.
    assert payload.index('"a"') < payload.index('"b"')


# ── _TxPool adapter ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tx_pool_delegates_fetch() -> None:
    conn = _FakeConnection()
    conn.fetch_responses.append([{"x": 1}])
    pool = _TxPool(conn)
    rows = await pool.fetch("SELECT 1")
    assert rows == [{"x": 1}]
    assert conn.fetches[-1][0] == "SELECT 1"


@pytest.mark.asyncio
async def test_tx_pool_delegates_fetchrow() -> None:
    conn = _FakeConnection()
    conn.fetchrow_responses.append({"x": 2})
    pool = _TxPool(conn)
    row = await pool.fetchrow("SELECT 2")
    assert row == {"x": 2}


@pytest.mark.asyncio
async def test_tx_pool_delegates_fetchval_and_execute() -> None:
    conn = _FakeConnection()
    pool = _TxPool(conn)
    await pool.fetchval("SELECT 3")
    await pool.execute("INSERT", 1, 2)
    assert any("SELECT 3" in s for s, _ in conn.fetches)
    assert any("INSERT" in s for s, _ in conn.executes)


@pytest.mark.asyncio
async def test_tx_pool_acquire_yields_same_connection() -> None:
    conn = _FakeConnection()
    pool = _TxPool(conn)
    async with pool.acquire() as got:
        assert got is conn


@pytest.mark.asyncio
async def test_acquire_context_aexit_returns_none() -> None:
    conn = _FakeConnection()
    ctx = _AcquireContext(conn)
    async with ctx as got:
        assert got is conn


# ── Runtime registry behaviour (touched by the runner) ────────────────────


def test_registry_idempotent_re_register_same_handler() -> None:
    registry = workflow_registry()

    async def h(_c: WorkflowContext) -> dict[str, Any]:
        return {}

    register_workflow("__test_idempotent", h, registry=registry)
    # Re-register with the same handler is a no-op, not a raise.
    register_workflow("__test_idempotent", h, registry=registry)
    registry.unregister("__test_idempotent")


def test_registry_rejects_handler_collision() -> None:
    registry = workflow_registry()

    async def h1(_c: WorkflowContext) -> dict[str, Any]:
        return {}

    async def h2(_c: WorkflowContext) -> dict[str, Any]:
        return {}

    register_workflow("__test_collision", h1, registry=registry)
    try:
        with pytest.raises(ValueError, match="already registered"):
            register_workflow("__test_collision", h2, registry=registry)
    finally:
        registry.unregister("__test_collision")


def test_registry_snapshot_and_names() -> None:
    registry = workflow_registry()

    async def h(_c: WorkflowContext) -> dict[str, Any]:
        return {}

    register_workflow("__test_snap", h, registry=registry)
    try:
        snap = registry.snapshot()
        assert "__test_snap" in snap
        assert "__test_snap" in registry.names()
    finally:
        registry.unregister("__test_snap")


def test_workflow_agent_returns_decision() -> None:
    """Sanity-check: the agent wraps the handler return in a Decision."""
    registry = workflow_registry()

    async def h(ctx: WorkflowContext) -> dict[str, Any]:
        return {"hello": "world", "depot": str(ctx.depot_id)}

    register_workflow("__test_decision", h, registry=registry)
    try:
        agent = WorkflowAgent(registry=registry)
        depot_id = uuid4()
        ctx = WorkflowContext(
            depot_id=depot_id,
            now=datetime(2026, 1, 1, tzinfo=timezone.utc),
            visible_depot_ids=[depot_id],
            static_pool=None,
            ts_pool=None,
        )
        import asyncio

        decision = asyncio.run(agent.run_turn("__test_decision", ctx))
    finally:
        registry.unregister("__test_decision")

    assert isinstance(decision, Decision)
    assert decision.workflow_name == "__test_decision"
    assert decision.output["hello"] == "world"
    assert decision.depot_id == depot_id


# ── Example scenarios round-trip ──────────────────────────────────────────


def test_example_scenarios_load_cleanly() -> None:
    """Every YAML under tests/golden/workflows/_examples/ must parse and
    validate. Catches scenario authoring regressions in plain pytest
    (no DB required)."""
    from pathlib import Path

    examples_dir = (
        Path(__file__).parent.parent.parent / "golden" / "workflows" / "_examples"
    )
    paths = sorted(examples_dir.glob("*.yaml"))
    assert paths, "expected at least one example scenario"
    for path in paths:
        scenario = load_scenario(path.read_text(encoding="utf-8"))
        assert scenario["id"] == path.stem
        # severity is one of the allowed values; runner.load_scenario
        # already validated this — assert here for self-documentation.
        assert scenario["severity"] in {"blocking", "warning", "info"}
