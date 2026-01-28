"""Tests for vehicle resolver."""

import pytest
from adapters.vdv463.vehicle_resolver import VehicleResolver


@pytest.mark.asyncio
async def test_resolve_vehicle_id_not_found():
    """Test resolving non-existent vehicle ID."""
    resolver = VehicleResolver()
    
    vehicle_id = await resolver.resolve_vehicle_id("bus_999")
    
    assert vehicle_id is None


@pytest.mark.asyncio
async def test_resolve_vehicle_id_cached():
    """Test resolving cached vehicle ID."""
    resolver = VehicleResolver()
    resolver.add_mapping("bus_101", "vehicle-uuid-123")
    
    vehicle_id = await resolver.resolve_vehicle_id("bus_101")
    
    assert vehicle_id == "vehicle-uuid-123"


@pytest.mark.asyncio
async def test_add_mapping():
    """Test adding vehicle mapping."""
    resolver = VehicleResolver()
    
    resolver.add_mapping("bus_102", "vehicle-uuid-456")
    
    vehicle_id = await resolver.resolve_vehicle_id("bus_102")
    assert vehicle_id == "vehicle-uuid-456"
