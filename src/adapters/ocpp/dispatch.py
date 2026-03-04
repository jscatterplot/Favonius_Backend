"""Charging profile dispatch from optimization results.

Reference: Development plan Step 3.1, PRD.md#9-1-ocpp-integration
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg

from src.core.models import OptimizationResult

from .charge_point import convert_schedule_to_ocpp_profile
from .mapping import get_vehicle_to_charger_map
from .server import OCPPServer
from ...db.pools import DatabasePools

logger = logging.getLogger(__name__)


async def dispatch_charging_profiles(
    server: OCPPServer,
    optimization_result: OptimizationResult,
    vehicle_to_charger_map: Optional[dict[str, tuple[str, int]]] = None,
    pools: Optional[DatabasePools] = None,
    depot_id: Optional[str | UUID] = None,
    delta_t: float = 0.25,
) -> dict[str, bool]:
    """Dispatch optimization results as charging profiles to chargers.

    Converts OptimizationResult.schedule to OCPP SetChargingProfile messages
    and sends them to the appropriate chargers.

    Args:
        server: OCPPServer instance
        optimization_result: Optimization result from MILP solver
        vehicle_to_charger_map: Optional mapping from vehicle_id to (charge_point_id, connector_id).
                               If None and pools+depot_id provided, will be built automatically.
        pools: Optional DatabasePools (static=Supabase for mapping, ts=TimescaleDB for commands)
        depot_id: Optional depot identifier (required if vehicle_to_charger_map is None)
        delta_t: Time step duration in hours (default 0.25 = 15 minutes)

    Returns:
        Dictionary mapping vehicle_id to success status (True/False)

    Raises:
        ValueError: If vehicle_to_charger_map is None and pool/depot_id not provided

    Example:
        >>> # With explicit mapping
        >>> vehicle_map = {
        ...     'bus_1': ('charger_1', 1),
        ...     'bus_2': ('charger_2', 1),
        ... }
        >>> results = await dispatch_charging_profiles(
        ...     server, opt_result, vehicle_map
        ... )
        >>> # With automatic mapping
        >>> results = await dispatch_charging_profiles(
        ...     server, opt_result, pools=pools, depot_id=depot_id
        ... )
    """
    # Build mapping if not provided
    if vehicle_to_charger_map is None:
        if pools is None or depot_id is None:
            raise ValueError(
                "Either vehicle_to_charger_map or (pools and depot_id) must be provided"
            )
        try:
            vehicle_to_charger_map = await get_vehicle_to_charger_map(
                pools.static, depot_id, use_cache=True
            )
            logger.debug(
                f"Built vehicle-to-charger mapping for {len(vehicle_to_charger_map)} vehicles"
            )
        except Exception as e:
            logger.error(f"Error building vehicle-to-charger mapping: {e}")
            raise

    results: dict[str, bool] = {}

    logger.info(f"Dispatching charging profiles for {len(optimization_result.schedule)} vehicles")

    for vehicle_id, schedule_data in optimization_result.schedule.items():
        # Get charger mapping
        charger_info = vehicle_to_charger_map.get(vehicle_id)
        if not charger_info:
            logger.warning(
                f"No charger mapping for vehicle {vehicle_id}. "
                f"Available vehicles: {list(vehicle_to_charger_map.keys())[:5]}..."
            )
            results[vehicle_id] = False
            continue

        charge_point_id, connector_id = charger_info

        # Check if charge point is connected
        charge_point = server.get_charge_point(charge_point_id)
        if not charge_point:
            logger.warning(f"Charge point {charge_point_id} not connected for vehicle {vehicle_id}")
            results[vehicle_id] = False
            continue

        # Convert schedule to OCPP format
        # Schedule format: {'charging_power': [power_kw, ...], 'soc': [...], ...}
        charging_power = schedule_data.get("charging_power", [])
        if not charging_power:
            logger.warning(f"No charging power in schedule for vehicle {vehicle_id}")
            results[vehicle_id] = False
            continue

        # Create schedule as list of (timestep, power_kw) tuples
        schedule = [(t, power) for t, power in enumerate(charging_power) if power is not None]

        if not schedule:
            logger.warning(f"Empty schedule for vehicle {vehicle_id}")
            results[vehicle_id] = False
            continue

        # Convert to OCPP profile
        try:
            ocpp_profile = convert_schedule_to_ocpp_profile(schedule, delta_t=delta_t)
        except Exception as e:
            logger.error(f"Error converting schedule for vehicle {vehicle_id}: {e}")
            results[vehicle_id] = False
            continue

        # Send SetChargingProfile
        try:
            success = await charge_point.set_charging_profile(connector_id, ocpp_profile)
            results[vehicle_id] = success

            # Store command in database if pools provided (charging_commands is in TimescaleDB)
            if pools and success:
                await _store_charging_command(
                    pools.ts,
                    vehicle_id,
                    charge_point_id,
                    connector_id,
                    ocpp_profile,
                    optimization_result,
                )

            logger.info(
                f"Charging profile dispatched for vehicle {vehicle_id} "
                f"to {charge_point_id}:{'Accepted' if success else 'Rejected'}"
            )
        except Exception as e:
            logger.error(f"Error dispatching charging profile for vehicle {vehicle_id}: {e}")
            results[vehicle_id] = False

    success_count = sum(1 for v in results.values() if v)
    logger.info(f"Charging profile dispatch complete: {success_count}/{len(results)} successful")

    return results


async def _store_charging_command(
    pool: asyncpg.Pool,
    vehicle_id: str,
    charge_point_id: str,
    connector_id: int,
    charging_profile: list[dict],
    optimization_result: OptimizationResult,
) -> None:
    """Store charging command in database.

    Args:
        pool: Database connection pool
        vehicle_id: Vehicle identifier
        charge_point_id: Charge point identifier
        connector_id: Connector identifier
        charging_profile: OCPP charging profile
        optimization_result: Optimization result for reference
    """
    # Note: This assumes charging_commands table exists (PRD Section 6.1)
    # For now, we'll use a simplified approach
    query = """
    INSERT INTO charging_commands (
        charger_id, vehicle_id, issued_at, profile_json, status
    )
    VALUES ($1::uuid, $2::uuid, $3, $4::jsonb, $5)
    ON CONFLICT DO NOTHING
    """

    import json

    profile_json = json.dumps(charging_profile)
    status = "pending"  # Will be updated when charger responds

    try:
        async with pool.acquire() as conn:
            await conn.execute(
                query,
                charge_point_id,  # Using charge_point_id as charger_id
                vehicle_id,
                datetime.utcnow(),
                profile_json,
                status,
            )
        logger.debug(f"Stored charging command for vehicle {vehicle_id}")
    except asyncpg.PostgresError as e:
        logger.error(f"Database error storing charging command: {e}")
        # Don't raise - command was sent, just logging failed
    except Exception as e:
        logger.error(f"Unexpected error storing charging command: {e}")
