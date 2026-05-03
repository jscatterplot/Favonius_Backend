"""Database query helpers with parameterized queries.

SECURITY: All queries use parameterized statements to prevent SQL injection.
See PRD_v2.md Section 10.4.

Reference: Development Plan Step 4.5.7
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID

logger = logging.getLogger(__name__)


def _coerce_jsonb_dict(value: Any) -> dict[str, Any]:
    """Return JSONB value as dict for response-model compatibility."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            logger.warning("Invalid JSONB string payload encountered")
    return {}


# ============ TELEMETRY QUERIES ============


async def get_vehicle_telemetry(
    db,
    vehicle_id: UUID,
    start_time: datetime,
    end_time: datetime,
) -> list[dict]:
    """Get telemetry for a vehicle in time range.

    Uses parameterized query - NEVER interpolate user input.

    Args:
        db: Database connection or pool
        vehicle_id: Vehicle UUID
        start_time: Start of time range
        end_time: End of time range

    Returns:
        List of telemetry records as dictionaries
    """
    # CORRECT: Use $1, $2 placeholders
    query = """
        SELECT time, soc, charging_kw, max_charge_kw, is_plugged, charger_id
        FROM telemetry
        WHERE vehicle_id = $1
          AND time >= $2
          AND time <= $3
        ORDER BY time DESC
    """
    return await db.fetch(query, vehicle_id, start_time, end_time)


async def get_latest_vehicle_soc(
    db,
    vehicle_id: UUID,
    max_age: Optional[timedelta] = None,
) -> Optional[float]:
    """Get most recent SoC for a vehicle.

    Args:
        db: Database connection or pool
        vehicle_id: Vehicle UUID
        max_age: Optional maximum age for telemetry

    Returns:
        SoC value (0.0-1.0) or None if not found/stale
    """
    if max_age is None:
        max_age = timedelta(minutes=15)  # Default per PRD Section 5.3

    cutoff_time = datetime.utcnow() - max_age

    query = """
        SELECT soc
        FROM telemetry
        WHERE vehicle_id = $1
          AND time >= $2
          AND soc IS NOT NULL
        ORDER BY time DESC
        LIMIT 1
    """
    row = await db.fetchrow(query, vehicle_id, cutoff_time)
    return row["soc"] if row else None


async def insert_telemetry(
    db,
    vehicle_id: UUID,
    charger_id: Optional[UUID],
    soc: Optional[float],
    charging_kw: Optional[float],
    max_charge_kw: Optional[float],
    timestamp: datetime,
    is_plugged: Optional[bool] = None,
    location_lat: Optional[float] = None,
    location_lon: Optional[float] = None,
    odometer_km: Optional[float] = None,
) -> None:
    """Insert telemetry record with parameterized query.

    Args:
        db: Database connection or pool
        vehicle_id: Vehicle UUID
        charger_id: Optional charger UUID that reported this telemetry
        soc: State of charge (0.0-1.0)
        charging_kw: Current charging power
        max_charge_kw: Max charge power from OCPP
        timestamp: Telemetry timestamp
        is_plugged: Whether vehicle is plugged in
        location_lat: Latitude
        location_lon: Longitude
        odometer_km: Odometer reading
    """
    query = """
        INSERT INTO telemetry
            (time, vehicle_id, charger_id, soc, charging_kw, max_charge_kw,
             is_plugged, location_lat, location_lon, odometer_km)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    """
    await db.execute(
        query,
        timestamp,
        vehicle_id,
        charger_id,
        soc,
        charging_kw,
        max_charge_kw,
        is_plugged,
        location_lat,
        location_lon,
        odometer_km,
    )


# ============ PRICE QUERIES ============


async def get_prices(
    db,
    depot_id: UUID,
    start_time: datetime,
    end_time: datetime,
) -> list[dict]:
    """Get prices for a depot in time range.

    Args:
        db: Database connection or pool
        depot_id: Depot UUID
        start_time: Start of time range
        end_time: End of time range

    Returns:
        List of price records
    """
    query = """
        SELECT time, energy_kwh, demand_kw, source
        FROM prices
        WHERE depot_id = $1
          AND time >= $2
          AND time <= $3
        ORDER BY time ASC
    """
    return await db.fetch(query, depot_id, start_time, end_time)


async def insert_price(
    db,
    depot_id: UUID,
    timestamp: datetime,
    energy_kwh: float,
    demand_kw: Optional[float] = None,
    source: Optional[str] = None,
) -> None:
    """Insert or update a price record.

    Args:
        db: Database connection or pool
        depot_id: Depot UUID
        timestamp: Price timestamp
        energy_kwh: Energy price in $/kWh
        demand_kw: Optional demand charge in $/kW
        source: Price source (e.g., 'caiso_dam', 'utility_tou')
    """
    query = """
        INSERT INTO prices (time, depot_id, energy_kwh, demand_kw, source)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (time, depot_id) DO UPDATE
        SET energy_kwh = EXCLUDED.energy_kwh,
            demand_kw = EXCLUDED.demand_kw,
            source = EXCLUDED.source
    """
    await db.execute(query, timestamp, depot_id, energy_kwh, demand_kw, source)


# ============ SCHEDULE QUERIES ============


async def get_schedules(
    db,
    vehicle_id: UUID,
    start_time: datetime,
    end_time: Optional[datetime] = None,
) -> list[dict]:
    """Get schedules for a vehicle.

    Args:
        db: Database connection or pool
        vehicle_id: Vehicle UUID
        start_time: Start of time range
        end_time: Optional end of time range

    Returns:
        List of schedule records
    """
    if end_time is None:
        end_time = start_time + timedelta(hours=24)

    query = """
        SELECT id AS schedule_id, route_id, departure_time, return_time,
               actual_return_time, energy_kwh, required_soc, dest_site_id AS dest_depot_id
        FROM schedules
        WHERE vehicle_id = $1
          AND departure_time >= $2
          AND departure_time <= $3
        ORDER BY departure_time ASC
    """
    return await db.fetch(query, vehicle_id, start_time, end_time)


async def get_depot_schedules(
    db,
    depot_id: UUID,
    start_time: datetime,
    end_time: Optional[datetime] = None,
) -> list[dict]:
    """Get all schedules for vehicles at a depot.

    Args:
        db: Database connection or pool
        depot_id: Depot UUID
        start_time: Start of time range
        end_time: Optional end of time range

    Returns:
        List of schedule records with vehicle info
    """
    if end_time is None:
        end_time = start_time + timedelta(hours=24)

    query = """
        SELECT s.id AS schedule_id, s.vehicle_id, v.external_id,
               s.route_id, s.departure_time, s.return_time,
               s.actual_return_time, s.energy_kwh, s.required_soc, s.dest_site_id AS dest_depot_id
        FROM schedules s
        JOIN vehicles v ON s.vehicle_id = v.id
        WHERE v.site_id = $1
          AND s.departure_time >= $2
          AND s.departure_time <= $3
        ORDER BY s.departure_time ASC
    """
    return await db.fetch(query, depot_id, start_time, end_time)


async def get_vehicle_ids_for_depot(db, depot_id: UUID, vehicle_ids: list[UUID]) -> set[str]:
    """Return vehicle ids from the requested set that belong to the depot."""
    if not vehicle_ids:
        return set()

    query = """
        SELECT id::text AS vehicle_id
        FROM vehicles
        WHERE site_id = $1
          AND id = ANY($2::uuid[])
    """
    rows = await db.fetch(query, depot_id, vehicle_ids)
    return {row["vehicle_id"] for row in rows}


async def create_manual_schedule(
    db,
    *,
    vehicle_id: UUID,
    route_id: str,
    departure_time: datetime,
    return_time: datetime,
    required_soc: float,
    energy_kwh: Optional[float],
) -> dict:
    """Create one manually-entered vehicle schedule row."""
    query = """
        INSERT INTO schedules (
            vehicle_id,
            route_id,
            departure_time,
            return_time,
            required_soc,
            energy_kwh
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id::text AS schedule_id,
                  vehicle_id::text AS vehicle_id,
                  route_id,
                  departure_time,
                  return_time,
                  required_soc,
                  energy_kwh
    """
    row = await db.fetchrow(
        query,
        vehicle_id,
        route_id,
        departure_time,
        return_time,
        required_soc,
        energy_kwh,
    )
    return dict(row)


