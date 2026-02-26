"""Integration tests for VDV 463 adapter."""

import json
from datetime import datetime

import pytest

from adapters.vdv463.messages import (
    ValidationMode,
    build_provide_charging_requests_response,
    parse_message,
)


class TestIntegration:
    """Integration tests for VDV 463 message flow."""

    def test_end_to_end_provide_charging_requests(self):
        """Test end-to-end ProvideChargingRequests flow."""
        # Simulate incoming message from BMS/ITCS
        incoming_message = [
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
                        "chargingPointId": "cp_001",
                        "chargingRequestData": {
                            "expectedArrivalTimeAtChargingPoint": "2025-01-28T10:00:00Z",
                            "requestedTimeForDeparture": "2025-01-28T14:00:00Z",
                            "minTargetSoc": 20,
                            "maxTargetSoc": 95,
                        },
                        "chargingPriority": 1,
                    },
                    {
                        "vehicleId": "bus_102",
                        "chargingRequestId": "cr-002",
                        "chargingRequestData": {
                            "expectedArrivalTimeAtChargingPoint": "2025-01-28T11:00:00Z",
                            "requestedTimeForDeparture": "2025-01-28T15:00:00Z",
                            "minTargetSoc": 30,
                            "maxTargetSoc": 98,
                        },
                    },
                ]
            },
        ]

        # Parse message
        envelope = parse_message(json.dumps(incoming_message), ValidationMode.HARD)

        assert envelope.message_type == 1
        assert envelope.source == "BMS"
        assert envelope.message_action == "ProvideChargingRequests"
        assert envelope.validation_status == "ok"

        # Build response
        response = build_provide_charging_requests_response(envelope)

        assert response[0] == 2  # Confirmation
        assert response[1] == "CMS"
        assert response[5] == "ProvideChargingRequests"
        assert response[6] == {}  # Empty payload per spec

        # Verify response can be serialized
        response_json = json.dumps(response)
        assert isinstance(response_json, str)
        assert len(response_json) > 0

    def test_error_handling_flow(self):
        """Test error handling flow."""
        # Invalid message (missing required fields)
        invalid_message = [
            1,
            "BMS",
            "presystem_001",
            datetime.utcnow().isoformat() + "Z",
            "msg-123",
            "ProvideChargingRequests",
            {
                "chargingRequestList": [
                    {
                        # Missing vehicleId, chargingRequestId, chargingRequestData
                        "chargingRequestData": {"minTargetSoc": 20, "maxTargetSoc": 95},
                    }
                ]
            },
        ]

        # In HARD mode, should raise exception
        with pytest.raises(Exception):  # VDV463ValidationError or similar
            parse_message(json.dumps(invalid_message), ValidationMode.HARD)

        # In SOFT mode, should return with warnings
        envelope = parse_message(json.dumps(invalid_message), ValidationMode.SOFT)
        assert envelope.validation_status == "warning"
        assert len(envelope.validation_warnings) > 0
