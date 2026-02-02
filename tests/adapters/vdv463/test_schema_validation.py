"""VDV 463 schema validation and edge-case tests (Sprint 3)."""

import json
import os
import pytest
from datetime import datetime
from uuid import uuid4

os.environ.setdefault("VDV463_VALIDATION_MODE", "soft")

from adapters.vdv463.messages import (
    ValidationMode,
    parse_message,
    parse_charging_request_item,
    get_schema_registry,
    get_validation_mode,
    VDV463ValidationError,
    build_provide_charging_information_message,
    DepotInfo,
    ChargingStationInfo,
    ChargingPointInfo,
)


def _valid_message_id():
    return str(uuid4())


class TestSchemaValidation:
    """Schema validation against official or minimal schemas."""

    def test_validation_mode_default_soft(self):
        """Default validation mode is SOFT (user-configurable)."""
        mode = get_validation_mode()
        assert mode in (ValidationMode.SOFT, ValidationMode.HARD)
        try:
            os.environ["VDV463_VALIDATION_MODE"] = "soft"
            assert get_validation_mode() == ValidationMode.SOFT
            os.environ["VDV463_VALIDATION_MODE"] = "hard"
            assert get_validation_mode() == ValidationMode.HARD
        finally:
            os.environ["VDV463_VALIDATION_MODE"] = "soft"

    def test_valid_boot_notification_parses(self):
        """Valid BootNotification message parses."""
        msg = [
            1,
            "BMS",
            "presystem_001",
            datetime.utcnow().isoformat() + "Z",
            _valid_message_id(),
            "BootNotification",
            {"presystem": "BMS"},
        ]
        envelope = parse_message(json.dumps(msg), ValidationMode.SOFT)
        assert envelope.message_action == "BootNotification"
        assert envelope.payload.get("presystem") == "BMS"

    def test_valid_provide_charging_requests_with_uuid_parses(self):
        """Valid ProvideChargingRequests with UUID messageId parses."""
        msg = [
            1,
            "BMS",
            "presystem_001",
            datetime.utcnow().isoformat() + "Z",
            _valid_message_id(),
            "ProvideChargingRequests",
            {
                "chargingRequestList": [
                    {
                        "vehicleId": "bus_101",
                        "chargingRequestId": "cr-001",
                        "chargingRequestData": {
                            "expectedArrivalTimeAtChargingPoint": "2025-01-28T10:00:00Z",
                            "requestedTimeForDeparture": "2025-01-28T14:00:00Z",
                            "minTargetSoc": 20,
                            "maxTargetSoc": 95,
                        },
                    }
                ]
            },
        ]
        envelope = parse_message(json.dumps(msg), ValidationMode.SOFT)
        assert envelope.message_action == "ProvideChargingRequests"
        assert len(envelope.payload.get("chargingRequestList", [])) == 1

    def test_invalid_message_structure_short_array(self):
        """Too-short message array raises or warns per mode."""
        msg = [1, "BMS"]
        with pytest.raises(VDV463ValidationError):
            parse_message(json.dumps(msg), ValidationMode.HARD)
        envelope = parse_message(json.dumps(msg), ValidationMode.SOFT)
        assert envelope.validation_status == "warning" or envelope.validation_warnings

    def test_invalid_action_unknown_enum(self):
        """Unknown message action triggers structure validation (SOFT may warn)."""
        msg = [
            1,
            "BMS",
            "presystem_001",
            datetime.utcnow().isoformat() + "Z",
            _valid_message_id(),
            "UnknownAction",
            {},
        ]
        with pytest.raises(VDV463ValidationError):
            parse_message(json.dumps(msg), ValidationMode.HARD)
        envelope = parse_message(json.dumps(msg), ValidationMode.SOFT)
        assert envelope.message_action == "UnknownAction"


