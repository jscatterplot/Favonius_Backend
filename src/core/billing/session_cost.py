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

from ...db.queries import fetch_or_pull_prices_by_zone

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
    # ``fetch_or_pull_prices_by_zone`` checks ``electricity_prices`` first
    # and falls back to the ENTSO-E API on cache miss — that's what saves
    # billing in deployments where the WS-handler price feeder isn't
    # populating the table.
    if price_lookup is None:
        async with ts_pool.acquire() as conn:
            price_map = await fetch_or_pull_prices_by_zone(
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
            WITH samples AS (
                SELECT
                    t.time AS raw_time,
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
                  -- 15-minute lookback admits the most recent pre-start
                  -- sample so the start_time → first-in-window interval
                  -- gets integrated. Earlier draft filtered ``t.time >=
                  -- start_time`` strictly, which systematically dropped
                  -- one cadence-tick of energy at session head and
                  -- could push the coverage/mismatch gate below the
                  -- ±10% threshold for short sessions. The bucketed CTE
                  -- below clamps the anchor sample's effective time to
                  -- ``start_time`` so its interval starts at the session
                  -- boundary, not at the raw pre-start timestamp.
                  AND t.time >= ${time_start}::timestamptz - INTERVAL '15 minutes'
                  AND t.time < ${time_end}
                  AND t.charging_kw IS NOT NULL
                  AND t.charging_kw > 0
            ),
            bucketed AS (
                -- Clamp the pre-start anchor's effective sample time to
                -- ``start_time``. Samples whose entire interval is
                -- pre-session (raw_time < start_time AND next_time <=
                -- start_time) are dropped — they have no overlap.
                SELECT
                    GREATEST(raw_time, ${time_start}::timestamptz) AS time,
                    charging_kw,
                    next_time,
                    next_kw
                FROM samples
                WHERE next_time IS NULL
                   OR next_time > ${time_start}::timestamptz
            )
            SELECT
                time_bucket('1 hour', time) AS hour,
                MIN(time) AS first_time,
                MAX(time) AS last_time,
                COUNT(*) AS sample_count,
                -- observed_seconds: SUM of (i) every standard interval
                -- (sample → next sample, clipped at end_time) plus
                -- (ii) the tail interval (last sample → end_time).
                -- Without (ii), the last in-window sample contributes
                -- 0 because LEAD(time) is NULL, systematically losing
                -- 0–1 sample-cadence of coverage and energy per
                -- session (a few percent on dense telemetry). The
                -- ``next_time IS NULL`` filter selects exactly the
                -- last row of the CTE — by ORDER BY, it's the latest
                -- in-window sample.
                COALESCE(
                    SUM(
                        EXTRACT(
                            EPOCH FROM (
                                LEAST(next_time, ${time_end}::timestamptz) - time
                            )
                        )
                    ) FILTER (WHERE next_time IS NOT NULL),
                    0
                ) + COALESCE(
                    SUM(
                        EXTRACT(EPOCH FROM (${time_end}::timestamptz - time))
                    ) FILTER (WHERE next_time IS NULL),
                    0
                ) AS observed_seconds,
                -- Trapezoidal integration: average the two endpoints
                -- of each interval before multiplying by Δt. Earlier
                -- draft used left-Riemann (kw_i × Δt) which mis-prices
                -- ramping/tapering sessions. Clip each segment at
                -- end_time so LEAD past the session window is not billed.
                -- The tail term assumes the last observed kW persists
                -- to end_time (left-Riemann); we have no later sample
                -- to average with, and dropping it would systematically
                -- undercount.
                COALESCE(
                    SUM(
                        (charging_kw + COALESCE(next_kw, charging_kw)) / 2.0
                        * EXTRACT(
                            EPOCH FROM (
                                LEAST(next_time, ${time_end}::timestamptz) - time
                            )
                        ) / 3600.0
                    ) FILTER (WHERE next_time IS NOT NULL),
                    0
                ) + COALESCE(
                    SUM(
                        charging_kw
                        * EXTRACT(EPOCH FROM (${time_end}::timestamptz - time))
                        / 3600.0
                    ) FILTER (WHERE next_time IS NULL),
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

    Cascade: ``vehicle_id`` → ``(station_id, connector_id, transaction_id)``
    → ``(station_id, connector_id)``. The transaction-id stage scopes the
    query to the OCPP transaction (one-to-one with a charging_sessions row)
    when telemetry carries that column; on empty result we fall through to
    the station+connector query so legacy telemetry with NULL
    ``transaction_id`` still gets priced (e.g. MeterValues that arrived
    before the StartTransaction handler associated them, or reconnects
    that re-emitted samples without the txn id).
    """
    if vehicle_id is not None:
        sql = _GRANULAR_TELEMETRY_SQL.format(
            where_clause="t.vehicle_id = $1",
            time_start=2,
            time_end=3,
        )
        rows = await conn.fetch(sql, vehicle_id, start_time, end_time)
        if rows:
            return rows

    if station_id is None or connector_id is None:
        return []

    if transaction_id is not None:
        # First try the strict tx-scoped path. Telemetry rows from
        # earlier or later transactions on the same connector are
        # excluded — that's the whole point of carrying transaction_id.
        sql = _GRANULAR_TELEMETRY_SQL.format(
            where_clause=(
                "t.station_id = $1 AND t.connector_id = $2 "
                "AND t.transaction_id = $3"
            ),
            time_start=4,
            time_end=5,
        )
        rows = await conn.fetch(
            sql, station_id, connector_id, transaction_id, start_time, end_time,
        )
        if rows:
            return rows
        # Fall-through: catch the "MeterValues arrived before
        # StartTransaction tagged them" / reconnect-without-tx-id
        # case by also accepting samples whose transaction_id is NULL.
        # Critically, we still exclude samples whose transaction_id
        # is a *different* non-NULL value (a back-to-back or
        # overlapping session on the same connector) — otherwise
        # cross-session telemetry would bleed into one cost.
        sql = _GRANULAR_TELEMETRY_SQL.format(
            where_clause=(
                "t.station_id = $1 AND t.connector_id = $2 "
                "AND (t.transaction_id IS NULL OR t.transaction_id = $3)"
            ),
            time_start=4,
            time_end=5,
        )
        return await conn.fetch(
            sql, station_id, connector_id, transaction_id, start_time, end_time,
        )

    # Session has no transaction_id (typically imported pre-migration-035
    # rows). The unscoped station+connector path is the only granular
    # option for these; the time window provides the only safety net
    # against cross-session bleed. Acceptable for imported rows since
    # there's no transaction_id to compare against anyway.
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
    """Fallback strategy: total energy × duration-weighted average price.

    For a session spanning multiple hours (say 10:30 → 12:30), each
    hour-bucket price gets a weight equal to the fraction of the
    hour the session was actually active — 0.5h for hour 10, 1.0h
    for hour 11, 0.5h for hour 12 in this example. Earlier draft
    used an unweighted arithmetic mean across the bucket prices,
    which systematically misbilled sessions whose boundary-hour
    prices differed from mid-session prices (e.g. evening price
    spikes catching a session's tail). The weighted form collapses
    to the unweighted mean when the session is exactly aligned to
    hour boundaries, so this is a strict accuracy improvement.
    """
    expected_hours = _expected_hour_buckets(start_time, end_time)
    covered = [h for h in expected_hours if _price_for_hour(price_map, h) is not None]
    if not expected_hours or len(covered) < len(expected_hours):
        return SessionCostResult(
            cost=None,
            source="unpriceable",
        )

    # Normalise start/end to UTC-aware so the timedelta arithmetic
    # below matches the hour-floor keys (which are UTC-aware).
    # ``_normalize_hour`` floors to the hour — we need the actual
    # start/end offset within the hour to weight boundary buckets
    # by their real overlap fraction.
    if start_time.tzinfo is None:
        start_aware = start_time.replace(tzinfo=timezone.utc)
    else:
        start_aware = start_time.astimezone(timezone.utc)
    if end_time.tzinfo is None:
        end_aware = end_time.replace(tzinfo=timezone.utc)
    else:
        end_aware = end_time.astimezone(timezone.utc)

    weighted_sum = 0.0
    weight_total = 0.0
    for hour in expected_hours:
        price = _price_for_hour(price_map, hour)
        if price is None:
            continue
        hour_start = _normalize_hour(hour)
        hour_end = hour_start + timedelta(hours=1)
        overlap_start = max(start_aware, hour_start)
        overlap_end = min(end_aware, hour_end)
        overlap_seconds = (overlap_end - overlap_start).total_seconds()
        if overlap_seconds <= 0:
            continue
        weight = overlap_seconds / 3600.0
        weighted_sum += price * weight
        weight_total += weight

    if weight_total <= 0:
        # ``expected_hours`` is non-empty and all hours have prices, so
        # this only triggers when start == end (zero-duration close).
        # Treat like 'no_energy' — there's nothing to bill across.
        return SessionCostResult(cost=None, source="no_energy")

    avg_price = weighted_sum / weight_total
    cost = Decimal(str(delivered_kwh * avg_price)).quantize(Decimal("0.0001"))
    return SessionCostResult(
        cost=cost,
        source="fallback_average",
        avg_price_used=avg_price,
    )


def _normalize_hour(dt: datetime) -> datetime:
    """Floor to UTC hour start for price-map lookups.

    Convert-then-floor: for non-whole-hour offset timezones (IST +05:30,
    NPT +05:45), flooring in local time before converting to UTC yields
    a UTC datetime that's NOT an hour boundary (e.g. ``14:15+05:30``
    floored to ``14:00+05:30`` → ``08:30 UTC``), missing the price-map
    key. All current ENTSO-E zones are whole-hour offsets so the bug
    is dormant, but the canonical helper ``_hour_floor_utc`` in
    ``src/db/queries.py`` does it the right way around; bringing this
    helper into sync prevents future drift between key-write and
    key-read paths.
    """
    if dt.tzinfo is None:
        aware = dt.replace(tzinfo=timezone.utc)
    else:
        aware = dt.astimezone(timezone.utc)
    return aware.replace(minute=0, second=0, microsecond=0)


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
