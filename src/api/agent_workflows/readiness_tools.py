"""Readiness-workflow tools for the depot agent — Sprint 4.

The PRD (``docs/PRD_Depot_Agent.md`` §6.1) describes a daily readiness
check that verifies each vehicle scheduled to depart in a window will
clear its route's ``required_soc``, that the chargers / driver
assignments / charging plans are in a working state, and surfaces any
gaps to the depot manager. Each of those checks is a single typed tool
call.

This module ships the five tools §6.1 lists. They plug into the
Sprint-2 :class:`~src.api.agent_workflows.tools.ToolRegistry` runtime
(``src/api/agent_workflows/runtime.py::WorkflowAgent.run_turn``), which
dispatches the LLM's tool-use as ``await fn(**llm_input)``. To keep
that signature stable while still scoping every read to the caller's
visible depots, we expose a **factory**:

    registry = build_readiness_tool_registry(
        static_pool=...,
        ts_pool=...,
        auth=auth_context,
        depot_id=depot_id,
    )

The factory captures the auth context and pools in closures, so each
tool callable takes only the ``**kwargs`` the LLM input schema
declares (no auth threading through Anthropic).

Auth boundary
-------------
Every tool filters by :attr:`AuthContext.visible_depot_ids` against
``sites.organization_id`` — the same boundary the chat agent's
``src/api/agent/resolve.py`` enforces. Out-of-scope reads return
``[]`` or a ``None``-bearing dict; they never raise. Raising on missing
data would abort the agent turn, and the workflow orchestrator is the
layer that decides how to react to gaps.

SQL safety
----------
All SQL is parameterised (``$1, $2, …``). LLM-provided strings are
coerced to ``UUID`` via :func:`uuid.UUID` so a malformed value surfaces
as a `tool_failure` to the runtime instead of getting interpolated.

Telemetry freshness
-------------------
The 15-minute staleness threshold matches the optimizer's
``StateAssembler``: we import
:data:`src.security.data_freshness.MAX_TELEMETRY_AGE` rather than
redefining it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from src.api.agent.auth_context import AuthContext
from src.api.agent_workflows.tools import ToolRegistry
from src.security.data_freshness import MAX_TELEMETRY_AGE

# ----------------------------------------------------------------------- #
# JSON schemas (one per tool — exposed to the LLM via the registry).
# ----------------------------------------------------------------------- #


_OBJECT_NO_EXTRA: dict[str, Any] = {"additionalProperties": False}


_SCHEDULED_DEPARTURES_SCHEMA: dict[str, Any] = {
    "type": "object",
    **_OBJECT_NO_EXTRA,
    "properties": {
        "depot_id": {"type": "string", "format": "uuid"},
        "window_start": {"type": "string", "format": "date-time"},
        "window_end": {"type": "string", "format": "date-time"},
    },
    "required": ["depot_id", "window_start", "window_end"],
}


_VEHICLE_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    **_OBJECT_NO_EXTRA,
    "properties": {"vehicle_id": {"type": "string", "format": "uuid"}},
    "required": ["vehicle_id"],
}


_CHARGER_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    **_OBJECT_NO_EXTRA,
    "properties": {"charger_id": {"type": "string", "format": "uuid"}},
    "required": ["charger_id"],
}


_ROUTE_ID_SCHEMA: dict[str, Any] = {
    "type": "object",
    **_OBJECT_NO_EXTRA,
    "properties": {"route_id": {"type": "string"}},
    "required": ["route_id"],
}


# ----------------------------------------------------------------------- #
# Helpers
# ----------------------------------------------------------------------- #


def _depot_in_scope(depot_id: UUID, auth: AuthContext) -> bool:
    return depot_id in set(auth.visible_depot_ids)


def _parse_uuid(value: Any) -> UUID:
    """Coerce a value (str | UUID) to ``UUID``. Bubble up ``ValueError``."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _parse_datetime(value: Any) -> datetime:
    """Coerce an ISO-8601 string to a timezone-aware ``datetime``.

    The LLM may produce ``...Z`` (UTC zulu); :py:meth:`datetime.fromisoformat`
    in Python 3.11+ handles that. Naive datetimes get coerced to UTC.
    """
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _now(now: Optional[datetime]) -> datetime:
    """Return ``now`` if provided (test injection), else UTC now."""
    if now is None:
        return datetime.now(tz=timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


# ----------------------------------------------------------------------- #
# Tool factories
# ----------------------------------------------------------------------- #


def _make_get_scheduled_departures(
    static_pool: Any,
    auth: AuthContext,
):
    async def get_scheduled_departures(
        depot_id: str,
        window_start: str,
        window_end: str,
    ) -> dict[str, Any]:
        """List scheduled departures for ``depot_id`` in [start, end)."""
        depot_uuid = _parse_uuid(depot_id)
        start_dt = _parse_datetime(window_start)
        end_dt = _parse_datetime(window_end)

        if not _depot_in_scope(depot_uuid, auth):
            return {"departures": []}

        rows = await static_pool.fetch(
            """
            SELECT
                s.vehicle_id::text  AS vehicle_id,
                s.route_id,
                s.departure_time,
                s.required_soc
            FROM schedules s
            JOIN vehicles v ON v.id = s.vehicle_id
            WHERE v.site_id = $1
              AND s.departure_time >= $2
              AND s.departure_time <  $3
            ORDER BY s.departure_time
            """,
            depot_uuid,
            start_dt,
            end_dt,
        )
        return {
            "departures": [
                {
                    "vehicle_id": row["vehicle_id"],
                    "route_id": row["route_id"],
                    "departure_time": row["departure_time"].isoformat(),
                    "required_soc": row["required_soc"],
                }
                for row in rows
            ]
        }

    return get_scheduled_departures


def _make_get_vehicle_state(
    static_pool: Any,
    ts_pool: Any,
    auth: AuthContext,
    *,
    now_fn,
):
    async def get_vehicle_state(vehicle_id: str) -> dict[str, Any]:
        """Return live state for one vehicle with a freshness flag."""
        vid = _parse_uuid(vehicle_id)
        visible = await static_pool.fetchrow(
            """
            SELECT v.id AS vehicle_id
            FROM vehicles v
            WHERE v.id = $1
              AND v.site_id = ANY($2::uuid[])
            """,
            vid,
            auth.visible_depot_ids,
        )
        empty = {
            "vehicle_id": str(vid),
            "current_soc": None,
            "plugged_in_to": None,
            "max_charge_kw": None,
            "last_telemetry_at": None,
            "telemetry_fresh": False,
        }
        if visible is None:
            return empty

        row = await ts_pool.fetchrow(
            """
            SELECT
                time            AS last_telemetry_at,
                soc             AS current_soc,
                charger_id      AS plugged_in_to,
                max_charge_kw,
                is_plugged
            FROM telemetry
            WHERE vehicle_id = $1
            ORDER BY time DESC
            LIMIT 1
            """,
            vid,
        )
        if row is None:
            return empty

        last_at = row["last_telemetry_at"]
        fresh = (now_fn() - last_at) <= MAX_TELEMETRY_AGE
        if not fresh:
            return {
                "vehicle_id": str(vid),
                "current_soc": None,
                "plugged_in_to": None,
                "max_charge_kw": None,
                "last_telemetry_at": last_at.isoformat(),
                "telemetry_fresh": False,
            }

        plugged_in_to = row["plugged_in_to"] if row["is_plugged"] else None
        return {
            "vehicle_id": str(vid),
            "current_soc": row["current_soc"],
            "plugged_in_to": str(plugged_in_to) if plugged_in_to else None,
            "max_charge_kw": row["max_charge_kw"],
            "last_telemetry_at": last_at.isoformat(),
            "telemetry_fresh": True,
        }

    return get_vehicle_state


def _make_get_charger_state(
    static_pool: Any,
    ts_pool: Any,
    auth: AuthContext,
):
    async def get_charger_state(charger_id: str) -> dict[str, Any]:
        cid = _parse_uuid(charger_id)
        station = await static_pool.fetchrow(
            """
            SELECT
                cs.id         AS charger_id,
                cs.station_id AS ocpp_id,
                cs.site_id
            FROM charging_stations cs
            WHERE cs.id = $1
              AND cs.site_id = ANY($2::uuid[])
            """,
            cid,
            auth.visible_depot_ids,
        )
        empty = {
            "charger_id": str(cid),
            "status": None,
            "current_kw": None,
            "fault_code": None,
            "last_update_at": None,
        }
        if station is None:
            return empty

        status_row = await ts_pool.fetchrow(
            """
            SELECT status, error_code, timestamp
            FROM connector_status
            WHERE station_id = $1
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            station["ocpp_id"],
        )
        power_row = await ts_pool.fetchrow(
            """
            SELECT charging_kw, time
            FROM telemetry
            WHERE charger_id = $1
            ORDER BY time DESC
            LIMIT 1
            """,
            cid,
        )

        last_update_at = None
        if status_row is not None:
            last_update_at = status_row["timestamp"]
        if power_row is not None:
            if last_update_at is None or power_row["time"] > last_update_at:
                last_update_at = power_row["time"]

        return {
            "charger_id": str(cid),
            "status": status_row["status"] if status_row else None,
            "current_kw": power_row["charging_kw"] if power_row else None,
            "fault_code": status_row["error_code"] if status_row else None,
            "last_update_at": last_update_at.isoformat() if last_update_at else None,
        }

    return get_charger_state


def _make_get_charging_plan(
    static_pool: Any,
    ts_pool: Any,
    auth: AuthContext,
):
    async def get_charging_plan(vehicle_id: str) -> dict[str, Any]:
        vid = _parse_uuid(vehicle_id)
        vehicle = await static_pool.fetchrow(
            """
            SELECT v.id, v.site_id
            FROM vehicles v
            WHERE v.id = $1
              AND v.site_id = ANY($2::uuid[])
            """,
            vid,
            auth.visible_depot_ids,
        )
        if vehicle is None:
            return {"plan": []}

        run = await ts_pool.fetchrow(
            """
            SELECT schedule_json, horizon_start
            FROM optimization_runs
            WHERE depot_id = $1
            ORDER BY run_time DESC
            LIMIT 1
            """,
            vehicle["site_id"],
        )
        if run is None or not run["schedule_json"]:
            return {"plan": []}

        payload = run["schedule_json"]
        if isinstance(payload, str):
            import json

            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return {"plan": []}
        if not isinstance(payload, dict):
            return {"plan": []}

        schedule = payload.get("schedule")
        if not isinstance(schedule, dict):
            return {"plan": []}
        per_vehicle = schedule.get(str(vid))
        if not isinstance(per_vehicle, dict):
            return {"plan": []}
        powers = per_vehicle.get("charging_power") or []
        socs = per_vehicle.get("soc") or []
        n = min(len(powers), len(socs))
        return {
            "plan": [
                {
                    "timestep": i,
                    "target_kw": powers[i],
                    "projected_soc": socs[i],
                }
                for i in range(n)
            ]
        }

    return get_charging_plan


def _make_get_driver_assignment(
    static_pool: Any,
    auth: AuthContext,
):
    async def get_driver_assignment(route_id: str) -> dict[str, Any]:
        empty = {
            "route_id": route_id,
            "driver_id": None,
            "driver_name": None,
            "shift_start": None,
            "shift_end": None,
            "shift_valid_for_route": False,
        }
        row = await static_pool.fetchrow(
            """
            SELECT
                s.driver_id,
                d.display_name AS driver_name,
                s.departure_time,
                s.return_time
            FROM schedules s
            JOIN vehicles v ON v.id = s.vehicle_id
            LEFT JOIN drivers d ON d.id = s.driver_id
            WHERE s.route_id = $1
              AND v.site_id = ANY($2::uuid[])
            ORDER BY s.departure_time DESC
            LIMIT 1
            """,
            route_id,
            auth.visible_depot_ids,
        )
        if row is None:
            return empty

        if row["driver_id"] is None:
            return {**empty, "shift_valid_for_route": True}

        # Shifts are not yet plumbed in V1; surface None and treat the
        # assignment as valid (no evidence to invalidate it). The
        # readiness orchestrator will tighten this once the shift table
        # lands.
        return {
            "route_id": route_id,
            "driver_id": str(row["driver_id"]),
            "driver_name": row["driver_name"],
            "shift_start": None,
            "shift_end": None,
            "shift_valid_for_route": True,
        }

    return get_driver_assignment


# ----------------------------------------------------------------------- #
# Public factory
# ----------------------------------------------------------------------- #


def build_readiness_tool_registry(
    *,
    static_pool: Any,
    ts_pool: Any,
    auth: AuthContext,
    now: Optional[datetime] = None,
) -> ToolRegistry:
    """Build a Sprint-2 :class:`ToolRegistry` for the readiness workflow.

    The returned registry has the five tools §6.1 lists, each bound to
    ``static_pool`` / ``ts_pool`` / ``auth`` via closure. The registry
    is what the caller passes to
    :meth:`~src.api.agent_workflows.runtime.WorkflowAgent.run_turn` as
    ``tool_registry``; the runtime then dispatches each LLM tool call
    as ``await fn(**llm_input)``.

    Args:
        static_pool: asyncpg pool (or anything with ``fetch`` /
            ``fetchrow``) pointed at the Supabase-shaped static schema.
        ts_pool: asyncpg pool pointed at the time-series schema
            (``telemetry``, ``connector_status``, ``optimization_runs``).
        auth: The caller's :class:`AuthContext`. Tools filter every
            read by ``auth.visible_depot_ids``.
        now: Optional clock override for the telemetry freshness
            check. Defaults to ``datetime.now(timezone.utc)``. Tests
            pass a fixed datetime to make the staleness boundary
            deterministic.

    Returns:
        A fully populated :class:`ToolRegistry`. Callers may
        ``unregister`` or augment it before handing it to the runtime.
    """
    registry = ToolRegistry()

    def _now_fn() -> datetime:
        return _now(now)

    registry.register(
        "get_scheduled_departures",
        description=(
            "List the depot's scheduled departures whose departure_time falls in "
            "[window_start, window_end). Each item has vehicle_id, route_id, "
            "departure_time (ISO-8601), required_soc (0..1)."
        ),
        input_schema=_SCHEDULED_DEPARTURES_SCHEMA,
        fn=_make_get_scheduled_departures(static_pool, auth),
    )
    registry.register(
        "get_vehicle_state",
        description=(
            "Return live state for one vehicle: current_soc, plugged_in_to "
            "(charger uuid or null), max_charge_kw, last_telemetry_at, and "
            "telemetry_fresh (false when the latest telemetry is older than 15 "
            "minutes — in that case current_soc / plugged_in_to / max_charge_kw "
            "are null and the caller must surface the staleness)."
        ),
        input_schema=_VEHICLE_ID_SCHEMA,
        fn=_make_get_vehicle_state(static_pool, ts_pool, auth, now_fn=_now_fn),
    )
    registry.register(
        "get_charger_state",
        description=(
            "Return live OCPP state for one charger: status (latest "
            "connector_status), current_kw, fault_code (set when status="
            "'Faulted'), and last_update_at (newest of status and telemetry)."
        ),
        input_schema=_CHARGER_ID_SCHEMA,
        fn=_make_get_charger_state(static_pool, ts_pool, auth),
    )
    registry.register(
        "get_charging_plan",
        description=(
            "Return the per-timestep charging plan for one vehicle from the "
            "most recent optimization run for its depot. Each item is "
            "{timestep, target_kw, projected_soc}. Empty when the vehicle has "
            "no plan in the latest run."
        ),
        input_schema=_VEHICLE_ID_SCHEMA,
        fn=_make_get_charging_plan(static_pool, ts_pool, auth),
    )
    registry.register(
        "get_driver_assignment",
        description=(
            "Return the driver assigned to route_id: driver_id, driver_name, "
            "shift_start, shift_end, shift_valid_for_route. driver_id is null "
            "when no driver is assigned; shift_start/shift_end are null in V1 "
            "(shift table not yet plumbed) and shift_valid_for_route defaults "
            "to true when a driver is assigned."
        ),
        input_schema=_ROUTE_ID_SCHEMA,
        fn=_make_get_driver_assignment(static_pool, auth),
    )
    return registry


__all__ = ["build_readiness_tool_registry"]
