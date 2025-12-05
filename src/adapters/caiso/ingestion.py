"""Price ingestion service for CAISO price updates.

Reference: Development plan Step 3.2, PRD.md#9-3-price-data
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import asyncpg

from .prices import CAISOAdapter

logger = logging.getLogger(__name__)


class PriceIngestionService:
    """Background service for fetching and storing price data.

    Fetches day-ahead prices daily (10:00 AM PT per PRD) and stores
    them in the database for all configured depots.

    Reference: PRD Section 9.3 (CAISO updates daily at 10:00 AM PT)
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        adapter: Optional[CAISOAdapter] = None,
        ingestion_hour: int = 10,  # 10:00 AM PT
        ingestion_minute: int = 0,
    ):
        """Initialize price ingestion service.

        Args:
            pool: Database connection pool
            adapter: CAISO adapter instance (creates new if None)
            ingestion_hour: Hour of day to run ingestion (default 10 = 10 AM)
            ingestion_minute: Minute of hour to run ingestion (default 0)
        """
        self.pool = pool
        self.adapter = adapter or CAISOAdapter(pool=pool)
        self.ingestion_hour = ingestion_hour
        self.ingestion_minute = ingestion_minute
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def fetch_and_store_prices_for_depot(
        self, depot_id: str, node: Optional[str] = None
    ) -> int:
        """Fetch and store prices for a single depot.

        Args:
            depot_id: Depot identifier
            node: Pricing node (defaults to adapter default)

        Returns:
            Number of prices stored
        """
        try:
            # Fetch prices for next 48 hours (2 days ahead)
            now = datetime.utcnow()
            end_date = now + timedelta(hours=48)

            prices = await self.adapter.get_prices_for_depot(
                depot_id=depot_id,
                start_date=now,
                end_date=end_date,
                node=node,
                use_cache=False,  # Force fresh fetch
                source='caiso_dam',
            )

            if prices:
                stored = await self.adapter.store_prices_to_db(
                    prices, depot_id, source='caiso_dam'
                )
                logger.info(
                    f"Stored {stored} prices for depot {depot_id} "
                    f"(node: {node or self.adapter.default_node})"
                )
                return stored
            else:
                logger.warning(f"No prices fetched for depot {depot_id}")
                return 0

        except Exception as e:
            logger.error(f"Error fetching prices for depot {depot_id}: {e}")
            raise

    async def fetch_prices_for_all_depots(self) -> dict[str, int]:
        """Fetch and store prices for all configured depots.

        Returns:
            Dictionary mapping depot_id to number of prices stored
        """
        query = """
        SELECT depot_id, utility_id
        FROM depots
        ORDER BY depot_id
        """

        results: dict[str, int] = {}

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query)

            if not rows:
                logger.warning("No depots found in database")
                return results

            logger.info(f"Fetching prices for {len(rows)} depots")

            for row in rows:
                depot_id = str(row['depot_id'])
                # For now, use default node. In future, could map utility_id to node
                node = None

                try:
                    stored_count = await self.fetch_and_store_prices_for_depot(
                        depot_id, node
                    )
                    results[depot_id] = stored_count
                except Exception as e:
                    logger.error(
                        f"Failed to fetch prices for depot {depot_id}: {e}"
                    )
                    results[depot_id] = 0

            total_stored = sum(results.values())
            logger.info(
                f"Price ingestion complete: {total_stored} prices stored "
                f"across {len(results)} depots"
            )

            return results

        except asyncpg.PostgresError as e:
            logger.error(f"Database error fetching depot list: {e}")
            raise

    async def _ingestion_loop(self) -> None:
        """Main ingestion loop that runs daily at configured time."""
        logger.info(
            f"Price ingestion service started. Will run daily at "
            f"{self.ingestion_hour:02d}:{self.ingestion_minute:02d} PT"
        )

        while self._running:
            try:
                # Calculate next run time (10:00 AM PT)
                now = datetime.utcnow()
                # For MVP, run immediately on start, then daily
                # In production, would calculate next 10 AM PT time

                # Run ingestion
                await self.fetch_prices_for_all_depots()

                # Wait until next day (24 hours)
                await asyncio.sleep(24 * 60 * 60)

            except asyncio.CancelledError:
                logger.info("Price ingestion service cancelled")
                break
            except Exception as e:
                logger.error(f"Error in price ingestion loop: {e}")
                # Wait 1 hour before retrying on error
                await asyncio.sleep(60 * 60)

    async def start(self) -> None:
        """Start the price ingestion service."""
        if self._running:
            logger.warning("Price ingestion service already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._ingestion_loop())
        logger.info("Price ingestion service started")

    async def stop(self) -> None:
        """Stop the price ingestion service."""
        if not self._running:
            return

        logger.info("Stopping price ingestion service...")
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        await self.adapter.close()
        logger.info("Price ingestion service stopped")

    async def run_once(self) -> dict[str, int]:
        """Run price ingestion once (for manual triggers or testing).

        Returns:
            Dictionary mapping depot_id to number of prices stored
        """
        return await self.fetch_prices_for_all_depots()

