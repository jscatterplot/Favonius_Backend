"""Orchestrator that runs the daily readiness workflow end-to-end.

Wraps :func:`run_daily_readiness_check` with the data-gathering side
effects: it calls the ``ToolBundle`` helpers, builds a
:class:`ToolCall` trace as it goes, and persists the final
:class:`ReadinessDecision` to the ``workflow_decisions`` table.

The router and the scheduler both call into here so they share a
single execution path — and therefore identical observability and
audit guarantees.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from .readiness import (
    ReadinessDecision,
    ToolCall,
    WORKFLOW_NAME,
    WORKFLOW_VERSION,
    run_daily_readiness_check,
)
from .tools import ToolBundle

logger = logging.getLogger(__name__)


# Default window the readiness check covers when the caller doesn't
# pass one. PRD §6.1 default is "60 minutes before earliest departure";
# the router/scheduler typically supply an explicit window aligned to
# the next morning, so this default is only used by ad-hoc tooling.
DEFAULT_WINDOW_HOURS = 4


async def execute_readiness_workflow(
    *,
    depot_id: UUID,
    organization_id: Optional[UUID],
    triggered_by: str,
    triggered_by_user_id: Optional[UUID],
    tools: ToolBundle,
    depot_timezone: str = "UTC",
    window_start_utc: Optional[datetime] = None,
    window_end_utc: Optional[datetime] = None,
) -> ReadinessDecision:
    """Gather inputs, run the workflow, return the :class:`ReadinessDecision`.

    Does *not* persist — the caller decides whether to write the row
    (the router always does; the scheduler always does). Splitting
    persist from compute keeps the function easy to test.

    Args:
        depot_id: Depot UUID.
        organization_id: Owning organization (``None`` for cross-org).
        triggered_by: ``'manual' | 'scheduler' | 'event'``.
        triggered_by_user_id: UUID of the triggering user when manual.
        tools: A :class:`ToolBundle` (database-backed or static).
        depot_timezone: IANA zone name. Used to format the §6.1
            ``window`` string in local time.
        window_start_utc: Start of the readiness window in UTC.
            Defaults to ``now()``.
        window_end_utc: End of the readiness window in UTC. Defaults
            to ``window_start_utc + DEFAULT_WINDOW_HOURS``.
    """
    start_ts = time.monotonic()
    now_utc = datetime.now(timezone.utc)
    if window_start_utc is None:
        window_start_utc = now_utc
    if window_end_utc is None:
        window_end_utc = window_start_utc + timedelta(hours=DEFAULT_WINDOW_HOURS)

    tz = _safe_zoneinfo(depot_timezone)
    window_start_local = window_start_utc.astimezone(tz).replace(tzinfo=None)
    window_end_local = window_end_utc.astimezone(tz).replace(tzinfo=None)

    tool_calls: list[ToolCall] = []

    departures = await _time_call(
        tool_calls,
        "get_scheduled_departures",
        {"depot_id": str(depot_id), "window_start": window_start_utc, "window_end": window_end_utc},
        lambda: tools.get_scheduled_departures(depot_id, window_start_utc, window_end_utc),
        summarise=lambda r: {"count": len(r)},
    )

    vehicle_ids = sorted({str(d["vehicle_id"]) for d in departures})
    route_ids = sorted({str(d["route_id"]) for d in departures})

    vehicle_states = await _time_call(
        tool_calls,
        "get_vehicle_state",
        {"vehicle_ids": vehicle_ids},
        lambda: tools.get_vehicle_state(vehicle_ids),
        summarise=lambda r: {"count": len(r)},
    )

    # Vehicles report their current charger via telemetry; we look up
    # state for each unique charger_id we see.
    charger_ids = sorted(
        {str(v.get("charger_id")) for v in vehicle_states.values() if v.get("charger_id")}
    )
    charger_states = await _time_call(
        tool_calls,
        "get_charger_state",
        {"charger_ids": charger_ids},
        lambda: tools.get_charger_state(charger_ids),
        summarise=lambda r: {
            "count": len(r),
            "faulted": sum(
                1
                for v in r.values()
                if str(v.get("status", "")).lower() in {"faulted", "unavailable"}
            ),
        },
    )

    charging_plans = await _time_call(
        tool_calls,
        "get_charging_plan",
        {"vehicle_ids": vehicle_ids},
        lambda: tools.get_charging_plan(vehicle_ids),
        summarise=lambda r: {"count": len(r)},
    )

    driver_assignments = await _time_call(
        tool_calls,
        "get_driver_assignment",
        {"route_ids": route_ids},
        lambda: tools.get_driver_assignment(route_ids),
        summarise=lambda r: {"count": len(r)},
    )

    alternates = await _time_call(
        tool_calls,
        "find_alternate_chargers",
        {"depot_id": str(depot_id), "kw_required": 50.0},
        lambda: tools.find_alternate_chargers(depot_id, 50.0),
        summarise=lambda r: {"count": len(r)},
    )

    duration_ms = int((time.monotonic() - start_ts) * 1000)
    decision = run_daily_readiness_check(
        depot_id=depot_id,
        organization_id=organization_id,
        triggered_by=triggered_by,
        triggered_by_user_id=triggered_by_user_id,
        window_start_local=window_start_local,
        window_end_local=window_end_local,
        departures=departures,
        vehicle_states=vehicle_states,
        charger_states=charger_states,
        charging_plans=charging_plans,
        driver_assignments=driver_assignments,
        alternate_chargers=alternates,
        tool_calls=tool_calls,
        duration_ms=duration_ms,
    )
    return decision


async def persist_decision(ts_pool: Any, decision: ReadinessDecision) -> None:
    """Insert one row into ``workflow_decisions`` (migration 037)."""
    output_json = json.dumps(decision.output.to_payload(), default=str)
    tool_calls_json = json.dumps([tc.to_dict() for tc in decision.tool_calls], default=str)
    async with ts_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO workflow_decisions (
                decision_id, workflow_name, workflow_version, depot_id,
                organization_id, triggered_by, triggered_by_user_id,
                inputs_hash, tool_calls, output, permission_tier,
                status, duration_ms, created_at
            )
            VALUES (
                $1::uuid, $2, $3, $4::uuid,
                $5::uuid, $6, $7::uuid,
                $8, $9::jsonb, $10::jsonb, $11,
                $12, $13, $14
            )
            """,
            str(decision.decision_id),
            decision.workflow_name,
            decision.workflow_version,
            str(decision.depot_id),
            str(decision.organization_id) if decision.organization_id else None,
            decision.triggered_by,
            str(decision.triggered_by_user_id) if decision.triggered_by_user_id else None,
            decision.inputs_hash,
            tool_calls_json,
            output_json,
            decision.permission_tier,
            decision.status,
            decision.duration_ms,
            decision.created_at,
        )


