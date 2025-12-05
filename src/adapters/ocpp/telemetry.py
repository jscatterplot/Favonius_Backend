"""Telemetry storage helpers for OCPP data.

Reference: Development plan Step 3.1, PRD.md#6-1-database-schema
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg

from .mapping import get_vehicle_id_from_ocpp_id

logger = logging.getLogger(__name__)


async def store_meter_values(
    pool: asyncpg.Pool,
    charge_point_id: str,
    connector_id: int,
    soc: float,
    power_kw: float,
    timestamp: datetime,
    vehicle_id: Optional[str | UUID] = None,
) -> None:
    """Store meter values in TimescaleDB telemetry table.

    Args:
        pool: AsyncPG connection pool
        charge_point_id: Charge point identifier (OCPP ID)
        connector_id: Connector identifier
        soc: State of charge (0.0-1.0)
        power_kw: Charging power in kW
        timestamp: Meter reading timestamp
        vehicle_id: Optional vehicle identifier (if known, will be looked up if None)

    Raises:
        asyncpg.PostgresError: If database operation fails
        ValueError: If vehicle_id cannot be determined
    """
    # Resolve vehicle_id from charge_point_id if not provided
    if vehicle_id is None:
        vehicle_id = await get_vehicle_id_from_ocpp_id(pool, charge_point_id, connector_id)
        if vehicle_id is None:
            logger.warning(
                f"Could not find vehicle_id for charge_point_id={charge_point_id}, "
                f"connector_id={connector_id}. Skipping telemetry storage."
            )
            return

    query = """
    INSERT INTO telemetry (time, vehicle_id, soc, charging_kw, is_plugged)
    VALUES ($1, $2::uuid, $3, $4, $5)
    ON CONFLICT DO NOTHING
    """

    is_plugged = power_kw > 0.1  # Consider plugged if charging

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                query, timestamp, str(vehicle_id), soc, power_kw, is_plugged
            )
        logger.debug(
            f"Stored meter values: {charge_point_id}, connector {connector_id}, "
            f"vehicle_id={vehicle_id}, SoC={soc:.2f}, Power={power_kw:.2f}kW"
        )
    except asyncpg.PostgresError as e:
        logger.error(f"Database error storing meter values: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error storing meter values: {e}")
        raise


async def store_status_update(
    pool: asyncpg.Pool,
    charge_point_id: str,
    connector_id: int,
    status: str,
    error_code: Optional[str],
    timestamp: Optional[datetime] = None,
) -> None:
    """Store connector status update.

    Note: This is a placeholder. In production, you might have a
    charger_status table or update the chargers table.

    Args:
        pool: AsyncPG connection pool
        charge_point_id: Charge point identifier
        connector_id: Connector identifier
        status: Connector status (Available, Preparing, Charging, etc.)
        error_code: Optional error code
        timestamp: Optional timestamp (defaults to now)

    Raises:
        asyncpg.PostgresError: If database operation fails
    """
    if timestamp is None:
        timestamp = datetime.utcnow()

    # For now, just log the status update
    # In production, you'd insert into a charger_status table or update chargers table
    logger.debug(
        f"Status update: {charge_point_id}, connector {connector_id}, "
        f"status={status}, error_code={error_code}"
    )

    # Example query (uncomment when charger_status table exists):
    # query = """
    # INSERT INTO charger_status (charge_point_id, connector_id, status, error_code, timestamp)
    # VALUES ($1, $2, $3, $4, $5)
    # ON CONFLICT (charge_point_id, connector_id) DO UPDATE
    # SET status = $3, error_code = $4, timestamp = $5
    # """
    # try:
    #     async with pool.acquire() as conn:
    #         await conn.execute(query, charge_point_id, connector_id, status, error_code, timestamp)
    # except asyncpg.PostgresError as e:
    #     logger.error(f"Database error storing status update: {e}")
    #     raise

