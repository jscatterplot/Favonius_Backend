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
from ...db.pools import DatabasePools

logger = logging.getLogger(__name__)


async def store_meter_values(
    pools: DatabasePools,
    charge_point_id: str,
    connector_id: int,
    soc: float,
    power_kw: float,
    timestamp: datetime,
    vehicle_id: Optional[str | UUID] = None,
    max_charge_kw: Optional[float] = None,
    charger_id: Optional[UUID] = None,
    energy_kwh: Optional[float] = None,
) -> None:
    """Store meter values in TimescaleDB telemetry table.

    Per PRD Section 8.4, if max_charge_kw is provided, it will be:
    1. Stored in telemetry table (ts pool)
    2. Dynamically updated in vehicles table (static pool) for data consistency

    Args:
        pools: Dual database pools (static=Supabase, ts=TimescaleDB)
        charge_point_id: Charge point identifier (OCPP ID)
        connector_id: Connector identifier
        soc: State of charge (0.0-1.0)
        power_kw: Charging power in kW
        timestamp: Meter reading timestamp
        vehicle_id: Optional vehicle identifier (looked up if None)
        max_charge_kw: Optional max charge rate from OCPP (kW)
        charger_id: Optional charger UUID
        energy_kwh: Optional cumulative energy (Energy.Active.Import.Register)
    """
    # Resolve vehicle_id via charger lookup in Supabase (static).
    # Missing mapping should not block charger-keyed telemetry writes.
    if vehicle_id is None:
        vehicle_id = await get_vehicle_id_from_ocpp_id(pools.static, charge_point_id, connector_id)
        if vehicle_id is None:
            logger.debug(
                f"Could not find vehicle_id for charge_point_id={charge_point_id}, "
                f"connector_id={connector_id}. Continuing with charger-keyed telemetry."
            )

    # Resolve charger_id from Supabase (chargers table is static)
    if charger_id is None:
        try:
            async with pools.static.acquire() as conn:
                charger_row = await conn.fetchrow(
                    "SELECT id AS charger_id FROM charging_stations WHERE station_id = $1", charge_point_id
                )
                if charger_row:
                    charger_id = charger_row["charger_id"]
        except Exception as e:
            logger.debug(f"Could not resolve charger_id for {charge_point_id}: {e}")

    query = """
    INSERT INTO telemetry (
        time, station_id, connector_id, transaction_id,
        vehicle_id, charger_id, soc, charging_kw, is_plugged, max_charge_kw
    )
    VALUES ($1, $2, $3, NULL, $4::uuid, $5::uuid, $6, $7, $8, $9)
    ON CONFLICT (time, station_id, connector_id) DO UPDATE
    SET vehicle_id = COALESCE(EXCLUDED.vehicle_id, telemetry.vehicle_id),
        charger_id = COALESCE(EXCLUDED.charger_id, telemetry.charger_id),
        soc = COALESCE(EXCLUDED.soc, telemetry.soc),
        charging_kw = COALESCE(EXCLUDED.charging_kw, telemetry.charging_kw),
        is_plugged = COALESCE(EXCLUDED.is_plugged, telemetry.is_plugged),
        max_charge_kw = COALESCE(EXCLUDED.max_charge_kw, telemetry.max_charge_kw)
    """

    is_plugged = power_kw > 0.1

    try:
        # Write telemetry to TimescaleDB (ts pool)
        async with pools.ts.acquire() as conn:
            await conn.execute(
                query,
                timestamp,
                charge_point_id,
                connector_id,
                str(vehicle_id) if vehicle_id else None,
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
        # vehicles table is in Supabase (static pool)
        if vehicle_id and max_charge_kw and max_charge_kw > 0:
            await _update_vehicle_max_charge_kw(pools.static, vehicle_id, max_charge_kw, timestamp)

    except asyncpg.PostgresError as e:
        logger.error(f"Database error storing meter values: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error storing meter values: {e}")
        raise


async def _update_vehicle_max_charge_kw(
    static_pool: asyncpg.Pool,
    vehicle_id: UUID | str,
    max_charge_kw: float,
    timestamp: datetime,
) -> None:
    """Update vehicle's max_charge_kw in Supabase from OCPP MeterValues.

    Per PRD Section 8.4, OCPP values take precedence over static config.
    vehicles table lives in Supabase (static pool).
    """
    try:
        async with static_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE vehicles
                SET max_charge_rate_kw = $1
                WHERE id = $2::uuid
                  AND (max_charge_rate_kw IS NULL OR max_charge_rate_kw != $1)
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
    ts_pool: asyncpg.Pool,
    charge_point_id: str,
    connector_id: int,
    status: str,
    error_code: Optional[str],
    timestamp: Optional[datetime] = None,
) -> None:
    """Store connector status update in TimescaleDB.

    Upserts into the connector_status table (migration 004) if it exists.
    Falls back to logging if table is not yet created.

    connector_status is an operational/time-series table → TimescaleDB (ts pool).

    Args:
        ts_pool: TimescaleDB connection pool
        charge_point_id: Charge point identifier
        connector_id: Connector identifier
        status: Connector status (Available, Preparing, Charging, etc.)
        error_code: Optional error code
        timestamp: Optional timestamp (defaults to now)
    """
    if timestamp is None:
        timestamp = datetime.utcnow()

    try:
        async with ts_pool.acquire() as conn:
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
