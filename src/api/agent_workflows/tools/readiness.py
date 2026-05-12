"""Readiness-workflow tools for the depot agent.

The PRD (``docs/PRD_Depot_Agent.md`` §6.1) describes a daily readiness
check that, per vehicle scheduled to depart in a configurable window,
verifies the projected SoC at departure will clear the route's
``required_soc`` and that the chargers / driver assignments / charging
plans are in a working state. Each of those checks is a single typed
tool call; this module ships five tools that cover the §6.1 input list:

- :func:`get_scheduled_departures`
- :func:`get_vehicle_state`
- :func:`get_charger_state`
- :func:`get_charging_plan`
- :func:`get_driver_assignment`

Auth boundary
-------------
Every tool takes an :class:`~src.api.agent.auth_context.AuthContext` and
filters by ``visible_depot_ids``. If the referenced entity belongs to a
depot outside that set, the tool returns an empty result (lists) or
``None``-bearing dict (singletons) — never a row. This mirrors the
boundary used by ``src/api/agent/resolve.py``: a tenant user cannot see
another tenant's data even if they pass a known UUID.

SQL safety
----------
All SQL is parameterised (``$1, $2, …``); no string interpolation. The
queries are short and stable so the lint pattern stays trivial.

Empty-data contract
-------------------
Per the workflow contract, tools never raise on missing data. An empty
result set returns ``[]`` or a row of ``None`` fields with the
appropriate freshness / validity flags. The orchestrator is expected to
reason about gaps; an exception would abort the agent turn.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from src.api.agent.auth_context import AuthContext
from src.api.agent_workflows.tools.registry import register_tool
from src.security.data_freshness import MAX_TELEMETRY_AGE

# ----------------------------------------------------------------------- #
# Helpers
# ----------------------------------------------------------------------- #


def _depot_in_scope(depot_id: UUID, auth: AuthContext) -> bool:
    """Return True if ``depot_id`` is in the caller's visible-depot set."""
    return depot_id in set(auth.visible_depot_ids)


