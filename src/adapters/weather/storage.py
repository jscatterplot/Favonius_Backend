"""Weather storage functions for Open-Meteo adapter.

Reference: Development plan Step 3.3, PRD.md#6-1-database-schema
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from uuid import UUID

import asyncpg

if TYPE_CHECKING:
    from .openmeteo import WeatherData

logger = logging.getLogger(__name__)


def convert_solar_radiation_wm2_to_calcm2(solar_wm2: float) -> float:
    """Convert solar radiation from W/m² to cal/cm².

    Conversion factor: 1 W/m² = 0.001433 cal/cm²/min
    For daily sum: multiply by 1440 minutes = 2.064 cal/cm² per W/m²

    Args:
        solar_wm2: Solar radiation in W/m²

    Returns:
        Solar radiation in cal/cm²
    """
    # Daily conversion: W/m² * 1440 min/day * 0.001433 cal/cm²/min
    return solar_wm2 * 2.064


async def store_weather_forecasts(
    pool: asyncpg.Pool,
    forecasts: list[WeatherData],
    depot_id: str | UUID,
) -> int:
    """Store weather forecasts to database.

    Converts solar_radiation from W/m² to cal/cm² for surrogate model.
    Uses ON CONFLICT for upsert logic.

    Args:
        pool: Database connection pool
        forecasts: List of WeatherData objects to store
        depot_id: Depot identifier

    Returns:
        Number of forecasts stored

    Raises:
        asyncpg.PostgresError: If database operation fails
    """
    if not forecasts:
        logger.warning("No weather forecasts to store")
        return 0

    depot_id_str = str(depot_id)

    query = """
    INSERT INTO weather_forecasts (
        time, depot_id, temp_f, temp_max_f, temp_min_f, 
        precip_in, solar_rad, fetched_at
    )
    VALUES ($1, $2::uuid, $3, $4, $5, $6, $7, NOW())
    ON CONFLICT (time, depot_id) DO UPDATE
    SET temp_f = EXCLUDED.temp_f,
        temp_max_f = EXCLUDED.temp_max_f,
        temp_min_f = EXCLUDED.temp_min_f,
        precip_in = EXCLUDED.precip_in,
        solar_rad = EXCLUDED.solar_rad,
        fetched_at = EXCLUDED.fetched_at
    """

    stored_count = 0

    try:
        async with pool.acquire() as conn:
            for forecast in forecasts:
                # Convert solar radiation from W/m² to cal/cm²
                solar_rad_calcm2 = convert_solar_radiation_wm2_to_calcm2(
                    forecast.solar_radiation
                )

                await conn.execute(
                    query,
                    forecast.timestamp,
                    depot_id_str,
                    forecast.temperature_f,
                    forecast.temperature_max_f,
                    forecast.temperature_min_f,
                    forecast.precipitation_inches,
                    solar_rad_calcm2,
                )
                stored_count += 1

        logger.info(
            f"Stored {stored_count} weather forecasts for depot {depot_id_str}"
        )
        return stored_count

    except asyncpg.PostgresError as e:
        logger.error(f"Database error storing weather forecasts: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error storing weather forecasts: {e}")
        raise


async def get_cached_forecasts(
    pool: asyncpg.Pool,
    depot_id: str | UUID,
    start_time: datetime,
    end_time: datetime,
) -> list[dict]:
    """Get cached weather forecasts from database.

    Args:
        pool: Database connection pool
        depot_id: Depot identifier
        start_time: Start of time window
        end_time: End of time window

    Returns:
        List of forecast dictionaries with keys: time, temp_f, temp_max_f,
        temp_min_f, precip_in, solar_rad
    """
    depot_id_str = str(depot_id)

    query = """
    SELECT time, temp_f, temp_max_f, temp_min_f, precip_in, solar_rad
    FROM weather_forecasts
    WHERE depot_id = $1::uuid
      AND time >= $2
      AND time < $3
    ORDER BY time
    """

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, depot_id_str, start_time, end_time)

        forecasts = [dict(row) for row in rows]
        logger.debug(
            f"Retrieved {len(forecasts)} cached forecasts for depot {depot_id_str} "
            f"from {start_time} to {end_time}"
        )
        return forecasts

    except asyncpg.PostgresError as e:
        logger.error(f"Database error getting cached forecasts: {e}")
        raise


async def get_latest_forecast(
    pool: asyncpg.Pool, depot_id: str | UUID
) -> Optional[dict]:
    """Get the most recent weather forecast for a depot.

    Args:
        pool: Database connection pool
        depot_id: Depot identifier

    Returns:
        Forecast dictionary or None if no forecasts found
    """
    depot_id_str = str(depot_id)

    query = """
    SELECT time, temp_f, temp_max_f, temp_min_f, precip_in, solar_rad
    FROM weather_forecasts
    WHERE depot_id = $1::uuid
    ORDER BY time DESC
    LIMIT 1
    """

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, depot_id_str)

        if row:
            return dict(row)
        return None

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
    FROM depots
    WHERE depot_id = $1::uuid
    LIMIT 1
    """

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, depot_id_str)

        if row and row['latitude'] is not None and row['longitude'] is not None:
            return (float(row['latitude']), float(row['longitude']))
        return None

    except asyncpg.PostgresError as e:
        logger.error(f"Database error getting depot location: {e}")
        raise

