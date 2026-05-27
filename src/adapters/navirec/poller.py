"""Navirec live-feed poller.

Background loop that pulls the fleet's latest telematics readings from Navirec
and upserts them into the ``vehicle_telemetry`` hypertable, where
``StateAssembler._get_vehicle_socs`` merges them with charger telemetry.

Shape of one cycle (one Navirec API round for the whole fleet — no N+1):

1. Build / reuse a cached ``{normalized_plate → (vehicle_id, depot_id)}`` map
   from the static DB. Plates that map to more than one vehicle are dropped
   (ambiguous → unsafe to attribute), per the resolution policy.
2. Fetch all vehicles once; map each to a :class:`VehicleTelemetryReading`.
3. Resolve plate → vehicle, grouping rows by depot. Unmatched plates are
   counted and skipped; readings older than the 24h merge window are skipped
   (they'd never be queried); readings past the 15-min freshness window are
   flagged but still written under their true device timestamp.
4. Write each depot's batch under a per-depot Postgres advisory lock so only
   one worker writes a given depot per cycle (multi-replica safe). A depot that
   errors is isolated — the others still get written.

The DB writer (:func:`write_depot_readings`) is shared with the historical
backfill CLI so the upsert / idempotency contract is identical on both paths.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ...monitoring.metrics import (
    NAVIREC_AMBIGUOUS_PLATES,
    NAVIREC_DEPOT_FAILURES,
    NAVIREC_LOCK_SKIPS,
    NAVIREC_POLL_CYCLES,
    NAVIREC_POLL_DURATION,
    NAVIREC_READINGS_WRITTEN,
    NAVIREC_STALE_READINGS,
    NAVIREC_UNMATCHED_PLATES,
)
from ...security.data_freshness import MAX_TELEMETRY_AGE
from .client import NavirecClient, NavirecClientError
from .mapping import VehicleTelemetryReading, navirec_vehicle_to_reading, normalize_plate

logger = logging.getLogger(__name__)

# A reading older than the merge query's lower bound would never be selected,
# so the live path drops it (the backfill path writes regardless).
_LIVE_MAX_AGE = timedelta(hours=24)

# Plate maps change rarely (a vehicle's plate is near-static); cache like the
# 5-min _get_depot_config cache in src/api/main.py.
_PLATE_MAP_TTL_S = 300.0

# {normalized_plate: (vehicle_id, depot_id)}
PlateMap = dict[str, tuple[str, str]]

# Batched, idempotent upsert. unnest() lets one statement insert all rows AND
# RETURN one row per actual insert, so we can count true inserts (ON CONFLICT
# DO NOTHING suppresses the RETURNING for duplicates) instead of overcounting.
_UPSERT_SQL = """
    INSERT INTO vehicle_telemetry
        (time, vehicle_id, soc, location_lat, location_lon, source, raw_fields)
    SELECT * FROM unnest(
        $1::timestamptz[], $2::uuid[], $3::float8[],
        $4::float8[], $5::float8[], $6::text[], $7::jsonb[]
    )
    ON CONFLICT (vehicle_id, time) DO NOTHING
    RETURNING 1
"""


def _default_interval_s() -> float:
    try:
        return max(30.0, float(os.getenv("NAVIREC_POLL_INTERVAL_S", "300")))
    except ValueError:
        return 300.0


def _poll_enabled() -> bool:
    return os.getenv("NAVIREC_POLL_ENABLED", "false").strip().lower() == "true"


async def build_plate_map(static_pool: Any, depot_id: Optional[str] = None) -> PlateMap:
    """Build ``{normalized_plate → (vehicle_id, depot_id)}`` from the static DB.

    Plates that resolve to more than one vehicle (after normalization) are
    dropped — attributing a reading to the wrong vehicle is worse than missing
    it. Emits the ambiguous count as a gauge.

    When ``depot_id`` is given the map is scoped to that depot, so a plate that
    also exists in another depot doesn't get dropped as globally-ambiguous and
    silently skip valid vehicles for a depot-targeted backfill.
    """
    async with static_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text AS vehicle_id, site_id::text AS depot_id, license_plate
            FROM vehicles
            WHERE license_plate IS NOT NULL AND site_id IS NOT NULL
              AND ($1::uuid IS NULL OR site_id = $1::uuid)
            """,
            depot_id,
        )

    plate_map: PlateMap = {}
    ambiguous: set[str] = set()
    for row in rows:
        key = normalize_plate(row["license_plate"])
        if not key:
            continue
        if key in ambiguous:
            continue
        if key in plate_map and plate_map[key][0] != row["vehicle_id"]:
            del plate_map[key]
            ambiguous.add(key)
            continue
        plate_map[key] = (row["vehicle_id"], row["depot_id"])

    NAVIREC_AMBIGUOUS_PLATES.set(len(ambiguous))
    if ambiguous:
        logger.warning("Navirec: dropped %d ambiguous plate(s) from resolution map", len(ambiguous))
    return plate_map