async def get_schedule_for_depot(db, *, depot_id: UUID, schedule_id: UUID) -> Optional[dict]:
    """Fetch one schedule only if it belongs to a vehicle in the depot."""
    query = """
        SELECT s.id::text AS schedule_id,
               s.vehicle_id::text AS vehicle_id,
               s.route_id,
               s.departure_time,
               s.return_time,
               s.required_soc,
               s.energy_kwh
        FROM schedules s
        JOIN vehicles v ON v.id = s.vehicle_id
        WHERE s.id = $1
          AND v.site_id = $2
    """
    row = await db.fetchrow(query, schedule_id, depot_id)
    return dict(row) if row else None


async def update_manual_schedule(
    db,
    *,
    depot_id: UUID,
    schedule_id: UUID,
    vehicle_id: UUID,
    route_id: str,
    departure_time: datetime,
    return_time: datetime,
    required_soc: float,
    energy_kwh: Optional[float],
) -> Optional[dict]:
    """Update one manually-entered schedule while preserving depot scoping."""
    query = """
        UPDATE schedules s
        SET vehicle_id = $3,
            route_id = $4,
            departure_time = $5,
            return_time = $6,
            required_soc = $7,
            energy_kwh = $8
        FROM vehicles old_v, vehicles new_v
        WHERE s.id = $1
          AND old_v.id = s.vehicle_id
          AND old_v.site_id = $2
          AND new_v.id = $3
          AND new_v.site_id = $2
        RETURNING s.id::text AS schedule_id,
                  s.vehicle_id::text AS vehicle_id,
                  s.route_id,
                  s.departure_time,
                  s.return_time,
                  s.required_soc,
                  s.energy_kwh
    """
    row = await db.fetchrow(
        query,
        schedule_id,
        depot_id,
        vehicle_id,
        route_id,
        departure_time,
        return_time,
        required_soc,
        energy_kwh,
    )
    return dict(row) if row else None


# ============ OPTIMIZATION RUN QUERIES ============


async def insert_optimization_run(
    db,
    run_id: UUID,
    depot_id: UUID,
    trigger_reason: str,
    horizon_start: datetime,
    horizon_end: datetime,
    solve_time_s: float,
    objective_value: float,
    peak_demand_kw: float,
    status: str,
    schedule_json: dict,
) -> None:
    """Insert optimization run record.

    Args:
        db: Database connection or pool
        run_id: Run UUID
        depot_id: Depot UUID
        trigger_reason: Trigger reason string
        horizon_start: Optimization horizon start
        horizon_end: Optimization horizon end
        solve_time_s: Solve time in seconds
        objective_value: Objective function value
        peak_demand_kw: Peak demand in kW
        status: Optimization status
        schedule_json: Full schedule as JSON
    """
    import json

    query = """
        INSERT INTO optimization_runs
            (run_id, depot_id, run_time, trigger_reason, horizon_start, horizon_end,
             solve_time_s, objective_value, peak_demand_kw, status, schedule_json)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    """
    await db.execute(
        query,
        run_id,
        depot_id,
        datetime.utcnow(),
        trigger_reason,
        horizon_start,
        horizon_end,
        solve_time_s,
        objective_value,
        peak_demand_kw,
        status,
        json.dumps(schedule_json),
    )


async def get_latest_optimization_run(
    db,
    depot_id: UUID,
) -> Optional[dict]:
    """Get most recent optimization run for a depot.

    Args:
        db: Database connection or pool
        depot_id: Depot UUID

    Returns:
        Most recent optimization run or None
    """
    query = """
        SELECT run_id, run_time, trigger_reason, horizon_start, horizon_end,
               solve_time_s, objective_value, peak_demand_kw, status, schedule_json
        FROM optimization_runs
        WHERE depot_id = $1
        ORDER BY run_time DESC
        LIMIT 1
    """
    return await db.fetchrow(query, depot_id)


# ============ INTER-DEPOT MESSAGE QUERIES ============


async def insert_interdepot_message(
    db,
    origin_depot_id: UUID,
    dest_depot_id: UUID,
    vehicle_id: UUID,
    departure_time: datetime,
    expected_soc: float,
    arrival_time: datetime,
    battery_kwh: float,
    max_charge_kw: float,
) -> UUID:
    """Insert inter-depot handoff message.

    Args:
        db: Database connection or pool
        origin_depot_id: Origin depot UUID
        dest_depot_id: Destination depot UUID
        vehicle_id: Vehicle UUID
        departure_time: Departure timestamp
        expected_soc: Expected SoC at arrival
        arrival_time: Expected arrival timestamp
        battery_kwh: Battery capacity
        max_charge_kw: Max charge power

    Returns:
        Generated message UUID
    """
    query = """
        INSERT INTO interdepot_messages
            (origin_depot_id, dest_depot_id, vehicle_id, departure_time,
             expected_soc, arrival_time, battery_kwh, max_charge_kw)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING message_id
    """
    row = await db.fetchrow(
        query,
        origin_depot_id,
        dest_depot_id,
        vehicle_id,
        departure_time,
        expected_soc,
        arrival_time,
        battery_kwh,
        max_charge_kw,
    )
    return row["message_id"]


async def get_pending_interdepot_messages(
    db,
    dest_depot_id: UUID,
) -> list[dict]:
    """Get pending inter-depot messages for a destination depot.

    Args:
        db: Database connection or pool
        dest_depot_id: Destination depot UUID

    Returns:
        List of pending message records
    """
    query = """
        SELECT message_id, origin_depot_id, vehicle_id, departure_time,
               expected_soc, arrival_time, battery_kwh, max_charge_kw, created_at
        FROM interdepot_messages
        WHERE dest_depot_id = $1
          AND status = 'pending'
        ORDER BY arrival_time ASC
    """
    return await db.fetch(query, dest_depot_id)


async def acknowledge_interdepot_message(
    db,
    message_id: UUID,
) -> bool:
    """Acknowledge receipt of inter-depot message.

    Args:
        db: Database connection or pool
        message_id: Message UUID

    Returns:
        True if message was updated, False if not found
    """
    query = """
        UPDATE interdepot_messages
        SET status = 'acknowledged',
            acknowledged_at = $2
        WHERE message_id = $1
          AND status = 'pending'
    """
    result = await db.execute(query, message_id, datetime.utcnow())
    return result != "UPDATE 0"


# ============ BUILDING LOAD QUERIES ============


async def get_building_load(
    db,
    depot_id: UUID,
    start_time: datetime,
    end_time: datetime,
) -> list[dict]:
    """Get building load data for a depot.

    Args:
        db: Database connection or pool
        depot_id: Depot UUID
        start_time: Start of time range
        end_time: End of time range

    Returns:
        List of building load records
    """
    query = """
        SELECT time, power_kw, source
        FROM building_load
        WHERE depot_id = $1
          AND time >= $2
          AND time <= $3
        ORDER BY time ASC
    """
    return await db.fetch(query, depot_id, start_time, end_time)


async def insert_building_load(
    db,
    depot_id: UUID,
    timestamp: datetime,
    power_kw: float,
    source: str,
) -> None:
    """Insert or update building load record.

    Args:
        db: Database connection or pool
        depot_id: Depot UUID
        timestamp: Timestamp
        power_kw: Building load in kW
        source: Source ('meter', 'api', 'forecast')
    """
    query = """
        INSERT INTO building_load (time, depot_id, power_kw, source)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (time, depot_id) DO UPDATE
        SET power_kw = EXCLUDED.power_kw,
            source = EXCLUDED.source
    """
    await db.execute(query, timestamp, depot_id, power_kw, source)


# ============ TRIGGER LOG QUERIES ============


async def insert_trigger_log(
    db,
    depot_id: UUID,
    trigger_type: str,
    details: Optional[dict] = None,
    run_id: Optional[UUID] = None,
) -> UUID:
    """Insert trigger log entry.

    Args:
        db: Database connection or pool
        depot_id: Depot UUID
        trigger_type: Trigger type string
        details: Optional details as JSON
        run_id: Optional resulting optimization run ID

    Returns:
        Generated trigger log UUID
    """
    import json

    query = """
        INSERT INTO trigger_log (depot_id, trigger_type, trigger_time, details, run_id)
        VALUES ($1, $2, $3, $4, $5)
        RETURNING trigger_id
    """
    details_json = json.dumps(details) if details else None
    row = await db.fetchrow(query, depot_id, trigger_type, datetime.utcnow(), details_json, run_id)
    return row["trigger_id"]


