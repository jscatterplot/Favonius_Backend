"""Compiler for the ``consumption_by_user`` intent.

Aggregates ``charging_sessions`` rows for one or more drivers over a
time window, grouped by depot-local day. The driver-vs-card branch
matters: OCPP sessions land with ``card_id`` populated (from RFID
Authorize) and ``driver_id`` is derived later via
``rfid_card_driver_assignments``. Filtering on ``driver_id`` alone
silently drops sessions whose card was unassigned at the time of
charging or whose driver mapping arrived late. The compiler ORs both
columns so the formatter can attribute the rows in Python.

See ``docs/plans/agent_search_architecture_v0.md`` §3.6 and §5 for the
full design and the index requirements (migration 026).
"""

from __future__ import annotations

from typing import Any

from src.api.agent.auth_context import ResolvedEntity, ResolvedTimeWindow
from src.api.agent.plan import QueryPlan

# Bucketing is on depot-local day; ``$5`` is the IANA zone string.
# NULLS LAST keeps card-only (driver_id IS NULL) rows at the bottom of
# each daily group rather than letting Postgres' default NULLS FIRST
# rearrange the formatter's per-driver narrative.
_SQL = """
        SELECT
            cs.driver_id,
            cs.card_id,
            DATE_TRUNC('day', cs.start_time AT TIME ZONE $5) AS day_local,
            SUM(cs.energy_delivered_kwh) AS energy_kwh,
            SUM(cs.cost_total)           AS cost_total,
            COUNT(*)                     AS session_count
        FROM charging_sessions cs
        WHERE (
            cs.driver_id = ANY($1::uuid[])
            OR cs.card_id  = ANY($2::uuid[])
          )
          AND cs.start_time >= $3
          AND cs.start_time <  $4
        GROUP BY cs.driver_id, cs.card_id, day_local
        ORDER BY day_local, cs.driver_id NULLS LAST
"""


def compile_consumption_by_user(
    plan: QueryPlan,
    resolved: list[ResolvedEntity],
    window: ResolvedTimeWindow,
) -> tuple[str, list[Any]]:
    """Compile a ``consumption_by_user`` plan to parameterized SQL.

    Args:
        plan: The validated :class:`QueryPlan` from the LLM. The intent
            literal is consumed implicitly via this dispatch path.
        resolved: Server-side resolved entities. Drivers with a
            ``primary_id`` are subjects; everything else is ignored
            here (the resolver-level "not found" handling already ran
            before this compiler is reached, so reaching this point
            with no driver subjects is a programming error worth
            raising for).
        window: The resolved UTC bounds plus the depot timezone the
            bounds were computed against.

    Returns:
        A ``(sql, params)`` tuple where ``params`` matches the
        ``$1..$5`` placeholders in order:
        ``[driver_ids, card_ids, start_utc, end_utc, timezone]``.

    Raises:
        ValueError: When ``resolved`` contains no driver entity with a
            populated ``primary_id``.
    """
    drivers = [e for e in resolved if e.kind == "driver" and e.primary_id is not None]
    if not drivers:
        raise ValueError(
            "consumption_by_user requires at least one resolved driver subject; "
            "got none. The resolver should have surfaced this as 'not found' "
            "before calling the compiler."
        )

    driver_ids: list[Any] = [e.primary_id for e in drivers]
    card_ids: list[Any] = [c for e in drivers for c in e.card_ids]

    params: list[Any] = [
        driver_ids,
        card_ids,
        window.start_utc,
        window.end_utc,
        window.timezone,
    ]
    return _SQL, params
