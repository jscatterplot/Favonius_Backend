"""Compiler for the ``consumption_by_user`` intent.

Aggregates ``charging_sessions`` rows over a time window, bucketed by
depot-local period (day by default, month when the plan asks for it).
The compiler services three subject shapes:

1. **driver / RFID card** — the original path. OCPP sessions land with
   ``card_id`` populated (from RFID Authorize) and ``driver_id`` is
   derived later via ``rfid_card_driver_assignments``. Filtering on
   ``driver_id`` alone silently drops sessions whose card was unassigned
   at the time of charging, so the compiler ORs both columns and groups
   by both.
2. **vehicle / fleet** — a vehicle mention may resolve to many vehicles
   (a make or vehicle-type phrase like "the renault vans"). The resolver
   returns one entity per matched vehicle; the compiler sums across all
   of them, grouping by ``vehicle_id``. ``charging_sessions.vehicle_id``
   stores the vehicle UUID as text on both the live OCPP and the
   imported-session write paths, so the predicate compares against
   stringified UUIDs.
3. **depot-wide** — a no-subject total ("how much was consumed last
   month"). The caller passes the set of charger ``station_id``\\ s for
   the visible depots (the auth boundary); the compiler filters on
   ``station_id`` because it is the only depot linkage populated on
   every session row.

Every path also selects ``energy_sample_count`` (the count of sessions
with a non-NULL ``energy_delivered_kwh``) so the formatter can tell
"no sessions in the window" apart from "sessions exist but none recorded
energy" — instead of rendering a NULL ``SUM`` as a misleading "none
consumed". See :func:`summarize_consumption_rows`.

See ``docs/plans/agent_search_architecture_v0.md`` §3.6 and §5 for the
original design and the index requirements (migration 026 for
driver/card, migration 045 for vehicle).
"""

from __future__ import annotations

from typing import Any, Optional

from src.api.agent.auth_context import ResolvedEntity, ResolvedTimeWindow
from src.api.agent.plan import QueryPlan

# Shared aggregate column list (identical across every subject shape).
_AGG_COLS: tuple[str, ...] = (
    "SUM(cs.energy_delivered_kwh) AS energy_kwh",
    "SUM(cs.cost_total)           AS cost_total",
    "COUNT(*)                     AS session_count",
    "COUNT(cs.energy_delivered_kwh) AS energy_sample_count",
)


def _bucket(plan: QueryPlan) -> str:
    """Pick the DATE_TRUNC bucket from the plan's ``group_by``.

    Day is the default; month only when the user asked to group by month
    and did NOT also ask for day. The SELECT alias stays ``day_local``
    regardless so the formatter payload keeps a stable column name.
    """
    if "month" in plan.group_by and "day" not in plan.group_by:
        return "month"
    return "day"


def _assemble(
    select_cols: list[str],
    where: str,
    group_by: str,
    order_by: str,
) -> str:
    """Render the canonical SELECT block (stable whitespace for golden tests)."""
    cols_block = ",\n            ".join(select_cols)
    return (
        "\n        SELECT\n"
        f"            {cols_block}\n"
        "        FROM charging_sessions cs\n"
        f"{where}"
        f"        GROUP BY {group_by}\n"
        f"        ORDER BY {order_by}\n"
    )


def _compile_depot_wide(
    station_ids: list[str],
    depot_ids: list[Any],
    window: ResolvedTimeWindow,
    bucket: str,
) -> tuple[str, list[Any]]:
    """Depot-wide total: every session on the given chargers, bucketed by period."""
    select_cols = [
        f"DATE_TRUNC('{bucket}', cs.start_time AT TIME ZONE $5) AS day_local",
        *_AGG_COLS,
    ]
    where = (
        "        WHERE cs.station_id = ANY($1::text[])\n"
        "          AND cs.site_id = ANY($2::uuid[])\n"
        "          AND cs.start_time >= $3\n"
        "          AND cs.start_time <  $4\n"
    )
    sql = _assemble(select_cols, where, group_by="day_local", order_by="day_local")
    params: list[Any] = [list(station_ids), list(depot_ids), window.start_utc, window.end_utc, window.timezone]
    return sql, params


