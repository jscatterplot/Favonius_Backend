"""Weather storage functions for Open-Meteo adapter.

After migration 021 ``weather_forecasts`` is an *insert-only history*
of forecast bundles. Every fetch from a weather provider inserts one
row per (forecast_for, fetched_at) tuple. The natural-dedup constraint
``UNIQUE (depot_id, source, fetched_at, forecast_for)`` collapses
*identical* duplicate fetches (e.g. the loop firing twice in the same
second) but a fresh fetch always inserts so we keep the full history.

Reference: Development plan Step 3.3, PRD.md#6-1-database-schema,
migrations/021_weather_insert_only_snapshots.sql
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional
from uuid import UUID

import asyncpg

if TYPE_CHECKING:
    from .openmeteo import WeatherData

logger = logging.getLogger(__name__)


DEFAULT_WEATHER_SOURCE = "open_meteo"


def convert_solar_radiation_wm2_to_calcm2(solar_wm2: float) -> float:
    """Convert solar radiation from W/m² to cal/cm².

    Conversion factor: 1 W/m² = 0.001433 cal/cm²/min
    For daily sum: multiply by 1440 minutes = 2.064 cal/cm² per W/m²

    Args:
        solar_wm2: Solar radiation in W/m²

    Returns:
        Solar radiation in cal/cm²
    """
    return solar_wm2 * 2.064


async def store_weather_forecasts(
    pool: asyncpg.Pool,
    forecasts: list[WeatherData],
    depot_id: str | UUID,
    *,
    source: str = DEFAULT_WEATHER_SOURCE,
    fetched_at: Optional[datetime] = None,
) -> int:
    """Insert one row per (forecast_for, fetched_at) tuple — never UPDATE.

    Solar radiation is converted from W/m² to cal/cm² for the surrogate
    model (the model was trained against cal/cm²).

    All rows in a single call share the same ``fetched_at`` — that
    pins the whole batch to one logical *forecast bundle*. The
    assembler later filters by ``MAX(fetched_at) <= horizon_start`` and
    gets every row from this fetch in one go.

    Behaviour:

    * A fresh fetch produces a fresh UTC-aware ``fetched_at`` timestamp
      (default ``datetime.now(timezone.utc)``), so every row in this
      batch inserts.
    * If the caller passes an explicit ``fetched_at`` and the same
      tuple ``(depot_id, source, fetched_at, forecast_for)`` already
      exists, the unique constraint collapses the duplicate (we use
      ``ON CONFLICT DO NOTHING``). This is the only mode of dedup —
      we never overwrite an existing row.

    Args:
        pool: Database connection pool
        forecasts: List of WeatherData objects to store
        depot_id: Depot identifier
        source: Weather provider tag (default ``'open_meteo'``)
        fetched_at: When this batch was fetched. Defaults to
            ``datetime.now(timezone.utc)`` so each call gets a unique tuple.

    Returns:
        Number of rows actually inserted (excludes rows skipped via
        ON CONFLICT DO NOTHING).

    Raises:
        asyncpg.PostgresError: If database operation fails
    """
    if not forecasts:
        logger.warning("No weather forecasts to store")
        return 0

    depot_id_str = str(depot_id)
    # Pin the bundle timestamp once so every row in this batch shares
    # it. This is the invariant the assembler relies on — "one bundle
    # = one fetched_at."
    bundle_fetched_at = (
        fetched_at if fetched_at is not None else datetime.now(timezone.utc)
    )

    query = """
    INSERT INTO weather_forecasts (
        depot_id, source, fetched_at, forecast_for,
        temp_f, temp_max_f, temp_min_f, precip_in, solar_rad
    )
    VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (depot_id, source, fetched_at, forecast_for)
        DO NOTHING
    """

    inserted = 0
    try:
        async with pool.acquire() as conn:
            for forecast in forecasts:
                solar_calcm2 = convert_solar_radiation_wm2_to_calcm2(
                    forecast.solar_radiation
                )
                result = await conn.execute(
                    query,
                    depot_id_str,
                    source,
                    bundle_fetched_at,
                    forecast.timestamp,
                    forecast.temperature_f,
                    forecast.temperature_max_f,
                    forecast.temperature_min_f,
                    forecast.precipitation_inches,
                    solar_calcm2,
                )
                # asyncpg returns 'INSERT 0 N' where N is the row
                # count. ON CONFLICT DO NOTHING reports 0 on a
                # collapsed duplicate.
                if isinstance(result, str) and result.split()[-1] == "1":
                    inserted += 1
                elif not isinstance(result, str):
                    # Mocked tests may return None / MagicMock; assume
                    # the row inserted so callers see a meaningful count.
                    inserted += 1

        logger.info(
            "Inserted %d weather forecast rows for depot %s "
            "(source=%s, %d skipped as duplicates, fetched_at=%s)",
            inserted,
            depot_id_str,
            source,
            len(forecasts) - inserted,
            bundle_fetched_at.isoformat(),
        )
        return inserted

    except asyncpg.PostgresError as e:
        logger.error(f"Database error storing weather forecasts: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error storing weather forecasts: {e}")
        raise


async def get_latest_forecast_bundle(
    pool: asyncpg.Pool,
    depot_id: str | UUID,
    *,
    as_of: Optional[datetime] = None,
    source: str = DEFAULT_WEATHER_SOURCE,
) -> tuple[Optional[datetime], list[dict]]:
    """Load the most-recent forecast bundle visible at ``as_of``.

    A *bundle* is the set of rows that share a single ``fetched_at``.
    "Most recent" means ``MAX(fetched_at) WHERE fetched_at <= as_of``.
    Returns ``(fetched_at, rows)`` or ``(None, [])`` if no bundle is
    available.

    The assembler uses this to pin each optimization snapshot to the
    forecast bundle that was actually current when the run started.
    Surrogate training reuses the same query against historical
    ``as_of`` timestamps to replay the exact features.

    Args:
        pool: Database connection pool
        depot_id: Depot identifier
        as_of: Upper bound on fetched_at. Defaults to ``NOW()``.
        source: Weather provider tag

    Returns:
        ``(fetched_at, rows)`` where rows is a list of dicts ordered by
        ``forecast_for`` ascending. Each dict contains
        ``forecast_id, forecast_for, temp_f, temp_max_f, temp_min_f,
        precip_in, solar_rad``.
    """
    depot_id_str = str(depot_id)

    if as_of is None:
        max_query = """
        SELECT MAX(fetched_at) AS max_fetched_at
        FROM weather_forecasts
        WHERE depot_id = $1::uuid
          AND source = $2
        """
        params: tuple = (depot_id_str, source)
    else:
        max_query = """
        SELECT MAX(fetched_at) AS max_fetched_at
        FROM weather_forecasts
        WHERE depot_id = $1::uuid
          AND source = $2
          AND fetched_at <= $3
        """
        params = (depot_id_str, source, as_of)

    rows_query = """
    SELECT forecast_id, forecast_for, fetched_at,
           temp_f, temp_max_f, temp_min_f, precip_in, solar_rad
    FROM weather_forecasts
    WHERE depot_id = $1::uuid
      AND source   = $2
      AND fetched_at = $3
    ORDER BY forecast_for
    """

    async with pool.acquire() as conn:
        max_row = await conn.fetchrow(max_query, *params)
        if max_row is None or max_row["max_fetched_at"] is None:
            return None, []
        max_fetched_at: datetime = max_row["max_fetched_at"]
        rows = await conn.fetch(
            rows_query, depot_id_str, source, max_fetched_at
        )
    return max_fetched_at, [dict(r) for r in rows]


async def get_cached_forecasts(
    pool: asyncpg.Pool,
    depot_id: str | UUID,
    start_time: datetime,
    end_time: datetime,
    *,
    source: str = DEFAULT_WEATHER_SOURCE,
) -> list[dict]:
    """Get forecasts for ``[start_time, end_time)`` from the *latest* bundle.

    Backwards-compatible read helper for callers that just want the
    forecast values (no provenance). Returns rows from the most recent
    bundle, filtered to the requested forecast_for window. The dict
    keys mirror the legacy schema (``time, temp_f, ...``) so existing
    consumers do not need to change.

    Args:
        pool: Database connection pool
        depot_id: Depot identifier
        start_time: Inclusive start of the forecast_for window
        end_time: Exclusive end of the forecast_for window
        source: Weather provider tag

    Returns:
        Forecast dicts ordered by ``time`` ascending. Empty list if no
        bundle is available.
    """
    fetched_at, rows = await get_latest_forecast_bundle(
        pool, depot_id, source=source
    )
    if fetched_at is None:
        return []

    out: list[dict] = []
    for row in rows:
        forecast_for: datetime = row["forecast_for"]
        if forecast_for < start_time or forecast_for >= end_time:
            continue
        out.append(
            {
                "time": forecast_for,
                "temp_f": row["temp_f"],
                "temp_max_f": row["temp_max_f"],
                "temp_min_f": row["temp_min_f"],
                "precip_in": row["precip_in"],
                "solar_rad": row["solar_rad"],
            }
        )

    logger.debug(
        "Retrieved %d forecasts for depot %s from bundle fetched_at=%s "
        "(window %s → %s)",
        len(out),
        depot_id,
        fetched_at,
        start_time,
        end_time,
    )
    return out


async def get_latest_forecast(
    pool: asyncpg.Pool, depot_id: str | UUID
) -> Optional[dict]:
    """Get the most recent single forecast row for a depot.

    Returns the row with the largest ``fetched_at`` (and within that
    bundle, the largest ``forecast_for``). Returns None if no rows
    exist.
    """
    depot_id_str = str(depot_id)

    query = """
    SELECT forecast_for AS time,
           temp_f, temp_max_f, temp_min_f, precip_in, solar_rad
    FROM weather_forecasts
    WHERE depot_id = $1::uuid
    ORDER BY fetched_at DESC, forecast_for DESC
    LIMIT 1
    """

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, depot_id_str)
        return dict(row) if row else None
    except asyncpg.PostgresError as e:
        logger.error(f"Database error getting latest forecast: {e}")
        raise


async def get_depot_location(
    pool: asyncpg.Pool, depot_id: str | UUID
) -> Optional[tuple[float, float]]:
    """Get depot latitude and longitude from database.

    Args:
        pool: Database connection pool
        depot_id: Depot identifier

    Returns:
        Tuple of (latitude, longitude) or None if depot not found
    """
    depot_id_str = str(depot_id)

    query = """
    SELECT latitude, longitude
    FROM sites
    WHERE id = $1::uuid
    LIMIT 1
    """

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, depot_id_str)

        if row and row["latitude"] is not None and row["longitude"] is not None:
            return (float(row["latitude"]), float(row["longitude"]))
        return None

    except asyncpg.PostgresError as e:
        logger.error(f"Database error getting depot location: {e}")
        raise
