"""Per-vehicle distance from CAN-bus odometer deltas.

The denominator of the EV "price per kilometre" comparison
(:mod:`src.core.billing.cost_per_km`). Distance over a window is the net change
of a vehicle's odometer between its first and last reading in that window.

Why first/last-in-window rather than per-segment integration (cf.
:mod:`session_cost`, which LEAD-integrates instantaneous kW): a vehicle's
odometer is monotonic by physics, so the net delta *is* the distance driven —
no need to sum segments. The only complication is the occasional non-monotonic
artefact (ECU swap / counter reset / rollover), handled explicitly below.

Honesty over completeness: a vehicle we cannot measure (fewer than two
odometer readings, or an incoherent reset) reports ``distance_km = None``
("unknown"), never a fabricated ``0.0``. A zero would silently understate the
fleet's distance and inflate €/km; the caller excludes unknowns from the
comparison instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# A single vehicle is implausible to drive more than this in one reporting
# window (a week). A computed distance above it is taken as a corrupt odometer
# value rather than a real trip, and reported as unknown. Generous: even a
# long-haul truck at 130 km/h non-stop for a week is ~22k km, but a depot fleet
# vehicle realistically tops out far lower — 10k/week flags clear corruption
# without rejecting heavy legitimate use.
MAX_DISTANCE_KM_PER_WINDOW = 10_000.0


@dataclass(frozen=True)
class VehicleDistance:
    """Distance outcome for one vehicle over a window.

    ``distance_km is None`` means "unknown" — too few readings or an
    unrecoverable odometer artefact. Callers must treat ``None`` as "exclude
    from the comparison", never as zero.
    """

    vehicle_id: str
    distance_km: Optional[float]
    first_odometer_km: Optional[float]
    last_odometer_km: Optional[float]
    reading_count: int
    had_reset: bool = False


# First and last odometer (by time) plus min/max and count, per vehicle, over
# the half-open [start, end) window. array_agg(... ORDER BY time) gives a
# deterministic first/last even when several readings share a timestamp.
_DISTANCE_SQL = """
    SELECT
        vehicle_id::text AS vehicle_id,
        (array_agg(odometer_km ORDER BY time ASC,  ctid ASC))[1]  AS first_odo,
        (array_agg(odometer_km ORDER BY time DESC, ctid DESC))[1] AS last_odo,
        min(odometer_km) AS min_odo,
        max(odometer_km) AS max_odo,
        count(*)         AS n
    FROM vehicle_telemetry
    WHERE vehicle_id = ANY($1::uuid[])
      AND time >= $2
      AND time <  $3
      AND odometer_km IS NOT NULL
    GROUP BY vehicle_id
"""


def _distance_from_bounds(
    *,
    first_odo: Optional[float],
    last_odo: Optional[float],
    min_odo: Optional[float],
    max_odo: Optional[float],
    n: int,
) -> tuple[Optional[float], bool]:
    """Derive (distance_km, had_reset) from a vehicle's odometer bounds.

    Pure so the edge-case logic is unit-testable without a DB.

    - ``n < 2``: cannot form a delta → unknown (None).
    - monotonic (``last >= first``): ``last - first``.
    - non-monotonic (``last < first``): treat as one mid-window reset — the
      vehicle ran ``first → max`` (pre-reset peak) then ``min → last``
      (post-reset). Use ``(max - first) + (last - min)`` when that is a sane,
      non-negative number; otherwise the artefact is unrecoverable → unknown.
    - any result above :data:`MAX_DISTANCE_KM_PER_WINDOW` is treated as corrupt
      → unknown (flagged ``had_reset`` so the caller can surface it).
    """
    if n < 2 or first_odo is None or last_odo is None:
        return None, False

    if last_odo >= first_odo:
        distance = last_odo - first_odo
        if distance > MAX_DISTANCE_KM_PER_WINDOW:
            return None, True
        return distance, False

    # last < first → odometer went backwards within the window (reset/rollover).
    if min_odo is None or max_odo is None:
        return None, True
    recovered = (max_odo - first_odo) + (last_odo - min_odo)
    if recovered < 0 or recovered > MAX_DISTANCE_KM_PER_WINDOW:
        return None, True
    return recovered, True


async def compute_distances_for_depot(
    ts_pool: Any,
    *,
    vehicle_ids: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, VehicleDistance]:
    """Return ``{vehicle_id: VehicleDistance}`` for the given vehicles + window.

    One query over ``vehicle_telemetry``. Vehicles with no odometer readings in
    the window are present in the result with ``distance_km=None`` and
    ``reading_count=0`` so the caller can distinguish "unknown" from "absent".
    """
    # Seed every requested vehicle as unknown so the caller always gets a row.
    result: dict[str, VehicleDistance] = {
        vid: VehicleDistance(
            vehicle_id=vid,
            distance_km=None,
            first_odometer_km=None,
            last_odometer_km=None,
            reading_count=0,
        )
        for vid in vehicle_ids
    }
    if not vehicle_ids:
        return result

    async with ts_pool.acquire() as conn:
        rows = await conn.fetch(_DISTANCE_SQL, vehicle_ids, start, end)

    for row in rows:
        n = int(row["n"])
        distance, had_reset = _distance_from_bounds(
            first_odo=row["first_odo"],
            last_odo=row["last_odo"],
            min_odo=row["min_odo"],
            max_odo=row["max_odo"],
            n=n,
        )
        if had_reset:
            logger.info(
                "Odometer reset/anomaly for vehicle %s over [%s, %s): "
                "first=%s last=%s min=%s max=%s → distance=%s",
                row["vehicle_id"],
                start,
                end,
                row["first_odo"],
                row["last_odo"],
                row["min_odo"],
                row["max_odo"],
                distance,
            )
        result[row["vehicle_id"]] = VehicleDistance(
            vehicle_id=row["vehicle_id"],
            distance_km=distance,
            first_odometer_km=row["first_odo"],
            last_odometer_km=row["last_odo"],
            reading_count=n,
            had_reset=had_reset,
        )
    return result
