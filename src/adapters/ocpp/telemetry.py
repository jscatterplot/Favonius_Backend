"""Telemetry storage helpers for OCPP data.

Reference: Development plan Step 3.1, PRD_v2.md#6-1-database-schema
Per PRD Section 8.4, max_charge_kw from OCPP MeterValues must be dynamically
updated in the vehicles table for data consistency.

Updates from original:
 - store_meter_values now accepts energy_kwh (cumulative energy)
 - store_status_update now actually persists to connector_status table
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
    max_charge_kw: Optional[float] = None,
    charger_id: Optional[UUID] = None,
    energy_kwh: Optional[float] = None,
    ts_pool: Optional[asyncpg.Pool] = None,
) -> None:
    """Store meter values in the telemetry hypertable.

    Per PRD Section 8.4, if max_charge_kw is provided, it will be:
    1. Stored in telemetry table (Timescale via ts_pool)
    2. Dynamically updated in vehicles table (Supabase via pool)

    Args:
        pool: Supabase pool — used for chargers/vehicles lookups and updates
        charge_point_id: Charge point identifier (OCPP ID)
        connector_id: Connector identifier
        soc: State of charge (0.0-1.0)
        power_kw: Charging power in kW
        timestamp: Meter reading timestamp
        vehicle_id: Optional vehicle identifier (looked up if None)
        max_charge_kw: Optional max charge rate from OCPP (kW)
        charger_id: Optional charger UUID
        energy_kwh: Optional cumulative energy (Energy.Active.Import.Register)
        ts_pool: Timescale pool for telemetry INSERT; falls back to pool when None
    """
    _ts = ts_pool if ts_pool is not None else pool

    # Resolve vehicle_id from charge_point_id if not provided (Supabase)
    if vehicle_id is None:
        vehicle_id = await get_vehicle_id_from_ocpp_id(pool, charge_point_id, connector_id)
        if vehicle_id is None:
            logger.warning(
                f"Could not find vehicle_id for charge_point_id={charge_point_id}, "
                f"connector_id={connector_id}. Skipping telemetry storage."
            )
            return

    # Resolve charger_id if not provided (Supabase)
    if charger_id is None:
        try:
            async with pool.acquire() as conn:
                charger_row = await conn.fetchrow(
                    "SELECT charger_id FROM chargers WHERE ocpp_id = $1", charge_point_id
                )
                if charger_row:
                    charger_id = charger_row["charger_id"]
        except Exception as e:
            logger.debug(f"Could not resolve charger_id for {charge_point_id}: {e}")

    query = """
    INSERT INTO telemetry (time, vehicle_id, charger_id, soc, charging_kw, is_plugged, max_charge_kw)
    VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6, $7)
    ON CONFLICT (time, vehicle_id) DO NOTHING
    """

    is_plugged = power_kw > 0.1

    try:
        # Write telemetry to Timescale hypertable
        async with _ts.acquire() as conn:
            await conn.execute(
                query,
                timestamp,
                str(vehicle_id),
                str(charger_id) if charger_id else None,
                soc,
                power_kw,
                is_plugged,
                max_charge_kw,
            )
        logger.debug(
            f"Stored meter values: {charge_point_id}, connector {connector_id}, "
            f"vehicle_id={vehicle_id}, SoC={soc:.2f}, Power={power_kw:.2f}kW"
            + (f", Energy={energy_kwh:.2f}kWh" if energy_kwh else "")
            + (f", max_charge_kw={max_charge_kw:.2f}kW" if max_charge_kw else "")
        )

        # CRITICAL: Update vehicles table if max_charge_kw provided (per PRD Section 8.4)
        # This write goes to Supabase (vehicles is a static table)
        if max_charge_kw and max_charge_kw > 0:
            await _update_vehicle_max_charge_kw(pool, vehicle_id, max_charge_kw, timestamp)

    except asyncpg.PostgresError as e:
        logger.error(f"Database error storing meter values: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error storing meter values: {e}")
        raise


async def _update_vehicle_max_charge_kw(
    pool: asyncpg.Pool,
    vehicle_id: UUID | str,
    max_charge_kw: float,
    timestamp: datetime,
) -> None:
    """Update vehicle's max_charge_kw in database from OCPP MeterValues.

    Per PRD Section 8.4, OCPP values take precedence over static config.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE vehicles
                SET max_charge_kw = $1
                WHERE vehicle_id = $2::uuid
                  AND (max_charge_kw IS NULL OR max_charge_kw != $1)
                """,
                max_charge_kw,
                str(vehicle_id),
            )
            logger.info(
                f"Updated vehicle {vehicle_id} max_charge_kw to {max_charge_kw} kW "
                f"from OCPP MeterValues at {timestamp}"
            )
    except Exception as e:
        logger.error(f"Failed to update vehicle {vehicle_id} max_charge_kw: {e}", exc_info=True)


async def store_status_update(
    pool: asyncpg.Pool,
    charge_point_id: str,
    connector_id: int,
    status: str,
    error_code: Optional[str],
    timestamp: Optional[datetime] = None,
) -> None:
    """Store connector status update in database.

    Upserts into the connector_status table (migration 004) if it exists.
    Falls back to logging if table is not yet created.

    Args:
        pool: AsyncPG connection pool
        charge_point_id: Charge point identifier
        connector_id: Connector identifier
        status: Connector status (Available, Preparing, Charging, etc.)
        error_code: Optional error code
        timestamp: Optional timestamp (defaults to now)
    """
    if timestamp is None:
        timestamp = datetime.utcnow()

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO connector_status
                    (charger_ocpp_id, connector_id, status, error_code, updated_at)
                VALUES ($1, $2, $3, $4, $5::timestamptz)
                ON CONFLICT (charger_ocpp_id, connector_id)
                DO UPDATE SET
                    status = EXCLUDED.status,
                    error_code = EXCLUDED.error_code,
                    updated_at = EXCLUDED.updated_at
                """,
                charge_point_id,
                connector_id,
                status,
                error_code,
                timestamp.isoformat() if isinstance(timestamp, datetime) else timestamp,
            )
            logger.debug(
                f"Stored status update: {charge_point_id}, connector {connector_id}, "
                f"status={status}"
            )
    except asyncpg.UndefinedTableError:
        logger.debug(
            f"connector_status table not found, logging only: "
            f"{charge_point_id}:{connector_id}={status}"
        )
    except asyncpg.PostgresError as e:
        logger.error(f"Database error storing status update: {e}")
        raise
