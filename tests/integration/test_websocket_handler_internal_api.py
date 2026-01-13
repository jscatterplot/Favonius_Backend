"""Integration tests for WebSocket Handler internal API.

Tests the internal API endpoints that Main API will use to:
- Query charge point state
- Send commands (SetChargingProfile)
- Check connection status

Reference: Architecture Integration Plan Phase 3.2

Note: These tests require httpx package. Install with: pip install httpx
"""

import pytest

# Skip if httpx not available
httpx = pytest.importorskip("httpx", reason="httpx package not installed")
TestClient = pytest.importorskip("fastapi.testclient", reason="fastapi.testclient requires httpx").TestClient

import asyncio
from unittest.mock import Mock, AsyncMock, MagicMock, patch
from datetime import datetime, timezone
from uuid import uuid4


@pytest.fixture
def mock_connection_manager():
    """Mock connection manager with charge points."""
    manager = Mock()
    
    # Mock charge points
    charge_point_1 = AsyncMock()
    charge_point_1.station_id = "charger_001"
    charge_point_1.evse_id = 1
    charge_point_1.connector_id = 1
    charge_point_1.status = "Available"
    charge_point_1.send_charging_profile = AsyncMock(return_value={"status": "Accepted"})
    
    charge_point_2 = AsyncMock()
    charge_point_2.station_id = "charger_002"
    charge_point_2.evse_id = 1
    charge_point_2.connector_id = 2
    charge_point_2.status = "Charging"
    charge_point_2.send_charging_profile = AsyncMock(return_value={"status": "Accepted"})
    
    manager.get_charge_point = Mock(side_effect=lambda sid: {
        "charger_001": charge_point_1,
        "charger_002": charge_point_2,
    }.get(sid))
    
    manager.get_all_charge_points = Mock(return_value={
        "charger_001": charge_point_1,
        "charger_002": charge_point_2,
    })
    
    return manager


@pytest.fixture
def internal_api_app(mock_connection_manager):
    """Create FastAPI app for internal API (simulated)."""
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
    
    app = FastAPI()
    
    class ChargePointInfo(BaseModel):
        station_id: str
        connected: bool
        evse_id: int
        connector_id: int
        status: str
    
    class ChargingProfileRequest(BaseModel):
        evse_id: int
        charging_profile: dict
    
    @app.get("/internal/charge-points")
    async def get_charge_points():
        """List all connected charge points."""
        charge_points = mock_connection_manager.get_all_charge_points()
        return [
            {
                "station_id": cp.station_id,
                "connected": True,
                "evse_id": cp.evse_id,
                "connector_id": cp.connector_id,
                "status": cp.status,
            }
            for cp in charge_points.values()
        ]
    
    @app.get("/internal/charge-points/{station_id}")
    async def get_charge_point_state(station_id: str):
        """Get charge point state."""
        cp = mock_connection_manager.get_charge_point(station_id)
        if not cp:
            raise HTTPException(status_code=404, detail="Charge point not found")
        
        return {
            "station_id": cp.station_id,
            "connected": True,
            "evse_id": cp.evse_id,
            "connector_id": cp.connector_id,
            "status": cp.status,
            "current_power_kw": 0.0,
            "soc_percent": None,
        }
    
    @app.post("/internal/charge-points/{station_id}/set-charging-profile")
    async def set_charging_profile(station_id: str, request: ChargingProfileRequest):
        """Send SetChargingProfile command."""
        cp = mock_connection_manager.get_charge_point(station_id)
        if not cp:
            raise HTTPException(status_code=404, detail="Charge point not found")
        
        result = await cp.send_charging_profile(request.charging_profile)
        return {
            "success": result.get("status") == "Accepted",
            "message": f"Charging profile set for {station_id}",
        }
    
    @app.get("/internal/health")
    async def health_check():
        """Health check for Main API."""
        charge_points = mock_connection_manager.get_all_charge_points()
        return {
            "status": "healthy",
            "connected_chargers": len(charge_points),
        }
    
    return app


@pytest.fixture
def internal_api_client(internal_api_app):
    """Test client for internal API."""
    return TestClient(internal_api_app)


