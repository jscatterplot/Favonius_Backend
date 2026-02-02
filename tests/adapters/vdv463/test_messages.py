"""Tests for VDV 463 message parsing and validation."""

import json
import pytest
from datetime import datetime
from adapters.vdv463.messages import (
    ValidationMode,
    VDVMessageEnvelope,
    ChargingRequest,
    parse_message,
    parse_charging_request_item,
    build_error,
    build_provide_charging_requests_response,
    build_provide_charging_information_message,
    VDV463ValidationError,
    DepotInfo,
    ChargingPointInfo,
)


class TestParseMessage:
    """Test message parsing."""
    
    def test_parse_valid_provide_charging_requests(self):
        """Test parsing valid ProvideChargingRequests message (chargingRequestData structure)."""
        message = [
            1,  # Request
            "BMS",
            "presystem_001",
            datetime.utcnow().isoformat() + "Z",
            "msg-123",
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
                            "expectedSocAtArrival": 25,
                        },
                        "chargingPriority": 1,
                    }
                ]
            },
        ]

        envelope = parse_message(json.dumps(message), ValidationMode.HARD)

        assert envelope.message_type == 1
        assert envelope.source == "BMS"
        assert envelope.presystem_id == "presystem_001"
        assert envelope.message_action == "ProvideChargingRequests"
        assert envelope.validation_status == "ok"
    
    def test_parse_invalid_json(self):
        """Test parsing invalid JSON."""
        with pytest.raises(VDV463ValidationError) as exc_info:
            parse_message("invalid json", ValidationMode.HARD)
        
        assert exc_info.value.error_code == "InvalidJSON"
    
    def test_parse_invalid_structure(self):
        """Test parsing invalid message structure."""
        message = [1, "BMS"]  # Too short
        
        with pytest.raises(VDV463ValidationError) as exc_info:
            parse_message(json.dumps(message), ValidationMode.HARD)
        
        assert "InvalidMessageStructure" in exc_info.value.error_code or "SchemaValidationError" in exc_info.value.error_code
    
    def test_parse_soft_mode_with_warnings(self):
        """Test parsing in SOFT mode with validation warnings."""
        message = [
            1,
            "BMS",
            "presystem_001",
            datetime.utcnow().isoformat() + "Z",
            "msg-123",
            "ProvideChargingRequests",
            {
                "chargingRequestList": [
                    {
                        "vehicleId": "bus_101",
                        # Missing required fields
                    }
                ]
            },
        ]
        
        envelope = parse_message(json.dumps(message), ValidationMode.SOFT)
        
        assert envelope.validation_status == "warning"
        assert len(envelope.validation_warnings) > 0


class TestBuildError:
    """Test error message building."""
    
    def test_build_error_message(self):
        """Test building VDV 463 error message."""
        envelope = VDVMessageEnvelope(
            message_type=1,
            source="BMS",
            presystem_id="presystem_001",
            timestamp=datetime.utcnow().isoformat() + "Z",
            message_id="msg-123",
            message_action="ProvideChargingRequests",
        )
        
        error_msg = build_error(
            envelope,
            "SchemaValidationError",
            "Invalid payload structure",
            {"field": "chargingRequestList"},
        )
        
        assert error_msg[0] == 3  # Error type
        assert error_msg[1] == "CMS"
        assert error_msg[2] == "presystem_001"
        assert error_msg[5] == "ProvideChargingRequests"
        assert error_msg[6]["errorCode"] == "SchemaValidationError"
        assert error_msg[6]["errorDescription"] == "Invalid payload structure"


class TestBuildResponse:
    """Test response message building."""
    
    def test_build_provide_charging_requests_response(self):
        """Test building ProvideChargingRequestsResponse."""
        envelope = VDVMessageEnvelope(
            message_type=1,
            source="BMS",
            presystem_id="presystem_001",
            timestamp=datetime.utcnow().isoformat() + "Z",
            message_id="msg-123",
            message_action="ProvideChargingRequests",
        )
        
        response = build_provide_charging_requests_response(envelope)
        
        assert response[0] == 2  # Confirmation
        assert response[1] == "CMS"
        assert response[2] == "presystem_001"
        assert response[5] == "ProvideChargingRequests"
        assert response[6] == {}  # Empty payload per spec
    
    def test_build_provide_charging_information(self):
        """Test building ProvideChargingInformation message."""
        depot_info = DepotInfo(
            depot_id="depot_001",
            charging_stations=[
                ChargingPointInfo(
                    charging_point_id="cp_001",
                    status="Available",
                    current_power_kw=0.0,
                ),
                ChargingPointInfo(
                    charging_point_id="cp_002",
                    status="Occupied",
                    current_power_kw=50.0,
                    vehicle_id="bus_101",
                ),
            ],
        )
        
        message = build_provide_charging_information_message(
            "presystem_001",
            [depot_info],
        )
        
        assert message[0] == 1  # Request (CMS → BMS/ITCS)
        assert message[1] == "CMS"
        assert message[2] == "presystem_001"
        assert message[5] == "ProvideChargingInformation"
        
        payload = message[6]
        assert "depotInfoList" in payload
        assert len(payload["depotInfoList"]) == 1
        assert payload["depotInfoList"][0]["depotId"] == "depot_001"


class TestChargingRequest:
    """Test ChargingRequest dataclass."""

    def test_charging_request_creation(self):
        """Test creating ChargingRequest."""
        request = ChargingRequest(
            charging_request_id="cr-001",
            vehicle_external_id="bus_101",
            charging_point_id="cp_001",
            arrival_time="2025-01-28T10:00:00Z",
            departure_time="2025-01-28T14:00:00Z",
            min_target_soc=0.2,
            max_target_soc=0.95,
            priority=1,
        )

        assert request.charging_request_id == "cr-001"
        assert request.vehicle_external_id == "bus_101"
        assert request.min_target_soc == 0.2
        assert request.max_target_soc == 0.95
        assert request.priority == 1

    def test_parse_charging_request_item_charging_request_data(self):
        """Test parse_charging_request_item with official chargingRequestData structure."""
        req_data = {
            "vehicleId": "bus_101",
            "chargingRequestId": "cr-001",
            "chargingPointId": "cp-uuid",
            "chargingRequestData": {
                "expectedArrivalTimeAtChargingPoint": "2026-01-19T22:00:00Z",
                "requestedTimeForDeparture": "2026-01-20T05:30:00Z",
                "minTargetSoc": 90,
                "maxTargetSoc": 100,
                "expectedSocAtArrival": 22,
            },
            "priority": 1,
            "chargingInstruction": "Normal",
        }
        req = parse_charging_request_item(req_data, "ok")
        assert req.charging_request_id == "cr-001"
        assert req.vehicle_external_id == "bus_101"
        assert req.arrival_time == "2026-01-19T22:00:00Z"
        assert req.departure_time == "2026-01-20T05:30:00Z"
        # SoC 0-100 normalized to 0-1
        assert req.min_target_soc == 0.9
        assert req.max_target_soc == 1.0
        assert req.expected_soc_at_arrival == 0.22
        assert req.priority == 1
        assert req.charging_instruction == "Normal"