def _now(now: Optional[datetime]) -> datetime:
    """Return ``now`` if provided (test injection), else UTC now.

    The tools accept an optional ``now`` parameter so unit tests can
    fix the staleness window without monkey-patching the clock.
    """
    if now is None:
        return datetime.now(tz=timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


# ----------------------------------------------------------------------- #
# 1. get_scheduled_departures
# ----------------------------------------------------------------------- #


@register_tool("get_scheduled_departures")
async def get_scheduled_departures(
    auth: AuthContext,
    static_pool: Any,
    depot_id: UUID,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    """Return scheduled departures for ``depot_id`` in [start, end).

    Joins ``schedules`` against ``vehicles`` to scope by ``site_id``,
    then filters to the requested time window. Caller's visible depots
    gate the result: a depot outside ``auth.visible_depot_ids`` returns
    an empty list (no 404 is appropriate inside a tool — the orchestrator
    will see the empty list and reason about it).
    """
    if not _depot_in_scope(depot_id, auth):
        return []

    rows = await static_pool.fetch(
        """
        SELECT
            s.vehicle_id,
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
        depot_id,
        window_start,
        window_end,
    )
    return [
        {
            "vehicle_id": row["vehicle_id"],
            "route_id": row["route_id"],
            "departure_time": row["departure_time"],
            "required_soc": row["required_soc"],
        }
        for row in rows
    ]


# ----------------------------------------------------------------------- #
# 2. get_vehicle_state
# ----------------------------------------------------------------------- #


@register_tool("get_vehicle_state")
async def get_vehicle_state(
    auth: AuthContext,
    static_pool: Any,
    ts_pool: Any,
    vehicle_id: UUID,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Return live state for one vehicle, with freshness flag.

    Pulls the latest ``telemetry`` row keyed by ``vehicle_id``. If the
    row is older than :data:`MAX_TELEMETRY_AGE` (15 minutes, the same
    threshold the optimizer's ``StateAssembler`` enforces), the SoC /
    plug-in / charging fields are coerced to ``None`` and
    ``telemetry_fresh`` is set ``False``. The caller still sees the
    timestamp, so it can decide how to surface the gap.

    Tenant scoping is enforced by joining ``vehicles`` to ``sites`` and
    filtering on ``auth.visible_depot_ids``. A vehicle in another
    tenant's organization returns a row of ``None`` fields with
    ``vehicle_id`` set to the queried UUID — never a real row.
    """
    # First step: confirm the vehicle is visible. A single query against
    # the static pool avoids a follow-up telemetry read for cross-tenant
    # accesses.
    visible = await static_pool.fetchrow(
        """
        SELECT v.id AS vehicle_id
        FROM vehicles v
        WHERE v.id = $1
          AND v.site_id = ANY($2::uuid[])
        """,
        vehicle_id,
        auth.visible_depot_ids,
    )
    empty = {
        "vehicle_id": vehicle_id,
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
        vehicle_id,
    )
    if row is None:
        return empty

    last_at = row["last_telemetry_at"]
    fresh = (_now(now) - last_at) <= MAX_TELEMETRY_AGE
    if not fresh:
        return {
            "vehicle_id": vehicle_id,
            "current_soc": None,
            "plugged_in_to": None,
            "max_charge_kw": None,
            "last_telemetry_at": last_at,
            "telemetry_fresh": False,
        }

    # ``plugged_in_to`` only makes sense when ``is_plugged`` is true.
    # Otherwise the latest ``charger_id`` is the last charger the vehicle
    # touched, which is misleading for a readiness check.
    plugged_in_to = row["plugged_in_to"] if row["is_plugged"] else None

    return {
        "vehicle_id": vehicle_id,
        "current_soc": row["current_soc"],
        "plugged_in_to": plugged_in_to,
        "max_charge_kw": row["max_charge_kw"],
        "last_telemetry_at": last_at,
        "telemetry_fresh": True,
    }


# ----------------------------------------------------------------------- #
# 3. get_charger_state
# ----------------------------------------------------------------------- #


@register_tool("get_charger_state")
async def get_charger_state(
    auth: AuthContext,
    static_pool: Any,
    ts_pool: Any,
    charger_id: UUID,
) -> dict[str, Any]:
    """Return live state for one charger.

    Composes two reads:
    1. ``charging_stations`` (static) — confirms tenant scope via
       ``site_id`` and yields the OCPP ``station_id`` we need to query
       ``connector_status``.
    2. ``connector_status`` (time-series) — newest row per station gives
       us current OCPP status + fault_code; we ``LIMIT 1`` because the
       readiness check is per-charger, not per-connector.

    ``current_kw`` reads from the latest ``telemetry`` row for the
    charger (keyed on ``charger_id``); ``None`` if none yet. Out-of-scope
    chargers return a row of ``None`` fields.
    """
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
        charger_id,
        auth.visible_depot_ids,
    )
    empty = {
        "charger_id": charger_id,
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
        charger_id,
    )

    # last_update_at is the latest of the two sources (status or
    # telemetry); whichever is newer reflects "when did we last hear
    # anything from this charger". Both can be NULL on a brand-new
    # charger with no traffic yet.
    last_update_at = None
    if status_row is not None:
        last_update_at = status_row["timestamp"]
    if power_row is not None:
        if last_update_at is None or power_row["time"] > last_update_at:
            last_update_at = power_row["time"]

    return {
        "charger_id": charger_id,
        "status": status_row["status"] if status_row else None,
        "current_kw": power_row["charging_kw"] if power_row else None,
        "fault_code": status_row["error_code"] if status_row else None,
        "last_update_at": last_update_at,
    }


# ----------------------------------------------------------------------- #
# 4. get_charging_plan
# ----------------------------------------------------------------------- #


@register_tool("get_charging_plan")
async def get_charging_plan(
    auth: AuthContext,
    static_pool: Any,
    ts_pool: Any,
    vehicle_id: UUID,
) -> list[dict[str, Any]]:
    """Return the projected charging plan for a vehicle.

    Reads the most recent ``optimization_runs.schedule_json`` for the
    depot owning ``vehicle_id`` and unpacks the per-timestep schedule
    for that vehicle. The schedule shape is documented in
    ``src/core/optimizer/solver.py``:

        schedule_json["schedule"][vehicle_id] = {
            "charging_power": [kW per timestep],
            "soc": [projected SoC per timestep, 0..1],
        }

    Returns an empty list when:
    - The vehicle is not in the caller's scope.
    - The depot has no optimization run yet.
    - The latest run did not produce a per-vehicle entry (e.g. the
      vehicle was idle and the solver omitted it).

    The caller never has to disambiguate "no plan" from "plan is
    empty"; both are ``[]``.
    """
    vehicle = await static_pool.fetchrow(
        """
        SELECT v.id, v.site_id
        FROM vehicles v
        WHERE v.id = $1
          AND v.site_id = ANY($2::uuid[])
        """,
        vehicle_id,
        auth.visible_depot_ids,
    )
    if vehicle is None:
        return []

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
        return []

    payload = run["schedule_json"]
    # asyncpg returns JSONB as ``dict`` by default; if a test wires a
    # string into the column we accept that too.
    if isinstance(payload, str):
        import json

        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    if not isinstance(payload, dict):
        return []

    per_vehicle = (payload.get("schedule") or {}).get(str(vehicle_id))
    if per_vehicle is None:
        return []
    powers = per_vehicle.get("charging_power") or []
    socs = per_vehicle.get("soc") or []
    # Both lists are emitted by the solver in lockstep; tolerate
    # mismatched lengths by truncating to the shorter so a malformed
    # schedule still returns useful prefix instead of raising.
    n = min(len(powers), len(socs))
    return [
        {
            "timestep": i,
            "target_kw": powers[i],
            "projected_soc": socs[i],
        }
        for i in range(n)
    ]


# ----------------------------------------------------------------------- #
# 5. get_driver_assignment
# ----------------------------------------------------------------------- #


@register_tool("get_driver_assignment")
async def get_driver_assignment(
    auth: AuthContext,
    static_pool: Any,
    route_id: str,
) -> dict[str, Any]:
    """Return the driver assigned to ``route_id`` (string from schedules).

    The ``schedules.route_id`` column is a free-form string keyed by the
    upstream route system. We look up the most recent schedule row for
    that route (within the caller's visible depots) and return its
    driver assignment. ``shift_start`` / ``shift_end`` are exposed for
    API parity with the PRD §5.1 ``Driver`` dataclass but are not yet
    populated in V1 — they return ``None`` and
    ``shift_valid_for_route`` defaults to ``True`` (no evidence to
    invalidate the assignment).

    The caller still gets a useful answer when no driver is assigned:
    ``driver_id`` is ``None`` and ``shift_valid_for_route`` is
    ``False`` (an unassigned route cannot be covered by a shift).
    """
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
        return {
            **empty,
            "shift_valid_for_route": False,
        }

    return {
        "route_id": route_id,
        "driver_id": row["driver_id"],
        "driver_name": row["driver_name"],
        # Shifts not yet plumbed; return None and treat the assignment
        # as valid. The readiness orchestrator will tighten this once
        # the shift table lands.
        "shift_start": None,
        "shift_end": None,
        "shift_valid_for_route": True,
    }
