"""Month-to-date savings summary computation.

Backs ``GET /depots/{depot_id}/savings-summary``. Returns one
:class:`SavingsSummary` per depot covering the calendar month so far
(in the depot's local timezone, projected to UTC for the response).

Baseline strategy: ``flat-rate``. The "what would you have paid without
optimization" figure is ``total_energy_kwh * avg_day_ahead_price`` over
the same window. We treat the volume-weighted average ENTSO-E price as
the unmanaged-scenario reference because (a) operators understand it,
(b) it falls naturally out of the same ``electricity_prices`` data the
optimizer already uses, and (c) it does not require us to simulate a
counterfactual schedule per session.

When prices are unknown (no bidding zone, or no price rows in the
window), baseline cost degrades to ``0.0`` rather than raising — the
frontend's "TodayEmpty" card shows ``—`` instead of an error, and any
back-fill of prices on the next run will surface real numbers.

All seven response fields are required; ``saved_pct`` is forced to
``0.0`` when baseline is zero (so the frontend never has to special-case
NaN/Infinity).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from ..db import queries as db_queries

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SavingsSummary:
    """Plain dataclass mirror of the response shape (snake_case)."""

    current_month_eur: float
    baseline_month_eur: float
    saved_eur: float
    saved_pct: float
    period_start: datetime
    period_end: datetime
    as_of: datetime
    # True when a flat-rate baseline could actually be computed (a bidding
    # zone resolved AND priced energy existed in the window). False means the
    # baseline is *unknown* (no zone / no prices / no energy), not genuinely
    # zero — callers must not interpret a 0.0 baseline as "no savings". The
    # ``/savings-summary`` route ignores this; the chat-agent savings intent
    # uses it to avoid mis-reporting "no price data" after multi-depot
    # aggregation (where signed baselines could otherwise cancel to 0).
    baseline_known: bool = True


def _safe_zone(tz_name: Optional[str]) -> ZoneInfo:
    """Resolve an IANA tz name to ``ZoneInfo``, falling back to UTC.

    A missing or invalid depot timezone degrades to UTC rather than
    raising — a small wall-clock skew on a savings window is preferable
    to a 500.
    """
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:  # pragma: no cover - belt-and-braces
            return ZoneInfo("UTC")
    return ZoneInfo("UTC")


def _month_start_local_as_utc(now_utc: datetime, tz_name: Optional[str]) -> datetime:
    """First instant of the current calendar month in depot tz, returned as UTC.

    Falls back to UTC if ``tz_name`` is missing or invalid — preferable
    to a 500 here; the small month-boundary skew is acceptable for the
    "savings month-to-date" summary.
    """
    tz = _safe_zone(tz_name)
    local_now = now_utc.astimezone(tz)
    month_start_local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return month_start_local.astimezone(timezone.utc)


# Depot-local overnight window edges: charging typically runs from the
# evening return (17:00) through the morning departure (07:00).
OVERNIGHT_START_HOUR = 17
OVERNIGHT_END_HOUR = 7


def overnight_window_utc(now_utc: datetime, tz_name: Optional[str]) -> tuple[datetime, datetime]:
    """Most-recent overnight window in depot tz, returned as UTC bounds.

    "Overnight" is the completed evening→morning window anchored on the
    depot's *current* local date: depot-local 17:00 of the previous day
    (inclusive) to 07:00 of the current day (exclusive). Asked in the
    morning this is "last night"; asked late evening it is still the
    night that just ended (deterministic, complete data) rather than the
    one in progress.

    DST-aware via :class:`ZoneInfo`: the local wall-clock edges stay
    17:00 / 07:00 across a transition while the UTC instants shift (so a
    spring-forward night spans 13 UTC hours, a fall-back night 15). Falls
    back to UTC when ``tz_name`` is missing or invalid.

    Args:
        now_utc: Current instant (UTC-aware). The window is anchored to
            this instant's local calendar day.
        tz_name: Depot IANA timezone (e.g. ``Europe/Vilnius``).

    Returns:
        ``(start_utc, end_utc)`` — both timezone-aware UTC datetimes,
        ``start_utc < end_utc``.
    """
    tz = _safe_zone(tz_name)
    local_now = now_utc.astimezone(tz)
    end_local = local_now.replace(hour=OVERNIGHT_END_HOUR, minute=0, second=0, microsecond=0)
    if local_now < end_local:
        end_local -= timedelta(days=1)
    start_local = (end_local - timedelta(days=1)).replace(
        hour=OVERNIGHT_START_HOUR, minute=0, second=0, microsecond=0
    )
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


async def _aggregate_priced_sessions(
    ts_conn,
    *,
    station_ids: list[str],
    depot_id: str,
    period_start: datetime,
    period_end: datetime,
) -> tuple[float, float]:
    """Return ``(actual_cost_eur, total_energy_kwh)`` for the period.

    Includes BOTH live OCPP rows (matched by ``station_id``) and imported
    backfill rows (matched by ``site_id``) — mirrors the OR-filter in
    :func:`db_queries.list_completed_sessions_for_depot`. Only rows where
    ``cost_total IS NOT NULL`` and ``energy_delivered_kwh IS NOT NULL``
    contribute, so unpriceable sessions don't inflate the energy total
    against a baseline they couldn't be priced against.

    Scope is ``start_time`` in ``[period_start, period_end)`` — month-to-date
    means started-this-month, including sessions still in progress.
    """
    if station_ids:
        rows = await ts_conn.fetchrow(
            """
            SELECT COALESCE(SUM(cost_total), 0)::float            AS actual,
                   COALESCE(SUM(energy_delivered_kwh), 0)::float  AS energy
              FROM charging_sessions
             WHERE (station_id = ANY($1) OR site_id = $2::uuid)
               AND start_time >= $3
               AND start_time <  $4
               AND cost_total IS NOT NULL
               AND energy_delivered_kwh IS NOT NULL
            """,
            station_ids,
            depot_id,
            period_start,
            period_end,
        )
    else:
        rows = await ts_conn.fetchrow(
            """
            SELECT COALESCE(SUM(cost_total), 0)::float            AS actual,
                   COALESCE(SUM(energy_delivered_kwh), 0)::float  AS energy
              FROM charging_sessions
             WHERE site_id = $1::uuid
               AND start_time >= $2
               AND start_time <  $3
               AND cost_total IS NOT NULL
               AND energy_delivered_kwh IS NOT NULL
            """,
            depot_id,
            period_start,
            period_end,
        )

    actual = float(rows["actual"] or 0.0)
    energy = float(rows["energy"] or 0.0)
    return actual, energy


async def _avg_price_eur_per_kwh(
    ts_conn,
    *,
    bidding_zone: str,
    period_start: datetime,
    period_end: datetime,
) -> Optional[float]:
    """Volume-unweighted average ENTSO-E day-ahead price for the window.

    Returns ``None`` if no price rows exist for the period — callers
    treat that as ``baseline=0``. We deliberately don't volume-weight by
    actual consumption: the baseline question is "what would a customer
    that didn't optimize have paid", and the most-defensible answer is
    the time-averaged price across the period.

    ``electricity_prices.lmp_price_mwh`` is stored as €/MWh; this helper
    divides by 1000 so the caller always works in €/kWh.
    """
    row = await ts_conn.fetchrow(
        """
        SELECT AVG(lmp_price_mwh)::float AS avg_eur_mwh
          FROM electricity_prices
         WHERE node_id = $1
           AND market_type = 'ENTSOE_DAM'
           AND time >= $2
           AND time <  $3
        """,
        bidding_zone,
        period_start,
        period_end,
    )
    if row is None or row["avg_eur_mwh"] is None:
        return None
    return float(row["avg_eur_mwh"]) / 1000.0


async def _compute_savings(
    static_pool,
    ts_pool,
    depot_id: str,
    *,
    now: Optional[datetime],
    window: "Callable[[dict], tuple[datetime, datetime]]",
) -> SavingsSummary:
    """Single-fetch savings core shared by both public entry points.

    Loads the depot's timezone + charger roster + bidding zone in **one**
    static-pool acquisition, then asks ``window(depot_row)`` for the UTC
    ``[start, end)`` bounds (the month-to-date wrapper needs the depot tz;
    the explicit-window wrapper ignores the row). Centralising the fetch
    here removes the previous double ``get_depot_by_id`` read.

    A *missing depot* raises ``ValueError("Depot ... not found")`` — the
    API's global handler maps "not found" to HTTP 404. Every other
    "missing data" path degrades to an *unknown* baseline
    (``baseline_known=False``) rather than raising.
    """
    as_of = now if now is not None else datetime.now(timezone.utc)

    async with static_pool.acquire() as static_conn:
        depot = await db_queries.get_depot_by_id(static_conn, depot_id)
        if depot is None:
            raise ValueError(f"Depot {depot_id} not found")
        ocpp_id_map = await db_queries.charger_id_by_ocpp_id(static_conn, depot_id=depot_id)
        bidding_zone = await db_queries.resolve_bidding_zone(static_conn, UUID(depot_id))

    period_start, period_end = window(depot)
    station_ids = list(ocpp_id_map.keys())

    async with ts_pool.acquire() as ts_conn:
        actual_cost, total_energy_kwh = await _aggregate_priced_sessions(
            ts_conn,
            station_ids=station_ids,
            depot_id=depot_id,
            period_start=period_start,
            period_end=period_end,
        )

        avg_price = None
        if bidding_zone and total_energy_kwh > 0:
            avg_price = await _avg_price_eur_per_kwh(
                ts_conn,
                bidding_zone=bidding_zone,
                period_start=period_start,
                period_end=period_end,
            )

    # avg_price is None only when there are no price rows for the window
    # (or no zone / no energy, so we never queried) — that's the genuine
    # "unknown baseline" case (baseline_known=False) and degrades to 0. A
    # *negative* average is NOT missing data: ENTSO-E day-ahead prices go
    # negative in high-renewable hours, and a depot exposed to them has a
    # real (negative) unmanaged baseline. Preserve the sign.
    baseline_known = avg_price is not None
    # Narrow on ``avg_price is not None`` directly (not the bool alias) so the
    # type checker can prove the multiplication operands are both floats.
    baseline_cost = round(total_energy_kwh * avg_price, 2) if avg_price is not None else 0.0
    actual_cost = round(actual_cost, 2)
    saved = round(baseline_cost - actual_cost, 2)
    # Percentage is signed against the baseline's magnitude: a negative
    # baseline with a worse-than-baseline actual must read as a negative
    # saved_pct (we did worse), not flip positive from sign cancellation.
    # Only a zero baseline is undefined → 0.0.
    saved_pct = round((saved / abs(baseline_cost)) * 100.0, 1) if baseline_cost != 0 else 0.0

    return SavingsSummary(
        current_month_eur=actual_cost,
        baseline_month_eur=baseline_cost,
        saved_eur=saved,
        saved_pct=saved_pct,
        period_start=period_start,
        period_end=period_end,
        as_of=as_of,
        baseline_known=baseline_known,
    )


async def compute_savings_for_window(
    static_pool,
    ts_pool,
    depot_id: str,
    *,
    period_start: datetime,
    period_end: datetime,
    now: Optional[datetime] = None,
) -> SavingsSummary:
    """Compute the savings summary for one depot over an explicit window.

    The window bounds are already resolved to UTC by the caller (the chat
    agent's savings intent for an overnight / relative window). Loads the
    depot context once and aggregates priced sessions + the average
    day-ahead price over ``[period_start, period_end)``.

    Args:
        static_pool: Supabase / static schema pool (``sites`` + ``charging_stations``).
        ts_pool: TimescaleDB pool (``charging_sessions`` + ``electricity_prices``).
        depot_id: Depot UUID.
        period_start: Inclusive UTC window start.
        period_end: Exclusive UTC window end.
        now: Test injection point for the response's ``as_of``.

    Raises:
        ValueError: When no depot exists for ``depot_id`` (→ HTTP 404).
    """
    return await _compute_savings(
        static_pool,
        ts_pool,
        depot_id,
        now=now,
        window=lambda _depot: (period_start, period_end),
    )


async def compute_savings_summary(
    static_pool,
    ts_pool,
    depot_id: str,
    *,
    now: Optional[datetime] = None,
) -> SavingsSummary:
    """Compute the month-to-date savings summary for one depot.

    The only month-specific work is placing the period start at the first
    instant of the current calendar month in the depot's local timezone
    (so a Vilnius depot's "this month" starts at Vilnius midnight on the
    1st, not UTC midnight) and ending the window at ``now``. The depot row
    is fetched once inside :func:`_compute_savings`; the window callable
    reads its timezone — no separate pre-fetch.

    Raises:
        ValueError: When no depot exists for ``depot_id`` (→ HTTP 404).
    """
    as_of = now if now is not None else datetime.now(timezone.utc)
    return await _compute_savings(
        static_pool,
        ts_pool,
        depot_id,
        now=as_of,
        window=lambda depot: (_month_start_local_as_utc(as_of, depot.get("timezone")), as_of),
    )
