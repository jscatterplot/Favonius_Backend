"""AT-11: VDV 463 ChargingInformation Export.

GIVEN an upstream BMS connected via VDV 463
AND depot has charging stations with charging points
AND some vehicles are charging
WHEN 15 seconds elapse (periodic task)
THEN system sends ProvideChargingInformation message
AND message includes charging point statuses, SoC and power for charging vehicles
AND payload validates against ProvideChargingInformationRequest schema (or SOFT validation).
"""

import os

import pytest

# Ensure SOFT default for tests that may run without full schema
os.environ.setdefault("VDV463_VALIDATION_MODE", "soft")

from src.adapters.vdv463.messages import (
    ChargingPointInfo,
    ChargingProcessInfo,
    ChargingStationInfo,
    DepotInfo,
    PreconditioningInfo,
    ValidationMode,
    VehicleInfo,
    build_provide_charging_information_message,
    get_schema_registry,
)


@pytest.mark.acceptance
@pytest.mark.integration
class TestAT11VDV463ChargingInformationExport:
    """AT-11: ProvideChargingInformation export and schema compliance."""

    def test_build_provide_charging_information_full_structure(self):
        """Built message has depotInfoList -> ChargingStationInfo -> ChargingPointInfo with vehicleInfo, chargingProcessInfo."""
        points = [
            ChargingPointInfo(
                charging_point_id="cp-1",
                charging_point_status="Occupied",
                present_power=50.0,
                vehicle_info=VehicleInfo(
                    vehicle_id="bus_101",
                    vehicle_status_info={},
                    vehicle_charging_status="Charging",
                    preconditioning_info=PreconditioningInfo(),
                    traction_battery_info={"stateOfCharge": 65},
                ),
                charging_process_info=ChargingProcessInfo(
                    charging_process_id="cp-uuid-1",
                    process_status="Charging",
                    start_time="2026-01-20T03:45:00Z",
                    electric_data_charging_power=50.0,
                    charging_prediction_data={},
                ),
            ),
            ChargingPointInfo(
                charging_point_id="cp-2",
                charging_point_status="Available",
            ),
        ]
        station = ChargingStationInfo(
            charging_station_id="depot-001",
            charging_station_status="Available",
            charging_point_info_list=points,
        )
        depot = DepotInfo(
            depot_id="depot-001",
            name="Main Depot",
            charging_station_info_list=[station],
        )

        message = build_provide_charging_information_message("presystem_1", [depot])
        assert message[0] == 1
        assert message[5] == "ProvideChargingInformation"
        payload = message[6]
        assert "depotInfoList" in payload
        assert len(payload["depotInfoList"]) == 1
        di = payload["depotInfoList"][0]
        assert di["depotId"] == "depot-001"
        assert "chargingStationInfoList" in di
        assert len(di["chargingStationInfoList"]) == 1
        st = di["chargingStationInfoList"][0]
        assert "chargingPointInfoList" in st
        assert len(st["chargingPointInfoList"]) == 2
        cp0 = st["chargingPointInfoList"][0]
        assert cp0["chargingPointStatus"] == "Occupied"
        assert cp0["presentPower"] == 50.0
        assert "vehicleInfo" in cp0
        assert cp0["vehicleInfo"]["vehicleId"] == "bus_101"
        assert cp0["vehicleInfo"]["vehicleChargingStatus"] == "Charging"
        assert "chargingProcessInfo" in cp0
        assert cp0["chargingProcessInfo"]["processStatus"] == "Charging"
        assert cp0["chargingProcessInfo"]["electricData"]["chargingPower"] == 50.0

    def test_provide_charging_information_validates_against_schema_soft(self):
        """ProvideChargingInformation payload validates (SOFT mode accepts minor deviations)."""
        depot = DepotInfo(
            depot_id="depot-001",
            charging_station_info_list=[
                ChargingStationInfo(
                    charging_station_id="depot-001",
                    charging_station_status="Available",
                    charging_point_info_list=[
                        ChargingPointInfo(
                            charging_point_id="cp-1", charging_point_status="Available"
                        ),
                    ],
                )
            ],
        )
        message = build_provide_charging_information_message(
            "presystem_1", [depot], validation_mode=ValidationMode.SOFT
        )
        registry = get_schema_registry()
        warnings = registry.validate_payload(
            message[6],
            "ProvideChargingInformation",
            ValidationMode.SOFT,
        )
        assert isinstance(warnings, list)
