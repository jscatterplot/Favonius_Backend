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
    Uses charger_vehicle_access table to find which chargers each vehicle can access.
    For vehicles with multiple accessible chargers, returns the first one found.

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
    SELECT DISTINCT ON (v.id)
        v.id::text as vehicle_id,
        c.station_id as charge_point_id,
        1 as connector_id  -- Default to connector 1
    FROM vehicles v
    JOIN charger_vehicle_access cva ON v.id = cva.vehicle_id
    JOIN charging_stations c ON cva.charging_station_id = c.id
    WHERE v.site_id = $1::uuid
      AND cva.is_accessible = TRUE
      AND c.station_id IS NOT NULL
    ORDER BY v.id, c.station_id
    """

    mapping: dict[str, tuple[str, int]] = {}

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, depot_id_str)

        for row in rows:
            vehicle_id = row["vehicle_id"]
            charge_point_id = row["charge_point_id"]
            connector_id = row["connector_id"]
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


async def get_charger_id_from_ocpp_id(pool: asyncpg.Pool, ocpp_id: str) -> Optional[UUID]:
    """Get charger_id from OCPP charge point ID.

    Args:
        pool: Database connection pool
        ocpp_id: OCPP charge point identifier

    Returns:
        Charger UUID or None if not found
    """
    query = """
    SELECT id AS charger_id
    FROM charging_stations
    WHERE station_id = $1
    LIMIT 1
    """

    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, ocpp_id)
        return row["charger_id"] if row else None
    except asyncpg.PostgresError as e:
        logger.error(f"Database error getting charger_id: {e}")
        raise


async def get_vehicle_id_from_ocpp_id(
    pool: asyncpg.Pool, ocpp_id: str, connector_id: int = 1
) -> Optional[UUID]:
    """Get vehicle_id from OCPP charge point ID and connector.

    This function finds which vehicle is currently connected to a charger
    by looking up the charger's ocpp_id. Note: This requires active transaction
    or telemetry data to determine the actual vehicle connection.

    Args:
        pool: Database connection pool
        ocpp_id: OCPP charge point identifier (charger's ocpp_id)
        connector_id: Connector identifier (default 1)

    Returns:
        Vehicle UUID or None if not found

    Note:
        This is a simplified implementation. In production, this should
        query telemetry or transaction data to find the active vehicle
        connection rather than using a static mapping.
    """
    # For now, return None as this requires active transaction/telemetry lookup
    # The proper implementation would query telemetry table for recent connections
    # or use transaction_manager to find active sessions
    logger.warning(
        f"get_vehicle_id_from_ocpp_id called for charger {ocpp_id} - "
        "implementation needs active transaction/telemetry lookup"
    )
    return None


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