async def get_recent_triggers(
    db,
    depot_id: UUID,
    limit: int = 100,
) -> list[dict]:
    """Get recent trigger events for a depot.

    Args:
        db: Database connection or pool
        depot_id: Depot UUID
        limit: Maximum number of records to return

    Returns:
        List of trigger log records
    """
    query = """
        SELECT trigger_id, trigger_type, trigger_time, details, run_id
        FROM trigger_log
        WHERE depot_id = $1
        ORDER BY trigger_time DESC
        LIMIT $2
    """
    return await db.fetch(query, depot_id, limit)


# ============ VEHICLE QUERIES ============


async def get_vehicle_by_id_tag(
    db,
    id_tag: str,
) -> Optional[dict]:
    """Get vehicle by OCPP id_tag.

    Args:
        db: Database connection or pool
        id_tag: OCPP idTag string

    Returns:
        Vehicle record or None if not found
    """
    query = """
        SELECT id AS vehicle_id, site_id AS depot_id, external_id, vehicle_type,
               battery_capacity_kwh AS battery_kwh, max_charge_rate_kw AS max_charge_kw, id_tag
        FROM vehicles
        WHERE id_tag = $1
    """
    return await db.fetchrow(query, id_tag)


def _identity_record(row: Any) -> Optional[dict]:
    """Convert an asyncpg row to a plain dict with list relationship fields."""
    if not row:
        return None
    result = dict(row)
    for key in ("assigned_vehicle_ids", "assigned_driver_ids"):
        if key in result and result[key] is None:
            result[key] = []
    return result


async def list_fleet_identity(db, *, depot_id: str, organization_id: str) -> Optional[dict]:
    """Return vehicles, drivers, and RFID cards for an organization-owned depot."""
    depot_exists = await db.fetchval(
        """
        SELECT EXISTS(
            SELECT 1
            FROM sites
            WHERE id = $1::uuid
              AND organization_id = $2::uuid
        )
        """,
        depot_id,
        organization_id,
    )
    if not depot_exists:
        return None

    vehicles = await db.fetch(
        """
        SELECT id::text AS vehicle_id,
               site_id::text AS depot_id,
               external_id,
               display_name,
               vehicle_type,
               battery_capacity_kwh AS battery_kwh,
               max_charge_rate_kw AS max_charge_kw,
               id_tag,
               vin,
               license_plate,
               status,
               created_at,
               updated_at
        FROM vehicles
        WHERE site_id = $1::uuid
        ORDER BY external_id
        """,
        depot_id,
    )
    drivers = await db.fetch(
        """
        SELECT id::text AS driver_id,
               site_id::text AS depot_id,
               external_driver_id,
               display_name,
               email,
               phone,
               status,
               created_at,
               updated_at
        FROM drivers
        WHERE site_id = $1::uuid
        ORDER BY display_name
        """,
        depot_id,
    )
    cards = await db.fetch(
        """
        SELECT c.id::text AS card_id,
               c.site_id::text AS depot_id,
               c.id_tag,
               c.label,
               c.status,
               c.notes,
               COALESCE(
                   ARRAY_AGG(DISTINCT cva.vehicle_id::text)
                       FILTER (WHERE cva.vehicle_id IS NOT NULL),
                   ARRAY[]::text[]
               ) AS assigned_vehicle_ids,
               COALESCE(
                   ARRAY_AGG(DISTINCT cda.driver_id::text)
                       FILTER (WHERE cda.driver_id IS NOT NULL),
                   ARRAY[]::text[]
               ) AS assigned_driver_ids,
               c.created_at,
               c.updated_at
        FROM rfid_cards c
        LEFT JOIN rfid_card_vehicle_assignments cva ON cva.card_id = c.id
        LEFT JOIN rfid_card_driver_assignments cda ON cda.card_id = c.id
        WHERE c.site_id = $1::uuid
        GROUP BY c.id
        ORDER BY c.label NULLS LAST, c.id_tag
        """,
        depot_id,
    )
    return {
        "vehicles": [dict(row) for row in vehicles],
        "drivers": [dict(row) for row in drivers],
        "rfid_cards": [_identity_record(row) for row in cards],
    }


async def create_vehicle_identity(
    db,
    *,
    depot_id: str,
    organization_id: str,
    external_id: str,
    vehicle_type: str,
    battery_kwh: float,
    max_charge_kw: float,
    display_name: Optional[str],
    id_tag: Optional[str],
    vin: Optional[str],
    license_plate: Optional[str],
    vehicle_status: str,
) -> Optional[dict]:
    """Create a vehicle under an organization-owned depot."""
    query = """
        INSERT INTO vehicles (
            site_id, external_id, vehicle_type, battery_capacity_kwh, max_charge_rate_kw,
            display_name, id_tag, vin, license_plate, status
        )
        SELECT d.id, $3, $4, $5, $6, $7, $8, $9, $10, $11
        FROM sites d
        WHERE d.id = $1::uuid
          AND d.organization_id = $2::uuid
        RETURNING id::text AS vehicle_id,
                  site_id::text AS depot_id,
                  external_id,
                  display_name,
                  vehicle_type,
                  battery_capacity_kwh AS battery_kwh,
                  max_charge_rate_kw AS max_charge_kw,
                  id_tag,
                  vin,
                  license_plate,
                  status,
                  created_at,
                  updated_at
    """
    row = await db.fetchrow(
        query,
        depot_id,
        organization_id,
        external_id,
        vehicle_type,
        battery_kwh,
        max_charge_kw,
        display_name,
        id_tag,
        vin,
        license_plate,
        vehicle_status,
    )
    return dict(row) if row else None


async def update_vehicle_identity(
    db,
    *,
    depot_id: str,
    organization_id: str,
    vehicle_id: str,
    display_name: Optional[str],
    external_id: Optional[str],
    vehicle_type: Optional[str],
    battery_kwh: Optional[float],
    max_charge_kw: Optional[float],
    vin: Optional[str],
    license_plate: Optional[str],
    vehicle_status: Optional[str],
) -> Optional[dict]:
    """Patch vehicle identity fields under an organization-owned depot."""
    query = """
        UPDATE vehicles v
        SET display_name = COALESCE($4, display_name),
            external_id = COALESCE($5, external_id),
            vehicle_type = COALESCE($6, vehicle_type),
            battery_capacity_kwh = COALESCE($7, battery_capacity_kwh),
            max_charge_rate_kw = COALESCE($8, max_charge_rate_kw),
            vin = COALESCE($9, vin),
            license_plate = COALESCE($10, license_plate),
            status = COALESCE($11, status),
            updated_at = NOW()
        FROM sites d
        WHERE v.site_id = d.id
          AND v.site_id = $1::uuid
          AND d.organization_id = $2::uuid
          AND v.id = $3::uuid
        RETURNING v.id::text AS vehicle_id,
                  v.site_id::text AS depot_id,
                  v.external_id,
                  v.display_name,
                  v.vehicle_type,
                  v.battery_capacity_kwh AS battery_kwh,
                  v.max_charge_rate_kw AS max_charge_kw,
                  v.id_tag,
                  v.vin,
                  v.license_plate,
                  v.status,
                  v.created_at,
                  v.updated_at
    """
    row = await db.fetchrow(
        query,
        depot_id,
        organization_id,
        vehicle_id,
        display_name,
        external_id,
        vehicle_type,
        battery_kwh,
        max_charge_kw,
        vin,
        license_plate,
        vehicle_status,
    )
    return dict(row) if row else None


async def set_vehicle_primary_id_tag(
    db,
    *,
    depot_id: str,
    organization_id: str,
    vehicle_id: str,
    id_tag: Optional[str],
) -> Optional[dict]:
    """Set or clear the OCPP primary idTag on an organization-owned vehicle."""
    query = """
        UPDATE vehicles v
        SET id_tag = $4,
            updated_at = NOW()
        FROM sites d
        WHERE v.site_id = d.id
          AND v.site_id = $1::uuid
          AND d.organization_id = $2::uuid
          AND v.id = $3::uuid
        RETURNING v.id::text AS vehicle_id,
                  v.site_id::text AS depot_id,
                  v.external_id,
                  v.display_name,
                  v.vehicle_type,
                  v.battery_capacity_kwh AS battery_kwh,
                  v.max_charge_rate_kw AS max_charge_kw,
                  v.id_tag,
                  v.vin,
                  v.license_plate,
                  v.status,
                  v.created_at,
                  v.updated_at
    """
    row = await db.fetchrow(query, depot_id, organization_id, vehicle_id, id_tag)
    return dict(row) if row else None


