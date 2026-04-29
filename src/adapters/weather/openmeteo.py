"""Weather data adapter using Open-Meteo API.

Reference: Development plan Step 3.3, PRD.md#9-2-weather-api-open-meteo

This implementation follows the official openmeteo-requests library documentation:
- https://pypi.org/project/openmeteo-requests/
- https://github.com/open-meteo/python-requests
- https://open-meteo.com/en/docs
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

import asyncpg
import numpy as np
import pandas as pd
from typing import Any

try:
    import openmeteo_requests
except ImportError:  # pragma: no cover - optional dependency for runtime API calls
    openmeteo_requests = None  # type: ignore[assignment]
try:
    import requests_cache
except ImportError:  # pragma: no cover - optional dependency for runtime API calls
    requests_cache = None  # type: ignore[assignment]
try:
    from retry_requests import retry
except ImportError:  # pragma: no cover - optional dependency for runtime API calls
    retry = None  # type: ignore[assignment]

from .storage import (
    get_depot_location,
    get_latest_forecast_bundle,
    store_weather_forecasts,
)

logger = logging.getLogger(__name__)


@dataclass
class WeatherData:
    """Weather observation/forecast data.

    Attributes:
        timestamp: Forecast timestamp
        temperature_f: Average temperature in Fahrenheit
        temperature_max_f: Maximum temperature in Fahrenheit
        temperature_min_f: Minimum temperature in Fahrenheit
        precipitation_inches: Daily precipitation in inches
        solar_radiation: Solar radiation in W/m² (daily average)
    """

    timestamp: datetime
    temperature_f: float
    temperature_max_f: float
    temperature_min_f: float
    precipitation_inches: float
    solar_radiation: float  # W/m² average


def _create_openmeteo_client(
    cache_dir: str = ".cache/openmeteo",
    cache_expire_after: int = 3600,
    retries: int = 5,
    backoff_factor: float = 0.2,
) -> Any:
    """Create an Open-Meteo client with caching and retry support.

    Follows official documentation recommendations:
    - Uses requests-cache for local SQLite caching
    - Uses retry-requests for automatic retry on failures

    Args:
        cache_dir: Directory for cache storage
        cache_expire_after: Cache expiration in seconds (default: 1 hour)
        retries: Number of retry attempts (default: 5)
        backoff_factor: Exponential backoff factor (default: 0.2)

    Returns:
        Configured openmeteo_requests.Client
    """
    if openmeteo_requests is None:
        raise RuntimeError(
            "openmeteo_requests package is unavailable. Install optional weather dependencies."
        )
    if requests_cache is None or retry is None:
        raise RuntimeError(
            "requests-cache/retry-requests packages are unavailable. Install optional weather dependencies."
        )
    # Ensure cache directory exists
    Path(cache_dir).mkdir(parents=True, exist_ok=True)

    # requests_cache.CachedSession creates cache file at {cache_dir}.sqlite
    # when given a directory path, so we use a file path explicitly
    cache_file = f"{cache_dir}.sqlite"

    # Setup cached session with SQLite backend
    cache_session = requests_cache.CachedSession(
        cache_file,
        expire_after=cache_expire_after,
        backend="sqlite",
    )

    # Add retry mechanism with exponential backoff
    retry_session = retry(
        cache_session,
        retries=retries,
        backoff_factor=backoff_factor,
    )

    # Create Open-Meteo client with configured session
    return openmeteo_requests.Client(session=retry_session)


class OpenMeteoAdapter:
    """Adapter for Open-Meteo weather API.

    Reference: PRD Section 9.2, Development plan Step 3.3

    Provides weather forecasts for energy consumption prediction.
    Free tier: 10,000 requests/day.

    This implementation follows the official openmeteo-requests documentation:
    - Uses the official openmeteo-requests library with FlatBuffers
    - Implements request-level caching with requests-cache
    - Implements automatic retry with retry-requests
    - Uses list format for API parameters
    """

    BASE_URL = "https://api.open-meteo.com/v1/forecast"

    # Variable indices for daily forecast (order must match API request)
    DAILY_TEMP_MAX_INDEX = 0
    DAILY_TEMP_MIN_INDEX = 1
    DAILY_PRECIP_INDEX = 2
    DAILY_SOLAR_INDEX = 3

    def __init__(
        self,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        pool: Optional[asyncpg.Pool] = None,
        cache_dir: str = ".cache/openmeteo",
        cache_expire_after: int = 3600,
        retries: int = 5,
        backoff_factor: float = 0.2,
    ):
        """Initialize Open-Meteo adapter.

        Args:
            latitude: Depot latitude (optional if using depot_id)
            longitude: Depot longitude (optional if using depot_id)
            pool: Optional database connection pool for depot lookup and storage
            cache_dir: Directory for request cache storage
            cache_expire_after: Cache expiration in seconds (default: 1 hour)
            retries: Number of retry attempts for failed requests
            backoff_factor: Exponential backoff factor for retries
        """
        self.latitude = latitude
        self.longitude = longitude
        self.pool = pool
        self.cache_dir = cache_dir
        self.cache_expire_after = cache_expire_after
        self.retries = retries
        self.backoff_factor = backoff_factor

        # Create the Open-Meteo client with caching and retry
        self._client = _create_openmeteo_client(
            cache_dir=cache_dir,
            cache_expire_after=cache_expire_after,
            retries=retries,
            backoff_factor=backoff_factor,
        )

        logger.info(
            f"Initialized OpenMeteoAdapter for ({latitude}, {longitude}) "
            f"with cache_dir={cache_dir}, retries={retries}"
        )

    def _fetch_forecast_sync(self, days: int) -> list[WeatherData]:
        """Synchronous forecast fetch using official openmeteo-requests library.

        Uses FlatBuffers format for efficient data transfer.

        Args:
            days: Number of forecast days (max: 16)

        Returns:
            List of WeatherData objects
        """
        # Build parameters using list format (per official documentation)
        params = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "daily": [
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_sum",
                "shortwave_radiation_sum",
            ],
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "forecast_days": days,
            "timezone": "auto",
        }

        # Make API request using official library
        # Returns list of responses (one per location)
        responses = self._client.weather_api(self.BASE_URL, params=params)

        if not responses:
            logger.warning("No response from Open-Meteo API")
            return []

        # Process the first response (single location)
        response = responses[0]

        # Log response metadata
        logger.debug(
            f"Open-Meteo response: lat={response.Latitude():.4f}, "
            f"lon={response.Longitude():.4f}, "
            f"elevation={response.Elevation()}m, "
            f"utc_offset={response.UtcOffsetSeconds()}s"
        )

        # Extract daily forecast data using FlatBuffers
        daily = response.Daily()
        if daily is None:
            logger.warning("No daily forecast data in API response")
            return []

        # Extract time range for creating date index
        # Time() returns start timestamp in seconds since epoch
        # TimeEnd() returns end timestamp
        # Interval() returns the interval between data points in seconds
        start_time = daily.Time()
        end_time = daily.TimeEnd()
        interval = daily.Interval()

        # Create date range using pandas (as recommended in documentation)
        dates = pd.date_range(
            start=pd.to_datetime(start_time, unit="s", utc=True),
            end=pd.to_datetime(end_time, unit="s", utc=True),
            freq=pd.Timedelta(seconds=interval),
            inclusive="left",
        )

        # Extract variable data as numpy arrays
        # Order must match the order in the 'daily' parameter list
        daily_variables = list(daily.Variables())

        if len(daily_variables) < 4:
            logger.warning(f"Expected 4 daily variables, got {len(daily_variables)}")
            return []

        temp_max = daily_variables[self.DAILY_TEMP_MAX_INDEX].ValuesAsNumpy()
        temp_min = daily_variables[self.DAILY_TEMP_MIN_INDEX].ValuesAsNumpy()
        precipitation = daily_variables[self.DAILY_PRECIP_INDEX].ValuesAsNumpy()
        solar_radiation = daily_variables[self.DAILY_SOLAR_INDEX].ValuesAsNumpy()

        # Build WeatherData objects
        results = []
        for i, date in enumerate(dates):
            if i >= len(temp_max):
                break

            # Convert pandas Timestamp to Python datetime
            dt = date.to_pydatetime()

            # Calculate average temperature
            t_max = float(temp_max[i]) if not np.isnan(temp_max[i]) else 75.0
            t_min = float(temp_min[i]) if not np.isnan(temp_min[i]) else 65.0
            temp_avg = (t_max + t_min) / 2.0

            # Handle NaN values with defaults
            precip = float(precipitation[i]) if not np.isnan(precipitation[i]) else 0.0
            solar = float(solar_radiation[i]) if not np.isnan(solar_radiation[i]) else 500.0

            results.append(
                WeatherData(
                    timestamp=dt,
                    temperature_f=temp_avg,
                    temperature_max_f=t_max,
                    temperature_min_f=t_min,
                    precipitation_inches=precip,
                    solar_radiation=solar,
                )
            )

        logger.info(f"Fetched {len(results)} days of weather forecast using FlatBuffers")
        return results

    async def get_forecast(self, days: int = 7) -> list[WeatherData]:
        """Fetch weather forecast.

        Args:
            days: Number of forecast days (default: 7, max: 16)

        Returns:
            List of WeatherData objects, one per day

        Raises:
            Exception: If API request fails after retries
        """
        if days > 16:
            days = 16  # Open-Meteo maximum
            logger.warning(f"Forecast days limited to 16 (requested: {days})")

        # Run sync client in thread pool to avoid blocking
        loop = asyncio.get_event_loop()
        try:
            results = await loop.run_in_executor(None, self._fetch_forecast_sync, days)
            return results
        except Exception as e:
            logger.error(f"Error fetching weather forecast: {e}")
            raise

    async def store_forecasts_to_db(
        self, forecasts: list[WeatherData], depot_id: str | UUID
    ) -> int:
        """Store fetched forecasts to database.

        Args:
            forecasts: List of WeatherData objects to store
            depot_id: Depot identifier

        Returns:
            Number of forecasts stored

        Raises:
            RuntimeError: If database pool not configured
        """
        if not self.pool:
            raise RuntimeError("Database pool not configured for OpenMeteoAdapter")

        return await store_weather_forecasts(self.pool, forecasts, depot_id)

    async def get_forecasts_for_depot(
        self,
        depot_id: str | UUID,
        days: int = 7,
        use_cache: bool = True,
    ) -> list[WeatherData]:
        """Fetch (or read) a forecast bundle for a depot.

        Behaviour after migration 021 is **insert-only**: a fresh fetch
        always writes a new bundle to ``weather_forecasts`` (one row per
        forecast_for, all sharing a single fetched_at). The DB is no
        longer used as a *cache* — every fetch is a new historical
        observation.

        ``use_cache`` is retained for read-only callers (e.g. dashboards)
        that just want the latest stored bundle without hitting the
        external API:

        * ``use_cache=True``  → return the latest bundle from the DB
          (read path; no API call). Falls through to a fresh fetch if
          the DB has no bundle yet.
        * ``use_cache=False`` → always fetch fresh and insert a new
          bundle. This is the **canonical ingestion path**.

        Args:
            depot_id: Depot identifier
            days: Number of forecast days (default: 7, max: 16)
            use_cache: If True, prefer the latest stored bundle. If
                False, always fetch fresh and insert a new bundle.

        Returns:
            List of WeatherData objects

        Raises:
            RuntimeError: If database pool not configured or depot
                location not found
        """
        if not self.pool:
            raise RuntimeError("Database pool not configured for OpenMeteoAdapter")

        # Get depot location if not already set
        if self.latitude is None or self.longitude is None:
            location = await get_depot_location(self.pool, depot_id)
            if location is None:
                raise RuntimeError(f"Could not find location for depot {depot_id}")
            self.latitude, self.longitude = location
            logger.info(
                f"Loaded location for depot {depot_id}: ({self.latitude}, {self.longitude})"
            )

        if use_cache:
            try:
                fetched_at, rows = await get_latest_forecast_bundle(
                    self.pool, depot_id
                )
                if rows:
                    now = datetime.now(timezone.utc)
                    end_time = now + timedelta(days=days)
                    forecasts: list[WeatherData] = []
                    for row in rows:
                        forecast_for = row["forecast_for"]
                        if forecast_for < now or forecast_for >= end_time:
                            continue
                        # Convert solar_rad from cal/cm² back to W/m² for
                        # the in-memory WeatherData type.
                        solar_wm2 = (row["solar_rad"] or 0.0) / 2.064
                        forecasts.append(
                            WeatherData(
                                timestamp=forecast_for,
                                temperature_f=row["temp_f"],
                                temperature_max_f=row["temp_max_f"],
                                temperature_min_f=row["temp_min_f"],
                                precipitation_inches=row["precip_in"],
                                solar_radiation=solar_wm2,
                            )
                        )
                    logger.debug(
                        "Using %d forecasts from latest bundle "
                        "(fetched_at=%s) for depot %s",
                        len(forecasts),
                        fetched_at,
                        depot_id,
                    )
                    return forecasts
            except Exception as e:
                logger.warning(
                    f"Error reading latest forecast bundle: {e}, fetching new"
                )

        # Canonical ingestion path: fetch fresh and insert a new bundle.
        forecasts = await self.get_forecast(days=days)

        if self.pool and forecasts:
            try:
                await self.store_forecasts_to_db(forecasts, depot_id)
            except Exception as e:
                logger.warning(f"Error storing forecasts to database: {e}")

        return forecasts

    async def close(self) -> None:
        """Close the adapter and clean up resources.

        Note: The openmeteo-requests Client uses requests/niquests sessions
        which are cleaned up automatically when the object is garbage collected.
        """
        # The underlying session will be garbage collected
        self._client = None
        logger.debug("OpenMeteoAdapter closed")

    def clear_cache(self) -> None:
        """Clear the request cache.

        Useful for testing or when fresh data is needed.
        """
        # requests_cache creates cache file at {cache_dir}.sqlite (sibling to directory)
        cache_file = Path(f"{self.cache_dir}.sqlite")
        if cache_file.exists():
            try:
                cache_file.unlink()
                logger.info(f"Cleared cache file: {cache_file}")
            except Exception as e:
                logger.warning(f"Failed to clear cache file {cache_file}: {e}")
        else:
            logger.debug(f"Cache file not found: {cache_file}")