class _PlateMapCache:
    """Process-wide TTL cache for the plate map (mirrors _get_depot_config)."""

    def __init__(self, ttl_s: float = _PLATE_MAP_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._value: Optional[PlateMap] = None
        self._fetched_at: float = 0.0

    async def get(self, static_pool: Any, *, force: bool = False) -> PlateMap:
        now = asyncio.get_event_loop().time()
        if not force and self._value is not None and (now - self._fetched_at) < self._ttl_s:
            return self._value
        self._value = await build_plate_map(static_pool)
        self._fetched_at = now
        return self._value


_plate_map_cache = _PlateMapCache()


def _json_safe(obj: Any) -> Any:
    """Recursively replace non-finite floats (NaN/Infinity) with ``None``.

    ``json.dumps`` emits ``NaN``/``Infinity`` tokens by default, which Postgres
    ``jsonb`` rejects — and since each depot writes as one batched insert, a
    single such value in ``raw_fields`` would fail the whole batch. Sanitize so
    the row is stored with the bad field nulled instead.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _reading_to_row(vehicle_id: str, reading: VehicleTelemetryReading) -> tuple[Any, ...]:
    return (
        reading.time,
        vehicle_id,
        reading.soc,
        reading.latitude,
        reading.longitude,
        "navirec",
        json.dumps(_json_safe(reading.raw_fields)),
    )


def resolve_readings(
    readings: list[VehicleTelemetryReading],
    plate_map: PlateMap,
) -> tuple[dict[str, list[tuple[Any, ...]]], int]:
    """Group readings into per-depot upsert rows; return (rows_by_depot, unmatched)."""
    rows_by_depot: dict[str, list[tuple[Any, ...]]] = {}
    unmatched = 0
    for reading in readings:
        hit = plate_map.get(reading.vehicle_plate)
        if hit is None:
            unmatched += 1
            continue
        vehicle_id, depot_id = hit
        rows_by_depot.setdefault(depot_id, []).append(_reading_to_row(vehicle_id, reading))
    return rows_by_depot, unmatched


async def write_depot_readings(
    ts_pool: Any,
    depot_id: str,
    rows: list[tuple[Any, ...]],
    *,
    record_throughput: bool = True,
) -> Optional[int]:
    """Upsert one depot's rows under a per-depot advisory lock.

    Returns the number of rows **actually inserted** (duplicates suppressed by
    ``ON CONFLICT (vehicle_id, time) DO NOTHING`` are not counted), or ``None``
    if another worker held the lock (skipped this cycle). Shared by the live
    poller and the historical backfill.

    ``record_throughput`` increments the live-poller throughput counter; the
    one-shot backfill passes ``False`` so a large historical run doesn't spike a
    metric that dashboards read as live ingestion.
    """
    if not rows:
        return 0
    # Transpose row tuples into per-column arrays for the unnest() upsert.
    columns = list(zip(*rows))
    lock_key = f"navirec_poll:{depot_id}"
    async with ts_pool.acquire() as conn:
        locked = await conn.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", lock_key
        )
        if not locked:
            NAVIREC_LOCK_SKIPS.labels(depot_id=depot_id).inc()
            return None
        try:
            inserted = await conn.fetch(_UPSERT_SQL, *[list(col) for col in columns])
        finally:
            await conn.fetchval("SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock_key)
    count = len(inserted)
    if count and record_throughput:
        NAVIREC_READINGS_WRITTEN.labels(depot_id=depot_id).inc(count)
    return count


async def poll_once(
    static_pool: Any,
    ts_pool: Any,
    client: NavirecClient,
    *,
    plate_map: Optional[PlateMap] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Run one poll cycle. Returns a summary dict (used by tests + logging)."""
    now = now or datetime.now(timezone.utc)
    if plate_map is None:
        plate_map = await _plate_map_cache.get(static_pool)

    # Fetch + map all vehicles (one paginated API round).
    fresh: list[VehicleTelemetryReading] = []
    stale_dropped = 0
    async for raw in client.iter_vehicles():
        reading = navirec_vehicle_to_reading(raw)
        if reading is None:
            continue
        age = now - reading.time
        # Future-dated (clock skew / bad data): would always win freshest-wins,
        # so drop any reading time-stamped ahead of now rather than let it
        # override real charger telemetry.
        if age < timedelta(0):
            stale_dropped += 1
            NAVIREC_STALE_READINGS.inc()
            continue
        if age > _LIVE_MAX_AGE:
            stale_dropped += 1
            continue
        if age > MAX_TELEMETRY_AGE:
            NAVIREC_STALE_READINGS.inc()
        fresh.append(reading)

    rows_by_depot, unmatched = resolve_readings(fresh, plate_map)
    if unmatched:
        NAVIREC_UNMATCHED_PLATES.inc(unmatched)

    written: dict[str, int] = {}
    failed: list[str] = []
    for depot_id, rows in rows_by_depot.items():
        try:
            count = await write_depot_readings(ts_pool, depot_id, rows)
        except Exception:  # noqa: BLE001 — isolate one depot's failure from the rest
            logger.exception("Navirec: depot %s write failed (isolated)", depot_id)
            NAVIREC_DEPOT_FAILURES.labels(depot_id=depot_id).inc()
            failed.append(depot_id)
            continue
        if count is not None:
            written[depot_id] = count

    return {
        "matched": sum(len(r) for r in rows_by_depot.values()),
        "written": written,
        "unmatched": unmatched,
        "stale_dropped": stale_dropped,
        "failed_depots": failed,
    }


async def run_navirec_poll_loop(
    static_pool: Any,
    ts_pool: Any,
    *,
    interval_s: Optional[float] = None,
    client: Optional[NavirecClient] = None,
) -> None:
    """Background loop: poll Navirec every ``interval_s``. Gated by env flag.

    Never raises out to the caller — a failed cycle is logged and the loop
    continues after the interval. Disabled (returns immediately) unless
    ``NAVIREC_POLL_ENABLED=true``.
    """
    if not _poll_enabled():
        NAVIREC_POLL_CYCLES.labels(outcome="skipped_disabled").inc()
        logger.info("Navirec poller disabled (set NAVIREC_POLL_ENABLED=true to enable)")
        return

    interval = interval_s if interval_s is not None else _default_interval_s()
    owns_client = client is None
    try:
        client = client or NavirecClient()
    except NavirecClientError as exc:
        logger.warning("Navirec poller not started: %s", exc)
        return

    logger.info("Navirec poller started (interval=%.0fs)", interval)
    try:
        while True:
            start = asyncio.get_event_loop().time()
            try:
                summary = await poll_once(static_pool, ts_pool, client)
                NAVIREC_POLL_CYCLES.labels(outcome="ok").inc()
                logger.debug("Navirec poll cycle: %s", summary)
            except Exception:  # noqa: BLE001 — keep the loop alive across cycles
                logger.exception("Navirec poll cycle failed")
                NAVIREC_POLL_CYCLES.labels(outcome="fetch_error").inc()
            finally:
                NAVIREC_POLL_DURATION.observe(asyncio.get_event_loop().time() - start)
            await asyncio.sleep(interval)
    finally:
        if owns_client:
            await client.aclose()