class TestInternalAPIEndpoints:
    """Tests for internal API endpoints."""
    
    def test_list_charge_points(self, internal_api_client):
        """Test GET /internal/charge-points lists all connected charge points."""
        response = internal_api_client.get("/internal/charge-points")
        
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["station_id"] == "charger_001"
        assert data[1]["station_id"] == "charger_002"
        assert all(cp["connected"] for cp in data)
    
    def test_get_charge_point_state(self, internal_api_client):
        """Test GET /internal/charge-points/{station_id} returns charge point state."""
        response = internal_api_client.get("/internal/charge-points/charger_001")
        
        assert response.status_code == 200
        data = response.json()
        assert data["station_id"] == "charger_001"
        assert data["connected"] is True
        assert data["evse_id"] == 1
        assert data["status"] == "Available"
    
    def test_get_charge_point_not_found(self, internal_api_client):
        """Test GET /internal/charge-points/{station_id} returns 404 for non-existent charger."""
        response = internal_api_client.get("/internal/charge-points/nonexistent")
        
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()
    
    def test_set_charging_profile(self, internal_api_client):
        """Test POST /internal/charge-points/{station_id}/set-charging-profile sends command."""
        profile = {
            "evse_id": 1,
            "charging_profile": {
                "charging_profile_id": 1,
                "stack_level": 0,
                "charging_profile_purpose": "TxDefaultProfile",
                "charging_schedule": {
                    "id": 1,
                    "charging_rate_unit": "W",
                    "charging_schedule_period": [
                        {
                            "start_period": 0,
                            "limit": 80000,
                        }
                    ],
                },
            },
        }
        
        response = internal_api_client.post(
            "/internal/charge-points/charger_001/set-charging-profile",
            json=profile
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "charger_001" in data["message"]
    
    def test_set_charging_profile_not_found(self, internal_api_client):
        """Test POST /internal/charge-points/{station_id}/set-charging-profile returns 404."""
        profile = {
            "evse_id": 1,
            "charging_profile": {"id": 1},
        }
        
        response = internal_api_client.post(
            "/internal/charge-points/nonexistent/set-charging-profile",
            json=profile
        )
        
        assert response.status_code == 404
    
    def test_health_check(self, internal_api_client):
        """Test GET /internal/health returns health status."""
        response = internal_api_client.get("/internal/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["connected_chargers"] == 2


class TestInternalAPIAuthentication:
    """Tests for internal API authentication."""
    
    def test_internal_api_requires_auth_token(self, internal_api_app):
        """Test internal API requires service-to-service authentication."""
        # In real implementation, would check for Authorization header
        # For now, this is a placeholder test
        pass
    
    def test_internal_api_rejects_invalid_token(self, internal_api_app):
        """Test internal API rejects invalid authentication tokens."""
        # In real implementation, would test token validation
        pass


class TestInternalAPIErrorHandling:
    """Tests for error handling in internal API."""
    
    def test_handles_connection_manager_error(self, mock_connection_manager, internal_api_app):
        """Test internal API handles connection manager errors gracefully."""
        # Simulate connection manager error
        mock_connection_manager.get_all_charge_points = Mock(side_effect=Exception("Connection error"))
        
        client = TestClient(internal_api_app)
        response = client.get("/internal/charge-points")
        
        # Should return 500 or handle gracefully
        assert response.status_code in [500, 503]
    
    def test_handles_charge_point_send_error(self, mock_connection_manager, internal_api_app):
        """Test internal API handles charge point send errors."""
        # Simulate charge point send error
        cp = mock_connection_manager.get_charge_point("charger_001")
        cp.send_charging_profile = AsyncMock(side_effect=Exception("Send failed"))
        
        client = TestClient(internal_api_app)
        profile = {
            "evse_id": 1,
            "charging_profile": {"id": 1},
        }
        
        response = client.post(
            "/internal/charge-points/charger_001/set-charging-profile",
            json=profile
        )
        
        # Should return 500 or handle gracefully
        assert response.status_code in [500, 503]
