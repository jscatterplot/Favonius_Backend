"""Agent-SQL golden gate (PLAN.md session S2).

Replays each hand-written scenario in ``tests/golden/agent_sql.yaml`` through
the depot chat agent's SQL mode — the real
:func:`src.api.agent_workflows.runtime.run_qa_turn` loop, the real
``src/api/agent/sql_validator`` + ``sql_executor`` (role swap + EXPLAIN
preflight) — against the curated ``agent_views.*`` table-functions, reading
the scenario's ``graph_snapshot`` rows loaded into a transactional savepoint
on a real TimescaleDB + Supabase pair. The model is a deterministic
:class:`FakeAnthropicClient` replaying the scenario's ``llm_trace`` so the
gate never calls a live LLM.

Per scenario the gate asserts:
  * ``QAResult.status`` matches ``expected.status`` (usually ``success``),
  * the set of ``agent_views.*`` functions the run_select_* calls touched
    matches ``expected.agent_views_used`` (exact set),
  * ``expected.sql_validator_rejected`` matches whether any run_select_* call
    was rejected by the validator,
  * every ``final_answer_must_include`` substring is present and every
    ``final_answer_must_not_include`` substring is absent in the answer
    (the operator tone gate: no leaked SQL / UUIDs / view names).

Run with::

    pytest -m agent_sql_golden                 # the gate
    pytest -m agent_sql_golden --diag          # + dump trace/SQL on failure

Requires the TimescaleDB + Supabase test pair (see tests/golden/conftest.py
and tests/golden/agent_sql/supabase_bootstrap.sql). Skips locally when the
pair is unreachable; fails the job in CI.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

import pytest
import yaml

from src.api.agent.auth_context import AuthContext
from src.api.agent.controller import _sql_functions_accessed
from src.api.agent.prompts import build_sql_agent_system_prompt, format_sql_agent_user_message
from src.api.agent.sql_tools import SQL_AGENT_TOOL_NAMES, build_sql_agent_tool_registry
from src.api.agent_workflows.eval.runner import FakeAnthropicClient
from src.api.agent_workflows.runtime import QAResult, run_qa_turn

# ── Paths ──────────────────────────────────────────────────────────────────

_HERE = Path(__file__).parent
_SCENARIOS_PATH = _HERE / "agent_sql.yaml"
_SCHEMA_PATH = _HERE / "agent_sql" / "_schema.yaml"

# ── Stable defaults (mirror the workflow runner's fixed UUIDs) ──────────────

_DEFAULT_USER_ID = UUID("aa000000-0000-4000-8000-0000000000aa")
_DEFAULT_ORG_ID = UUID("bb000000-0000-4000-8000-0000000000bb")
_DEFAULT_SCENARIO_NOW = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)

_RUN_SELECT_TOOLS = ("run_select_ts", "run_select_static")

# The structured ``error_kind`` codes the sql_validator can emit. Used to tell
# a validator rejection apart from an executor error (role_error / plan_error /
# timeout / exec_error) when computing ``sql_validator_rejected``.
_VALIDATOR_REJECTION_KINDS = frozenset(
    {
        "empty_sql",
        "too_long",
        "parse_error",
        "multi_statement",
        "non_select",
        "lock_clause_not_allowed",
        "forbidden_schema",
        "table_not_allowed",
        "missing_argument",
        "bad_function_argument",
        "no_data_source",
        "bad_placeholder",
        "dangerous_fn",
        "function_not_allowed",
        "missing_time_filter",
        "offset_not_allowed",
        "non_literal_limit",
    }
)

# Executor-side failures (role swap / EXPLAIN preflight / timeout / exec). Distinct
# from validator rejections: these mean the SQL parsed but did not run against the
# real schema, so they are always a scenario bug in a well-formed golden case.
_EXECUTOR_ERROR_KINDS = frozenset({"role_error", "plan_error", "timeout", "exec_error"})


class ScenarioError(ValueError):
    """Raised when a scenario is structurally invalid (before it can run)."""


# ── YAML loading + schema validation ────────────────────────────────────────


def _load_all_scenarios() -> list[dict[str, Any]]:
    """Load every scenario from agent_sql.yaml (empty list if absent)."""
    if not _SCENARIOS_PATH.is_file():
        return []
    raw = yaml.safe_load(_SCENARIOS_PATH.read_text(encoding="utf-8"))
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ScenarioError("agent_sql.yaml must be a top-level list of scenarios")
    scenarios: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ScenarioError(
                f"agent_sql.yaml entry {index} must be a mapping, got {type(item).__name__}"
            )
        scenarios.append(item)
    return scenarios


_SCENARIO_JSON_SCHEMA: Optional[dict[str, Any]] = None


def _scenario_schema() -> dict[str, Any]:
    global _SCENARIO_JSON_SCHEMA
    if _SCENARIO_JSON_SCHEMA is None:
        if not _SCHEMA_PATH.is_file():
            raise ScenarioError(f"agent-SQL schema file missing: {_SCHEMA_PATH}")
        raw = yaml.safe_load(_SCHEMA_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ScenarioError("agent_sql/_schema.yaml must be a mapping")
        _SCENARIO_JSON_SCHEMA = raw
    return _SCENARIO_JSON_SCHEMA


def _validate_against_schema(scenario: dict[str, Any]) -> None:
    from jsonschema import Draft7Validator, draft7_format_checker
    from jsonschema.exceptions import ValidationError

    try:
        Draft7Validator(_scenario_schema(), format_checker=draft7_format_checker).validate(scenario)
    except ValidationError as exc:
        loc = ".".join(str(p) for p in exc.path) if exc.path else "<root>"
        raise ScenarioError(
            f"scenario {scenario.get('id', '<unknown>')!r}: schema violation at "
            f"{loc}: {exc.message}"
        ) from exc


_ALL_SCENARIOS: list[dict[str, Any]] = _load_all_scenarios()
_SCENARIO_IDS = [str(s.get("id", f"scenario_{i}")) for i, s in enumerate(_ALL_SCENARIOS)]


# ── Time resolution ──────────────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^([+-])(\d+)([smhd])$")


def _parse_scenario_now(raw: Any) -> datetime:
    if raw is None:
        return _DEFAULT_SCENARIO_NOW
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _resolve_dt(value: Any, scenario_now: datetime) -> datetime:
    """Resolve an ISO string / numeric-second offset / duration suffix to UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return scenario_now + timedelta(seconds=float(value))
    if isinstance(value, str):
        match = _DURATION_RE.match(value)
        if match:
            sign, magnitude, unit = match.groups()
            seconds = int(magnitude) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
            return scenario_now + timedelta(seconds=(-seconds if sign == "-" else seconds))
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise ScenarioError(f"unsupported time value {value!r} ({type(value).__name__})")


