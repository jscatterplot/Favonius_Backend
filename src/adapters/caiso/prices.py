"""CAISO price feed adapter.

Reference: Development plan Step 3.2, PRD.md#9-3-price-data
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

import asyncpg
import httpx

from .storage import store_prices, get_cached_prices, get_latest_price

logger = logging.getLogger(__name__)


@dataclass
class CAISOPrice:
    """CAISO electricity price data.

    Attributes:
        timestamp: Price timestamp
        lmp: Locational Marginal Price ($/MWh)
        energy: Energy component ($/MWh)
        congestion: Congestion component ($/MWh)
        loss: Loss component ($/MWh)
        node: Pricing node identifier
    """

    timestamp: datetime
    lmp: float  # Locational Marginal Price ($/MWh)
    energy: float  # Energy component
    congestion: float  # Congestion component
    loss: float  # Loss component
    node: str  # Pricing node


class CAISOAdapter:
    """Adapter for CAISO OASIS price data.

    Reference: PRD Section 9.3, Development plan Step 3.2

    For MVP, implements mock TOU prices matching PG&E E-19 structure.
    Future: Integrate with CAISO OASIS API for real-time LMP data.
    """

    BASE_URL = "http://oasis.caiso.com/oasisapi/SingleZip"

    def __init__(
        self,
        default_node: str = "SLAP_PGAE-APND",
        pool: Optional[asyncpg.Pool] = None,
    ):
        """Initialize CAISO adapter.

        Args:
            default_node: Default pricing node (PG&E service area)
            pool: Optional database connection pool for price storage
        """
        self.default_node = default_node
        self.pool = pool
        self.client = httpx.AsyncClient(timeout=30.0)
        logger.info(f"Initialized CAISOAdapter with node: {default_node}")

    async def get_day_ahead_prices(
        self,
        start_date: datetime,
        end_date: datetime,
        node: Optional[str] = None,
    ) -> list[CAISOPrice]:
        """Fetch Day-Ahead LMP prices from CAISO OASIS.

        Args:
            start_date: Start of price window
            end_date: End of price window
            node: Pricing node (defaults to instance default)

        Returns:
            List of CAISOPrice objects for each hour in the window

        Note:
            For MVP, returns mock TOU prices. Future implementation will
            call CAISO OASIS API which returns ZIP files with CSV data.
        """
        node = node or self.default_node

        # For MVP: Return mock TOU prices (PG&E E-19 structure)
        # Future: Implement actual CAISO OASIS API call
        # params = {
        #     'queryname': 'PRC_LMP',
        #     'startdatetime': start_date.strftime('%Y%m%dT07:00-0000'),
        #     'enddatetime': end_date.strftime('%Y%m%dT07:00-0000'),
        #     'market_run_id': 'DAM',
        #     'node': node,
        #     'resultformat': '6',  # CSV format
        # }
        # response = await self.client.get(self.BASE_URL, params=params)
        # # Parse ZIP file and extract CSV data

        prices = []
        current = start_date.replace(minute=0, second=0, microsecond=0)

        while current < end_date:
            hour = current.hour

            # Mock TOU structure (PG&E E-19)
            # Peak: 4pm-9pm (16:00-20:59)
            if hour in range(16, 21):
                lmp = 250.0  # $/MWh
            # Partial-peak: 9am-4pm, 9pm-midnight (09:00-15:59, 21:00-23:59)
            elif hour in range(9, 16) or hour in range(21, 24):
                lmp = 150.0  # $/MWh
            # Off-peak: midnight-9am (00:00-08:59)
            else:
                lmp = 80.0  # $/MWh

            # Convert $/MWh to $/kWh
            lmp_per_kwh = lmp / 1000.0

            prices.append(
                CAISOPrice(
                    timestamp=current,
                    lmp=lmp,
                    energy=lmp * 0.8,
                    congestion=lmp * 0.15,
                    loss=lmp * 0.05,
                    node=node,
                )
            )
            current += timedelta(hours=1)

        logger.debug(
            f"Generated {len(prices)} mock prices from {start_date} to {end_date}"
        )
        return prices

    async def get_current_price(
        self, node: Optional[str] = None
    ) -> Optional[CAISOPrice]:
        """Get current real-time price.

        Args:
            node: Pricing node (defaults to instance default)

        Returns:
            CAISOPrice for current hour, or None if unavailable
        """
        now = datetime.utcnow()
        prices = await self.get_day_ahead_prices(
            now, now + timedelta(hours=1), node
        )
        return prices[0] if prices else None

    async def store_prices_to_db(
        self,
        prices: list[CAISOPrice],
        depot_id: str | UUID,
        source: str = 'caiso_dam',
        demand_charge_per_kw: Optional[float] = None,
    ) -> int:
        """Store fetched prices to database.

        Args:
            prices: List of CAISOPrice objects to store
            depot_id: Depot identifier
            source: Price source ('caiso_dam', 'utility_tou', etc.)
            demand_charge_per_kw: Optional demand charge rate ($/kW)

        Returns:
            Number of prices stored

        Raises:
            RuntimeError: If database pool not configured
        """
        if not self.pool:
            raise RuntimeError("Database pool not configured for CAISOAdapter")

        return await store_prices(
            self.pool, prices, depot_id, source, demand_charge_per_kw
        )

    async def get_prices_for_depot(
        self,
        depot_id: str | UUID,
        start_date: datetime,
        end_date: datetime,
        node: Optional[str] = None,
        use_cache: bool = True,
        source: str = 'caiso_dam',
    ) -> list[CAISOPrice]:
        """Get prices for a specific depot, with caching support.

        First tries to get cached prices from database. If not available
        or use_cache=False, fetches new prices and stores them.

        Args:
            depot_id: Depot identifier
            start_date: Start of price window
            end_date: End of price window
            node: Pricing node (defaults to instance default)
            use_cache: Whether to use cached prices (default True)
            source: Price source for storage ('caiso_dam', 'utility_tou', etc.)

        Returns:
            List of CAISOPrice objects
        """
        # Try to get cached prices first
        if use_cache and self.pool:
            try:
                cached = await get_cached_prices(
                    self.pool, depot_id, start_date, end_date
                )
                if cached:
                    # Convert cached prices back to CAISOPrice objects
                    prices = []
                    for row in cached:
                        # Convert $/kWh back to $/MWh for LMP
                        lmp = row['energy_kwh'] * 1000.0
                        prices.append(
                            CAISOPrice(
                                timestamp=row['time'],
                                lmp=lmp,
                                energy=lmp * 0.8,  # Estimate components
                                congestion=lmp * 0.15,
                                loss=lmp * 0.05,
                                node=node or self.default_node,
                            )
                        )
                    logger.debug(
                        f"Using {len(prices)} cached prices for depot {depot_id}"
                    )
                    return prices
            except Exception as e:
                logger.warning(f"Error getting cached prices: {e}, fetching new")

        # Fetch new prices
        prices = await self.get_day_ahead_prices(start_date, end_date, node)

        # Store to database if pool available
        if self.pool and prices:
            try:
                await self.store_prices_to_db(prices, depot_id, source=source)
            except Exception as e:
                logger.warning(f"Error storing prices to database: {e}")

        return prices

    async def close(self) -> None:
        """Close HTTP client."""
        await self.client.aclose()

