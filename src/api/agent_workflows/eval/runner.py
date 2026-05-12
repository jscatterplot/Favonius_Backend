"""Scenario runner for the workflow evaluation harness.

Responsibilities:

1. **Validate** a scenario dict against the schema in
   ``tests/golden/workflows/_schema.yaml``.
2. **Load** the scenario's ``graph_snapshot`` into a transactional
   savepoint on the real test database (never a mocked schema — we want
   to catch schema drift).
3. **Build** a :class:`WorkflowContext` anchored to the scenario's
   ``scenario_now`` so output is deterministic.
4. **Invoke** the registered workflow via :class:`WorkflowAgent.run_turn`.
5. **Compare** the resulting :class:`Decision.output` against the
   scenario's ``expected`` block and return an :class:`EvalResult`.
6. **Rollback** the transaction so the test database is unchanged
   afterwards.

The transaction lives on a single asyncpg connection. The workflow code
path normally reads from a pool; the runner wraps the open connection
in a thin :class:`_TxPool` adapter so the workflow's `pool.fetch(...)`
calls land on the same transaction. This is the only way the workflow
can see the scenario's rows before the rollback.

Time handling
-------------
Snapshots are deterministic: workflows read ``ctx.now`` instead of
``datetime.now()``. Time-relative values inside ``graph_snapshot`` can
be expressed three ways and are all resolved to absolute timestamps
before the workflow ever sees them:

* an ISO-8601 string — used as-is
* a numeric offset in seconds from ``scenario_now`` (``"+3600"`` or
  ``"-1800"``)
* a duration suffix (``"+2h"``, ``"-15m"``, ``"+45s"``)

Anything else is a scenario authoring bug and surfaces from
:func:`_resolve_time` as a :class:`ScenarioLoadError`.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from uuid import UUID, uuid4

from src.api.agent_workflows.runtime import (
    Decision,
    WorkflowAgent,
    WorkflowContext,
    workflow_registry,
)


# ── Errors ────────────────────────────────────────────────────────────────


class ScenarioLoadError(ValueError):
    """Raised when a scenario fails validation or DB loading."""


# ── Public types ──────────────────────────────────────────────────────────


@dataclass
class EvalResult:
    """Outcome of running one scenario.

    A ``passed=False`` result carries a unified diff so a pytest failure
    can print exactly which assertion fired.
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
        """Return a unified diff between expected and actual."""
        return diff_actual_vs_expected(self.expected, self.actual)


# ── Scenario validation ───────────────────────────────────────────────────


_REQUIRED_TOP_LEVEL = {
    "id",
    "workflow",
    "description",
    "graph_snapshot",
    "expected",
    "severity",
    "scenario_now",
}

_ALLOWED_STATUS = {"all_clear", "exceptions_present"}
_ALLOWED_SEVERITY = {"blocking", "warning", "info"}