def _coerce_uuid(value: Any, *, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ScenarioError(f"{field_name}: invalid UUID {value!r}") from exc


def _maybe_uuid(value: Any, *, field_name: str) -> Optional[UUID]:
    return None if value is None else _coerce_uuid(value, field_name=field_name)


# ── Auth context ─────────────────────────────────────────────────────────────


def _depot_org_map(snapshot: dict[str, Any], default_org: UUID) -> dict[UUID, UUID]:
    """Map each snapshot depot_id → its organization_id (default when absent)."""
    out: dict[UUID, UUID] = {}
    for depot in snapshot.get("depots") or []:
        depot_id = _coerce_uuid(depot["depot_id"], field_name="depots.depot_id")
        org = depot.get("organization_id")
        out[depot_id] = (
            _coerce_uuid(org, field_name="depots.organization_id") if org else default_org
        )
    return out


def _build_auth_context(scenario: dict[str, Any], snapshot: dict[str, Any]) -> AuthContext:
    auth_raw = scenario.get("auth") or {}
    org_id = _coerce_uuid(
        auth_raw.get("organization_id", _DEFAULT_ORG_ID), field_name="auth.organization_id"
    )
    user_id = _coerce_uuid(auth_raw.get("user_id", _DEFAULT_USER_ID), field_name="auth.user_id")
    role = auth_raw.get("role", "customer_operator")

    if "visible_depot_ids" in auth_raw:
        visible = [
            _coerce_uuid(d, field_name="auth.visible_depot_ids")
            for d in auth_raw["visible_depot_ids"]
        ]
    else:
        visible = [
            _coerce_uuid(d["depot_id"], field_name="depots.depot_id")
            for d in snapshot.get("depots") or []
        ]
    return AuthContext(
        user_id=user_id,
        organization_id=org_id,
        role=role,
        visible_depot_ids=visible,
    )


# ── _TxPool: asyncpg.Pool-shaped facade over one transaction-bound connection ─


class _AcquireCtx:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def __aenter__(self) -> Any:
        return self._conn

    async def __aexit__(self, *_exc: Any) -> None:
        # The sql_executor sets `SET LOCAL ROLE agent_reader_ts` and
        # `SET LOCAL transaction_read_only = on` inside a SAVEPOINT.  When the
        # SAVEPOINT is RELEASED (committed), PostgreSQL preserves those SET LOCAL
        # changes in the outer transaction — meaning the connection stays
        # read-only and runs as agent_reader_ts for the rest of the outer
        # transaction.  That blocks the `_on_step` callback and
        # `agent_runs_close` from writing to agent_runs (which run via the
        # *same* connection when _TxPool is used).  Resetting both here (after
        # the executor's `async with pool.acquire()` context exits, before
        # `on_step` fires) restores full write access.  Safe to swallow errors
        # because the test will fail loudly on the subsequent write attempt.
        try:
            await self._conn.execute("RESET ROLE")
            await self._conn.execute("SET LOCAL transaction_read_only = off")
        except Exception:  # noqa: BLE001
            pass


class _TxPool:
    """Hands the sql_executor / resolver the scenario's transaction-bound connection.

    The executor does ``async with pool.acquire() as conn: async with
    conn.transaction(readonly=True): …`` — nested on our open transaction that
    becomes a SAVEPOINT, so the LLM's SQL reads the uncommitted snapshot and
    everything rolls back at the end. The resolver calls ``pool.fetch(...)``
    directly, so we passthrough the query verbs too.
    """

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self._conn)

    async def fetch(self, query: str, *args: Any) -> Any:
        return await self._conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return await self._conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        return await self._conn.fetchval(query, *args)

    async def execute(self, query: str, *args: Any) -> Any:
        return await self._conn.execute(query, *args)


