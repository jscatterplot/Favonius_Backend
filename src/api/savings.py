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
from datetime import datetime, timezone
from typing import Optional
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


def _month_start_local_as_utc(now_utc: datetime, tz_name: Optional[str]) -> datetime:
    """First instant of the current calendar month in depot tz, returned as UTC.

    Falls back to UTC if ``tz_name`` is missing or invalid — preferable
    to a 500 here; the small month-boundary skew is acceptable for the
    "savings month-to-date" summary.
    """
    if tz_name:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:  # pragma: no cover - belt-and-braces
            tz = ZoneInfo("UTC")
    else:
        tz = ZoneInfo("UTC")

    local_now = now_utc.astimezone(tz)
    month_start_local = local_now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return month_start_local.astimezone(timezone.utc)


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


async def compute_savings_summary(
    static_pool,
    ts_pool,
    depot_id: str,
    *,
    now: Optional[datetime] = None,
) -> SavingsSummary:
    """Compute the month-to-date savings summary for one depot.

    Treats every "missing data" path as a zero-valued summary instead of
    an error: no sessions yet → zeros; no bidding zone → zero baseline;
    no electricity-price rows in the window → zero baseline. The
    frontend's "TodayEmpty" panel renders these as ``—``.

    Args:
        static_pool: Supabase / static schema pool (``sites`` + ``charging_stations``).
        ts_pool: TimescaleDB pool (``charging_sessions`` + ``electricity_prices``).
        depot_id: Depot UUID.
        now: Test injection point for "current time". Defaults to ``datetime.now(UTC)``.
    """
    as_of = now if now is not None else datetime.now(timezone.utc)

    async with static_pool.acquire() as static_conn:
        depot = await db_queries.get_depot_by_id(static_conn, depot_id)
        ocpp_id_map = await db_queries.charger_id_by_ocpp_id(
            static_conn, depot_id=depot_id
        )
        bidding_zone = await db_queries.resolve_bidding_zone(static_conn, UUID(depot_id))

    timezone_name = depot.get("timezone") if depot else None
    period_start = _month_start_local_as_utc(as_of, timezone_name)
    period_end = as_of
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
    # "unknown baseline" case and degrades to 0. A *negative* average is
    # NOT missing data: ENTSO-E day-ahead prices go negative in
    # high-renewable hours, and a depot exposed to them has a real
    # (negative) unmanaged baseline. Preserve the sign.
    baseline_cost = (
        round(total_energy_kwh * avg_price, 2) if avg_price is not None else 0.0
    )
    actual_cost = round(actual_cost, 2)
    saved = round(baseline_cost - actual_cost, 2)
    # Percentage is signed against the baseline's magnitude: a negative
    # baseline with a worse-than-baseline actual must read as a negative
    # saved_pct (we did worse), not flip positive from sign cancellation.
    # Only a zero baseline is undefined → 0.0.
    saved_pct = (
        round((saved / abs(baseline_cost)) * 100.0, 1) if baseline_cost != 0 else 0.0
    )

    return SavingsSummary(
        current_month_eur=actual_cost,
        baseline_month_eur=baseline_cost,
        saved_eur=saved,
        saved_pct=saved_pct,
        period_start=period_start,
        period_end=period_end,
        as_of=as_of,
    )