def _validate_scenario(scenario: dict) -> None:
    missing = _REQUIRED_TOP_LEVEL - scenario.keys()
    if missing:
        raise ScenarioLoadError(
            f"scenario {scenario.get('id', '<unknown>')!r}: missing keys "
            f"{sorted(missing)!r}"
        )

    expected = scenario["expected"]
    if expected.get("status") not in _ALLOWED_STATUS:
        raise ScenarioLoadError(
            f"scenario {scenario['id']!r}: expected.status must be one of "
            f"{sorted(_ALLOWED_STATUS)}, got {expected.get('status')!r}"
        )

    if scenario["severity"] not in _ALLOWED_SEVERITY:
        raise ScenarioLoadError(
            f"scenario {scenario['id']!r}: severity must be one of "
            f"{sorted(_ALLOWED_SEVERITY)}, got {scenario['severity']!r}"
        )

    snapshot = scenario["graph_snapshot"]
    # ``graph_snapshot`` keys all default to [] when absent — but the key
    # itself must exist as a dict for the runner to iterate cleanly.
    if not isinstance(snapshot, dict):
        raise ScenarioLoadError(
            f"scenario {scenario['id']!r}: graph_snapshot must be a mapping"
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
    """Resolve an ISO string / numeric offset / duration suffix to a datetime.

    The runner uses this anywhere a snapshot field is time-relative —
    schedule departure/return times, telemetry timestamps, prices,
    building_load. The result is always timezone-aware UTC.
    """
    if value is None:
        raise ScenarioLoadError("time value is None — scenarios may not use null timestamps")

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    if isinstance(value, (int, float)):
        # Raw seconds offset.
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

        # Plain ISO string.
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ScenarioLoadError(f"unparseable time value {value!r}: {exc}") from exc
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    raise ScenarioLoadError(f"unsupported time value type {type(value).__name__}: {value!r}")


def _maybe_resolve_time(value: Any, scenario_now: datetime) -> Optional[datetime]:
    if value is None:
        return None
    return _resolve_time(value, scenario_now)


# ── Snapshot loading ──────────────────────────────────────────────────────


_DEPOT_COLUMNS = ("depot_id", "name", "latitude", "longitude", "max_grid_kw", "timezone")
_VEHICLE_COLUMNS = (
    "vehicle_id",
    "depot_id",
    "external_id",
    "vehicle_type",
    "battery_kwh",
    "max_charge_kw",
    "id_tag",
)
_CHARGER_COLUMNS = (
    "charger_id",
    "depot_id",
    "ocpp_id",
    "rated_kw",
    "efficiency",
    "connector_type",
    "status",
)
_SCHEDULE_COLUMNS = (
    "schedule_id",
    "vehicle_id",
    "route_id",
    "departure_time",
    "return_time",
    "energy_kwh",
    "required_soc",
)
_DRIVER_COLUMNS = (
    "driver_id",
    "depot_id",
    "external_driver_id",
    "display_name",
    "status",
)


def _coerce_uuid(value: Any, *, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            raise ScenarioLoadError(f"{field_name}: invalid UUID {value!r}") from exc
    raise ScenarioLoadError(f"{field_name}: expected UUID, got {type(value).__name__}")


async def _insert_depot(conn: Any, depot: dict, *, scenario_now: datetime) -> None:
    depot_id = _coerce_uuid(depot["depot_id"], field_name="depot.depot_id")
    await conn.execute(
        """
        INSERT INTO depots (depot_id, name, latitude, longitude, max_grid_kw, timezone)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        depot_id,
        depot.get("name", "Eval depot"),
        depot.get("latitude", 54.6872),  # Vilnius default
        depot.get("longitude", 25.2797),
        float(depot.get("max_grid_kw", 800.0)),
        depot.get("timezone", "UTC"),
    )


async def _insert_vehicles(
    conn: Any, vehicles: Iterable[dict], depot_id: UUID, *, scenario_now: datetime
) -> None:
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


async def _insert_chargers(
    conn: Any, chargers: Iterable[dict], depot_id: UUID, *, scenario_now: datetime
) -> None:
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


async def _insert_drivers(
    conn: Any, drivers: Iterable[dict], depot_id: UUID, *, scenario_now: datetime
) -> None:
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
    conn: Any, schedules: Iterable[dict], *, scenario_now: datetime
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
            _coerce_uuid(
                s.get("schedule_id") or uuid4(),
                field_name="schedule.schedule_id",
            ),
            _coerce_uuid(s["vehicle_id"], field_name="schedule.vehicle_id"),
            s.get("route_id", "R-0"),
            _resolve_time(s["departure_time"], scenario_now),
            _resolve_time(s["return_time"], scenario_now),
            float(s.get("energy_kwh", 0.0)),
            float(s.get("required_soc", 0.99)),
        )


async def _insert_telemetry(
    conn: Any, samples: Iterable[dict], *, scenario_now: datetime
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
    conn: Any, prices: Iterable[dict], depot_id: UUID, *, scenario_now: datetime
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
    conn: Any, samples: Iterable[dict], depot_id: UUID, *, scenario_now: datetime
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
    """Insert a scenario's ``graph_snapshot`` rows on the given connection.

    Returns the depot UUID. The caller is responsible for opening a
    transaction *before* calling this and rolling it back afterwards —
    the runner does that automatically; callers using ``load_snapshot``
    directly must do it themselves.
    """
    _validate_scenario(scenario)
    scenario_now = _parse_scenario_now(scenario["scenario_now"])
    snapshot = scenario["graph_snapshot"]

    depot = snapshot.get("depot")
    if depot is None:
        raise ScenarioLoadError(
            f"scenario {scenario['id']!r}: graph_snapshot.depot is required"
        )

    depot_id = _coerce_uuid(depot["depot_id"], field_name="depot.depot_id")
    await _insert_depot(conn, depot, scenario_now=scenario_now)
    await _insert_drivers(conn, snapshot.get("drivers", []), depot_id, scenario_now=scenario_now)
    await _insert_vehicles(conn, snapshot.get("vehicles", []), depot_id, scenario_now=scenario_now)
    await _insert_chargers(conn, snapshot.get("chargers", []), depot_id, scenario_now=scenario_now)
    await _insert_schedules(conn, snapshot.get("schedules", []), scenario_now=scenario_now)
    await _insert_telemetry(conn, snapshot.get("telemetry", []), scenario_now=scenario_now)
    await _insert_prices(conn, snapshot.get("prices", []), depot_id, scenario_now=scenario_now)
    await _insert_building_load(
        conn, snapshot.get("building_load", []), depot_id, scenario_now=scenario_now
    )
    return depot_id


# ── Expected-block assertion ──────────────────────────────────────────────


def _check_status(expected: dict, output: dict, failures: list[str]) -> None:
    want = expected.get("status")
    got = output.get("status")
    if want is not None and got != want:
        failures.append(f"status: expected {want!r}, got {got!r}")


def _check_coverage(expected: dict, output: dict, failures: list[str]) -> None:
    want = expected.get("coverage") or {}
    got = output.get("coverage") or {}
    for key in ("vehicles_checked", "chargers_checked", "routes_checked"):
        if key not in want:
            continue
        if got.get(key) != want[key]:
            failures.append(
                f"coverage.{key}: expected {want[key]!r}, got {got.get(key)!r}"
            )


def _check_exception_counts(expected: dict, output: dict, failures: list[str]) -> None:
    exceptions = output.get("exceptions") or []
    n = len(exceptions)
    lo = expected.get("exception_count_min")
    hi = expected.get("exception_count_max")
    if lo is not None and n < lo:
        failures.append(f"exception_count: {n} < min {lo}")
    if hi is not None and n > hi:
        failures.append(f"exception_count: {n} > max {hi}")


def _check_must_include(expected: dict, output: dict, failures: list[str]) -> None:
    must = expected.get("must_include_exceptions") or []
    exceptions = output.get("exceptions") or []
    for item in must:
        vid = item.get("vehicle_id")
        issue_substr = item.get("issue_contains", "")
        match = next(
            (
                e
                for e in exceptions
                if str(e.get("vehicle_id")) == str(vid)
                and issue_substr.lower() in str(e.get("issue", "")).lower()
            ),
            None,
        )
        if match is None:
            failures.append(
                f"must_include_exceptions: no exception with vehicle_id={vid!r} "
                f"and issue containing {issue_substr!r}"
            )


def _check_must_not_include(expected: dict, output: dict, failures: list[str]) -> None:
    forbidden = expected.get("must_not_include") or []
    exceptions = output.get("exceptions") or []
    excepted_vids = {str(e.get("vehicle_id")) for e in exceptions}
    for item in forbidden:
        vid = str(item.get("vehicle_id"))
        if vid in excepted_vids:
            failures.append(
                f"must_not_include: vehicle_id={vid!r} appeared in exceptions"
            )


def _check_action_allow_list(expected: dict, output: dict, failures: list[str]) -> None:
    allow = expected.get("action_type_allow_list")
    if allow is None:
        return
    allow_set = set(allow)
    for exc in output.get("exceptions") or []:
        action = (exc.get("proposed_action") or {}).get("type")
        if action is not None and action not in allow_set:
            failures.append(
                f"action_type_allow_list: action {action!r} not in {sorted(allow_set)!r}"
            )


def _check_expected(expected: dict, output: dict) -> list[str]:
    failures: list[str] = []
    _check_status(expected, output, failures)
    _check_coverage(expected, output, failures)
    _check_exception_counts(expected, output, failures)
    _check_must_include(expected, output, failures)
    _check_must_not_include(expected, output, failures)
    _check_action_allow_list(expected, output, failures)
    return failures


# ── Diff rendering ────────────────────────────────────────────────────────


def _stable_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, default=str)


def diff_actual_vs_expected(expected: dict, actual: dict) -> str:
    """Unified diff between expected and actual blocks.

    Sorted-key JSON so the diff is order-stable across runs.
    """
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


# ── Transaction adapter ───────────────────────────────────────────────────


class _TxPool:
    """Thin adapter so workflows can use ``pool.fetch`` / ``pool.acquire``
    against the runner's open transaction.

    asyncpg's :class:`asyncpg.Pool` API: workflows call ``pool.fetch``,
    ``pool.fetchrow``, ``pool.execute``, or ``async with pool.acquire()``.
    All of those need to land on the same connection inside the runner's
    transaction so the rolled-back snapshot is visible during the run.
    """

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

    def acquire(self) -> "_AcquireContext":
        return _AcquireContext(self._conn)


class _AcquireContext:
    """``async with pool.acquire() as conn`` that hands back the tx conn."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> None:
        return None


# ── The runner ────────────────────────────────────────────────────────────


def load_scenario(text: str) -> dict:
    """Parse a YAML scenario string into a dict and validate its shape.

    Importing PyYAML lazily so the harness module is import-safe in
    environments without yaml installed.
    """
    import yaml

    scenario = yaml.safe_load(text)
    if not isinstance(scenario, dict):
        raise ScenarioLoadError("scenario YAML must be a mapping at the top level")
    _validate_scenario(scenario)
    return scenario


async def run_scenario(
    scenario: dict,
    *,
    pool: Any,
    agent: Optional[WorkflowAgent] = None,
    parameters: Optional[dict[str, Any]] = None,
) -> EvalResult:
    """Run one scenario and return an :class:`EvalResult`.

    The scenario is loaded into a transaction on a single connection
    from ``pool``. The workflow runs against that same connection (via
    :class:`_TxPool`) so it can read the snapshot. The transaction is
    always rolled back when the function exits — pass or fail — so the
    underlying database is unchanged.

    Args:
        scenario: Parsed scenario dict (use :func:`load_scenario` to get
            one from YAML text).
        pool: asyncpg-style pool against the test database. The runner
            calls ``pool.acquire()`` once.
        agent: :class:`WorkflowAgent` to dispatch through. Defaults to a
            fresh agent over the process-wide registry.
        parameters: Optional workflow parameter overrides handed through
            to :class:`WorkflowContext.parameters`.

    Returns:
        An :class:`EvalResult` whose ``passed`` flag reflects whether
        the workflow's :class:`Decision.output` matched the scenario's
        ``expected`` block.
    """
    _validate_scenario(scenario)
    scenario_now = _parse_scenario_now(scenario["scenario_now"])
    workflow_name = scenario["workflow"]
    agent = agent or WorkflowAgent()

    async with pool.acquire() as conn:
        transaction = conn.transaction()
        await transaction.start()
        try:
            depot_id = await load_snapshot(conn, scenario)

            ctx = WorkflowContext(
                depot_id=depot_id,
                now=scenario_now,
                visible_depot_ids=[depot_id],
                static_pool=_TxPool(conn),
                ts_pool=_TxPool(conn),
                parameters=parameters or {},
            )

            decision = await agent.run_turn(workflow_name, ctx)
        finally:
            await transaction.rollback()

    failures = _check_expected(scenario["expected"], decision.output)
    return EvalResult(
        scenario_id=scenario["id"],
        workflow=workflow_name,
        passed=not failures,
        severity=scenario["severity"],
        decision=decision,
        failures=failures,
        expected=scenario["expected"],
        actual=decision.output,
    )


# ``workflow_registry`` is re-exported so test conftests can register
# fake workflows without depending on the internal layout.
__all__ = [
    "EvalResult",
    "ScenarioLoadError",
    "diff_actual_vs_expected",
    "load_scenario",
    "load_snapshot",
    "run_scenario",
    "workflow_registry",
]