class TestEdgeCases:
    """Edge cases: empty list, optional chargingPointId, duplicate ID."""

    def test_empty_charging_request_list_parses(self):
        """Empty chargingRequestList parses (delete-all for presystem)."""
        msg = [
            1,
            "BMS",
            "presystem_001",
            datetime.utcnow().isoformat() + "Z",
            _valid_message_id(),
            "ProvideChargingRequests",
            {"chargingRequestList": []},
        ]
        envelope = parse_message(json.dumps(msg), ValidationMode.SOFT)
        assert envelope.payload["chargingRequestList"] == []

    def test_optional_charging_point_id_in_item(self):
        """ChargingRequest without chargingPointId parses (optional)."""
        req = {
            "vehicleId": "bus_101",
            "chargingRequestId": "cr-002",
            "chargingRequestData": {
                "minTargetSoc": 50,
                "maxTargetSoc": 100,
            },
        }
        parsed = parse_charging_request_item(req, "ok")
        assert parsed.charging_request_id == "cr-002"
        assert parsed.vehicle_external_id == "bus_101"
        assert parsed.charging_point_id is None

    def test_duplicate_charging_request_id_detection_in_handler(self):
        """Duplicate chargingRequestId in same message is a handler-level error (DuplicateRequestId).
        Parsing allows duplicate IDs; handler must detect and return error.
        """
        msg = [
            1,
            "BMS",
            "presystem_001",
            datetime.utcnow().isoformat() + "Z",
            _valid_message_id(),
            "ProvideChargingRequests",
            {
                "chargingRequestList": [
                    {
                        "vehicleId": "bus_101",
                        "chargingRequestId": "cr-same",
                        "chargingRequestData": {"minTargetSoc": 50, "maxTargetSoc": 100},
                    },
                    {
                        "vehicleId": "bus_102",
                        "chargingRequestId": "cr-same",
                        "chargingRequestData": {"minTargetSoc": 50, "maxTargetSoc": 100},
                    },
                ]
            },
        ]
        envelope = parse_message(json.dumps(msg), ValidationMode.SOFT)
        list_ids = [r.get("chargingRequestId") for r in envelope.payload["chargingRequestList"]]
        assert list_ids.count("cr-same") == 2


class TestProvideChargingInformationSchema:
    """ProvideChargingInformation output structure and validation."""

    def test_build_with_charging_station_info_list(self):
        """Builder uses charging_station_info_list (full schema)."""
        depot = DepotInfo(
            depot_id="d1",
            charging_station_info_list=[
                ChargingStationInfo(
                    charging_station_id="d1",
                    charging_station_status="Available",
                    charging_point_info_list=[
                        ChargingPointInfo(charging_point_id="cp-1", charging_point_status="Available"),
                    ],
                )
            ],
        )
        msg = build_provide_charging_information_message("ps1", [depot])
        assert msg[5] == "ProvideChargingInformation"
        payload = msg[6]
        assert len(payload["depotInfoList"]) == 1
        assert len(payload["depotInfoList"][0]["chargingStationInfoList"]) == 1
        st0 = payload["depotInfoList"][0]["chargingStationInfoList"][0]
        assert "chargingPointInfoList" in st0 and len(st0["chargingPointInfoList"]) == 1
        assert st0["chargingPointInfoList"][0]["chargingPointStatus"] == "Available"

    def test_build_legacy_charging_stations(self):
        """Builder accepts legacy DepotInfo with charging_stations."""
        depot = DepotInfo(
            depot_id="d1",
            charging_stations=[
                ChargingPointInfo(charging_point_id="cp-1", status="Occupied", current_power_kw=30.0),
            ],
        )
        msg = build_provide_charging_information_message("ps1", [depot])
        payload = msg[6]
        assert len(payload["depotInfoList"]) == 1
        st_list = payload["depotInfoList"][0]["chargingStationInfoList"]
        assert len(st_list) == 1
        assert st_list[0]["chargingPointInfoList"][0]["chargingPointStatus"] == "Occupied"
        assert st_list[0]["chargingPointInfoList"][0]["presentPower"] == 30.0
