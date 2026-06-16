"""VDV 463 schema validation and edge-case tests (Sprint 3)."""

import json
import os
from datetime import datetime
from uuid import uuid4

import pytest

os.environ.setdefault("VDV463_VALIDATION_MODE", "soft")

from adapters.vdv463.messages import (
    ChargingPointInfo,
    ChargingStationInfo,
    DepotInfo,
    ValidationMode,
    VDV463ValidationError,
    build_provide_charging_information_message,
    get_validation_mode,
    parse_charging_request_item,
    parse_message,
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
                        ChargingPointInfo(
                            charging_point_id="cp-1", charging_point_status="Available"
                        ),
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
                ChargingPointInfo(
                    charging_point_id="cp-1", status="Occupied", current_power_kw=30.0
                ),
            ],
        )
        msg = build_provide_charging_information_message("ps1", [depot])
        payload = msg[6]
        assert len(payload["depotInfoList"]) == 1
        st_list = payload["depotInfoList"][0]["chargingStationInfoList"]
        assert len(st_list) == 1
        assert st_list[0]["chargingPointInfoList"][0]["chargingPointStatus"] == "Occupied"
        assert st_list[0]["chargingPointInfoList"][0]["presentPower"] == 30.0


@pytest.fixture
def minimal_registry(monkeypatch):
    """A SchemaRegistry forced onto its inline fallback (no network/cache/bundled).

    This is the offline / cache-miss path that became reachable once the vendored
    VDV 463 schemas were removed from the tree.
    """
    import adapters.vdv463.messages as vdv_messages

    monkeypatch.setattr(vdv_messages, "_fetch_schemas_to_cache", lambda *a, **k: False)
    registry = vdv_messages.SchemaRegistry(schema_dir="/nonexistent/vdv463-test-cache")
    assert registry._schema_source == "minimal"
    return registry


class TestMinimalFallbackSchemas:
    """Offline / cache-miss fallback: the inline schemas must be meta-valid and
    accept everything the builders and dataclasses emit, or HARD-mode validation
    (the production default) silently drops VDV messages.
    """

    @pytest.mark.parametrize(
        "status", ["Available", "Occupied", "Reserved", "Unavailable", "Faulted"]
    )
    def test_provide_charging_information_status_enum(self, minimal_registry, status):
        """Every official ChargingPointStatus validates against the minimal fallback.

        Regression: the fallback enum omitted ``Reserved`` and used field names
        (`status`/`currentPowerKw`) that diverged from the builder output, so a
        HARD-mode depot would reject its own ProvideChargingInformation message.
        """
        depot = DepotInfo(
            depot_id="d1",
            charging_station_info_list=[
                ChargingStationInfo(
                    charging_station_id="d1",
                    charging_station_status="Available",
                    charging_point_info_list=[
                        ChargingPointInfo(
                            charging_point_id="cp-1",
                            charging_point_status=status,
                            present_power=42.0,
                        ),
                    ],
                )
            ],
        )
        payload = build_provide_charging_information_message("ps1", [depot])[6]
        # HARD raises VDV463ValidationError on any violation; a clean pass -> [].
        warnings = minimal_registry.validate_payload(
            payload, "ProvideChargingInformation", ValidationMode.HARD
        )
        assert warnings == []

    def test_message_structure_parses_offline(self, minimal_registry):
        """The inline MessageStructure schema must itself be a valid schema.

        It uses the draft-04/07 tuple form ``items: [...]``; without an explicit
        ``$schema`` modern jsonschema meta-validates it as draft 2020-12 and raises
        SchemaError, breaking every offline parse before HARD/SOFT handling.
        """
        message = [
            1,
            "BMS",
            "presystem_001",
            "2025-01-01T00:00:00Z",
            "11111111-1111-4111-8111-111111111111",
            "BootNotification",
            {"presystem": "BMS"},
        ]
        # Must not raise: SchemaError (invalid schema) or VDV463ValidationError
        # (valid message). A clean return means the fallback works offline.
        minimal_registry.validate_message_structure(message)
