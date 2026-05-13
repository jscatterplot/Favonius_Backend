"""Scenario runner for the workflow evaluation harness.

Aligned with the sprint-1+2 substrate already on ``main``:

* Drives :class:`~src.api.agent_workflows.runtime.WorkflowAgent.run_turn`
  directly — no parallel "workflow context" abstraction.
* Builds a :class:`~src.api.agent_workflows.models.Workflow` (Pydantic),
  an :class:`~src.api.agent.auth_context.AuthContext`, and a
  per-scenario :class:`~src.api.agent_workflows.tools.ToolRegistry`
  from the YAML.
* Hands a :class:`FakeAnthropicClient` to the runtime that replays the
  scenario's ``llm_trace`` so the gate does not need a real LLM call.
* Loads ``graph_snapshot`` into a transaction on a single asyncpg
  connection. The harness's tool callables read from that same
  connection (the workflow sees only the snapshot rows), and the
  transaction is always rolled back — pass or fail.
* Asserts ``Decision.output`` + ``Decision.tool_calls`` against the
  ``expected`` block. ``Decision.id``, ``Decision.timestamp``, and
  ``Decision.inputs_hash`` are non-deterministic and excluded from
  every assertion.

The fake Anthropic client lives here rather than in tests/ because the
example scenarios under ``tests/golden/workflows/_examples/`` and the
runner's own unit tests both consume it.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from uuid import UUID, uuid4

from src.api.agent.auth_context import AuthContext
from src.api.agent_workflows.constraints import DepotConstraints
from src.api.agent_workflows.models import (
    Decision,
    PermissionTier,
    ToolCall,
    Workflow,
)
from src.api.agent_workflows.runtime import (
    EMIT_DECISION_TOOL_NAME,
    WorkflowAgent,
)
from src.api.agent_workflows.tools import ToolRegistry


# ── Errors ────────────────────────────────────────────────────────────────


class ScenarioLoadError(ValueError):
    """Raised when a scenario fails validation or DB loading."""


# ── Public types ──────────────────────────────────────────────────────────


@dataclass
class EvalResult:
    """Outcome of running one scenario.

    A ``passed=False`` result carries a unified diff (via :meth:`diff`)
    so a pytest failure can print exactly which assertion fired.
    """

    scenario_id: str
    workflow: str
    passed: bool
    severity: str
    decision: Optional[Decision]
    failures: list[str] = field(default_factory=list)
    expected: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)

    def diff(self) -> str:
        return diff_actual_vs_expected(self.expected, self.actual)


# ── Scenario validation ───────────────────────────────────────────────────


_REQUIRED_TOP_LEVEL = {
    "id",
    "description",
    "scenario_now",
    "workflow",
    "graph_snapshot",
    "llm_trace",
    "expected",
    "severity",
}

_ALLOWED_SEVERITY = {"blocking", "warning", "info"}
_ALLOWED_TIER = {t.value for t in PermissionTier}


def _validate_scenario(scenario: dict) -> None:
    missing = _REQUIRED_TOP_LEVEL - scenario.keys()
    if missing:
        raise ScenarioLoadError(
            f"scenario {scenario.get('id', '<unknown>')!r}: missing keys "
            f"{sorted(missing)!r}"
        )

    if scenario["severity"] not in _ALLOWED_SEVERITY:
        raise ScenarioLoadError(
            f"scenario {scenario['id']!r}: severity must be one of "
            f"{sorted(_ALLOWED_SEVERITY)}, got {scenario['severity']!r}"
        )

    tier = scenario.get("permission_tier")
    if tier is not None and tier not in _ALLOWED_TIER:
        raise ScenarioLoadError(
            f"scenario {scenario['id']!r}: permission_tier must be one of "
            f"{sorted(_ALLOWED_TIER)}, got {tier!r}"
        )

    snapshot = scenario["graph_snapshot"]
    if not isinstance(snapshot, dict):
        raise ScenarioLoadError(
            f"scenario {scenario['id']!r}: graph_snapshot must be a mapping"
        )

    workflow = scenario["workflow"]
    if not isinstance(workflow, dict):
        raise ScenarioLoadError(
            f"scenario {scenario['id']!r}: workflow must be a mapping"
        )
    for key in ("id", "name", "version", "prompt", "allowed_tools"):
        if key not in workflow:
            raise ScenarioLoadError(
                f"scenario {scenario['id']!r}: workflow.{key} is required"
            )

    trace = scenario["llm_trace"]
    if not isinstance(trace, list) or not trace:
        raise ScenarioLoadError(
            f"scenario {scenario['id']!r}: llm_trace must be a non-empty list"
        )
    for i, turn in enumerate(trace):
        if not isinstance(turn, dict) or "content" not in turn:
            raise ScenarioLoadError(
                f"scenario {scenario['id']!r}: llm_trace[{i}] missing 'content'"
            )


# ── Time resolution ───────────────────────────────────────────────────────


_DURATION_RE = re.compile(r"^([+-])(\d+)([smh])$")


def _parse_scenario_now(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str):
        raise ScenarioLoadError(f"scenario_now must be ISO-8601 string, got {type(raw).__name__}")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScenarioLoadError(f"invalid scenario_now: {raw!r} ({exc})") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _resolve_time(value: Any, scenario_now: datetime) -> datetime:
    """Resolve an ISO string / numeric offset / duration suffix to UTC datetime."""
    if value is None:
        raise ScenarioLoadError("time value is None — scenarios may not use null timestamps")

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, (int, float)):
        return scenario_now + timedelta(seconds=float(value))

    if isinstance(value, str):
        match = _DURATION_RE.match(value)
        if match:
            sign, magnitude, unit = match.groups()
            seconds = int(magnitude) * {"s": 1, "m": 60, "h": 3600}[unit]
            if sign == "-":
                seconds = -seconds
            return scenario_now + timedelta(seconds=seconds)

        if value.startswith(("+", "-")) and value.lstrip("+-").isdigit():
            return scenario_now + timedelta(seconds=int(value))

        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ScenarioLoadError(f"unparseable time value {value!r}: {exc}") from exc
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    raise ScenarioLoadError(f"unsupported time value type {type(value).__name__}: {value!r}")


# ── Snapshot loading ──────────────────────────────────────────────────────


def _coerce_uuid(value: Any, *, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise ScenarioLoadError(f"{field_name}: invalid UUID {value!r}") from exc
    raise ScenarioLoadError(f"{field_name}: expected UUID, got {type(value).__name__}")


async def _insert_depot(conn: Any, depot: dict) -> UUID:
    depot_id = _coerce_uuid(depot["depot_id"], field_name="depot.depot_id")
    await conn.execute(
        """
        INSERT INTO depots (depot_id, name, latitude, longitude, max_grid_kw, timezone)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        depot_id,
        depot.get("name", "Eval depot"),
        depot.get("latitude", 54.6872),
        depot.get("longitude", 25.2797),
        float(depot.get("max_grid_kw", 800.0)),
        depot.get("timezone", "UTC"),
    )
    return depot_id