async def find_recent_decision(
    ts_pool: Any,
    *,
    workflow_name: str,
    depot_id: UUID,
    within_seconds: int,
) -> Optional[dict[str, Any]]:
    """Return the most recent ``workflow_decisions`` row for the depot, if fresh.

    Used by the router to back the 60-second idempotency guarantee on
    ``POST /agent-workflows/today/{depot_id}``: repeat calls inside the
    window return the cached row rather than re-running the workflow.
    """
    async with ts_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                decision_id, workflow_name, workflow_version, depot_id,
                organization_id, triggered_by, triggered_by_user_id,
                inputs_hash, tool_calls, output, permission_tier,
                status, duration_ms, created_at
            FROM workflow_decisions
            WHERE workflow_name = $1
              AND depot_id = $2::uuid
              AND created_at > NOW() - make_interval(secs => $3)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            workflow_name,
            str(depot_id),
            int(within_seconds),
        )
    return dict(row) if row else None


async def fetch_decision(ts_pool: Any, decision_id: UUID) -> Optional[dict[str, Any]]:
    """Fetch one decision row by id. ``None`` if it doesn't exist."""
    async with ts_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                decision_id, workflow_name, workflow_version, depot_id,
                organization_id, triggered_by, triggered_by_user_id,
                inputs_hash, tool_calls, output, permission_tier,
                status, duration_ms, created_at
            FROM workflow_decisions
            WHERE decision_id = $1::uuid
            """,
            str(decision_id),
        )
    return dict(row) if row else None


async def list_decisions(
    ts_pool: Any,
    *,
    workflow_name: str,
    depot_ids: list[UUID],
    since: Optional[datetime] = None,
    limit: int = 50,
    before: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """List recent decisions for an audit-log view (PRD §7.3).

    Scoped by ``depot_ids`` (the caller's ``visible_depot_ids``) so a
    customer admin cannot enumerate other tenants' decisions. ``since``
    and ``before`` are exclusive bounds for cursoring backwards through
    history; the caller passes the oldest ``created_at`` from the
    previous page as ``before`` to fetch the next page.
    """
    if not depot_ids:
        return []
    args: list[Any] = [workflow_name, [str(d) for d in depot_ids]]
    where_extra = ""
    if since is not None:
        args.append(since)
        where_extra += f" AND created_at >= ${len(args)}"
    if before is not None:
        args.append(before)
        where_extra += f" AND created_at < ${len(args)}"
    args.append(int(limit))
    query = f"""
    SELECT
        decision_id, workflow_name, workflow_version, depot_id,
        organization_id, triggered_by, triggered_by_user_id,
        inputs_hash, tool_calls, output, permission_tier,
        status, duration_ms, created_at
    FROM workflow_decisions
    WHERE workflow_name = $1
      AND depot_id = ANY($2::uuid[])
      {where_extra}
    ORDER BY created_at DESC
    LIMIT ${len(args)}
    """
    async with ts_pool.acquire() as conn:
        rows = await conn.fetch(query, *args)
    return [dict(r) for r in rows]


# ── helpers ──────────────────────────────────────────────────────────────


async def _time_call(
    trace: list[ToolCall],
    name: str,
    args: dict[str, Any],
    fn,
    summarise,
) -> Any:
    """Run ``fn()``, time it, append a :class:`ToolCall`, return the result."""
    t0 = time.monotonic()
    try:
        result = await fn()
    except Exception as exc:
        trace.append(
            ToolCall(
                name=name,
                args=_serialise_args(args),
                result_summary={"error": str(exc)},
                duration_ms=int((time.monotonic() - t0) * 1000),
            )
        )
        raise
    trace.append(
        ToolCall(
            name=name,
            args=_serialise_args(args),
            result_summary=summarise(result),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
    )
    return result


def _serialise_args(args: dict[str, Any]) -> dict[str, Any]:
    """Coerce datetimes/UUIDs to ISO/str so the trace is JSON-safe."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, UUID):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _safe_zoneinfo(name: str) -> ZoneInfo:
    """Return a ``ZoneInfo`` for ``name``, falling back to UTC on errors."""
    try:
        return ZoneInfo(name)
    except Exception:  # pragma: no cover — defence against bad depot config
        logger.warning("Unknown depot timezone %r; falling back to UTC", name)
        return ZoneInfo("UTC")
