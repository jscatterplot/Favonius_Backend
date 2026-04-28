"""Database query helpers with parameterized queries.

SECURITY: All queries use parameterized statements to prevent SQL injection.
See PRD_v2.md Section 10.4.

Reference: Development Plan Step 4.5.7
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify_identifier(value: str, *, fallback: str = "item") -> str:
    """Return a lowercase URL-safe slug for generated external identifiers."""
    slug = _SLUG_RE.sub("-", value.lower()).strip("-")
    return slug or fallback


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
        SELECT schedule_id, route_id, departure_time, return_time,
               actual_return_time, energy_kwh, required_soc, dest_depot_id
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
        SELECT s.schedule_id, s.vehicle_id, v.external_id,
               s.route_id, s.departure_time, s.return_time,
               s.actual_return_time, s.energy_kwh, s.required_soc, s.dest_depot_id
        FROM schedules s
        JOIN vehicles v ON s.vehicle_id = v.vehicle_id
        WHERE v.depot_id = $1
          AND s.departure_time >= $2
          AND s.departure_time <= $3
        ORDER BY s.departure_time ASC
    """
    return await db.fetch(query, depot_id, start_time, end_time)


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
        SELECT vehicle_id, depot_id, external_id, vehicle_type,
               battery_kwh, max_charge_kw, id_tag
        FROM vehicles
        WHERE id_tag = $1
    """
    return await db.fetchrow(query, id_tag)


async def get_depot_by_id(db, depot_id: str) -> Optional[dict]:
    """Get depot metadata by depot_id.

    Args:
        db: Database connection or pool
        depot_id: Depot UUID string

    Returns:
        Dict with depot fields, or None if not found
    """
    query = """
        SELECT depot_id::text AS depot_id,
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
        FROM depots
        WHERE depot_id = $1::uuid
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
        SELECT depot_id::text AS depot_id,
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
        FROM depots
        WHERE depot_id = ANY($1::uuid[])
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
        SELECT depot_id::text AS depot_id,
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
        FROM depots
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
        SELECT depot_id::text AS depot_id,
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
        FROM depots
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


async def depot_belongs_to_organization(
    db, depot_id: str, organization_id: str
) -> bool:
    """Return True if the depot exists and is assigned to the organization."""
    q = """
        SELECT EXISTS(
            SELECT 1 FROM depots
            WHERE depot_id = $1::uuid AND organization_id = $2::uuid
        )
    """
    return bool(await db.fetchval(q, depot_id, organization_id))


async def get_depot_org_slug_context(db, *, depot_id: str, organization_id: str) -> Optional[dict]:
    """Return organization/depot names for an organization-owned depot."""
    query = """
        SELECT d.depot_id::text AS depot_id,
               d.name AS depot_name,
               d.organization_id::text AS organization_id,
               o.name AS organization_name
        FROM depots d
        JOIN organizations o ON o.organization_id = d.organization_id
        WHERE d.depot_id = $1::uuid AND d.organization_id = $2::uuid
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
    prefix = f"{organization_slug}-{depot_slug}"
    query = """
        SELECT ocpp_id
        FROM chargers
        WHERE ocpp_id ~ ('^' || $1 || '-[0-9]{3}$')
        ORDER BY ocpp_id DESC
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
        INSERT INTO chargers (
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
            connector_ids,
            network_notes,
            auth_required
        )
        VALUES (
            $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12, TRUE
        )
        RETURNING charger_id::text AS id,
                  depot_id::text AS depot_id,
                  ocpp_id,
                  display_name,
                  vendor,
                  model,
                  serial_number,
                  firmware,
                  rated_kw,
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
    async with db.transaction():
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
            NOW() + ($8::text || ' minutes')::interval
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
) -> dict:
    """Create a depot row scoped to organization and return metadata."""
    query = """
        INSERT INTO depots (
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
            building_load_source
        )
        VALUES (
            $1::uuid, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12::jsonb, $13::jsonb
        )
        RETURNING depot_id::text AS depot_id,
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
) -> Optional[dict]:
    """Update depot setup metadata and return updated row."""
    query = """
        UPDATE depots
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
            updated_at = NOW()
        WHERE depot_id = $1::uuid
        RETURNING depot_id::text AS depot_id,
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
    )
    if not row:
        return None
    result = dict(row)
    result["address"] = _coerce_jsonb_dict(result.get("address"))
    result["billing_metadata"] = _coerce_jsonb_dict(result.get("billing_metadata"))
    result["building_load_source"] = _coerce_jsonb_dict(result.get("building_load_source"))
    return result


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
        INSERT INTO battery_storage (depot_id, capacity_kwh, max_power_kw, soc_min, soc_max)
        VALUES ($1::uuid, $2, $3, $4, $5)
        ON CONFLICT (depot_id) DO UPDATE
        SET capacity_kwh = EXCLUDED.capacity_kwh,
            max_power_kw = EXCLUDED.max_power_kw,
            soc_min = EXCLUDED.soc_min,
            soc_max = EXCLUDED.soc_max
    """
    await db.execute(query, depot_id, capacity_kwh, max_power_kw, soc_min, soc_max)


async def delete_battery_storage(db, *, depot_id: str) -> None:
    """Delete battery config for depot."""
    await db.execute("DELETE FROM battery_storage WHERE depot_id = $1::uuid", depot_id)


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
        SET max_charge_kw = $2
        WHERE vehicle_id = $1
    """
    result = await db.execute(query, vehicle_id, max_charge_kw)
    return result != "UPDATE 0"