async def _insert_vehicles(conn: Any, vehicles: Iterable[dict], depot_id: UUID) -> None:
    for v in vehicles:
        await conn.execute(
            """
            INSERT INTO vehicles (
                vehicle_id, depot_id, external_id, vehicle_type,
                battery_kwh, max_charge_kw, id_tag
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            _coerce_uuid(v["vehicle_id"], field_name="vehicle.vehicle_id"),
            depot_id,
            v.get("external_id") or f"EXT-{uuid4().hex[:8]}",
            v.get("vehicle_type", "bus_large"),
            float(v.get("battery_kwh", 324.0)),
            float(v.get("max_charge_kw", 80.0)),
            v.get("id_tag"),
        )


async def _insert_chargers(conn: Any, chargers: Iterable[dict], depot_id: UUID) -> None:
    for c in chargers:
        await conn.execute(
            """
            INSERT INTO chargers (
                charger_id, depot_id, ocpp_id, rated_kw,
                efficiency, connector_type, status
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            _coerce_uuid(c["charger_id"], field_name="charger.charger_id"),
            depot_id,
            c.get("ocpp_id") or f"CP-{uuid4().hex[:8]}",
            float(c.get("rated_kw", 80.0)),
            float(c.get("efficiency", 0.95)),
            c.get("connector_type", "CCS"),
            c.get("status", "Available"),
        )


async def _insert_drivers(conn: Any, drivers: Iterable[dict], depot_id: UUID) -> None:
    for d in drivers:
        await conn.execute(
            """
            INSERT INTO drivers (
                driver_id, depot_id, external_driver_id, display_name, status
            )
            VALUES ($1, $2, $3, $4, $5)
            """,
            _coerce_uuid(d["driver_id"], field_name="driver.driver_id"),
            depot_id,
            d.get("external_driver_id"),
            d.get("display_name", "Eval driver"),
            d.get("status", "active"),
        )


async def _insert_schedules(
    conn: Any, schedules: Iterable[dict], scenario_now: datetime
) -> None:
    for s in schedules:
        await conn.execute(
            """
            INSERT INTO schedules (
                schedule_id, vehicle_id, route_id,
                departure_time, return_time,
                energy_kwh, required_soc
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            _coerce_uuid(s.get("schedule_id") or uuid4(), field_name="schedule.schedule_id"),
            _coerce_uuid(s["vehicle_id"], field_name="schedule.vehicle_id"),
            s.get("route_id", "R-0"),
            _resolve_time(s["departure_time"], scenario_now),
            _resolve_time(s["return_time"], scenario_now),
            float(s.get("energy_kwh", 0.0)),
            float(s.get("required_soc", 0.99)),
        )


async def _insert_telemetry(
    conn: Any, samples: Iterable[dict], scenario_now: datetime
) -> None:
    for sample in samples:
        charger_raw = sample.get("charger_id")
        charger_id = (
            _coerce_uuid(charger_raw, field_name="telemetry.charger_id")
            if charger_raw is not None
            else None
        )
        await conn.execute(
            """
            INSERT INTO telemetry (
                time, vehicle_id, charger_id, soc, is_plugged, charging_kw
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            _resolve_time(sample.get("time", scenario_now), scenario_now),
            _coerce_uuid(sample["vehicle_id"], field_name="telemetry.vehicle_id"),
            charger_id,
            float(sample["soc"]),
            bool(sample.get("is_plugged", False)),
            float(sample.get("charging_kw", 0.0)),
        )


async def _insert_prices(
    conn: Any, prices: Iterable[dict], depot_id: UUID, scenario_now: datetime
) -> None:
    for p in prices:
        await conn.execute(
            """
            INSERT INTO prices (time, depot_id, energy_kwh, source)
            VALUES ($1, $2, $3, $4)
            """,
            _resolve_time(p["time"], scenario_now),
            depot_id,
            float(p["energy_kwh"]),
            p.get("source", "scenario"),
        )


async def _insert_building_load(
    conn: Any, samples: Iterable[dict], depot_id: UUID, scenario_now: datetime
) -> None:
    for s in samples:
        await conn.execute(
            """
            INSERT INTO building_load (time, depot_id, power_kw, source)
            VALUES ($1, $2, $3, $4)
            """,
            _resolve_time(s["time"], scenario_now),
            depot_id,
            float(s["power_kw"]),
            s.get("source", "scenario"),
        )


async def load_snapshot(conn: Any, scenario: dict) -> UUID:
    """Insert ``graph_snapshot`` rows on the given (open-transaction) connection.

    The caller owns the transaction lifecycle. :func:`run_scenario`
    handles that; callers using ``load_snapshot`` directly must too.
    """
    _validate_scenario(scenario)
    scenario_now = _parse_scenario_now(scenario["scenario_now"])
    snapshot = scenario["graph_snapshot"]

    depot = snapshot.get("depot")
    if depot is None:
        raise ScenarioLoadError(
            f"scenario {scenario['id']!r}: graph_snapshot.depot is required"
        )

    depot_id = await _insert_depot(conn, depot)
    await _insert_drivers(conn, snapshot.get("drivers", []), depot_id)
    await _insert_vehicles(conn, snapshot.get("vehicles", []), depot_id)
    await _insert_chargers(conn, snapshot.get("chargers", []), depot_id)
    await _insert_schedules(conn, snapshot.get("schedules", []), scenario_now)
    await _insert_telemetry(conn, snapshot.get("telemetry", []), scenario_now)
    await _insert_prices(conn, snapshot.get("prices", []), depot_id, scenario_now)
    await _insert_building_load(
        conn, snapshot.get("building_load", []), depot_id, scenario_now
    )
    return depot_id


# ── Fake Anthropic client + content blocks ────────────────────────────────


class _Block:
    """Anthropic-style content block.

    The runtime treats blocks duck-typed: it reads ``.type``, ``.text``,
    ``.id``, ``.name``, ``.input``. A plain class with attributes is the
    smallest fake that exercises every read site.
    """

    def __init__(
        self,
        *,
        type: str,
        text: str = "",
        id: str = "",
        name: str = "",
        input: Optional[dict[str, Any]] = None,
    ) -> None:
        self.type = type
        self.text = text
        self.id = id
        self.name = name
        self.input = dict(input or {})


class _Usage:
    """Anthropic-style usage record."""

    def __init__(self, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    """Anthropic-style message response."""

    def __init__(
        self,
        *,
        content: list[_Block],
        stop_reason: str = "tool_use",
        usage: Optional[_Usage] = None,
    ) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


class _MessagesFake:
    """The ``client.messages`` facade used by the runtime."""

    def __init__(self, responses: list[_Response]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _Response:
        # Record what the runtime asked for so tests can assert that
        # tools, system prompt, etc., are wired correctly.
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError(
                "FakeAnthropicClient exhausted: scenario llm_trace too short "
                f"(call #{len(self.calls)} unmet)"
            )
        return self._responses.pop(0)


class FakeAnthropicClient:
    """Replay a canned ``llm_trace`` deterministically.

    Public surface matches the :class:`~src.api.agent_workflows.runtime._ClientFacade`
    Protocol — a top-level object with a ``messages`` attribute whose
    ``create`` is awaitable. Constructing one is cheap; tests reuse
    the same client across turns by extending the trace.
    """

    def __init__(self, trace: list[dict[str, Any]]) -> None:
        responses = [_response_from_trace_entry(entry) for entry in trace]
        self.messages = _MessagesFake(responses)


def _response_from_trace_entry(entry: dict[str, Any]) -> _Response:
    blocks: list[_Block] = []
    for raw in entry.get("content") or []:
        block_type = raw.get("type")
        if block_type == "text":
            blocks.append(_Block(type="text", text=raw.get("text", "")))
        elif block_type == "tool_use":
            blocks.append(
                _Block(
                    type="tool_use",
                    id=raw.get("id") or f"toolu_{uuid4().hex[:12]}",
                    name=raw.get("name", ""),
                    input=raw.get("input") or {},
                )
            )
        else:
            raise ScenarioLoadError(
                f"llm_trace content block has unsupported type {block_type!r}"
            )

    usage_raw = entry.get("usage")
    usage = None
    if usage_raw is not None:
        usage = _Usage(
            input_tokens=int(usage_raw.get("input_tokens") or 0),
            output_tokens=int(usage_raw.get("output_tokens") or 0),
        )

    return _Response(
        content=blocks,
        stop_reason=entry.get("stop_reason", "tool_use"),
        usage=usage,
    )


# ── Tool registry + harness tools ─────────────────────────────────────────


def _build_default_tool_registry(conn: Any, depot_id: UUID) -> ToolRegistry:
    """Build a ToolRegistry whose callables read from the open transaction.

    Sprint 5 will add real workflow-specific tools; for now the harness
    ships a small set of read-only tools that match the readiness
    workflow's PRD §6.1 shape. Each callable issues a query against the
    runner's open connection so the workflow sees the snapshot data
    and nothing else.

    The names here mirror PRD §6.1's tool list. When a scenario's
    ``workflow.allowed_tools`` lists a name not in this registry,
    the runtime raises :class:`ToolNotRegisteredError` and the scenario
    fails — which is exactly the protection the harness wants.
    """
    registry = ToolRegistry()

    async def get_scheduled_departures() -> dict[str, Any]:
        rows = await conn.fetch(
            """
            SELECT s.vehicle_id::text AS vehicle_id,
                   s.route_id,
                   s.departure_time::text AS departure_time,
                   s.required_soc
            FROM schedules s
            JOIN vehicles v ON v.vehicle_id = s.vehicle_id
            WHERE v.depot_id = $1
            ORDER BY s.departure_time
            """,
            depot_id,
        )
        return {"departures": [dict(r) for r in rows]}

    async def get_vehicle_state(vehicle_id: str) -> dict[str, Any]:
        vid = _coerce_uuid(vehicle_id, field_name="get_vehicle_state.vehicle_id")
        row = await conn.fetchrow(
            """
            SELECT soc, charger_id::text AS charger_id, is_plugged, charging_kw
            FROM telemetry
            WHERE vehicle_id = $1
            ORDER BY time DESC
            LIMIT 1
            """,
            vid,
        )
        if row is None:
            return {"vehicle_id": str(vid), "soc": None}
        return {"vehicle_id": str(vid), **dict(row)}

    async def get_charger_state(charger_id: str) -> dict[str, Any]:
        cid = _coerce_uuid(charger_id, field_name="get_charger_state.charger_id")
        row = await conn.fetchrow(
            "SELECT ocpp_id, status, rated_kw FROM chargers WHERE charger_id = $1",
            cid,
        )
        if row is None:
            return {"charger_id": str(cid), "status": "unknown"}
        return {"charger_id": str(cid), **dict(row)}

    registry.register(
        "get_scheduled_departures",
        description="List today's scheduled departures for the depot.",
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        fn=get_scheduled_departures,
    )
    registry.register(
        "get_vehicle_state",
        description="Latest SoC/plug/charger for one vehicle.",
        input_schema={
            "type": "object",
            "properties": {"vehicle_id": {"type": "string"}},
            "required": ["vehicle_id"],
        },
        fn=get_vehicle_state,
    )
    registry.register(
        "get_charger_state",
        description="OCPP status + rated power for one charger.",
        input_schema={
            "type": "object",
            "properties": {"charger_id": {"type": "string"}},
            "required": ["charger_id"],
        },
        fn=get_charger_state,
    )
    return registry


# ── Auth context builder ──────────────────────────────────────────────────


_DEFAULT_USER_ID = UUID("aa000000-0000-4000-8000-0000000000aa")
_DEFAULT_ORG_ID = UUID("bb000000-0000-4000-8000-0000000000bb")


def _build_auth_context(scenario: dict, depot_id: UUID) -> AuthContext:
    """Build the AuthContext the runtime needs.

    Scenarios may pin user_id/organization_id/role explicitly; absent
    fields fall back to stable defaults so an author doesn't have to
    repeat auth boilerplate.
    """
    auth_raw = scenario.get("auth") or {}
    user_id = _coerce_uuid(auth_raw.get("user_id", _DEFAULT_USER_ID), field_name="auth.user_id")
    organization_id = _coerce_uuid(
        auth_raw.get("organization_id", _DEFAULT_ORG_ID), field_name="auth.organization_id"
    )
    role = auth_raw.get("role", "customer_operator")
    return AuthContext(
        user_id=user_id,
        organization_id=organization_id,
        role=role,
        visible_depot_ids=[depot_id],
    )


# ── Expected-block assertion ──────────────────────────────────────────────


def _check_disposition(expected: dict, decision: Decision, failures: list[str]) -> None:
    want = expected.get("disposition")
    if want is not None and decision.disposition.value != want:
        failures.append(
            f"disposition: expected {want!r}, got {decision.disposition.value!r}"
        )


def _check_rule_applied(expected: dict, decision: Decision, failures: list[str]) -> None:
    if "rule_applied" not in expected:
        return
    want = expected["rule_applied"]
    if decision.rule_applied != want:
        failures.append(
            f"rule_applied: expected {want!r}, got {decision.rule_applied!r}"
        )


def _check_output(expected_output: dict, decision: Decision, failures: list[str]) -> None:
    output = decision.output or {}

    summary = str(output.get("summary") or "")
    if "summary" in expected_output:
        if summary != expected_output["summary"]:
            failures.append(
                f"output.summary: expected {expected_output['summary']!r}, got {summary!r}"
            )
    if "summary_contains" in expected_output:
        needle = expected_output["summary_contains"]
        if needle.lower() not in summary.lower():
            failures.append(
                f"output.summary_contains: {needle!r} not found in summary {summary!r}"
            )

    proposed = output.get("proposed_actions") or []

    if "proposed_action_types" in expected_output:
        actual_types = [str(a.get("type") or "") for a in proposed]
        want = list(expected_output["proposed_action_types"])
        if sorted(actual_types) != sorted(want):
            failures.append(
                f"proposed_action_types: expected (multiset) {sorted(want)!r}, got {sorted(actual_types)!r}"
            )

    lo = expected_output.get("proposed_action_count_min")
    hi = expected_output.get("proposed_action_count_max")
    if lo is not None and len(proposed) < lo:
        failures.append(f"proposed_action_count: {len(proposed)} < min {lo}")
    if hi is not None and len(proposed) > hi:
        failures.append(f"proposed_action_count: {len(proposed)} > max {hi}")

    for item in expected_output.get("must_propose_for_vehicle") or []:
        vid = str(item.get("vehicle_id"))
        action_substr = (item.get("action_type_contains") or "").lower()
        match = next(
            (
                a
                for a in proposed
                if str(a.get("vehicle_id")) == vid
                and (not action_substr or action_substr in str(a.get("type") or "").lower())
            ),
            None,
        )
        if match is None:
            failures.append(
                f"must_propose_for_vehicle: no proposed_action with vehicle_id={vid!r} "
                f"and type containing {action_substr!r}"
            )

    forbidden_vids = {str(i.get("vehicle_id")) for i in expected_output.get("must_not_propose_for_vehicle") or []}
    if forbidden_vids:
        leak = {
            str(a.get("vehicle_id"))
            for a in proposed
            if str(a.get("vehicle_id")) in forbidden_vids
        }
        if leak:
            failures.append(
                f"must_not_propose_for_vehicle: vehicle ids {sorted(leak)!r} appeared in proposed_actions"
            )

    violations = output.get("filtered_violations") or []
    lo = expected_output.get("filtered_violations_min")
    hi = expected_output.get("filtered_violations_max")
    if lo is not None and len(violations) < lo:
        failures.append(f"filtered_violations: {len(violations)} < min {lo}")
    if hi is not None and len(violations) > hi:
        failures.append(f"filtered_violations: {len(violations)} > max {hi}")

    if "coverage" in expected_output:
        coverage_want = expected_output["coverage"]
        coverage_got = output.get("coverage") or {}
        for key in ("vehicles_checked", "chargers_checked", "routes_checked"):
            if key in coverage_want and coverage_got.get(key) != coverage_want[key]:
                failures.append(
                    f"output.coverage.{key}: expected {coverage_want[key]!r}, "
                    f"got {coverage_got.get(key)!r}"
                )


def _check_tool_calls(expected_tc: dict, decision: Decision, failures: list[str]) -> None:
    if not expected_tc:
        return

    calls = decision.tool_calls or []
    names = [c.name for c in calls]

    lo = expected_tc.get("count_min")
    hi = expected_tc.get("count_max")
    if lo is not None and len(calls) < lo:
        failures.append(f"tool_calls.count: {len(calls)} < min {lo}")
    if hi is not None and len(calls) > hi:
        failures.append(f"tool_calls.count: {len(calls)} > max {hi}")

    sequence = expected_tc.get("names_in_order")
    if sequence:
        # Subsequence match — actual names may interleave with others.
        it = iter(names)
        ok = all(any(n == target for n in it) for target in sequence)
        if not ok:
            failures.append(
                f"tool_calls.names_in_order: {sequence!r} is not a subsequence of {names!r}"
            )

    all_ok = expected_tc.get("all_ok")
    if all_ok is not None:
        actual_all_ok = all(c.ok for c in calls)
        if all_ok and not actual_all_ok:
            failures.append("tool_calls.all_ok=true but at least one call had ok=false")
        if all_ok is False and actual_all_ok:
            failures.append("tool_calls.all_ok=false but every call had ok=true")


def _check_expected(expected: dict, decision: Decision) -> list[str]:
    failures: list[str] = []
    _check_disposition(expected, decision, failures)
    _check_rule_applied(expected, decision, failures)
    if "output" in expected:
        _check_output(expected["output"], decision, failures)
    if "tool_calls" in expected:
        _check_tool_calls(expected["tool_calls"], decision, failures)
    return failures


# ── Diff rendering ────────────────────────────────────────────────────────


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, default=str)


def diff_actual_vs_expected(expected: dict, actual: dict) -> str:
    expected_text = _stable_json(expected).splitlines()
    actual_text = _stable_json(actual).splitlines()
    diff = difflib.unified_diff(
        expected_text,
        actual_text,
        fromfile="expected",
        tofile="actual",
        lineterm="",
    )
    return "\n".join(diff)


# ── Workflow object from scenario ─────────────────────────────────────────


def _build_workflow(workflow_raw: dict, scenario_now: datetime) -> Workflow:
    """Turn the scenario's workflow block into a Sprint-1 :class:`Workflow`."""
    return Workflow(
        id=_coerce_uuid(workflow_raw["id"], field_name="workflow.id"),
        name=workflow_raw["name"],
        version=workflow_raw["version"],
        description=workflow_raw.get("description", ""),
        prompt=workflow_raw["prompt"],
        allowed_tools=list(workflow_raw.get("allowed_tools") or []),
        parameters=dict(workflow_raw.get("parameters") or {}),
        # Workflows in production are inserted with NOW(); the harness
        # mirrors that with the scenario anchor for reproducibility.
        created_at=scenario_now,
        updated_at=scenario_now,
    )


def _build_constraints(scenario: dict) -> DepotConstraints:
    raw = scenario.get("depot_constraints") or {}
    return DepotConstraints(
        min_departure_soc=float(raw.get("min_departure_soc", 0.99)),
        max_grid_kw=(
            float(raw["max_grid_kw"]) if raw.get("max_grid_kw") is not None else None
        ),
    )


# ── YAML loader ───────────────────────────────────────────────────────────


def load_scenario(text: str) -> dict:
    """Parse YAML scenario text, validate structurally, return as dict."""
    import yaml

    scenario = yaml.safe_load(text)
    if not isinstance(scenario, dict):
        raise ScenarioLoadError("scenario YAML must be a mapping at the top level")
    _validate_scenario(scenario)
    return scenario


# ── _TxPool adapter ───────────────────────────────────────────────────────


class _AcquireContext:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _TxPool:
    """asyncpg.Pool-shaped facade over a single transaction-bound connection."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def fetch(self, query: str, *args: Any) -> Any:
        return await self._conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return await self._conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await self._conn.fetchval(query, *args)

    async def execute(self, query: str, *args: Any) -> Any:
        return await self._conn.execute(query, *args)

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self._conn)


# ── The runner ────────────────────────────────────────────────────────────


async def run_scenario(
    scenario: dict,
    *,
    pool: Any,
    decision_repo: Any = None,
    tool_registry_builder: Optional[Any] = None,
) -> EvalResult:
    """Run one scenario and return an :class:`EvalResult`.

    Args:
        scenario: Parsed scenario dict (use :func:`load_scenario` to get
            one from YAML text).
        pool: asyncpg-style pool against the test database. The runner
            calls ``pool.acquire()`` once and opens a transaction on
            the returned connection.
        decision_repo: Optional :class:`DecisionRepo` passed through to
            the runtime. Defaults to ``None`` so the runtime constructs
            an in-memory record only (the harness doesn't need a
            persisted decision — it asserts against the returned
            :class:`Decision` directly).
        tool_registry_builder: Optional callable
            ``(conn, depot_id) -> ToolRegistry`` so scenarios can ship
            their own tool set. Defaults to
            :func:`_build_default_tool_registry`.

    Returns:
        :class:`EvalResult`. The transaction is rolled back before
        the function returns.
    """
    _validate_scenario(scenario)
    scenario_now = _parse_scenario_now(scenario["scenario_now"])

    workflow = _build_workflow(scenario["workflow"], scenario_now)
    constraints = _build_constraints(scenario)
    tier_value = scenario.get("permission_tier", "draft_and_wait")
    permission_tier = PermissionTier(tier_value)
    user_input = scenario.get("user_input")
    llm_trace = scenario["llm_trace"]

    async with pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            depot_id = await load_snapshot(conn, scenario)
            auth = _build_auth_context(scenario, depot_id)

            registry_builder = tool_registry_builder or _build_default_tool_registry
            tool_registry = registry_builder(conn, depot_id)

            fake_client = FakeAnthropicClient(llm_trace)
            agent = WorkflowAgent(
                anthropic_client=fake_client,
                decision_repo=decision_repo,
                constraints=constraints,
            )

            decision = await agent.run_turn(
                workflow,
                depot_id,
                auth,
                tool_registry,
                user_input=user_input,
                permission_tier=permission_tier,
            )
        finally:
            await transaction.rollback()

    failures = _check_expected(scenario["expected"], decision)
    actual_payload = {
        "disposition": decision.disposition.value,
        "rule_applied": decision.rule_applied,
        "output": decision.output,
        "tool_calls": [
            {"name": c.name, "ok": c.ok, "error": c.error}
            for c in decision.tool_calls
        ],
    }
    return EvalResult(
        scenario_id=scenario["id"],
        workflow=workflow.name,
        passed=not failures,
        severity=scenario["severity"],
        decision=decision,
        failures=failures,
        expected=scenario["expected"],
        actual=actual_payload,
    )


__all__ = [
    "EvalResult",
    "FakeAnthropicClient",
    "ScenarioLoadError",
    "diff_actual_vs_expected",
    "load_scenario",
    "load_snapshot",
    "run_scenario",
]
