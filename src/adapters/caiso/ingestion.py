"""Price ingestion service for CAISO and ENTSO-E price updates.

Reference: Development plan Step 3.2, PRD.md#9-3-price-data
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import asyncpg

from ..entsoe import ENTSOEAdapter, is_european_timezone
from .prices import CAISOAdapter

logger = logging.getLogger(__name__)


class PriceIngestionService:
    """Background service for fetching and storing price data.

    Fetches day-ahead prices daily and stores them in the database
    for all configured depots. Routes European depots to ENTSO-E
    Transparency Platform and US depots to CAISO.

    Reference: PRD Section 9.3
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        adapter: Optional[CAISOAdapter] = None,
        entsoe_adapter: Optional[ENTSOEAdapter] = None,
        ingestion_hour: int = 10,  # 10:00 AM PT
        ingestion_minute: int = 0,
    ):
        """Initialize price ingestion service.

        Args:
            pool: Database connection pool
            adapter: CAISO adapter instance (creates new if None)
            entsoe_adapter: ENTSO-E adapter instance (creates new if None)
            ingestion_hour: Hour of day to run ingestion (default 10 = 10 AM)
            ingestion_minute: Minute of hour to run ingestion (default 0)
        """
        self.pool = pool
        self.adapter = adapter or CAISOAdapter(pool=pool)
        self.entsoe_adapter = entsoe_adapter or ENTSOEAdapter(pool=pool)
        self.ingestion_hour = ingestion_hour
        self.ingestion_minute = ingestion_minute
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def fetch_and_store_prices_for_depot(
        self,
        depot_id: str,
        node: Optional[str] = None,
        depot_timezone: Optional[str] = None,
    ) -> int:
        """Fetch and store prices for a single depot.

        Routes to ENTSO-E for European depots (detected by timezone)
        or to CAISO for US depots.

        Args:
            depot_id: Depot identifier
            node: Pricing node for CAISO (defaults to adapter default)
            depot_timezone: Depot IANA timezone for region detection

        Returns:
            Number of prices stored
        """
        try:
            now = datetime.utcnow()
            end_date = now + timedelta(hours=48)

            if depot_timezone and is_european_timezone(depot_timezone):
                return await self._fetch_entsoe_prices(depot_id, now, end_date, depot_timezone)
            else:
                return await self._fetch_caiso_prices(depot_id, now, end_date, node)

        except Exception as e:
            logger.error(f"Error fetching prices for depot {depot_id}: {e}")
            raise

    async def _fetch_caiso_prices(
        self,
        depot_id: str,
        start_date: datetime,
        end_date: datetime,
        node: Optional[str] = None,
    ) -> int:
        """Fetch and store CAISO prices for a US depot."""
        prices = await self.adapter.get_prices_for_depot(
            depot_id=depot_id,
            start_date=start_date,
            end_date=end_date,
            node=node,
            use_cache=False,
            source="caiso_dam",
        )

        if prices:
            stored = await self.adapter.store_prices_to_db(prices, depot_id, source="caiso_dam")
            logger.info(
                f"Stored {stored} CAISO prices for depot {depot_id} "
                f"(node: {node or self.adapter.default_node})"
            )
            return stored
        else:
            logger.warning(f"No CAISO prices fetched for depot {depot_id}")
            return 0

    async def _fetch_entsoe_prices(
        self,
        depot_id: str,
        start_date: datetime,
        end_date: datetime,
        depot_timezone: str,
    ) -> int:
        """Fetch and store ENTSO-E prices for a European depot."""
        prices = await self.entsoe_adapter.get_prices_for_depot(
            depot_id=depot_id,
            start_date=start_date,
            end_date=end_date,
            depot_timezone=depot_timezone,
            use_cache=False,
            source="entsoe_dam",
        )

        if prices:
            stored = await self.entsoe_adapter.store_prices_to_db(
                prices, depot_id, source="entsoe_dam"
            )
            logger.info(
                f"Stored {stored} ENTSO-E prices for depot {depot_id} "
                f"(timezone: {depot_timezone})"
            )
            return stored
        else:
            logger.warning(
                f"No ENTSO-E prices fetched for depot {depot_id} " f"(timezone: {depot_timezone})"
            )
            return 0

    async def fetch_prices_for_all_depots(self) -> dict[str, int]:
        """Fetch and store prices for all configured depots.

        Routes each depot to the appropriate pricing source based on
        its timezone (European depots -> ENTSO-E, others -> CAISO).

        Returns:
            Dictionary mapping depot_id to number of prices stored
        """
        query = """
        SELECT id AS depot_id, utility_id, timezone
        FROM sites
        ORDER BY id
        """

        results: dict[str, int] = {}

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query)

            if not rows:
                logger.warning("No depots found in database")
                return results

            eu_count = sum(1 for r in rows if r["timezone"] and is_european_timezone(r["timezone"]))
            logger.info(
                f"Fetching prices for {len(rows)} depots "
                f"({eu_count} European, {len(rows) - eu_count} non-European)"
            )

            for row in rows:
                depot_id = str(row["depot_id"])
                depot_timezone = row.get("timezone")
                node = None

                try:
                    stored_count = await self.fetch_and_store_prices_for_depot(
                        depot_id, node, depot_timezone=depot_timezone
                    )
                    results[depot_id] = stored_count
                except Exception as e:
                    logger.error(f"Failed to fetch prices for depot {depot_id}: {e}")
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
                datetime.utcnow()
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
        await self.entsoe_adapter.close()
        logger.info("Price ingestion service stopped")

    async def run_once(self) -> dict[str, int]:
        """Run price ingestion once (for manual triggers or testing).

        Returns:
            Dictionary mapping depot_id to number of prices stored
        """
        return await self.fetch_prices_for_all_depots()
