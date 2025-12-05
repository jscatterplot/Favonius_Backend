"""Vehicle-to-charger mapping helper for OCPP integration.

Reference: Development plan Step 3.1, PRD.md#6-1-database-schema
"""

from __future__ import annotations

import logging
from typing import Optional
from uuid import UUID

import asyncpg

logger = logging.getLogger(__name__)

# Cache for vehicle-to-charger mappings
_mapping_cache: dict[str, dict[str, tuple[str, int]]] = {}


async def get_vehicle_to_charger_map(
    pool: asyncpg.Pool, depot_id: str | UUID, use_cache: bool = True
) -> dict[str, tuple[str, int]]:
    """Build vehicle-to-charger mapping for a depot.

    Maps vehicle_id (as string) to (charge_point_id, connector_id) tuples.
    Uses vehicle.ocpp_id to match with charger.ocpp_id.

    Args:
        pool: Database connection pool
        depot_id: Depot identifier
        use_cache: Whether to use cached mapping (default True)

    Returns:
        Dictionary mapping vehicle_id (str) to (charge_point_id, connector_id)
        where charge_point_id is the charger's ocpp_id and connector_id is 1

    Example:
        >>> mapping = await get_vehicle_to_charger_map(pool, depot_id)
        >>> # Returns: {'vehicle_uuid': ('charger_ocpp_id', 1), ...}
    """
    depot_id_str = str(depot_id)

    # Check cache first
    if use_cache and depot_id_str in _mapping_cache:
        logger.debug(f"Using cached mapping for depot {depot_id_str}")
        return _mapping_cache[depot_id_str]

    query = """
    SELECT 
        v.vehicle_id::text as vehicle_id,
        c.ocpp_id as charge_point_id,
        1 as connector_id  -- Default to connector 1
    FROM vehicles v
    JOIN chargers c ON v.ocpp_id = c.ocpp_id
    WHERE v.depot_id = $1::uuid
      AND v.ocpp_id IS NOT NULL
      AND c.ocpp_id IS NOT NULL
    """

    mapping: dict[str, tuple[str, int]] = {}

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, depot_id_str)

        for row in rows:
            vehicle_id = row['vehicle_id']
            charge_point_id = row['charge_point_id']
            connector_id = row['connector_id']
            mapping[vehicle_id] = (charge_point_id, connector_id)

        logger.info(
            f"Built vehicle-to-charger mapping for depot {depot_id_str}: "
            f"{len(mapping)} vehicles mapped"
        )

        # Cache the mapping
        if use_cache:
            _mapping_cache[depot_id_str] = mapping

        return mapping

    except asyncpg.PostgresError as e:
        logger.error(f"Database error building vehicle-to-charger mapping: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error building mapping: {e}")
        raise


async def get_charger_id_from_ocpp_id(
    pool: asyncpg.Pool, ocpp_id: str
) -> Optional[UUID]:
    """Get charger_id from OCPP charge point ID.

    Args:
        pool: Database connection pool
        ocpp_id: OCPP charge point identifier

    Returns:
        Charger UUID or None if not found
    """
    query = """
    SELECT charger_id
    FROM chargers
    WHERE ocpp_id = $1
    LIMIT 1
    """

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, ocpp_id)
        return row['charger_id'] if row else None
    except asyncpg.PostgresError as e:
        logger.error(f"Database error getting charger_id: {e}")
        raise


async def get_vehicle_id_from_ocpp_id(
    pool: asyncpg.Pool, ocpp_id: str, connector_id: int = 1
) -> Optional[UUID]:
    """Get vehicle_id from OCPP charge point ID and connector.

    Args:
        pool: Database connection pool
        ocpp_id: OCPP charge point identifier
        connector_id: Connector identifier (default 1)

    Returns:
        Vehicle UUID or None if not found
    """
    query = """
    SELECT v.vehicle_id
    FROM vehicles v
    JOIN chargers c ON v.ocpp_id = c.ocpp_id
    WHERE c.ocpp_id = $1
    LIMIT 1
    """

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, ocpp_id)
        return row['vehicle_id'] if row else None
    except asyncpg.PostgresError as e:
        logger.error(f"Database error getting vehicle_id: {e}")
        raise


def clear_mapping_cache(depot_id: Optional[str | UUID] = None) -> None:
    """Clear the vehicle-to-charger mapping cache.

    Args:
        depot_id: Optional depot ID to clear specific cache entry.
                 If None, clears all cached mappings.
    """
    if depot_id is None:
        _mapping_cache.clear()
        logger.debug("Cleared all vehicle-to-charger mapping caches")
    else:
        depot_id_str = str(depot_id)
        if depot_id_str in _mapping_cache:
            del _mapping_cache[depot_id_str]
            logger.debug(f"Cleared mapping cache for depot {depot_id_str}")