async def create_driver_identity(
    db,
    *,
    depot_id: str,
    organization_id: str,
    external_driver_id: Optional[str],
    display_name: str,
    email: Optional[str],
    phone: Optional[str],
    driver_status: str,
) -> Optional[dict]:
    """Create a driver under an organization-owned depot."""
    query = """
        INSERT INTO drivers (site_id, external_driver_id, display_name, email, phone, status)
        SELECT d.id, $3, $4, $5, $6, $7
        FROM sites d
        WHERE d.id = $1::uuid
          AND d.organization_id = $2::uuid
        RETURNING id::text AS driver_id,
                  site_id::text AS depot_id,
                  external_driver_id,
                  display_name,
                  email,
                  phone,
                  status,
                  created_at,
                  updated_at
    """
    row = await db.fetchrow(
        query,
        depot_id,
        organization_id,
        external_driver_id,
        display_name,
        email,
        phone,
        driver_status,
    )
    return dict(row) if row else None


async def update_driver_identity(
    db,
    *,
    depot_id: str,
    organization_id: str,
    driver_id: str,
    external_driver_id: Optional[str],
    display_name: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    driver_status: Optional[str],
) -> Optional[dict]:
    """Patch driver identity fields under an organization-owned depot."""
    query = """
        UPDATE drivers dr
        SET external_driver_id = COALESCE($4, external_driver_id),
            display_name = COALESCE($5, display_name),
            email = COALESCE($6, email),
            phone = COALESCE($7, phone),
            status = COALESCE($8, status),
            updated_at = NOW()
        FROM sites d
        WHERE dr.site_id = d.id
          AND dr.site_id = $1::uuid
          AND d.organization_id = $2::uuid
          AND dr.id = $3::uuid
        RETURNING dr.id::text AS driver_id,
                  dr.site_id::text AS depot_id,
                  dr.external_driver_id,
                  dr.display_name,
                  dr.email,
                  dr.phone,
                  dr.status,
                  dr.created_at,
                  dr.updated_at
    """
    row = await db.fetchrow(
        query,
        depot_id,
        organization_id,
        driver_id,
        external_driver_id,
        display_name,
        email,
        phone,
        driver_status,
    )
    return dict(row) if row else None


async def create_rfid_card(
    db,
    *,
    depot_id: str,
    organization_id: str,
    id_tag: str,
    label: Optional[str],
    card_status: str,
    notes: Optional[str],
    assigned_vehicle_ids: list[str],
    assigned_driver_ids: list[str],
) -> Optional[dict]:
    """Create an RFID card and current assignments under an org-owned depot."""
    row = await db.fetchrow(
        """
        INSERT INTO rfid_cards (site_id, id_tag, label, status, notes)
        SELECT d.id, $3, $4, $5, $6
        FROM sites d
        WHERE d.id = $1::uuid
          AND d.organization_id = $2::uuid
        RETURNING id::text AS card_id,
                  site_id::text AS depot_id,
                  id_tag,
                  label,
                  status,
                  notes,
                  created_at,
                  updated_at
        """,
        depot_id,
        organization_id,
        id_tag,
        label,
        card_status,
        notes,
    )
    if not row:
        return None
    card_id = row["card_id"]
    await replace_rfid_card_assignments(
        db,
        depot_id=depot_id,
        card_id=card_id,
        assigned_vehicle_ids=assigned_vehicle_ids,
        assigned_driver_ids=assigned_driver_ids,
    )
    return await get_rfid_card(
        db, depot_id=depot_id, organization_id=organization_id, card_id=card_id
    )


async def update_rfid_card(
    db,
    *,
    depot_id: str,
    organization_id: str,
    card_id: str,
    id_tag: Optional[str],
    label: Optional[str],
    card_status: Optional[str],
    notes: Optional[str],
    assigned_vehicle_ids: Optional[list[str]],
    assigned_driver_ids: Optional[list[str]],
) -> Optional[dict]:
    """Patch RFID card metadata and optionally replace assignments."""
    row = await db.fetchrow(
        """
        UPDATE rfid_cards c
        SET id_tag = COALESCE($4, id_tag),
            label = COALESCE($5, label),
            status = COALESCE($6, status),
            notes = COALESCE($7, notes),
            updated_at = NOW()
        FROM sites d
        WHERE c.site_id = d.id
          AND c.site_id = $1::uuid
          AND d.organization_id = $2::uuid
          AND c.id = $3::uuid
        RETURNING c.id::text AS card_id
        """,
        depot_id,
        organization_id,
        card_id,
        id_tag,
        label,
        card_status,
        notes,
    )
    if not row:
        return None
    if assigned_vehicle_ids is not None or assigned_driver_ids is not None:
        await replace_rfid_card_assignments(
            db,
            depot_id=depot_id,
            card_id=card_id,
            assigned_vehicle_ids=assigned_vehicle_ids,
            assigned_driver_ids=assigned_driver_ids,
        )
    return await get_rfid_card(
        db, depot_id=depot_id, organization_id=organization_id, card_id=card_id
    )


async def replace_rfid_card_assignments(
    db,
    *,
    depot_id: str,
    card_id: str,
    assigned_vehicle_ids: Optional[list[str]],
    assigned_driver_ids: Optional[list[str]],
) -> None:
    """Replace current RFID card vehicle/driver assignments."""
    if assigned_vehicle_ids is not None:
        vehicle_ids = list(dict.fromkeys(assigned_vehicle_ids))
        if vehicle_ids:
            valid_vehicle_count = await db.fetchval(
                """
                SELECT COUNT(*)
                FROM vehicles
                WHERE site_id = $1::uuid
                  AND id = ANY($2::uuid[])
                """,
                depot_id,
                vehicle_ids,
            )
            if valid_vehicle_count != len(vehicle_ids):
                raise ValueError("assigned_vehicle_ids must all belong to the card depot")
        await db.execute(
            "DELETE FROM rfid_card_vehicle_assignments WHERE card_id = $1::uuid",
            card_id,
        )
        if vehicle_ids:
            await db.executemany(
                """
                INSERT INTO rfid_card_vehicle_assignments (card_id, vehicle_id)
                VALUES ($1::uuid, $2::uuid)
                """,
                [(card_id, vehicle_id) for vehicle_id in vehicle_ids],
            )
    if assigned_driver_ids is not None:
        driver_ids = list(dict.fromkeys(assigned_driver_ids))
        if driver_ids:
            valid_driver_count = await db.fetchval(
                """
                SELECT COUNT(*)
                FROM drivers
                WHERE site_id = $1::uuid
                  AND id = ANY($2::uuid[])
                """,
                depot_id,
                driver_ids,
            )
            if valid_driver_count != len(driver_ids):
                raise ValueError("assigned_driver_ids must all belong to the card depot")
        await db.execute(
            "DELETE FROM rfid_card_driver_assignments WHERE card_id = $1::uuid",
            card_id,
        )
        if driver_ids:
            await db.executemany(
                """
                INSERT INTO rfid_card_driver_assignments (card_id, driver_id)
                VALUES ($1::uuid, $2::uuid)
                """,
                [(card_id, driver_id) for driver_id in driver_ids],
            )


async def get_rfid_card(
    db,
    *,
    depot_id: str,
    organization_id: str,
    card_id: str,
) -> Optional[dict]:
    """Return one RFID card with current assignments."""
    row = await db.fetchrow(
        """
        SELECT c.id::text AS card_id,
               c.site_id::text AS depot_id,
               c.id_tag,
               c.label,
               c.status,
               c.notes,
               COALESCE(
                   ARRAY_AGG(DISTINCT cva.vehicle_id::text)
                       FILTER (WHERE cva.vehicle_id IS NOT NULL),
                   ARRAY[]::text[]
               ) AS assigned_vehicle_ids,
               COALESCE(
                   ARRAY_AGG(DISTINCT cda.driver_id::text)
                       FILTER (WHERE cda.driver_id IS NOT NULL),
                   ARRAY[]::text[]
               ) AS assigned_driver_ids,
               c.created_at,
               c.updated_at
        FROM rfid_cards c
        JOIN sites d ON d.id = c.site_id
        LEFT JOIN rfid_card_vehicle_assignments cva ON cva.card_id = c.id
        LEFT JOIN rfid_card_driver_assignments cda ON cda.card_id = c.id
        WHERE c.site_id = $1::uuid
          AND d.organization_id = $2::uuid
          AND c.id = $3::uuid
        GROUP BY c.id
        """,
        depot_id,
        organization_id,
        card_id,
    )
    return _identity_record(row)