def compile_consumption_by_user(
    plan: QueryPlan,
    resolved: list[ResolvedEntity],
    window: ResolvedTimeWindow,
    *,
    station_ids: Optional[list[str]] = None,
    depot_ids: Optional[list[Any]] = None,
) -> tuple[str, list[Any]]:
    """Compile a ``consumption_by_user`` plan to parameterized SQL.

    Args:
        plan: The validated :class:`QueryPlan` from the LLM. ``group_by``
            selects day vs month bucketing.
        resolved: Server-side resolved entities. Drivers and RFID cards
            feed the ``driver_id`` / ``card_id`` filter; vehicles feed
            the ``vehicle_id`` filter (a fleet expands to many). Entities
            with ``primary_id=None`` (not found) are ignored here — the
            controller surfaces those as "not found" before compiling.
        window: The resolved UTC bounds plus the depot timezone the
            bounds were computed against.
        station_ids: When provided, compiles the **depot-wide** form
            (no subject filter) scoping to these charger OCPP ids. The
            caller resolves them from the visible depots; this is the
            tenant boundary for a no-subject total.

    Returns:
        A ``(sql, params)`` tuple whose ``params`` match the ``$1..$N``
        placeholders positionally.

    Raises:
        ValueError: For the subject form, when ``resolved`` contains no
            driver/card/vehicle subject with a populated ``primary_id``.
    """
    bucket = _bucket(plan)

    if station_ids is not None:
        return _compile_depot_wide(station_ids, depot_ids or [], window, bucket)

    drivers = [e for e in resolved if e.kind == "driver" and e.primary_id is not None]
    rfids = [e for e in resolved if e.kind == "rfid" and e.primary_id is not None]
    vehicles = [e for e in resolved if e.kind == "vehicle" and e.primary_id is not None]

    has_driver_or_card = bool(drivers or rfids)
    has_vehicle = bool(vehicles)
    if not has_driver_or_card and not has_vehicle:
        raise ValueError(
            "consumption_by_user requires at least one resolved driver, card, or "
            "vehicle subject (or station_ids for a depot-wide total); got none. "
            "The resolver should have surfaced this as 'not found' before calling "
            "the compiler."
        )

    select_id_cols: list[str] = []
    where_or: list[str] = []
    order_tail: list[str] = []
    params: list[Any] = []
    idx = 1

    if has_driver_or_card:
        driver_ids: list[Any] = [e.primary_id for e in drivers]
        card_ids: list[Any] = [c for e in drivers for c in e.card_ids]
        card_ids += [e.primary_id for e in rfids]
        select_id_cols += ["cs.driver_id", "cs.card_id"]
        order_tail.append("cs.driver_id NULLS LAST")
        where_or.append(f"cs.driver_id = ANY(${idx}::uuid[])")
        params.append(driver_ids)
        idx += 1
        where_or.append(f"cs.card_id  = ANY(${idx}::uuid[])")
        params.append(card_ids)
        idx += 1

    if has_vehicle:
        # charging_sessions.vehicle_id is VARCHAR; the resolver yields UUIDs.
        vehicle_ids = [str(e.primary_id) for e in vehicles]
        select_id_cols.append("cs.vehicle_id")
        order_tail.append("cs.vehicle_id NULLS LAST")
        where_or.append(f"cs.vehicle_id = ANY(${idx}::text[])")
        params.append(vehicle_ids)
        idx += 1

    start_idx, end_idx, tz_idx = idx, idx + 1, idx + 2
    params += [window.start_utc, window.end_utc, window.timezone]

    select_cols = list(select_id_cols)
    select_cols.append(f"DATE_TRUNC('{bucket}', cs.start_time AT TIME ZONE ${tz_idx}) AS day_local")
    select_cols += list(_AGG_COLS)

    where_block = "\n            OR ".join(where_or)
    where = (
        "        WHERE (\n"
        f"            {where_block}\n"
        "          )\n"
        f"          AND cs.start_time >= ${start_idx}\n"
        f"          AND cs.start_time <  ${end_idx}\n"
    )
    group_by = ", ".join(select_id_cols + ["day_local"])
    order_by = ", ".join(["day_local", *order_tail])
    return _assemble(select_cols, where, group_by, order_by), params


def summarize_consumption_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Collapse the per-group rows into a three-state result summary.

    The disposition distinguishes the three outcomes the formatter must
    render differently — collapsing them all to a NULL ``SUM`` is what
    made depot-wide questions answer a misleading "none consumed":

    - ``no_sessions``        — zero matching sessions in the window.
    - ``no_energy_recorded`` — sessions exist but none carry a
                               ``energy_delivered_kwh`` value (pre-meter
                               or some imported rows).
    - ``ok``                 — at least one session recorded energy.

    Totals are ``None`` (not ``0``) when nothing was recorded, so the
    formatter can say "not recorded" rather than print a false zero.
    """
    total_sessions = 0
    sessions_with_energy = 0
    total_energy = 0.0
    total_cost = 0.0
    cost_present = False
    for row in rows:
        total_sessions += int(row.get("session_count") or 0)
        sessions_with_energy += int(row.get("energy_sample_count") or 0)
        energy = row.get("energy_kwh")
        if energy is not None:
            total_energy += float(energy)
        cost = row.get("cost_total")
        if cost is not None:
            cost_present = True
            total_cost += float(cost)

    if total_sessions == 0:
        disposition = "no_sessions"
    elif sessions_with_energy == 0:
        disposition = "no_energy_recorded"
    else:
        disposition = "ok"

    return {
        "disposition": disposition,
        "total_sessions": total_sessions,
        "sessions_with_energy": sessions_with_energy,
        "total_energy_kwh": round(total_energy, 3) if sessions_with_energy else None,
        "total_cost": round(total_cost, 4) if cost_present else None,
        "group_count": len(rows),
    }
