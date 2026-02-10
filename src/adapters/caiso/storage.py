"""Price storage functions for CAISO adapter.

Reference: Development plan Step 3.2, PRD.md#6-1-database-schema
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from uuid import UUID

import asyncpg

if TYPE_CHECKING:
    from .prices import CAISOPrice

logger = logging.getLogger(__name__)


async def store_prices(
    pool: asyncpg.Pool,
    prices: list["CAISOPrice"],
    depot_id: str | UUID,
    source: str = 'caiso_dam',
    demand_charge_per_kw: Optional[float] = None,
) -> int:
    """Store CAISO prices to database.

    Converts LMP ($/MWh) to energy_kwh ($/kWh) and stores in prices table.
    Uses ON CONFLICT for upsert logic.

    Args:
        pool: Database connection pool
        prices: List of CAISOPrice objects to store
        depot_id: Depot identifier
        source: Price source ('caiso_dam', 'utility_tou', etc.)
        demand_charge_per_kw: Optional demand charge rate ($/kW)

    Returns:
        Number of prices stored

    Raises:
        asyncpg.PostgresError: If database operation fails
    """
    if not prices:
        logger.warning("No prices to store")
        return 0

    depot_id_str = str(depot_id)

    query = """
    INSERT INTO prices (time, depot_id, energy_kwh, demand_kw, source)
    VALUES ($1, $2::uuid, $3, $4, $5)
    ON CONFLICT (time, depot_id) DO UPDATE
    SET energy_kwh = EXCLUDED.energy_kwh,
        demand_kw = EXCLUDED.demand_kw,
        source = EXCLUDED.source
    """

    stored_count = 0

    try:
        async with pool.acquire() as conn:
            for price in prices:
                # Convert LMP from $/MWh to $/kWh
                energy_kwh = price.lmp / 1000.0

                await conn.execute(
                    query,
                    price.timestamp,
                    depot_id_str,
                    energy_kwh,
                    demand_charge_per_kw,
                    source,
                )
                stored_count += 1

        logger.info(
            f"Stored {stored_count} prices for depot {depot_id_str} "
            f"(source: {source})"
        )
        return stored_count

    except asyncpg.PostgresError as e:
        logger.error(f"Database error storing prices: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error storing prices: {e}")
        raise


async def get_cached_prices(
    pool: asyncpg.Pool,
    depot_id: str | UUID,
    start_time: datetime,
    end_time: datetime,
) -> list[dict]:
    """Get cached prices from database.

    Args:
        pool: Database connection pool
        depot_id: Depot identifier
        start_time: Start of time window
        end_time: End of time window

    Returns:
        List of price dictionaries with keys: time, energy_kwh, demand_kw, source
    """
    depot_id_str = str(depot_id)

    query = """
    SELECT time, energy_kwh, demand_kw, source
    FROM prices
    WHERE depot_id = $1::uuid
      AND time >= $2
      AND time < $3
    ORDER BY time
    """

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, depot_id_str, start_time, end_time)

        prices = [dict(row) for row in rows]
        logger.debug(
            f"Retrieved {len(prices)} cached prices for depot {depot_id_str} "
            f"from {start_time} to {end_time}"
        )
        return prices

    except asyncpg.PostgresError as e:
        logger.error(f"Database error getting cached prices: {e}")
        raise


async def get_latest_price(
    pool: asyncpg.Pool, depot_id: str | UUID
) -> Optional[dict]:
    """Get the most recent price for a depot.

    Args:
        pool: Database connection pool
        depot_id: Depot identifier

    Returns:
        Price dictionary or None if no prices found
    """
    depot_id_str = str(depot_id)

    query = """
    SELECT time, energy_kwh, demand_kw, source
    FROM prices
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
        logger.error(f"Database error getting latest price: {e}")
        raise