async def resolve_id_tag_identity(
    db,
    id_tag: str,
    *,
    station_id: Optional[str] = None,
) -> Optional[dict]:
    """Resolve an OCPP idTag to vehicle, driver, and card identity.

    Vehicle primary idTags are authoritative. Active RFID cards are accepted
    after vehicle lookup; inactive/lost/stolen cards deliberately do not match.
    If station_id is provided, the lookup is scoped to that charger's depot.
    """
    depot_filter = ""
    params: list[Any] = [id_tag]
    if station_id is not None:
        depot_filter = " AND EXISTS (SELECT 1 FROM charging_stations c WHERE c.station_id = $2 AND c.site_id = v.site_id)"
        params.append(station_id)

    vehicle_rows = await db.fetch(
        f"""
        SELECT v.id::text AS vehicle_id,
               v.site_id::text AS depot_id,
               NULL::text AS driver_id,
               NULL::text AS card_id,
               'vehicle'::text AS source
        FROM vehicles v
        WHERE v.id_tag = $1
          AND v.status = 'active'
          {depot_filter}
        LIMIT 2
        """,
        *params,
    )
    if len(vehicle_rows) > 1:
        logger.error("Rejecting idTag %r: multiple active vehicles share it", id_tag)
        return None
    if vehicle_rows:
        return dict(vehicle_rows[0])

    card_depot_filter = ""
    params = [id_tag]
    if station_id is not None:
        card_depot_filter = " AND EXISTS (SELECT 1 FROM charging_stations ch WHERE ch.station_id = $2 AND ch.site_id = c.site_id)"
        params.append(station_id)
    card_rows = await db.fetch(
        f"""
        SELECT c.id::text AS card_id,
               c.site_id::text AS depot_id,
               (
                   SELECT cva.vehicle_id::text
                   FROM rfid_card_vehicle_assignments cva
                   JOIN vehicles v ON v.id = cva.vehicle_id
                   WHERE cva.card_id = c.id
                     AND v.site_id = c.site_id
                     AND v.status = 'active'
                   ORDER BY v.external_id
                   LIMIT 1
               ) AS vehicle_id,
               (
                   SELECT cda.driver_id::text
                   FROM rfid_card_driver_assignments cda
                   JOIN drivers dr ON dr.id = cda.driver_id
                   WHERE cda.card_id = c.id
                     AND dr.site_id = c.site_id
                     AND dr.status = 'active'
                   ORDER BY dr.display_name
                   LIMIT 1
               ) AS driver_id,
               'rfid_card'::text AS source
        FROM rfid_cards c
        WHERE c.id_tag = $1
          AND c.status = 'active'
          {card_depot_filter}
        LIMIT 2
        """,
        *params,
    )
    if len(card_rows) > 1:
        logger.error("Rejecting idTag %r: multiple active RFID cards share it", id_tag)
        return None
    return dict(card_rows[0]) if card_rows else None


async def get_depot_by_id(db, depot_id: str) -> Optional[dict]:
    """Get depot metadata by depot_id.

    Args:
        db: Database connection or pool
        depot_id: Depot UUID string

    Returns:
        Dict with depot fields, or None if not found
    """
    query = """
        SELECT id::text AS depot_id,
               organization_id::text AS organization_id,
               name,
               latitude,
               longitude,
               timezone,
               currency,
               utility_id,
               max_grid_kw,
               demand_charge_rate_kw,
               demand_charge_billing_period,
               address,
               billing_metadata,
               building_load_source
        FROM sites
        WHERE id = $1::uuid
    """
    row = await db.fetchrow(query, depot_id)
    if not row:
        return None
    result = dict(row)
    result["address"] = _coerce_jsonb_dict(result.get("address"))
    result["billing_metadata"] = _coerce_jsonb_dict(result.get("billing_metadata"))
    result["building_load_source"] = _coerce_jsonb_dict(result.get("building_load_source"))
    return result


async def get_depots_by_ids(db, depot_ids: list[str]) -> list[dict]:
    """Get metadata for a list of depots by their IDs.

    Args:
        db: Database connection or pool
        depot_ids: List of depot UUID strings

    Returns:
        List of depot metadata dicts (same order not guaranteed)
    """
    query = """
        SELECT id::text AS depot_id,
               organization_id::text AS organization_id,
               name,
               latitude,
               longitude,
               timezone,
               currency,
               utility_id,
               max_grid_kw,
               demand_charge_rate_kw,
               demand_charge_billing_period,
               address,
               billing_metadata,
               building_load_source
        FROM sites
        WHERE id = ANY($1::uuid[])
        ORDER BY name
    """
    rows = await db.fetch(query, depot_ids)
    depots: list[dict] = []
    for row in rows:
        result = dict(row)
        result["address"] = _coerce_jsonb_dict(result.get("address"))
        result["billing_metadata"] = _coerce_jsonb_dict(result.get("billing_metadata"))
        result["building_load_source"] = _coerce_jsonb_dict(result.get("building_load_source"))
        depots.append(result)
    return depots


async def get_all_depots(db) -> list[dict]:
    """Get metadata for all depots (admin use).

    Args:
        db: Database connection or pool

    Returns:
        List of depot metadata dicts ordered by name
    """
    query = """
        SELECT id::text AS depot_id,
               organization_id::text AS organization_id,
               name,
               latitude,
               longitude,
               timezone,
               currency,
               utility_id,
               max_grid_kw,
               demand_charge_rate_kw,
               demand_charge_billing_period,
               address,
               billing_metadata,
               building_load_source
        FROM sites
        ORDER BY name
    """
    rows = await db.fetch(query)
    depots: list[dict] = []
    for row in rows:
        result = dict(row)
        result["address"] = _coerce_jsonb_dict(result.get("address"))
        result["billing_metadata"] = _coerce_jsonb_dict(result.get("billing_metadata"))
        result["building_load_source"] = _coerce_jsonb_dict(result.get("building_load_source"))
        depots.append(result)
    return depots


async def get_depots_for_organization(db, organization_id: str) -> list[dict]:
    """Return depot metadata rows for a single organization."""
    query = """
        SELECT id::text AS depot_id,
               organization_id::text AS organization_id,
               name,
               latitude,
               longitude,
               timezone,
               currency,
               utility_id,
               max_grid_kw,
               demand_charge_rate_kw,
               demand_charge_billing_period,
               address,
               billing_metadata,
               building_load_source
        FROM sites
        WHERE organization_id = $1::uuid
        ORDER BY name
    """
    rows = await db.fetch(query, organization_id)
    depots: list[dict] = []
    for row in rows:
        result = dict(row)
        result["address"] = _coerce_jsonb_dict(result.get("address"))
        result["billing_metadata"] = _coerce_jsonb_dict(result.get("billing_metadata"))
        result["building_load_source"] = _coerce_jsonb_dict(result.get("building_load_source"))
        depots.append(result)
    return depots


async def depot_belongs_to_organization(db, depot_id: str, organization_id: str) -> bool:
    """Return True if the depot exists and is assigned to the organization."""
    q = """
        SELECT EXISTS(
            SELECT 1 FROM sites
            WHERE id = $1::uuid AND organization_id = $2::uuid
        )
    """
    return bool(await db.fetchval(q, depot_id, organization_id))


async def get_depot_org_slug_context(db, *, depot_id: str, organization_id: str) -> Optional[dict]:
    """Return organization/depot names for an organization-owned depot."""
    query = """
        SELECT d.id::text AS depot_id,
               d.name AS depot_name,
               d.organization_id::text AS organization_id,
               o.name AS organization_name
        FROM sites d
        JOIN organizations o ON o.id = d.organization_id
        WHERE d.id = $1::uuid AND d.organization_id = $2::uuid
    """
    row = await db.fetchrow(query, depot_id, organization_id)
    return dict(row) if row else None


async def next_charger_ocpp_id(
    db,
    *,
    organization_slug: str,
    depot_slug: str,
) -> str:
    """Generate the next depot-scoped immutable OCPP charge point id."""
    # Use a separator that slugification never emits to keep org/depot boundaries unambiguous.
    prefix = f"{organization_slug}_{depot_slug}"
    query = """
        SELECT station_id AS ocpp_id
        FROM charging_stations
        WHERE station_id ~ ('^' || $1 || '-[0-9]+$')
        ORDER BY substring(station_id FROM '([0-9]+)$')::integer DESC
        LIMIT 1
    """
    latest = await db.fetchval(query, prefix)
    next_sequence = 1
    if latest:
        try:
            next_sequence = int(str(latest).rsplit("-", 1)[1]) + 1
        except (IndexError, ValueError):
            logger.warning("Unexpected generated ocpp_id format: %s", latest)
    return f"{prefix}-{next_sequence:03d}"


