"""Price-lookup abstraction for the historical-session import endpoint.

The import endpoint receives one row per HTTP call. A 5000-row XLSX therefore
triggers 5000 separate price queries unless we cache. The cache is module-scoped
(survives across requests) and keyed by ``(depot_id, hour_bucket_utc)`` with a
300-second TTL, mirroring the existing ``_depot_config_cache`` pattern.

The ``PriceSource`` Protocol exists so unit tests can inject a fake without
mocking ``asyncpg`` internals. The production implementation
(``TimescalePriceSource``) reads from the ``prices`` hypertable; tests typically
use ``StaticPriceSource(None)`` (no derivation, fall back to the import row's
``revenue`` field) or ``StaticPriceSource(Decimal("0.20"))`` (deterministic
known value for invariant assertions).
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Protocol


class PriceSource(Protocol):
    """Resolves an average ``$/kWh`` for a given depot and time window."""

    async def average_price_per_kwh(
        self,
        *,
        depot_id: str,
        start: datetime,
        end: datetime,
    ) -> Optional[Decimal]:
        """Return the time-weighted mean price, or ``None`` if any overlapped hour lacks data."""
        ...


def _hour_bucket(dt: datetime) -> int:
    """Floor-of-hour epoch seconds for a datetime, normalized to UTC."""
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    utc = aware.astimezone(timezone.utc)
    floored = utc.replace(minute=0, second=0, microsecond=0)
    return int(floored.timestamp())


# Module-level cache: shared across all import-endpoint invocations so a
# multi-row XLSX upload only queries each (depot, hour) once per TTL window.
_HOUR_PRICE_CACHE: dict[tuple[str, int], tuple[Optional[Decimal], float]] = {}
_HOUR_PRICE_CACHE_TTL_S: float = 300.0
_HOUR_PRICE_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}


def invalidate_price_cache() -> None:
    """Drop all cached price entries. Intended for tests."""
    _HOUR_PRICE_CACHE.clear()
    _HOUR_PRICE_LOCKS.clear()


class StaticPriceSource:
    """Returns a fixed price (or ``None``) regardless of inputs.

    Used in unit tests and as a no-op fallback when the database is not
    available.
    """

    def __init__(self, price_per_kwh: Optional[Decimal] = None) -> None:
        self._price = price_per_kwh

    async def average_price_per_kwh(
        self,
        *,
        depot_id: str,
        start: datetime,
        end: datetime,
    ) -> Optional[Decimal]:
        return self._price


class TimescalePriceSource:
    """Looks up energy prices from the ``prices`` hypertable.

    Uses a module-scoped per-hour cache so re-querying the same (depot, hour)
    pair within ``_HOUR_PRICE_CACHE_TTL_S`` reuses the prior result. A
    per-bucket ``asyncio.Lock`` single-flights concurrent misses so a
    multi-row import that all reference the same hour hits the DB once.
    """

    def __init__(self, ts_pool) -> None:
        self._pool = ts_pool

    async def average_price_per_kwh(
        self,
        *,
        depot_id: str,
        start: datetime,
        end: datetime,
    ) -> Optional[Decimal]:
        if end <= start:
            return None

        start_aware = start if start.tzinfo is not None else start.replace(tzinfo=timezone.utc)
        start_utc = start_aware.astimezone(timezone.utc)
        start_bucket = _hour_bucket(start_utc)
        end_aware = end if end.tzinfo is not None else end.replace(tzinfo=timezone.utc)
        end_utc = end_aware.astimezone(timezone.utc)
        # Treat ``end`` as exclusive for overlap so an end on ``:00`` does not
        # pull in the next calendar hour. ``max`` covers sub-second windows
        # starting immediately after an hour boundary (``end - 1µs`` can sit
        # in the prior hour).
        last_bucket = max(
            start_bucket,
            _hour_bucket(end_utc - timedelta(microseconds=1)),
        )
        buckets = list(range(start_bucket, last_bucket + 3600, 3600))

        total_weighted_price = Decimal(0)
        total_seconds = Decimal(0)
        for bucket in buckets:
            price = await self._lookup_bucket(depot_id, bucket)
            if price is None:
                return None
            bucket_start = datetime.fromtimestamp(bucket, tz=timezone.utc)
            bucket_end = datetime.fromtimestamp(bucket + 3600, tz=timezone.utc)
            overlap_start = max(start_utc, bucket_start)
            overlap_end = min(end_utc, bucket_end)
            overlap_seconds = (overlap_end - overlap_start).total_seconds()
            if overlap_seconds <= 0:
                continue
            weight = Decimal(str(overlap_seconds))
            total_weighted_price += price * weight
            total_seconds += weight

        if total_seconds <= 0:
            return None
        return total_weighted_price / total_seconds

    async def _lookup_bucket(self, depot_id: str, bucket: int) -> Optional[Decimal]:
        cache_key = (depot_id, bucket)
        now = time.time()

        cached = _HOUR_PRICE_CACHE.get(cache_key)
        if cached is not None and now - cached[1] < _HOUR_PRICE_CACHE_TTL_S:
            return cached[0]

        lock = _HOUR_PRICE_LOCKS.setdefault(cache_key, asyncio.Lock())
        async with lock:
            # Re-check inside the lock; first waiter populates, others hit cache.
            cached = _HOUR_PRICE_CACHE.get(cache_key)
            if cached is not None and time.time() - cached[1] < _HOUR_PRICE_CACHE_TTL_S:
                return cached[0]

            price = await self._fetch_bucket(depot_id, bucket)
            _HOUR_PRICE_CACHE[cache_key] = (price, time.time())
            return price

    async def _fetch_bucket(self, depot_id: str, bucket: int) -> Optional[Decimal]:
        """Query the ``prices`` hypertable for the average $/kWh in this hour."""
        bucket_start = datetime.fromtimestamp(bucket, tz=timezone.utc)
        bucket_end = datetime.fromtimestamp(bucket + 3600, tz=timezone.utc)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT AVG(energy_kwh)::numeric AS price
                FROM prices
                WHERE depot_id = $1::uuid
                  AND time >= $2
                  AND time <  $3
                """,
                depot_id,
                bucket_start,
                bucket_end,
            )
        if row is None or row["price"] is None:
            return None
        return Decimal(row["price"])
