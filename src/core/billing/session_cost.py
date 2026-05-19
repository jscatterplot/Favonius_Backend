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
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Mapping, Optional, Protocol
from uuid import UUID

import asyncpg

from ...db.queries import fetch_prices_with_fill

logger = logging.getLogger(__name__)

# Strategy gate thresholds. Tuneable here, not at call sites.
TELEMETRY_COVERAGE_MIN = 0.80   # ≥80% of session duration covered
ENERGY_RECONCILE_TOL = 0.10     # ±10% vs energy_delivered_kwh
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

    The default implementation hits ``prices`` for every call; the
    backfill script wraps this with an LRU keyed by ``(depot_id,
    hour_floor_utc)`` so repeated depots within a chunk don't re-query.
    The protocol shape matches ``fetch_prices_with_fill`` so a None
    default in :func:`compute_session_cost` just calls the helper.
    """

    async def __call__(
        self,
        depot_id: UUID,
        start: datetime,
        end: datetime,
    ) -> dict[datetime, float]:
        ...


async def compute_session_cost(
    ts_pool: asyncpg.Pool,
    session_row: Mapping[str, Any],
    *,
    price_lookup: Optional[PriceLookup] = None,
) -> SessionCostResult:
    """Return the cost of one charging session.

    Args:
        ts_pool: TimescaleDB connection pool.
        session_row: Row from ``charging_sessions``. Must include
            ``session_id``, ``site_id``, ``vehicle_id``, ``start_time``,
            ``end_time``, ``energy_delivered_kwh``, ``cost_total``,
            ``cost_total_source``. Extra keys are ignored.
        price_lookup: Optional injected price lookup (used by the
            backfill script to share a cache across rows). Defaults to
            a fresh ``fetch_prices_with_fill`` call.

    Returns:
        :class:`SessionCostResult`. Never raises on data shape — every
        unreachable-looking case returns an explicit source value.
    """
    # 0. Refuse to overwrite a manual / externally-supplied cost.
    existing_cost = session_row.get("cost_total")
    existing_source = session_row.get("cost_total_source")
    if existing_source == "manual" or (
        existing_cost is not None and Decimal(existing_cost) != Decimal(0)
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

    # Fetch prices once for the whole window. Both strategies read this map.
    if price_lookup is None:
        async with ts_pool.acquire() as conn:
            price_map = await fetch_prices_with_fill(
                conn,
                site_id,
                start_time,
                end_time,
                forward_fill_window=MAX_PRICE_GAP,
            )
    else:
        price_map = await price_lookup(site_id, start_time, end_time)

    # 1. Try granular (telemetry-driven).
    granular = await _try_granular(
        ts_pool,
        vehicle_id=vehicle_id,
        depot_id=site_id,
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


async def _try_granular(
    ts_pool: asyncpg.Pool,
    *,
    vehicle_id: Optional[UUID],
    depot_id: UUID,
    start_time: datetime,
    end_time: datetime,
    energy_delivered_kwh: Optional[float],
    price_map: dict[datetime, float],
) -> Optional[SessionCostResult]:
    """Granular-strategy attempt. Returns None to signal 'use fallback'."""
    if vehicle_id is None:
        return None

    async with ts_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                time_bucket('1 hour', t.time) AS hour,
                MIN(t.time) AS first_time,
                MAX(t.time) AS last_time,
                COUNT(*) AS sample_count,
                -- Trapezoidal: each sample's kW × seconds-to-next-sample-in-bucket / 3600.
                -- The last sample in each bucket contributes nothing (no LEAD).
                COALESCE(
                  SUM(
                    t.charging_kw *
                    EXTRACT(EPOCH FROM (LEAD(t.time) OVER w - t.time)) / 3600.0
                  ) FILTER (WHERE LEAD(t.time) OVER w IS NOT NULL),
                  0
                ) AS energy_kwh
            FROM telemetry t
            WHERE t.vehicle_id = $1
              AND t.time >= $2 AND t.time < $3
              AND t.charging_kw IS NOT NULL
              AND t.charging_kw > 0
            WINDOW w AS (PARTITION BY time_bucket('1 hour', t.time) ORDER BY t.time)
            GROUP BY hour
            ORDER BY hour
            """,
            vehicle_id,
            start_time,
            end_time,
        )

    if not rows:
        return None

    # Per-bucket cost requires the price for that hour. If any bucket is
    # missing a price, granular cannot be computed honestly.
    telem_energy = sum(float(r["energy_kwh"]) for r in rows)
    if telem_energy <= 0:
        return None

    # Coverage gate: telemetry timestamps span what fraction of the session?
    first = min(r["first_time"] for r in rows)
    last = max(r["last_time"] for r in rows)
    session_seconds = (end_time - start_time).total_seconds()
    coverage = (
        (last - first).total_seconds() / session_seconds
        if session_seconds > 0 else 0.0
    )

    # Energy reconciliation gate.
    if energy_delivered_kwh and energy_delivered_kwh > 0:
        mismatch = abs(telem_energy - energy_delivered_kwh) / energy_delivered_kwh
    else:
        mismatch = float("inf")

    if coverage < TELEMETRY_COVERAGE_MIN or mismatch > ENERGY_RECONCILE_TOL:
        logger.info(
            "Granular gate failed for vehicle %s [%s, %s): "
            "coverage=%.2f, mismatch=%.2f%%. Falling back to average.",
            vehicle_id, start_time, end_time, coverage, mismatch * 100,
        )
        return None

    # Compute cost. Every bucket must have a price.
    total_cost = Decimal(0)
    for r in rows:
        hour = r["hour"]
        price = price_map.get(hour)
        if price is None:
            # Partial pricing — refuse rather than fabricate.
            return SessionCostResult(
                cost=None,
                source="unpriceable",
                energy_kwh_from_telemetry=telem_energy,
                telemetry_coverage=coverage,
            )
        total_cost += Decimal(str(float(r["energy_kwh"]) * price))

    avg_price = (
        total_cost / Decimal(str(telem_energy)) if telem_energy > 0 else None
    )
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
    covered = [h for h in expected_hours if h in price_map]
    if not expected_hours or len(covered) < len(expected_hours):
        return SessionCostResult(
            cost=None,
            source="unpriceable",
        )

    avg_price = sum(price_map[h] for h in covered) / len(covered)
    cost = Decimal(str(delivered_kwh * avg_price)).quantize(Decimal("0.0001"))
    return SessionCostResult(
        cost=cost,
        source="fallback_average",
        avg_price_used=avg_price,
    )


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

    async with ts_pool.acquire() as conn:
        status = await conn.execute(
            """
            UPDATE charging_sessions
               SET cost_total        = COALESCE($2, cost_total),
                   cost_total_source = $3,
                   updated_at        = NOW()
             WHERE session_id = $1
               AND (cost_total IS NULL OR cost_total = 0)
               AND cost_total_source IS DISTINCT FROM 'manual'
            """,
            session_id,
            cost_value,
            result.source,
        )
    # asyncpg returns 'UPDATE <n>' for execute().
    try:
        affected = int(status.split()[-1])
    except (ValueError, IndexError):
        affected = 0
    return affected > 0