async def create_charger_with_credentials(
    db,
    *,
    depot_id: str,
    ocpp_id: str,
    display_name: str,
    vendor: Optional[str],
    model: Optional[str],
    serial_number: Optional[str],
    firmware: Optional[str],
    rated_kw: float,
    connector_type: str,
    connector_count: int,
    connector_ids: list[int],
    network_notes: Optional[str],
    password_hash: str,
) -> dict:
    """Create a charger and its Basic Auth credential in one transaction."""
    charger_query = """
        INSERT INTO charging_stations (
            site_id,
            station_id,
            max_power_kw,
            connector_type,
            display_name,
            vendor,
            model,
            serial_number,
            firmware,
            connector_count,
            connector_ids,
            network_notes,
            auth_required
        )
        VALUES (
            $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, TRUE
        )
        RETURNING id::text AS id,
                  site_id::text AS depot_id,
                  station_id AS ocpp_id,
                  display_name,
                  vendor,
                  model,
                  serial_number,
                  firmware,
                  max_power_kw AS rated_kw,
                  efficiency,
                  connector_type,
                  connector_count,
                  connector_ids,
                  network_notes,
                  status,
                  auth_required,
                  created_at
    """
    credential_query = """
        INSERT INTO station_credentials (station_id, username, password_hash, active)
        VALUES ($1, $1, $2, TRUE)
    """
    row = await db.fetchrow(
        charger_query,
        depot_id,
        ocpp_id,
        rated_kw,
        connector_type,
        display_name,
        vendor,
        model,
        serial_number,
        firmware,
        connector_count,
        json.dumps(connector_ids),
        network_notes,
    )
    await db.execute(credential_query, ocpp_id, password_hash)
    result = dict(row)
    result["connector_ids"] = result.get("connector_ids") or connector_ids
    return result


async def get_charger_onboarding_idempotency(
    db,
    *,
    organization_id: str,
    endpoint: str,
    idempotency_key: str,
) -> Optional[dict]:
    """Return an unexpired idempotency replay row, if present."""
    query = """
        SELECT request_hash, response_json, status_code
        FROM charger_onboarding_idempotency
        WHERE organization_id = $1::uuid
          AND endpoint = $2
          AND idempotency_key = $3
          AND expires_at > NOW()
    """
    row = await db.fetchrow(query, organization_id, endpoint, idempotency_key)
    return dict(row) if row else None


async def acquire_charger_onboarding_idempotency_lock(
    db,
    *,
    organization_id: str,
    endpoint: str,
    idempotency_key: str,
) -> None:
    """Acquire a transaction-scoped lock for charger onboarding idempotency key."""
    lock_scope = f"{organization_id}:{endpoint}:{idempotency_key}"
    await db.execute(
        "SELECT pg_advisory_xact_lock(hashtext($1), hashtext($2))",
        "charger_onboarding_idempotency",
        lock_scope,
    )


async def store_charger_onboarding_idempotency(
    db,
    *,
    organization_id: str,
    user_id: str,
    endpoint: str,
    idempotency_key: str,
    request_hash: str,
    response_json: dict,
    status_code: int,
    ttl_minutes: int = 30,
) -> None:
    """Store the short-lived one-time credential replay payload."""
    query = """
        INSERT INTO charger_onboarding_idempotency (
            organization_id,
            user_id,
            endpoint,
            idempotency_key,
            request_hash,
            response_json,
            status_code,
            expires_at
        )
        VALUES (
            $1::uuid,
            $2::uuid,
            $3,
            $4,
            $5,
            $6::jsonb,
            $7,
            NOW() + ($8::int * INTERVAL '1 minute')
        )
        ON CONFLICT (organization_id, endpoint, idempotency_key) DO UPDATE
        SET response_json = EXCLUDED.response_json,
            status_code = EXCLUDED.status_code,
            expires_at = EXCLUDED.expires_at,
            request_hash = EXCLUDED.request_hash
        WHERE charger_onboarding_idempotency.request_hash = EXCLUDED.request_hash
    """
    await db.execute(
        query,
        organization_id,
        user_id,
        endpoint,
        idempotency_key,
        request_hash,
        json.dumps(response_json),
        status_code,
        ttl_minutes,
    )


async def delete_expired_charger_onboarding_idempotency(db) -> None:
    """Remove expired onboarding replay payloads that may contain one-time secrets."""
    await db.execute("DELETE FROM charger_onboarding_idempotency WHERE expires_at <= NOW()")


async def create_depot_setup(
    db,
    *,
    organization_id: str,
    name: str,
    latitude: float,
    longitude: float,
    timezone: str,
    currency: str,
    utility_id: str,
    max_grid_kw: float,
    demand_charge_rate_kw: float,
    demand_charge_billing_period: str,
    address: dict[str, Any],
    billing_metadata: dict[str, Any],
    building_load_source: dict[str, Any],
    building_load_assumption_kw: float = 0.0,
    charger_vehicle_access_default: str = "explicit_matrix",
    tariff_type: str = "simple_demand",
    energy_cap_kwh: Optional[float] = None,
    under_cap_rate_per_kwh: Optional[float] = None,
    over_cap_penalty_per_kwh: Optional[float] = None,
    cap_billing_period: str = "monthly",
) -> dict:
    """Create a depot row scoped to organization and return metadata."""
    query = """
        INSERT INTO sites (
            organization_id,
            name,
            latitude,
            longitude,
            timezone,
            currency,
            utility_id,
            max_grid_kw,
            demand_charge_rate_kw,
            demand_charge_billing_period,
            address,
            billing_metadata,
            building_load_source,
            building_load_assumption_kw,
            charger_vehicle_access_default,
            tariff_type,
            energy_cap_kwh,
            under_cap_rate_per_kwh,
            over_cap_penalty_per_kwh,
            cap_billing_period
        )
        VALUES (
            $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10,
            $11::jsonb, $12::jsonb, $13::jsonb, $14,
            $15, $16, $17, $18, $19, $20
        )
        RETURNING id::text AS depot_id,
                  organization_id::text AS organization_id,
                  name,
                  latitude,
                  longitude,
                  timezone,
                  currency,
                  utility_id,
                  max_grid_kw,
                  demand_charge_rate_kw,
                  demand_charge_billing_period,
                  address,
                  billing_metadata,
                  building_load_source,
                  building_load_assumption_kw,
                  charger_vehicle_access_default,
                  tariff_type,
                  energy_cap_kwh,
                  under_cap_rate_per_kwh,
                  over_cap_penalty_per_kwh,
                  cap_billing_period
    """
    row = await db.fetchrow(
        query,
        organization_id,
        name,
        latitude,
        longitude,
        timezone,
        currency,
        utility_id,
        max_grid_kw,
        demand_charge_rate_kw,
        demand_charge_billing_period,
        json.dumps(address),
        json.dumps(billing_metadata),
        json.dumps(building_load_source),
        building_load_assumption_kw,
        charger_vehicle_access_default,
        tariff_type,
        energy_cap_kwh,
        under_cap_rate_per_kwh,
        over_cap_penalty_per_kwh,
        cap_billing_period,
    )
    result = dict(row)
    result["address"] = _coerce_jsonb_dict(result.get("address"))
    result["billing_metadata"] = _coerce_jsonb_dict(result.get("billing_metadata"))
    result["building_load_source"] = _coerce_jsonb_dict(result.get("building_load_source"))
    return result


