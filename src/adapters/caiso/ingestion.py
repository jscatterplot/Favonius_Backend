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
        fetched_zones: Optional[set[str]] = None,
    ) -> int:
        """Fetch and store prices for a single depot.

        Routes to ENTSO-E for European depots (detected by timezone)
        or to CAISO for US depots.

        Args:
            depot_id: Depot identifier
            node: Pricing node for CAISO (defaults to adapter default)
            depot_timezone: Depot IANA timezone for region detection
            fetched_zones: Optional set tracking ENTSO-E zones already
                pulled in the current ingestion run. When the resolved
                zone is already in the set, the per-depot pull reads from
                the cache instead of hitting the ENTSO-E API a second
                time. Pass a shared set when iterating multiple depots
                from the same run.

        Returns:
            Number of prices stored
        """
        try:
            now = datetime.utcnow()
            end_date = now + timedelta(hours=48)

            if depot_timezone and is_european_timezone(depot_timezone):
                return await self._fetch_entsoe_prices(
                    depot_id, now, end_date, depot_timezone,
                    fetched_zones=fetched_zones,
                )
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
        *,
        fetched_zones: Optional[set[str]] = None,
    ) -> int:
        """Fetch and store ENTSO-E prices for a European depot.

        Prices are persisted to ``electricity_prices`` keyed by ENTSO-E
        bidding zone — the same hypertable the WS-handler feeder and
        the read-through cache in ``src/db/queries.py`` use. The
        ``depot_id`` is retained as a log key and for the adapter's
        per-depot timezone resolution but is no longer a storage key.

        Resolves the bidding zone using the same cascade as the
        readers (``src.db.queries.resolve_bidding_zone``):
        ``sites.tariff_config['entsoe_zone']`` first, then
        ``get_bidding_zone(sites.timezone)``. Earlier draft used
        ``get_bidding_zone(depot_timezone)`` directly, which ignored
        operator overrides set in ``tariff_config`` — for a depot in
        a country with multiple bidding zones (DK, NO, SE, IT), or
        a depot whose timezone doesn't match its actual electricity
        market, ingestion stored data under one ``node_id`` while
        billing looked for another. The cascade resolver fixes that.

        When ``fetched_zones`` is supplied and already contains the
        resolved zone, the underlying adapter call reads from the
        ``electricity_prices`` cache (just-populated by an earlier
        depot in this run) instead of hitting the ENTSO-E API a
        second time. Without the dedup, a depot fleet with N sites
        in the same zone (e.g. multiple Lithuanian depots) would
        burn N API calls per ingestion tick.
        """
        from ..entsoe.mappings import get_bidding_zone
        from ...db.queries import resolve_bidding_zone

        # Prefer the canonical resolver — it honours
        # tariff_config['entsoe_zone'] overrides that the bare
        # timezone lookup misses. The CAISO ingestion service has a
        # single pool that may or may not host the ``sites`` table;
        # if the lookup raises (sites lives on a separate DB) we
        # fall back to the timezone-derived zone so ingestion still
        # runs.
        zone: Optional[str] = None
        try:
            from uuid import UUID
            zone = await resolve_bidding_zone(self.pool, UUID(str(depot_id)))
        except Exception as exc:
            logger.debug(
                "resolve_bidding_zone unavailable for depot %s "
                "(%s); falling back to timezone-derived zone",
                depot_id, exc,
            )

        if zone is None:
            zone = get_bidding_zone(depot_timezone)

        if zone is None:
            logger.warning(
                f"No ENTSO-E bidding zone for depot {depot_id} "
                f"(timezone: {depot_timezone}); skipping"
            )
            return 0

        # ``electricity_prices`` is keyed by zone, so once one depot
        # in this run has fetched + stored, every other depot in the
        # same zone is reading the same hypertable rows. Earlier draft
        # toggled ``use_cache=True`` for depots 2..N, but that read
        # back any non-empty slice from the table — including stale
        # rows left by a prior day's ingestion — and called it done.
        # Short-circuiting here keeps depots 2..N from hitting the
        # cache (or the API) at all: the first depot did the only
        # ingestion work the zone needs, and the calculator / optimizer
        # will read the freshly-stored rows directly when they next
        # consult ``electricity_prices``.
        if fetched_zones is not None and zone in fetched_zones:
            logger.debug(
                "Zone %s already ingested this run (depot=%s); skipping",
                zone, depot_id,
            )
            return 0

        prices = await self.entsoe_adapter.get_prices_for_depot(
            depot_id=depot_id,
            start_date=start_date,
            end_date=end_date,
            depot_timezone=depot_timezone,
            bidding_zone=zone,
            use_cache=False,
            source="entsoe_dam",
        )

        if prices:
            stored = await self.entsoe_adapter.store_prices_to_db(
                prices, zone, source="entsoe_dam"
            )
            logger.info(
                f"Stored {stored} ENTSO-E prices for zone {zone} "
                f"(depot={depot_id}, timezone={depot_timezone})"
            )
            if fetched_zones is not None:
                fetched_zones.add(zone)
            return stored
        else:
            logger.warning(
                f"No ENTSO-E prices fetched for depot {depot_id} (timezone: {depot_timezone})"
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

            # Shared across the per-run iteration so depots in the same
            # ENTSO-E bidding zone don't each fire an independent
            # ``GetPublicationDocument`` request. The first depot
            # populates ``electricity_prices`` (use_cache=False); every
            # later depot in that zone reads from cache.
            fetched_zones: set[str] = set()

            for row in rows:
                depot_id = str(row["depot_id"])
                depot_timezone = row.get("timezone")
                node = None

                try:
                    stored_count = await self.fetch_and_store_prices_for_depot(
                        depot_id, node, depot_timezone=depot_timezone,
                        fetched_zones=fetched_zones,
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