# ── Snapshot loaders ─────────────────────────────────────────────────────────


async def _load_static_snapshot(
    conn: Any, snapshot: dict[str, Any], org_map: dict[UUID, UUID]
) -> None:
    """Load depots/vehicles/drivers/chargers into the Supabase test DB.

    Mirrors the supabase_bootstrap.sql base tables (Supabase naming: sites,
    charging_stations, site_id FKs) which migration 040's agent_views.*
    functions read.
    """
    # Organizations first (sites.organization_id FK).
    for org in set(org_map.values()):
        await conn.execute(
            "INSERT INTO organizations (id, name) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            org,
            "Eval org",
        )

    for depot in snapshot.get("depots") or []:
        depot_id = _coerce_uuid(depot["depot_id"], field_name="depots.depot_id")
        tariff = (
            json.dumps({"entsoe_zone": depot["entsoe_zone"]}) if depot.get("entsoe_zone") else None
        )
        # address is jsonb NOT NULL DEFAULT '{}' in production (and in the
        # bootstrap); no eval reads it, so we leave it to the column default.
        await conn.execute(
            """
            INSERT INTO sites (id, organization_id, name, timezone, currency,
                               max_grid_kw, latitude, longitude, tariff_config)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            """,
            depot_id,
            org_map.get(depot_id, _DEFAULT_ORG_ID),
            depot["name"],
            depot.get("timezone", "Europe/Vilnius"),
            depot.get("currency", "EUR"),
            depot.get("max_grid_kw"),
            depot.get("latitude"),
            depot.get("longitude"),
            tariff,
        )

    for v in snapshot.get("vehicles") or []:
        await conn.execute(
            """
            INSERT INTO vehicles (id, site_id, vin, license_plate, battery_capacity_kwh,
                                  max_charge_rate_kw, v2g_capable, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            _coerce_uuid(v["vehicle_id"], field_name="vehicles.vehicle_id"),
            _coerce_uuid(v["depot_id"], field_name="vehicles.depot_id"),
            v.get("vin"),
            v.get("license_plate"),
            v.get("battery_capacity_kwh"),
            v.get("max_charge_rate_kw"),
            bool(v.get("v2g_capable", False)),
            v.get("status", "active"),
        )

    for d in snapshot.get("drivers") or []:
        await conn.execute(
            """
            INSERT INTO drivers (id, site_id, display_name, external_driver_id, email, status)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            _coerce_uuid(d["driver_id"], field_name="drivers.driver_id"),
            _coerce_uuid(d["depot_id"], field_name="drivers.depot_id"),
            d.get("display_name"),
            d.get("external_driver_id"),
            d.get("email"),
            d.get("status", "active"),
        )

    for c in snapshot.get("chargers") or []:
        await conn.execute(
            """
            INSERT INTO charging_stations (id, site_id, station_id, max_power_kw,
                                           connector_type, vendor, display_name)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            _coerce_uuid(c["charger_id"], field_name="chargers.charger_id"),
            _coerce_uuid(c["depot_id"], field_name="chargers.depot_id"),
            c.get("ocpp_id"),
            c.get("rated_kw"),
            c.get("connector_type", "CCS"),
            c.get("vendor"),
            c.get("display_name"),
        )


async def _load_ts_snapshot(
    conn: Any, snapshot: dict[str, Any], scenario_now: datetime, org_map: dict[UUID, UUID]
) -> None:
    """Load sessions/optimization_runs/alerts/prices/building_load/connector_status into TimescaleDB."""
    for i, s in enumerate(snapshot.get("sessions") or []):
        start = _resolve_dt(s["start_time"], scenario_now)
        end = _resolve_dt(s["end_time"], scenario_now) if s.get("end_time") is not None else None
        await conn.execute(
            """
            INSERT INTO charging_sessions (
                session_id, station_id, evse_id, connector_id, vehicle_id, driver_id,
                card_id, site_id, start_time, end_time, energy_delivered_kwh,
                cost_total, cost_total_source, source)
            VALUES (COALESCE($1, gen_random_uuid()), $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            """,
            _maybe_uuid(s.get("session_id"), field_name="sessions.session_id"),
            str(s.get("station_id", f"CP-{i + 1:02d}")),
            1,
            1,
            str(s["vehicle_id"]),
            _maybe_uuid(s.get("driver_id"), field_name="sessions.driver_id"),
            _maybe_uuid(s.get("card_id"), field_name="sessions.card_id"),
            _coerce_uuid(s["depot_id"], field_name="sessions.depot_id"),
            start,
            end,
            s.get("energy_kwh"),
            s.get("cost_total"),
            s.get("cost_total_source", "granular"),
            s.get("source", "live"),
        )

    for r in snapshot.get("optimization_runs") or []:
        run_time = _resolve_dt(r["run_time"], scenario_now)
        h_start = (
            _resolve_dt(r["horizon_start"], scenario_now) if r.get("horizon_start") else run_time
        )
        h_end = (
            _resolve_dt(r["horizon_end"], scenario_now)
            if r.get("horizon_end")
            else run_time + timedelta(hours=4)
        )
        await conn.execute(
            """
            INSERT INTO optimization_runs (
                run_id, depot_id, run_time, trigger_reason, horizon_start, horizon_end,
                solve_time_s, peak_demand_kw, status, solver_used, schedule_json)
            VALUES (COALESCE($1, gen_random_uuid()), $2, $3, $4, $5, $6, $7, $8, $9, $10, '{}'::jsonb)
            """,
            _maybe_uuid(r.get("run_id"), field_name="optimization_runs.run_id"),
            _coerce_uuid(r["depot_id"], field_name="optimization_runs.depot_id"),
            run_time,
            r.get("trigger_reason", "scheduled"),
            h_start,
            h_end,
            r.get("solve_time_s"),
            r.get("peak_demand_kw"),
            r.get("status", "optimal"),
            r.get("solver_used", "gurobi"),
        )

    for i, a in enumerate(snapshot.get("alerts") or []):
        depot_id = _coerce_uuid(a["depot_id"], field_name="alerts.depot_id")
        created = _resolve_dt(a["created_at"], scenario_now)
        org = (
            _coerce_uuid(a["organization_id"], field_name="alerts.organization_id")
            if a.get("organization_id")
            else org_map.get(depot_id, _DEFAULT_ORG_ID)
        )
        alert_id = _maybe_uuid(a.get("alert_id"), field_name="alerts.alert_id")
        await conn.execute(
            """
            INSERT INTO notification_alerts (
                id, organization_id, depot_id, alert_type, severity, title, dedup_key,
                status, first_occurrence_at, last_occurrence_at, created_at, updated_at)
            VALUES (COALESCE($1, gen_random_uuid()), $2, $3, $4, $5, $6, $7, $8, $9, $9, $9, $9)
            """,
            alert_id,
            org,
            depot_id,
            a["alert_type"],
            a["severity"],
            a.get("title", a["alert_type"]),
            a.get("dedup_key", f"eval-{a['alert_type']}-{i}"),
            a.get("status", "active"),
            created,
        )

    for p in snapshot.get("electricity_prices") or []:
        await conn.execute(
            """
            INSERT INTO electricity_prices (time, node_id, market_type, lmp_price_mwh)
            VALUES ($1, $2, $3, $4)
            """,
            _resolve_dt(p["time"], scenario_now),
            str(p["node_id"]),
            p.get("market_type", "ENTSOE_DAM"),
            p["lmp_price_mwh"],
        )

    for b in snapshot.get("building_load") or []:
        await conn.execute(
            "INSERT INTO building_load (time, depot_id, power_kw, source) VALUES ($1, $2, $3, $4)",
            _resolve_dt(b["time"], scenario_now),
            _coerce_uuid(b["depot_id"], field_name="building_load.depot_id"),
            b["power_kw"],
            b.get("source", "meter"),
        )

    for cs in snapshot.get("connector_status") or []:
        await conn.execute(
            """
            INSERT INTO connector_status (station_id, connector_id, depot_id, status, error_code, timestamp)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            str(cs["station_id"]),
            int(cs.get("connector_id", 1)),
            _coerce_uuid(cs["depot_id"], field_name="connector_status.depot_id"),
            cs["status"],
            cs.get("error_code"),
            _resolve_dt(cs["timestamp"], scenario_now),
        )


# ── Result + evaluation ──────────────────────────────────────────────────────


@dataclass
class ScenarioResult:
    scenario_id: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    qa: Optional[QAResult] = None
    error: Optional[str] = None


def _validator_rejected(qa: QAResult) -> bool:
    for tc in qa.tool_calls:
        if tc.name not in _RUN_SELECT_TOOLS:
            continue
        if (
            isinstance(tc.result, dict)
            and tc.result.get("error_kind") in _VALIDATOR_REJECTION_KINDS
        ):
            return True
    return False


def _executor_error_kinds(qa: QAResult) -> list[str]:
    """run_select calls that failed in the EXECUTOR (not the validator).

    A plan_error/role_error/timeout/exec_error means the canned SQL parsed but
    did not execute against the real schema (e.g. a hallucinated column caught
    by the EXPLAIN preflight). That is always a scenario bug — without flagging
    it, ``agent_views_used`` would still resolve via the SQL-text regex and the
    hand-written answer's substrings would still match, so a broken query could
    false-pass. Surfacing it keeps the gate honest.
    """
    kinds: list[str] = []
    for tc in qa.tool_calls:
        if tc.name not in _RUN_SELECT_TOOLS:
            continue
        if isinstance(tc.result, dict) and tc.result.get("error_kind") in _EXECUTOR_ERROR_KINDS:
            kinds.append(str(tc.result.get("error_kind")))
    return kinds


def _evaluate(scenario: dict[str, Any], qa: QAResult) -> list[str]:
    expected = scenario["expected"]
    failures: list[str] = []

    if qa.status != expected["status"]:
        failures.append(f"status: expected {expected['status']!r}, got {qa.status!r}")

    actual_fns = set(_sql_functions_accessed(qa.tool_calls))
    want_fns = {str(f).lower() for f in expected["agent_views_used"]}
    if actual_fns != want_fns:
        failures.append(
            f"agent_views_used: expected {sorted(want_fns)!r}, got {sorted(actual_fns)!r}"
        )

    exec_errors = _executor_error_kinds(qa)
    if exec_errors:
        failures.append(
            f"run_select hit executor error(s) {exec_errors!r} — the canned SQL parsed "
            "but did not execute against the real schema (check column/function names)"
        )

    rejected = _validator_rejected(qa)
    if rejected != bool(expected["sql_validator_rejected"]):
        failures.append(
            f"sql_validator_rejected: expected {bool(expected['sql_validator_rejected'])}, got {rejected}"
        )

    text = qa.text or ""
    for needle in expected["final_answer_must_include"]:
        if needle not in text:
            failures.append(f"final_answer must include {needle!r} — answer was {text!r}")
    for needle in expected["final_answer_must_not_include"]:
        if needle in text:
            failures.append(f"final_answer must NOT include {needle!r} — answer was {text!r}")

    return failures


# ── Runner ───────────────────────────────────────────────────────────────────


async def run_agent_sql_scenario(
    scenario: dict[str, Any], *, ts_pool: Any, static_pool: Any
) -> ScenarioResult:
    """Replay one scenario through run_qa_turn and grade it. Rolls back both DBs."""
    _validate_against_schema(scenario)
    scenario_id = str(scenario["id"])
    scenario_now = _parse_scenario_now(scenario.get("scenario_now"))
    snapshot = scenario["graph_snapshot"]
    org_map = _depot_org_map(snapshot, _DEFAULT_ORG_ID)
    auth = _build_auth_context(scenario, snapshot)

    qa: Optional[QAResult] = None
    error: Optional[str] = None

    async with ts_pool.acquire() as ts_conn, static_pool.acquire() as static_conn:
        ts_tx = ts_conn.transaction()
        static_tx = static_conn.transaction()
        await ts_tx.start()
        await static_tx.start()
        try:
            await _load_static_snapshot(static_conn, snapshot, org_map)
            await _load_ts_snapshot(ts_conn, snapshot, scenario_now, org_map)

            registry = build_sql_agent_tool_registry(
                _TxPool(static_conn),
                _TxPool(ts_conn),
                auth,
                page_context=scenario.get("page_context"),
            )
            fake = FakeAnthropicClient(scenario["llm_trace"])
            qa = await run_qa_turn(
                anthropic_client=fake,
                model="fake-golden-model",
                system_prompt=build_sql_agent_system_prompt(),
                user_message=format_sql_agent_user_message(str(scenario["question"])),
                tool_registry=registry,
                allowed_tools=SQL_AGENT_TOOL_NAMES,
                max_iterations=8,
                max_tokens=2048,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001 — record + roll back, surface as failure
            error = f"{type(exc).__name__}: {exc}"
        finally:
            await static_tx.rollback()
            await ts_tx.rollback()

    if qa is None:
        return ScenarioResult(
            scenario_id=scenario_id,
            passed=False,
            failures=[f"run_qa_turn raised before producing a result: {error}"],
            error=error,
        )

    failures = _evaluate(scenario, qa)
    return ScenarioResult(scenario_id=scenario_id, passed=not failures, failures=failures, qa=qa)


# ── Diagnostics (--diag) ─────────────────────────────────────────────────────


def _executed_sql_from_trace(scenario: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for turn in scenario.get("llm_trace") or []:
        for block in turn.get("content") or []:
            if block.get("type") == "tool_use" and block.get("name") in _RUN_SELECT_TOOLS:
                sql = (block.get("input") or {}).get("sql")
                if sql:
                    out.append(str(sql))
    return out


def _diag_report(scenario: dict[str, Any], result: ScenarioResult) -> str:
    lines = [
        f"\n===== --diag: {result.scenario_id} =====",
        f"question: {scenario.get('question')!r}",
    ]
    lines.append("\n-- llm_trace --")
    lines.append(json.dumps(scenario.get("llm_trace"), indent=2, default=str))
    lines.append("\n-- SQL in trace --")
    for sql in _executed_sql_from_trace(scenario):
        lines.append(f"  {sql}")
    if result.qa is not None:
        lines.append("\n-- executed tool calls --")
        for tc in result.qa.tool_calls:
            lines.append(f"  {tc.name} ok={tc.ok} error={tc.error!r}")
        lines.append(f"\n-- final answer --\n{result.qa.text!r}")
        lines.append(f"-- status --\n{result.qa.status}")
    if result.error:
        lines.append(f"\n-- runtime error --\n{result.error}")
    lines.append("\n-- failures --")
    lines.extend(f"  - {f}" for f in result.failures)
    return "\n".join(lines)


# ── Invariant tests (no DB) ──────────────────────────────────────────────────


def test_schema_file_exists() -> None:
    assert _SCHEMA_PATH.is_file(), f"agent-SQL _schema.yaml missing at {_SCHEMA_PATH}"


def test_scenario_ids_unique() -> None:
    if not _ALL_SCENARIOS:
        pytest.skip("no agent_sql scenarios yet")
    ids = [s.get("id") for s in _ALL_SCENARIOS]
    assert len(ids) == len(set(ids)), f"duplicate scenario ids: {ids!r}"


def test_scenarios_validate_against_schema() -> None:
    if not _ALL_SCENARIOS:
        pytest.skip("no agent_sql scenarios yet")
    failures: list[str] = []
    for scenario in _ALL_SCENARIOS:
        try:
            _validate_against_schema(scenario)
        except ScenarioError as exc:
            failures.append(str(exc))
    assert not failures, "schema-invalid scenarios:\n  " + "\n  ".join(failures)


def test_every_scenario_has_tone_gate() -> None:
    """Every scenario must forbid SELECT / UUID / agent_views in the answer (PLAN.md)."""
    if not _ALL_SCENARIOS:
        pytest.skip("no agent_sql scenarios yet")
    required = {"SELECT", "UUID", "agent_views"}
    failures = [
        s.get("id")
        for s in _ALL_SCENARIOS
        if not required.issubset(
            set((s.get("expected") or {}).get("final_answer_must_not_include") or [])
        )
    ]
    assert not failures, f"scenarios missing the {sorted(required)} tone gate: {failures!r}"


def test_suite_size_and_distribution() -> None:
    """Gate the full 20-question shape (7 energy / 7 ops / 6 pricing)."""
    from tests.golden.conftest import _ci_requires_db

    n = len(_ALL_SCENARIOS)
    if n < 20:
        msg = f"agent_sql suite has {n}/20 scenarios (expected 20)"
        if _ci_requires_db():
            pytest.fail(msg)
        pytest.skip(msg)
    by_cat: dict[str, int] = {}
    for s in _ALL_SCENARIOS:
        by_cat[s.get("category", "?")] = by_cat.get(s.get("category", "?"), 0) + 1
    assert n == 20, f"expected exactly 20 scenarios, got {n}"
    assert by_cat.get("energy_cost") == 7, f"energy_cost: {by_cat.get('energy_cost')} (want 7)"
    assert by_cat.get("ops_status") == 7, f"ops_status: {by_cat.get('ops_status')} (want 7)"
    assert (
        by_cat.get("pricing_market") == 6
    ), f"pricing_market: {by_cat.get('pricing_market')} (want 6)"


# ── The gate ─────────────────────────────────────────────────────────────────


@pytest.mark.agent_sql_golden
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    _ALL_SCENARIOS,
    ids=_SCENARIO_IDS or None,
)
async def test_agent_sql_golden(
    scenario: dict[str, Any],
    agent_sql_ts_pool: Any,
    agent_sql_static_pool: Any,
    request: pytest.FixtureRequest,
) -> None:
    """Run one scenario end-to-end and assert it passes (20/20 when complete)."""
    result = await run_agent_sql_scenario(
        scenario, ts_pool=agent_sql_ts_pool, static_pool=agent_sql_static_pool
    )
    if not result.passed and request.config.getoption("--diag"):
        print(_diag_report(scenario, result))
    assert result.passed, f"[{result.scenario_id}] " + "; ".join(result.failures)