async def update_depot_setup(
    db,
    *,
    depot_id: str,
    name: str,
    latitude: float,
    longitude: float,
    timezone: str,
    currency: str,
    utility_id: str,
    max_grid_kw: float,
    demand_charge_rate_kw: float,
    demand_charge_billing_period: str,
    address: dict[str, Any],
    billing_metadata: dict[str, Any],
    building_load_source: dict[str, Any],
    building_load_assumption_kw: float = 0.0,
    charger_vehicle_access_default: str = "explicit_matrix",
    tariff_type: str = "simple_demand",
    energy_cap_kwh: Optional[float] = None,
    under_cap_rate_per_kwh: Optional[float] = None,
    over_cap_penalty_per_kwh: Optional[float] = None,
    cap_billing_period: str = "monthly",
) -> Optional[dict]:
    """Update depot setup metadata and return updated row."""
    query = """
        UPDATE sites
        SET name = $2,
            latitude = $3,
            longitude = $4,
            timezone = $5,
            currency = $6,
            utility_id = $7,
            max_grid_kw = $8,
            demand_charge_rate_kw = $9,
            demand_charge_billing_period = $10,
            address = $11::jsonb,
            billing_metadata = $12::jsonb,
            building_load_source = $13::jsonb,
            building_load_assumption_kw = $14,
            charger_vehicle_access_default = $15,
            tariff_type = $16,
            energy_cap_kwh = $17,
            under_cap_rate_per_kwh = $18,
            over_cap_penalty_per_kwh = $19,
            cap_billing_period = $20,
            updated_at = NOW()
        WHERE id = $1::uuid
        RETURNING id::text AS depot_id,
                  organization_id::text AS organization_id,
                  name,
                  latitude,
                  longitude,
                  timezone,
                  currency,
                  utility_id,
                  max_grid_kw,
                  demand_charge_rate_kw,
                  demand_charge_billing_period,
                  address,
                  billing_metadata,
                  building_load_source,
                  building_load_assumption_kw,
                  charger_vehicle_access_default,
                  tariff_type,
                  energy_cap_kwh,
                  under_cap_rate_per_kwh,
                  over_cap_penalty_per_kwh,
                  cap_billing_period
    """
    row = await db.fetchrow(
        query,
        depot_id,
        name,
        latitude,
        longitude,
        timezone,
        currency,
        utility_id,
        max_grid_kw,
        demand_charge_rate_kw,
        demand_charge_billing_period,
        json.dumps(address),
        json.dumps(billing_metadata),
        json.dumps(building_load_source),
        building_load_assumption_kw,
        charger_vehicle_access_default,
        tariff_type,
        energy_cap_kwh,
        under_cap_rate_per_kwh,
        over_cap_penalty_per_kwh,
        cap_billing_period,
    )
    if not row:
        return None
    result = dict(row)
    result["address"] = _coerce_jsonb_dict(result.get("address"))
    result["billing_metadata"] = _coerce_jsonb_dict(result.get("billing_metadata"))
    result["building_load_source"] = _coerce_jsonb_dict(result.get("building_load_source"))
    return result


