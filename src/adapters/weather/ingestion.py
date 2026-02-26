"""Weather ingestion service for Open-Meteo forecast updates.

Reference: Development plan Step 3.3, PRD.md#9-2-weather-api-open-meteo
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional
from uuid import UUID

import asyncpg

from .openmeteo import OpenMeteoAdapter

logger = logging.getLogger(__name__)


class WeatherIngestionService:
    """Background service for fetching and storing weather forecasts.

    Fetches 7-day weather forecasts daily and stores them in the database
    for all configured depots.

    Reference: PRD Section 5.3 (Weather API → weather_forecasts table)
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        adapter: Optional[OpenMeteoAdapter] = None,
        forecast_days: int = 7,
    ):
        """Initialize weather ingestion service.

        Args:
            pool: Database connection pool
            adapter: OpenMeteo adapter instance (creates new per depot if None)
            forecast_days: Number of forecast days to fetch (default: 7)
        """
        self.pool = pool
        self.adapter = adapter
        self.forecast_days = forecast_days
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def fetch_and_store_forecasts_for_depot(self, depot_id: str | UUID) -> int:
        """Fetch and store weather forecasts for a single depot.

        Args:
            depot_id: Depot identifier

        Returns:
            Number of forecasts stored
        """
        try:
            # Create adapter for this depot (will lookup location from DB)
            adapter = OpenMeteoAdapter(pool=self.pool)

            # Fetch forecasts (will automatically store to DB)
            forecasts = await adapter.get_forecasts_for_depot(
                depot_id=depot_id,
                days=self.forecast_days,
                use_cache=False,  # Force fresh fetch
            )

            if forecasts:
                logger.info(f"Stored {len(forecasts)} weather forecasts for depot {depot_id}")
                return len(forecasts)
            else:
                logger.warning(f"No forecasts fetched for depot {depot_id}")
                return 0

        except Exception as e:
            logger.error(f"Error fetching weather forecasts for depot {depot_id}: {e}")
            raise
        finally:
            if adapter:
                await adapter.close()

    async def fetch_forecasts_for_all_depots(self) -> dict[str, int]:
        """Fetch and store weather forecasts for all configured depots.

        Returns:
            Dictionary mapping depot_id to number of forecasts stored
        """
        query = """
        SELECT depot_id, latitude, longitude
        FROM depots
        WHERE latitude IS NOT NULL
          AND longitude IS NOT NULL
        ORDER BY depot_id
        """

        results: dict[str, int] = {}

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query)

            if not rows:
                logger.warning("No depots with location data found in database")
                return results

            logger.info(f"Fetching weather forecasts for {len(rows)} depots")

            for row in rows:
                depot_id = str(row["depot_id"])

                try:
                    stored_count = await self.fetch_and_store_forecasts_for_depot(depot_id)
                    results[depot_id] = stored_count
                except Exception as e:
                    logger.error(f"Failed to fetch forecasts for depot {depot_id}: {e}")
                    results[depot_id] = 0

            total_stored = sum(results.values())
            logger.info(
                f"Weather ingestion complete: {total_stored} forecasts stored "
                f"across {len(results)} depots"
            )

            return results

        except asyncpg.PostgresError as e:
            logger.error(f"Database error fetching depot list: {e}")
            raise

    async def _ingestion_loop(self) -> None:
        """Main ingestion loop that runs daily."""
        logger.info("Weather ingestion service started. Will run daily.")

        while self._running:
            try:
                # Run ingestion
                await self.fetch_forecasts_for_all_depots()

                # Wait until next day (24 hours)
                await asyncio.sleep(24 * 60 * 60)

            except asyncio.CancelledError:
                logger.info("Weather ingestion service cancelled")
                break
            except Exception as e:
                logger.error(f"Error in weather ingestion loop: {e}")
                # Wait 1 hour before retrying on error
                await asyncio.sleep(60 * 60)

    async def start(self) -> None:
        """Start the weather ingestion service."""
        if self._running:
            logger.warning("Weather ingestion service already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._ingestion_loop())
        logger.info("Weather ingestion service started")

    async def stop(self) -> None:
        """Stop the weather ingestion service."""
        if not self._running:
            return

        logger.info("Stopping weather ingestion service...")
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self.adapter:
            await self.adapter.close()

        logger.info("Weather ingestion service stopped")

    async def run_once(self) -> dict[str, int]:
        """Run weather ingestion once (for manual triggers or testing).

        Returns:
            Dictionary mapping depot_id to number of forecasts stored
        """
        return await self.fetch_forecasts_for_all_depots()
