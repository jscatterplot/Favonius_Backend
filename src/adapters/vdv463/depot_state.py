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
) -> List[DepotInfo]:
    """
    Build depot charging information from DB for ProvideChargingInformation.

    Returns DepotInfo with charging_station_info_list (one station per depot)
    and full ChargingPointInfo (charging_point_status, present_power, vehicle_info,
    charging_process_info) per VDV 463 ProvideChargingInformationRequest schema.
    """
    if not pool or not depot_id:
        return []

    try:
        async with pool.acquire() as conn:
            if depot_name is None:
                row = await conn.fetchrow(
                    "SELECT name FROM depots WHERE depot_id = $1::uuid",
                    depot_id,
                )
                depot_name = row["name"] if row else str(depot_id)

            chargers = await conn.fetch(
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
                telem = await conn.fetchrow(
                    """
                    SELECT t.charging_kw, t.soc, t.vehicle_id, t.time, v.external_id
                    FROM telemetry t
                    LEFT JOIN vehicles v ON v.vehicle_id = t.vehicle_id
                    WHERE t.charger_id = $1::uuid
                    ORDER BY t.time DESC
                    LIMIT 1
                    """,
                    ch["charger_id"],
                )
                current_power = 0.0
                vehicle_external_id = None
                soc_pct = None
                telem_time = None
                if telem and telem["charging_kw"] is not None:
                    current_power = float(telem["charging_kw"])
                    vehicle_external_id = telem["external_id"] or (
                        str(telem["vehicle_id"]) if telem["vehicle_id"] else None
                    )
                    if telem.get("soc") is not None:
                        s = float(telem["soc"])
                        soc_pct = int(s * 100) if s <= 1 else int(s)
                    telem_time = telem.get("time")

                vehicle_info: Optional[VehicleInfo] = None
                charging_process_info: Optional[ChargingProcessInfo] = None
                if vehicle_external_id:
                    vehicle_charging_status = (
                        "Charging" if current_power and current_power > 0 else "ReadyToCharge"
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
                            "Charging" if current_power and current_power > 0 else "Preparing"
                        ),
                        start_time=start_time,
                        electric_data_charging_power=current_power or 0.0,
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