async def upsert_charger_vehicle_access(
    db,
    *,
    depot_id: str,
    entries: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Per-row upsert into ``charger_vehicle_access`` for a depot.

    Each entry must have ``charger_id``, ``vehicle_id``, ``is_accessible``.
    Validates that every charger and vehicle belongs to ``depot_id``;
    returns ``{"invalid_chargers": [...], "invalid_vehicles": [...]}`` for
    any IDs that don't, and writes nothing in that case so the caller can
    return a 400 without partial state.
    """
    charger_ids = {entry["charger_id"] for entry in entries}
    vehicle_ids = {entry["vehicle_id"] for entry in entries}

    valid_chargers = {
        row["charger_id"]
        for row in await db.fetch(
            "SELECT id::text AS charger_id FROM charging_stations "
            "WHERE site_id = $1::uuid AND id = ANY($2::uuid[])",
            depot_id,
            list(charger_ids),
        )
    }
    valid_vehicles = {
        row["vehicle_id"]
        for row in await db.fetch(
            "SELECT id::text AS vehicle_id FROM vehicles "
            "WHERE site_id = $1::uuid AND id = ANY($2::uuid[])",
            depot_id,
            list(vehicle_ids),
        )
    }
    invalid_chargers = sorted(charger_ids - valid_chargers)
    invalid_vehicles = sorted(vehicle_ids - valid_vehicles)
    if invalid_chargers or invalid_vehicles:
        return {
            "invalid_chargers": invalid_chargers,
            "invalid_vehicles": invalid_vehicles,
        }

    upsert_query = """
        INSERT INTO charger_vehicle_access (charging_station_id, vehicle_id, is_accessible)
        VALUES ($1::uuid, $2::uuid, $3)
        ON CONFLICT (charging_station_id, vehicle_id) DO UPDATE
        SET is_accessible = EXCLUDED.is_accessible
    """
    for entry in entries:
        await db.execute(
            upsert_query,
            entry["charger_id"],
            entry["vehicle_id"],
            bool(entry["is_accessible"]),
        )
    return {"invalid_chargers": [], "invalid_vehicles": []}


async def upsert_battery_storage(
    db,
    *,
    depot_id: str,
    capacity_kwh: float,
    max_power_kw: float,
    soc_min: float,
    soc_max: float,
) -> None:
    """Create/update depot battery row."""
    query = """
        INSERT INTO battery_storage (site_id, capacity_kwh, max_power_kw, soc_min, soc_max)
        VALUES ($1::uuid, $2, $3, $4, $5)
        ON CONFLICT (site_id) DO UPDATE
        SET capacity_kwh = EXCLUDED.capacity_kwh,
            max_power_kw = EXCLUDED.max_power_kw,
            soc_min = EXCLUDED.soc_min,
            soc_max = EXCLUDED.soc_max
    """
    await db.execute(query, depot_id, capacity_kwh, max_power_kw, soc_min, soc_max)


async def delete_battery_storage(db, *, depot_id: str) -> None:
    """Delete battery config for depot."""
    await db.execute("DELETE FROM battery_storage WHERE site_id = $1::uuid", depot_id)


async def update_vehicle_max_charge_kw(
    db,
    vehicle_id: UUID,
    max_charge_kw: float,
) -> bool:
    """Update vehicle max_charge_kw from OCPP MeterValues.

    Per PRD Section 8.4, OCPP values take precedence over static config.

    Args:
        db: Database connection or pool
        vehicle_id: Vehicle UUID
        max_charge_kw: Max charge power from OCPP

    Returns:
        True if updated, False if not found
    """
    query = """
        UPDATE vehicles
        SET max_charge_rate_kw = $2
        WHERE id = $1
    """
    result = await db.execute(query, vehicle_id, max_charge_kw)
    return result != "UPDATE 0"


# ── Admin cross-org queries ─────────────────────────────────────────────────


async def list_all_organizations(db) -> list[dict]:
    """Return all organizations (favonius_admin only)."""
    query = """
        SELECT id::text AS organization_id,
               name,
               created_at,
               updated_at
        FROM organizations
        ORDER BY name
    """
    rows = await db.fetch(query)
    return [dict(row) for row in rows]


async def get_charger_credentials_status(db, *, depot_id: str, charger_id: str) -> Optional[dict]:
    """Return credential status (configured / created_at / last_rotated_at).

    Joins ``chargers`` to ``station_credentials`` by ocpp_id. Returns None if the
    charger does not exist (caller must distinguish 403 vs 404 themselves).
    NEVER returns the password_hash.
    """
    query = """
        SELECT c.id::text AS charger_id,
               c.site_id::text AS depot_id,
               c.station_id AS ocpp_id,
               sc.created_at AS credentials_created_at,
               sc.last_rotated_at AS credentials_last_rotated_at,
               sc.active AS credentials_active
        FROM charging_stations c
        LEFT JOIN station_credentials sc
            ON sc.station_id = c.station_id AND sc.username = c.station_id
        WHERE c.id = $1::uuid AND c.site_id = $2::uuid
    """
    row = await db.fetchrow(query, charger_id, depot_id)
    return dict(row) if row else None


async def rotate_charger_credentials(
    db, *, depot_id: str, charger_id: str, new_password_hash: str
) -> Optional[dict]:
    """Replace ``station_credentials.password_hash`` for a charger.

    Returns ocpp_id and last_rotated_at, or None if the charger / credential
    does not exist. Never reads or returns the previous hash.
    """
    fetch_query = """
        SELECT station_id AS ocpp_id
        FROM charging_stations
        WHERE id = $1::uuid AND site_id = $2::uuid
        FOR UPDATE
    """
    row = await db.fetchrow(fetch_query, charger_id, depot_id)
    if row is None:
        return None
    ocpp_id = row["ocpp_id"]

    upsert_query = """
        INSERT INTO station_credentials (station_id, username, password_hash, active, last_rotated_at)
        VALUES ($1, $1, $2, TRUE, NOW())
        ON CONFLICT (station_id, username) DO UPDATE
        SET password_hash = EXCLUDED.password_hash,
            active = TRUE,
            last_rotated_at = NOW()
        RETURNING last_rotated_at, created_at
    """
    updated = await db.fetchrow(upsert_query, ocpp_id, new_password_hash)
    return {
        "ocpp_id": ocpp_id,
        "last_rotated_at": updated["last_rotated_at"],
        "created_at": updated["created_at"],
    }


# ============ Fleet List Helpers (GET /depots/{id}/chargers, /vehicles) ============


async def list_chargers_for_depot(db, *, depot_id: str) -> list[dict]:
    """Return static charger rows for a depot.

    Does NOT join runtime tables (those live on the Timescale pool). Caller
    is responsible for stitching telemetry/connector_status/sessions in.
    """
    query = """
        SELECT id::text                            AS id,
               site_id::text                       AS depot_id,
               station_id                          AS ocpp_id,
               display_name,
               vendor,
               model,
               serial_number,
               firmware_version                    AS firmware,
               max_power_kw                        AS rated_kw,
               efficiency,
               connector_type,
               connector_count,
               connector_ids,
               network_notes,
               auth_required,
               created_at
        FROM charging_stations
        WHERE site_id = $1::uuid
        ORDER BY station_id
    """
    rows = await db.fetch(query, depot_id)
    result: list[dict] = []
    for row in rows:
        item = dict(row)
        # connector_ids comes back as either a JSON string or a Python list
        # depending on asyncpg's jsonb codec config; normalize to list[int].
        raw_connector_ids = item.get("connector_ids")
        if isinstance(raw_connector_ids, str):
            try:
                parsed = json.loads(raw_connector_ids)
                item["connector_ids"] = parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                item["connector_ids"] = []
        elif raw_connector_ids is None:
            item["connector_ids"] = []
        result.append(item)
    return result


async def list_vehicles_for_depot(db, *, depot_id: str) -> list[dict]:
    """Return static vehicle rows for a depot."""
    query = """
        SELECT id::text                            AS id,
               site_id::text                       AS depot_id,
               external_id,
               display_name,
               vehicle_type,
               vin,
               license_plate,
               id_tag,
               battery_capacity_kwh,
               max_charge_rate_kw,
               max_discharge_rate_kw,
               COALESCE(v2g_capable, FALSE)        AS v2g_capable,
               make,
               model,
               year,
               status,
               created_at
        FROM vehicles
        WHERE site_id = $1::uuid
        ORDER BY external_id NULLS LAST, id
    """
    rows = await db.fetch(query, depot_id)
    return [dict(r) for r in rows]


async def latest_connector_status_by_stations(db, station_ids: list[str]) -> dict[str, dict]:
    """Latest connector_status row per station_id (across all connectors).

    Returns a mapping ``ocpp_id -> {ocpp_status, last_heartbeat_at}`` where:
      * ``ocpp_status`` is the StatusNotification value of the most recently
        updated connector on the station (most "interesting" wins for the
        4-state pill: Faulted > Charging > else).
      * ``last_heartbeat_at`` is the MAX timestamp across all connectors.
    """
    if not station_ids:
        return {}
    query = """
        WITH latest_per_connector AS (
            SELECT DISTINCT ON (station_id, connector_id)
                station_id, connector_id, status, timestamp
            FROM connector_status
            WHERE station_id = ANY($1)
            ORDER BY station_id, connector_id, timestamp DESC
        )
        SELECT station_id,
               array_agg(status ORDER BY
                   CASE status
                       WHEN 'Faulted' THEN 0
                       WHEN 'Charging' THEN 1
                       WHEN 'SuspendedEV' THEN 2
                       WHEN 'SuspendedEVSE' THEN 3
                       WHEN 'Preparing' THEN 4
                       WHEN 'Finishing' THEN 5
                       WHEN 'Reserved' THEN 6
                       WHEN 'Available' THEN 7
                       WHEN 'Unavailable' THEN 8
                       ELSE 9
                   END
               ) AS statuses_by_priority,
               MAX(timestamp) AS last_heartbeat_at
        FROM latest_per_connector
        GROUP BY station_id
    """
    rows = await db.fetch(query, station_ids)
    out: dict[str, dict] = {}
    for row in rows:
        statuses = row["statuses_by_priority"] or []
        out[row["station_id"]] = {
            "ocpp_status": statuses[0] if statuses else None,
            "last_heartbeat_at": row["last_heartbeat_at"],
        }
    return out


async def open_sessions_by_stations(db, station_ids: list[str]) -> dict[str, dict]:
    """Latest open ``charging_sessions`` row per station_id.

    Open = ``end_time IS NULL``. Returns mapping ``ocpp_id -> session dict``.
    Multiple open sessions on the same station should not happen but we pick
    the most recent ``start_time`` defensively.
    """
    if not station_ids:
        return {}
    query = """
        SELECT DISTINCT ON (station_id)
               station_id,
               session_id,
               vehicle_id::text  AS vehicle_id,
               start_time        AS started_at,
               current_power_kw,
               current_soc,
               target_soc,
               estimated_end_time AS estimated_end_at
        FROM charging_sessions
        WHERE station_id = ANY($1) AND end_time IS NULL
        ORDER BY station_id, start_time DESC
    """
    rows = await db.fetch(query, station_ids)
    return {row["station_id"]: dict(row) for row in rows}


async def latest_telemetry_by_vehicles(db, vehicle_ids: list[str]) -> dict[str, dict]:
    """Latest telemetry row per vehicle_id.

    Returns mapping ``vehicle_id -> {soc, charging_kw, charger_id, is_plugged, time}``.
    """
    if not vehicle_ids:
        return {}
    query = """
        SELECT DISTINCT ON (vehicle_id)
               vehicle_id::text  AS vehicle_id,
               time              AS last_seen_at,
               soc               AS current_soc,
               charging_kw       AS current_power_kw,
               charger_id::text  AS charger_id,
               is_plugged
        FROM telemetry
        WHERE vehicle_id = ANY($1::uuid[])
        ORDER BY vehicle_id, time DESC
    """
    rows = await db.fetch(query, vehicle_ids)
    return {row["vehicle_id"]: dict(row) for row in rows}


async def open_session_by_vehicles(db, vehicle_ids: list[str]) -> dict[str, dict]:
    """Latest open charging session per vehicle_id.

    Returns ``vehicle_id -> session dict`` with session_id and station_id so
    the caller can resolve the connected charger UUID from its OCPP id.
    """
    if not vehicle_ids:
        return {}
    query = """
        SELECT DISTINCT ON (vehicle_id)
               vehicle_id::text AS vehicle_id,
               session_id,
               station_id       AS ocpp_id,
               start_time       AS started_at,
               current_power_kw
        FROM charging_sessions
        WHERE vehicle_id = ANY($1::uuid[]) AND end_time IS NULL
        ORDER BY vehicle_id, start_time DESC
    """
    rows = await db.fetch(query, vehicle_ids)
    return {row["vehicle_id"]: dict(row) for row in rows}


async def next_departures_by_vehicles(db, vehicle_ids: list[str]) -> dict[str, dict]:
    """Next future ``schedules`` row per vehicle_id.

    "Next" = ``departure_time > now()`` ordered ASC, first row.
    """
    if not vehicle_ids:
        return {}
    query = """
        SELECT DISTINCT ON (vehicle_id)
               vehicle_id::text AS vehicle_id,
               id::text          AS schedule_id,
               route_id,
               departure_time,
               return_time,
               required_soc,
               energy_kwh
        FROM schedules
        WHERE vehicle_id = ANY($1::uuid[]) AND departure_time > NOW()
        ORDER BY vehicle_id, departure_time ASC
    """
    rows = await db.fetch(query, vehicle_ids)
    return {row["vehicle_id"]: dict(row) for row in rows}


async def active_schedule_by_vehicles(db, vehicle_ids: list[str]) -> dict[str, dict]:
    """Currently-active ``schedules`` row per vehicle_id (for in_route check).

    Active = ``departure_time <= now() < COALESCE(actual_return_time, return_time)``.
    """
    if not vehicle_ids:
        return {}
    query = """
        SELECT DISTINCT ON (vehicle_id)
               vehicle_id::text AS vehicle_id,
               id::text          AS schedule_id,
               departure_time,
               return_time,
               actual_return_time
        FROM schedules
        WHERE vehicle_id = ANY($1::uuid[])
          AND departure_time <= NOW()
          AND NOW() < COALESCE(actual_return_time, return_time)
        ORDER BY vehicle_id, departure_time DESC
    """
    rows = await db.fetch(query, vehicle_ids)
    return {row["vehicle_id"]: dict(row) for row in rows}


async def charger_id_by_ocpp_id(db, *, depot_id: str) -> dict[str, str]:
    """Return mapping ``ocpp_id -> charging_stations.id`` (UUID, as text) for a depot.

    Used to resolve ``charging_sessions.station_id`` (the OCPP id) to the
    canonical charger UUID that frontends use as their stable handle.
    """
    query = """
        SELECT station_id AS ocpp_id, id::text AS charger_id
        FROM charging_stations
        WHERE site_id = $1::uuid
    """
    rows = await db.fetch(query, depot_id)
    return {row["ocpp_id"]: row["charger_id"] for row in rows}
