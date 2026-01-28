"""Integration tests for VDV 463 adapter."""

import pytest
import json
from datetime import datetime
from adapters.vdv463.messages import (
    parse_message,
    build_provide_charging_requests_response,
    ValidationMode,
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
                        "chargingPointId": "cp_001",
                        "arrivalTime": "2025-01-28T10:00:00Z",
                        "departureTime": "2025-01-28T14:00:00Z",
                        "minTargetSoc": 0.2,
                        "maxTargetSoc": 0.95,
                        "chargingPriority": 1,
                    },
                    {
                        "vehicleId": "bus_102",
                        "arrivalTime": "2025-01-28T11:00:00Z",
                        "departureTime": "2025-01-28T15:00:00Z",
                        "minTargetSoc": 0.3,
                        "maxTargetSoc": 0.98,
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
                        # Missing vehicleId, arrivalTime, departureTime
                        "minTargetSoc": 0.2,
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
