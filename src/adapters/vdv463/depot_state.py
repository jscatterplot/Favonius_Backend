"""VDV 463 depot state for ProvideChargingInformation.

Queries depot, chargers, and telemetry to build real ChargingInformation payload.
Per PRD Section 9.6: depotInfoList -> ChargingStationInfo -> ChargingPointInfo,
with optional VehicleInfo and ChargingProcessInfo (full schema compliance).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from .messages import (
    ChargingPointInfo,
    ChargingProcessInfo,
    ChargingStationInfo,
    DepotInfo,
    PreconditioningInfo,
    VehicleInfo,
)


async def get_depot_charging_info(
    pool: Any,
    depot_id: str,
    depot_name: Optional[str] = None,
    static_pool: Any = None,
) -> List[DepotInfo]:
    """
    Build depot charging information from DB for ProvideChargingInformation.

    Returns DepotInfo with charging_station_info_list (one station per depot)
    and full ChargingPointInfo (charging_point_status, present_power, vehicle_info,
    charging_process_info) per VDV 463 ProvideChargingInformationRequest schema.

    Args:
        pool: TimescaleDB pool (for telemetry queries)
        depot_id: Depot UUID string
        depot_name: Optional depot name (looked up from DB if None)
        static_pool: Optional Supabase pool for static tables (depots, chargers, vehicles).
                     Falls back to pool if not provided (single-DB legacy mode).
    """
    if not pool or not depot_id:
        return []

    # In dual-DB mode, static tables live in Supabase; in legacy mode use the ts pool.
    _static = static_pool or pool

    try:
        async with _static.acquire() as static_conn:
            if depot_name is None:
                row = await static_conn.fetchrow(
                    "SELECT name FROM depots WHERE depot_id = $1::uuid",
                    depot_id,
                )
                depot_name = row["name"] if row else str(depot_id)

            chargers = await static_conn.fetch(
                """
                SELECT charger_id, ocpp_id, status, rated_kw
                FROM chargers
                WHERE depot_id = $1::uuid
                ORDER BY ocpp_id
                """,
                depot_id,
            )

        points: List[ChargingPointInfo] = []
        for ch in chargers:
            charger_id = str(ch["charger_id"])
            ocpp_id = ch["ocpp_id"]
            cp_status = _map_charger_status(ch["status"])

            # Telemetry is in TimescaleDB; vehicle external_id is in Supabase.
            # Fetch separately and join in Python to avoid cross-pool JOIN.
            async with pool.acquire() as ts_conn:
                telem_row = await ts_conn.fetchrow(
                    """
                    SELECT charging_kw, soc, vehicle_id, time
                    FROM telemetry
                    WHERE charger_id = $1::uuid
                    ORDER BY time DESC
                    LIMIT 1
                    """,
                    ch["charger_id"],
                )

            current_power = 0.0
            vehicle_external_id = None
            soc_pct = None
            telem_time = None

            if telem_row and telem_row["charging_kw"] is not None:
                current_power = float(telem_row["charging_kw"])
                telem_time = telem_row.get("time")
                if telem_row.get("soc") is not None:
                    s = float(telem_row["soc"])
                    soc_pct = int(s * 100) if s <= 1 else int(s)
                if telem_row["vehicle_id"]:
                    async with _static.acquire() as static_conn2:
                        v_row = await static_conn2.fetchrow(
                            "SELECT external_id FROM vehicles WHERE vehicle_id = $1",
                            telem_row["vehicle_id"],
                        )
                        vehicle_external_id = (
                            (v_row["external_id"] if v_row else None)
                            or str(telem_row["vehicle_id"])
                        )

            vehicle_info: Optional[VehicleInfo] = None
            charging_process_info: Optional[ChargingProcessInfo] = None
            if vehicle_external_id:
                vehicle_charging_status = (
                    "Charging" if current_power > 0 else "ReadyToCharge"
                )
                traction_battery_info = (
                    {"stateOfCharge": soc_pct} if soc_pct is not None else None
                )
                vehicle_info = VehicleInfo(
                    vehicle_id=vehicle_external_id,
                    vehicle_status_info={},
                    vehicle_charging_status=vehicle_charging_status,
                    preconditioning_info=PreconditioningInfo(),
                    traction_battery_info=traction_battery_info,
                )
                start_time = (
                    telem_time.isoformat()
                    if telem_time
                    else datetime.utcnow().isoformat() + "Z"
                )
                charging_process_info = ChargingProcessInfo(
                    charging_process_id=f"cp-{charger_id}",
                    process_status=(
                        "Charging" if current_power > 0 else "Preparing"
                    ),
                    start_time=start_time,
                    electric_data_charging_power=current_power,
                    charging_prediction_data={},
                )

            points.append(
                ChargingPointInfo(
                    charging_point_id=ocpp_id or charger_id,
                    charging_point_status=cp_status,
                    present_power=current_power if current_power else None,
                    vehicle_info=vehicle_info,
                    charging_process_info=charging_process_info,
                )
            )

        station = ChargingStationInfo(
            charging_station_id=depot_id,
            charging_station_status=_station_status_from_points(points),
            charging_point_info_list=points,
        )
        return [
            DepotInfo(
                depot_id=depot_id,
                name=depot_name,
                charging_station_info_list=[station],
            )
        ]
    except Exception as e:
        if "does not exist" in str(e).lower() or "vdv463" in str(e).lower():
            return []
        raise


def _station_status_from_points(points: List[ChargingPointInfo]) -> str:
    """Derive ChargingStationStatus from point statuses."""
    for p in points:
        if p.charging_point_status == "Faulted":
            return "Faulted"
        if p.charging_point_status == "Unavailable":
            return "Unavailable"
    return "Available"


def _map_charger_status(db_status: Optional[str]) -> str:
    """Map DB charger status to VDV 463 ChargingPointStatus enum."""
    if not db_status:
        return "Available"
    s = str(db_status).strip().lower()
    if s in ("available", "occupied", "faulted", "unavailable", "reserved"):
        return db_status.strip()
    if s == "faulted":
        return "Faulted"
    if s == "unavailable" or s == "reserved":
        return "Unavailable"
    if s == "occupied" or s == "charging":
        return "Occupied"
    return "Available"
