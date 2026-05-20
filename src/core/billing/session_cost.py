"""Per-charging-session electricity cost calculator.

Two strategies, one dispatcher.

1. **Granular** — When the session has telemetry covering ≥80% of
   [start_time, end_time] AND the telemetry-implied energy reconciles to
   within ±10% of ``charging_sessions.energy_delivered_kwh``, the cost is
   computed in TimescaleDB via ``time_bucket('1 hour', telemetry.time)``:
   trapezoidal integration of ``charging_kw`` per hour bucket, JOINed
   against ``prices`` on the bucket boundary. Result reflects exactly
   what was drawn when prices were what they were.

2. **Fallback average** — When telemetry is missing or fails the gate
   above but ``energy_delivered_kwh`` and the start/end timestamps are
   trustworthy AND ``prices`` cover the window (gap ≤ 1h), the cost is
   ``energy_delivered_kwh × avg(price over [start, end])``. This is the
   only viable approach for imported historical rows.

The chosen strategy and the gate's verdict are recorded in
``charging_sessions.cost_total_source`` (migration 040). Billing never
fabricates a price — if no prices cover the window, the row is left
``cost_total = NULL`` with ``cost_total_source = 'unpriceable'`` for the
operator to investigate.

Currency note: ``prices.energy_kwh`` is treated as the depot's currency
per kWh (the same assumption the optimizer's ``_get_prices`` makes). The
ENTSO-E feeder writes €/MWh ÷ 1000 = €/kWh; for non-EUR depots this
would be wrong. Out of scope for this module; see the follow-up tracked
in CLAUDE.md.

Fire-and-forget caveat: the OCPP close path schedules a post-commit
``asyncio.create_task`` that runs ``compute_session_cost`` +
``write_session_cost``. If the process dies before the task completes,
the row stays NULL and the backfill script picks it up on the next run
(its candidate predicate naturally catches this).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping, Optional, Protocol
from uuid import UUID

import asyncpg

from ...db.queries import fetch_prices_by_zone

logger = logging.getLogger(__name__)

# Strategy gate thresholds. Tuneable here, not at call sites.
TELEMETRY_COVERAGE_MIN = 0.80  # ≥80% of session duration covered
ENERGY_RECONCILE_TOL = 0.10  # ±10% vs energy_delivered_kwh
MAX_PRICE_GAP = timedelta(hours=1)  # forward-fill window


SessionCostSource = Literal[
    "granular",
    "fallback_average",
    "unpriceable",
    "pending_close",
    "no_energy",
    "no_depot",
    "manual",
]


@dataclass(frozen=True)
class SessionCostResult:
    """Outcome of one cost calculation.

    ``cost`` is ``None`` for any non-priceable state. ``source`` is the
    provenance string that goes into ``charging_sessions.cost_total_source``.
    The remaining fields are diagnostics — useful in tests and logs but
    not persisted.
    """

    cost: Optional[Decimal]
    source: SessionCostSource
    energy_kwh_from_telemetry: Optional[float] = None
    avg_price_used: Optional[float] = None
    telemetry_coverage: Optional[float] = None


class PriceLookup(Protocol):
    """Caller-injectable cache for the backfill script.

    The default implementation hits ``electricity_prices`` for every
    call; the backfill script wraps this with an LRU keyed by
    ``(bidding_zone, hour_floor_utc)`` so repeat zones within a chunk
    don't re-query. The protocol shape matches
    ``fetch_prices_by_zone`` so a None default in
    :func:`compute_session_cost` just calls the helper.
    """

    async def __call__(
        self,
        bidding_zone: str,
        start: datetime,
        end: datetime,
    ) -> dict[datetime, float]: ...


async def compute_session_cost(
    ts_pool: asyncpg.Pool,
    session_row: Mapping[str, Any],
    *,
    price_lookup: Optional[PriceLookup] = None,
) -> SessionCostResult:
    """Return the cost of one charging session.

    Args:
        ts_pool: TimescaleDB connection pool.
        session_row: Row from ``charging_sessions`` augmented by the
            caller with a ``bidding_zone`` key (resolved via
            :func:`src.db.queries.resolve_bidding_zone`). Must also
            include ``session_id``, ``site_id``, ``vehicle_id``,
            ``start_time``, ``end_time``, ``energy_delivered_kwh``,
            ``cost_total``, ``cost_total_source``. Extra keys are ignored.
            A missing or ``None`` ``bidding_zone`` short-circuits to
            ``unpriceable``.
        price_lookup: Optional injected price lookup (used by the
            backfill script to share a cache across rows). Defaults to
            a fresh ``fetch_prices_by_zone`` call.

    Returns:
        :class:`SessionCostResult`. Never raises on data shape — every
        unreachable-looking case returns an explicit source value.
    """
    # 0. Refuse to overwrite a manual / externally-supplied cost.
    existing_cost = session_row.get("cost_total")
    existing_source = session_row.get("cost_total_source")
    if existing_source == "manual" or (
        existing_cost is not None
        and Decimal(existing_cost) != Decimal(0)
        and existing_source not in (None, "fallback_average", "unpriceable")
    ):
        return SessionCostResult(
            cost=Decimal(existing_cost) if existing_cost is not None else None,
            source="manual",
        )

    end_time = session_row.get("end_time")
    if end_time is None:
        return SessionCostResult(cost=None, source="pending_close")

    site_id = session_row.get("site_id")
    if site_id is None:
        return SessionCostResult(cost=None, source="no_depot")

    start_time = session_row["start_time"]
    energy_delivered_kwh = session_row.get("energy_delivered_kwh")
    # charging_sessions.vehicle_id is VARCHAR; telemetry.vehicle_id is UUID.
    # Coerce so asyncpg binds the right type.
    vehicle_id_raw = session_row.get("vehicle_id")
    vehicle_id: Optional[UUID]
    if vehicle_id_raw is None:
        vehicle_id = None
    elif isinstance(vehicle_id_raw, UUID):
        vehicle_id = vehicle_id_raw
    else:
        try:
            vehicle_id = UUID(str(vehicle_id_raw))
        except (ValueError, TypeError):
            vehicle_id = None

    # The caller resolves the bidding zone (Supabase ``sites`` row →
    # ENTSO-E EIC code) and stuffs it into the row dict before calling.
    # No zone → no priceable map → ``unpriceable``.
    bidding_zone = session_row.get("bidding_zone")
    if not isinstance(bidding_zone, str) or not bidding_zone:
        return SessionCostResult(cost=None, source="unpriceable")

    # Fetch prices once for the whole window. Both strategies read this map.
    if price_lookup is None:
        async with ts_pool.acquire() as conn:
            price_map = await fetch_prices_by_zone(
                conn,
                bidding_zone,
                start_time,
                end_time,
                forward_fill_window=MAX_PRICE_GAP,
            )
    else:
        price_map = await price_lookup(bidding_zone, start_time, end_time)

    station_id = session_row.get("station_id")
    connector_id_raw = session_row.get("connector_id")
    connector_id: Optional[int]
    if connector_id_raw is None:
        connector_id = None
    else:
        try:
            connector_id = int(connector_id_raw)
        except (TypeError, ValueError):
            connector_id = None
    transaction_id_raw = session_row.get("transaction_id")
    transaction_id: Optional[int]
    if transaction_id_raw is None:
        transaction_id = None
    else:
        try:
            transaction_id = int(transaction_id_raw)
        except (TypeError, ValueError):
            transaction_id = None

    # 1. Try granular (telemetry-driven).
    granular = await _try_granular(
        ts_pool,
        vehicle_id=vehicle_id,
        station_id=station_id if isinstance(station_id, str) else None,
        connector_id=connector_id,
        transaction_id=transaction_id,
        start_time=start_time,
        end_time=end_time,
        energy_delivered_kwh=_as_float(energy_delivered_kwh),
        price_map=price_map,
    )
    if granular is not None:
        return granular

    # 2. Fallback to average price × energy_delivered_kwh.
    delivered = _as_float(energy_delivered_kwh)
    if delivered is None or delivered <= 0:
        return SessionCostResult(cost=None, source="no_energy")

    return _fallback_average(
        delivered_kwh=delivered,
        start_time=start_time,
        end_time=end_time,
        price_map=price_map,
    )


_GRANULAR_TELEMETRY_SQL = """
            WITH bucketed AS (
                SELECT
                    t.time,
                    t.charging_kw,
                    -- LEAD across the whole vehicle/charger timeline, not
                    -- partitioned by hour. Partitioning would set
                    -- next_time = NULL for the last sample in each hour
                    -- bucket, dropping the cross-hour interval — a
                    -- systematic undercount for typical telemetry
                    -- cadences that don't align to :00.
                    LEAD(t.time) OVER (ORDER BY t.time) AS next_time,
                    LEAD(t.charging_kw) OVER (ORDER BY t.time) AS next_kw
                FROM telemetry t
                WHERE {where_clause}
                  AND t.time >= ${time_start}
                  AND t.time < ${time_end}
                  AND t.charging_kw IS NOT NULL
                  AND t.charging_kw > 0
            )
            SELECT
                time_bucket('1 hour', time) AS hour,
                MIN(time) AS first_time,
                MAX(time) AS last_time,
                COUNT(*) AS sample_count,
                -- observed_seconds is the SUM of every interval's
                -- duration. With LEAD un-partitioned, this captures
                -- cross-hour intervals too. Used by the coverage gate
                -- (instead of last-first span, which let two sparse
                -- boundary samples masquerade as full coverage).
                COALESCE(
                    SUM(EXTRACT(EPOCH FROM (next_time - time)))
                        FILTER (WHERE next_time IS NOT NULL),
                    0
                ) AS observed_seconds,
                -- Trapezoidal integration: average the two endpoints
                -- of each interval before multiplying by Δt. Earlier
                -- draft used left-Riemann (kw_i × Δt) which mis-prices
                -- ramping/tapering sessions.
                COALESCE(
                    SUM(
                        (charging_kw + COALESCE(next_kw, charging_kw)) / 2.0
                        * EXTRACT(EPOCH FROM (next_time - time)) / 3600.0
                    ) FILTER (WHERE next_time IS NOT NULL),
                    0
                ) AS energy_kwh
            FROM bucketed
            GROUP BY time_bucket('1 hour', time)
            ORDER BY hour
            """


async def _fetch_granular_telemetry_rows(
    conn: asyncpg.Connection,
    *,
    vehicle_id: Optional[UUID],
    station_id: Optional[str],
    connector_id: Optional[int],
    transaction_id: Optional[int],
    start_time: datetime,
    end_time: datetime,
) -> list[asyncpg.Record]:
    """Load bucketed telemetry for granular billing.

    Prefer ``vehicle_id`` when present; otherwise use charger keys from the
    session row (migration 035 primary key). When ``transaction_id`` is set,
    scope charger telemetry to that OCPP transaction.
    """
    time_start = 2
    time_end = 3

    if vehicle_id is not None:
        sql = _GRANULAR_TELEMETRY_SQL.format(
            where_clause="t.vehicle_id = $1",
            time_start=time_start,
            time_end=time_end,
        )
        rows = await conn.fetch(sql, vehicle_id, start_time, end_time)
        if rows:
            return rows

    if station_id is None or connector_id is None:
        return []

    if transaction_id is not None:
        sql = _GRANULAR_TELEMETRY_SQL.format(
            where_clause=("t.station_id = $1 AND t.connector_id = $2 " "AND t.transaction_id = $3"),
            time_start=4,
            time_end=5,
        )
        return await conn.fetch(sql, station_id, connector_id, transaction_id, start_time, end_time)

    sql = _GRANULAR_TELEMETRY_SQL.format(
        where_clause="t.station_id = $1 AND t.connector_id = $2",
        time_start=3,
        time_end=4,
    )
    return await conn.fetch(sql, station_id, connector_id, start_time, end_time)


async def _try_granular(
    ts_pool: asyncpg.Pool,
    *,
    vehicle_id: Optional[UUID],
    station_id: Optional[str],
    connector_id: Optional[int],
    transaction_id: Optional[int],
    start_time: datetime,
    end_time: datetime,
    energy_delivered_kwh: Optional[float],
    price_map: dict[datetime, float],
) -> Optional[SessionCostResult]:
    """Granular-strategy attempt. Returns None to signal 'use fallback'."""
    async with ts_pool.acquire() as conn:
        rows = await _fetch_granular_telemetry_rows(
            conn,
            vehicle_id=vehicle_id,
            station_id=station_id,
            connector_id=connector_id,
            transaction_id=transaction_id,
            start_time=start_time,
            end_time=end_time,
        )

    if not rows:
        return None

    # Per-bucket cost requires the price for that hour. If any bucket is
    # missing a price, granular cannot be computed honestly.
    telem_energy = sum(float(r["energy_kwh"]) for r in rows)
    if telem_energy <= 0:
        return None

    # Coverage gate: fraction of the session window that telemetry
    # actually observed. observed_seconds is the SUM of every per-
    # interval Δt produced by the granular SQL — the honest measure.
    # The earlier draft used (last_sample - first_sample) /
    # session_duration, which let two sparse samples at the window's
    # edges fake near-full coverage while a big gap in between went
    # unnoticed. Fall back to last-first when the row doesn't carry
    # observed_seconds (legacy callers / test fakes that pre-date the
    # SQL change) so this stays backward-compatible.
    observed_seconds = sum(
        float(
            r["observed_seconds"]
            if "observed_seconds" in r
            else (r["last_time"] - r["first_time"]).total_seconds()
        )
        for r in rows
    )
    session_seconds = (end_time - start_time).total_seconds()
    coverage = observed_seconds / session_seconds if session_seconds > 0 else 0.0

    # Energy reconciliation gate.
    if energy_delivered_kwh and energy_delivered_kwh > 0:
        mismatch = abs(telem_energy - energy_delivered_kwh) / energy_delivered_kwh
    else:
        mismatch = float("inf")

    if coverage < TELEMETRY_COVERAGE_MIN or mismatch > ENERGY_RECONCILE_TOL:
        logger.info(
            "Granular gate failed for vehicle %s [%s, %s): "
            "coverage=%.2f, mismatch=%.2f%%. Falling back to average.",
            vehicle_id,
            start_time,
            end_time,
            coverage,
            mismatch * 100,
        )
        return None

    # Compute cost. Every bucket must have a price; missing bucket price
    # means granular cannot run honestly — fall back to session average.
    total_cost = Decimal(0)
    for r in rows:
        hour = r["hour"]
        price = _price_for_hour(price_map, hour)
        if price is None:
            return None
        total_cost += Decimal(str(float(r["energy_kwh"]) * price))

    avg_price = total_cost / Decimal(str(telem_energy)) if telem_energy > 0 else None
    return SessionCostResult(
        cost=total_cost.quantize(Decimal("0.0001")),
        source="granular",
        energy_kwh_from_telemetry=telem_energy,
        avg_price_used=float(avg_price) if avg_price is not None else None,
        telemetry_coverage=coverage,
    )


def _fallback_average(
    *,
    delivered_kwh: float,
    start_time: datetime,
    end_time: datetime,
    price_map: dict[datetime, float],
) -> SessionCostResult:
    """Fallback strategy: total energy × average price across the window."""
    expected_hours = _expected_hour_buckets(start_time, end_time)
    covered = [h for h in expected_hours if _price_for_hour(price_map, h) is not None]
    if not expected_hours or len(covered) < len(expected_hours):
        return SessionCostResult(
            cost=None,
            source="unpriceable",
        )

    bucket_prices = [_price_for_hour(price_map, h) for h in covered]
    avg_price = sum(p for p in bucket_prices if p is not None) / len(covered)
    cost = Decimal(str(delivered_kwh * avg_price)).quantize(Decimal("0.0001"))
    return SessionCostResult(
        cost=cost,
        source="fallback_average",
        avg_price_used=avg_price,
    )


def _normalize_hour(dt: datetime) -> datetime:
    """Floor to UTC hour start for price-map lookups."""
    floored = dt.replace(minute=0, second=0, microsecond=0)
    if floored.tzinfo is None:
        return floored.replace(tzinfo=timezone.utc)
    return floored.astimezone(timezone.utc)


def _price_for_hour(price_map: dict[datetime, float], hour: datetime) -> Optional[float]:
    """Match a telemetry bucket hour to a price-map key."""
    key = _normalize_hour(hour)
    if key in price_map:
        return price_map[key]
    naive = key.replace(tzinfo=None)
    if naive in price_map:
        return price_map[naive]
    aware = naive.replace(tzinfo=timezone.utc)
    if aware in price_map:
        return price_map[aware]
    return None


def _expected_hour_buckets(start: datetime, end: datetime) -> list[datetime]:
    """Inclusive list of hour-floor timestamps covering [start, end)."""
    cursor = start.replace(minute=0, second=0, microsecond=0)
    out: list[datetime] = []
    while cursor < end:
        out.append(cursor)
        cursor += timedelta(hours=1)
    return out


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def write_session_cost(
    ts_pool: asyncpg.Pool,
    session_id: UUID,
    result: SessionCostResult,
    *,
    conn: Optional[asyncpg.Connection] = None,
) -> bool:
    """Idempotent UPDATE on charging_sessions.

    Writes ``cost_total`` and ``cost_total_source`` only when the row is
    still eligible:

    * ``cost_total IS NULL OR cost_total = 0``
    * ``cost_total_source IS DISTINCT FROM 'manual'``

    Sources ``pending_close``, ``no_depot``, ``no_energy``, and
    ``unpriceable`` are recorded for diagnostics but ``cost_total``
    stays NULL.

    Returns ``True`` if the UPDATE affected one row, ``False`` if the
    row was no longer eligible (e.g. another writer raced ahead).
    """
    if result.source == "pending_close":
        return False  # Never write for open sessions.

    cost_value = result.cost if result.source in ("granular", "fallback_average") else None

    # Write ``cost_total`` directly — no COALESCE. Historical imports
    # land ``cost_total = 0``; when the calculator returns
    # ``'unpriceable'`` / ``'no_energy'`` / ``'no_depot'`` we want the
    # column to land as NULL (the module contract) so readers can tell
    # "billing couldn't price this" apart from "session was actually
    # free". COALESCE preserved the zero, contradicting the contract.
    update_sql = """
            UPDATE charging_sessions
               SET cost_total        = $2,
                   cost_total_source = $3,
                   updated_at        = NOW()
             WHERE session_id = $1
               AND (cost_total IS NULL OR cost_total = 0)
               AND cost_total_source IS DISTINCT FROM 'manual'
            """

    if conn is not None:
        status = await conn.execute(update_sql, session_id, cost_value, result.source)
    else:
        async with ts_pool.acquire() as pooled_conn:
            status = await pooled_conn.execute(update_sql, session_id, cost_value, result.source)
    # asyncpg returns 'UPDATE <n>' for execute().
    try:
        affected = int(status.split()[-1])
    except (ValueError, IndexError):
        affected = 0
    return affected > 0
