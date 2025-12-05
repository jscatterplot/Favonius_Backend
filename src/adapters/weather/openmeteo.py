"""Weather data adapter using Open-Meteo API.

Reference: Development plan Step 3.3, PRD.md#9-2-weather-api-open-meteo
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

import asyncpg
import httpx

from .storage import (
    convert_solar_radiation_wm2_to_calcm2,
    get_cached_forecasts,
    get_depot_location,
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


class OpenMeteoAdapter:
    """Adapter for Open-Meteo weather API.

    Reference: PRD Section 9.2, Development plan Step 3.3

    Provides weather forecasts for energy consumption prediction.
    Free tier: 10,000 requests/day.
    """

    BASE_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(
        self,
        latitude: Optional[float] = None,
        longitude: Optional[float] = None,
        pool: Optional[asyncpg.Pool] = None,
    ):
        """Initialize Open-Meteo adapter.

        Args:
            latitude: Depot latitude (optional if using depot_id)
            longitude: Depot longitude (optional if using depot_id)
            pool: Optional database connection pool for depot lookup and storage
        """
        self.latitude = latitude
        self.longitude = longitude
        self.pool = pool
        self.client = httpx.AsyncClient(timeout=10.0)
        logger.info(
            f"Initialized OpenMeteoAdapter for ({latitude}, {longitude})"
        )

    async def get_forecast(self, days: int = 7) -> list[WeatherData]:
        """Fetch weather forecast.

        Args:
            days: Number of forecast days (default: 7, max: 16)

        Returns:
            List of WeatherData objects, one per day

        Raises:
            httpx.HTTPStatusError: If API request fails
        """
        if days > 16:
            days = 16  # Open-Meteo maximum
            logger.warning(f"Forecast days limited to 16 (requested: {days})")

        params = {
            'latitude': self.latitude,
            'longitude': self.longitude,
            'daily': (
                'temperature_2m_max,temperature_2m_min,'
                'precipitation_sum,shortwave_radiation_sum'
            ),
            'hourly': 'temperature_2m',
            'temperature_unit': 'fahrenheit',
            'precipitation_unit': 'inch',
            'forecast_days': days,
        }

        try:
            response = await self.client.get(self.BASE_URL, params=params)
            response.raise_for_status()
            data = response.json()

            results = []
            daily = data.get('daily', {})

            if not daily.get('time'):
                logger.warning("No daily forecast data in API response")
                return []

            for i, date_str in enumerate(daily.get('time', [])):
                try:
                    dt = datetime.fromisoformat(date_str.replace('Z', '+00:00'))
                except ValueError:
                    # Fallback for date parsing
                    dt = datetime.fromisoformat(date_str)

                temp_max = daily.get('temperature_2m_max', [])
                temp_min = daily.get('temperature_2m_min', [])
                precip = daily.get('precipitation_sum', [])
                solar = daily.get('shortwave_radiation_sum', [])

                temp_avg = (
                    (temp_max[i] + temp_min[i]) / 2.0
                    if i < len(temp_max) and i < len(temp_min)
                    else 70.0
                )

                results.append(
                    WeatherData(
                        timestamp=dt,
                        temperature_f=temp_avg,
                        temperature_max_f=temp_max[i] if i < len(temp_max) else 75.0,
                        temperature_min_f=temp_min[i] if i < len(temp_min) else 65.0,
                        precipitation_inches=precip[i] if i < len(precip) else 0.0,
                        solar_radiation=solar[i] if i < len(solar) else 500.0,
                    )
                )

            logger.info(f"Fetched {len(results)} days of weather forecast")
            return results

        except httpx.HTTPStatusError as e:
            logger.error(f"Open-Meteo API error: {e}")
            raise
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
            raise RuntimeError(
                "Database pool not configured for OpenMeteoAdapter"
            )

        return await store_weather_forecasts(self.pool, forecasts, depot_id)

    async def get_forecasts_for_depot(
        self,
        depot_id: str | UUID,
        days: int = 7,
        use_cache: bool = True,
    ) -> list[WeatherData]:
        """Get weather forecasts for a specific depot, with caching support.

        First tries to get cached forecasts from database. If not available
        or use_cache=False, fetches new forecasts and stores them.

        Args:
            depot_id: Depot identifier
            days: Number of forecast days (default: 7, max: 16)
            use_cache: Whether to use cached forecasts (default True)

        Returns:
            List of WeatherData objects

        Raises:
            RuntimeError: If database pool not configured or depot location not found
        """
        if not self.pool:
            raise RuntimeError(
                "Database pool not configured for OpenMeteoAdapter"
            )

        # Get depot location if not already set
        if self.latitude is None or self.longitude is None:
            location = await get_depot_location(self.pool, depot_id)
            if location is None:
                raise RuntimeError(
                    f"Could not find location for depot {depot_id}"
                )
            self.latitude, self.longitude = location
            logger.info(
                f"Loaded location for depot {depot_id}: "
                f"({self.latitude}, {self.longitude})"
            )

        # Calculate time window
        now = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        end_time = now + timedelta(days=days)

        # Try to get cached forecasts first
        if use_cache:
            try:
                cached = await get_cached_forecasts(
                    self.pool, depot_id, now, end_time
                )
                if cached:
                    # Convert cached forecasts back to WeatherData objects
                    forecasts = []
                    for row in cached:
                        # Convert solar_rad from cal/cm² back to W/m² for WeatherData
                        solar_wm2 = row['solar_rad'] / 2.064
                        forecasts.append(
                            WeatherData(
                                timestamp=row['time'],
                                temperature_f=row['temp_f'],
                                temperature_max_f=row['temp_max_f'],
                                temperature_min_f=row['temp_min_f'],
                                precipitation_inches=row['precip_in'],
                                solar_radiation=solar_wm2,
                            )
                        )
                    logger.debug(
                        f"Using {len(forecasts)} cached forecasts for depot {depot_id}"
                    )
                    return forecasts
            except Exception as e:
                logger.warning(
                    f"Error getting cached forecasts: {e}, fetching new"
                )

        # Fetch new forecasts
        forecasts = await self.get_forecast(days=days)

        # Store to database if pool available
        if self.pool and forecasts:
            try:
                await self.store_forecasts_to_db(forecasts, depot_id)
            except Exception as e:
                logger.warning(f"Error storing forecasts to database: {e}")

        return forecasts

    async def close(self) -> None:
        """Close HTTP client."""
        await self.client.aclose()

