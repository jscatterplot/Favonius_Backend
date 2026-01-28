"""Vehicle ID resolver for VDV 463 integration.

Maps external vehicleId (from transit systems) to internal vehicle_id UUIDs.
"""

"""Vehicle ID resolver for VDV 463 integration.

Maps external vehicleId (from transit systems) to internal vehicle_id UUIDs.
"""

from typing import Optional
from uuid import UUID
import structlog


def get_logger(name: str) -> structlog.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)


class VehicleResolver:
    """Resolves external vehicle IDs to internal vehicle UUIDs.
    
    In Sprint 1, this is a stub implementation. Full database lookup
    will be implemented in Sprint 2.
    """
    
    def __init__(self):
        """Initialize vehicle resolver."""
        self.logger = get_logger(__name__)
        # In Sprint 1: simple in-memory mapping for testing
        # In Sprint 2: will query Supabase/TimescaleDB
        self._cache: dict[str, str] = {}
    
    async def resolve_vehicle_id(
        self, 
        vehicle_external_id: str,
        depot_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Resolve external vehicle ID to internal vehicle_id UUID.
        
        Args:
            vehicle_external_id: External vehicle identifier from VDV 463
            depot_id: Optional depot ID for scoping lookup
        
        Returns:
            Internal vehicle_id UUID string, or None if not found
        """
        # Sprint 1: Return cached value or None
        # In Sprint 2: Query database for vehicle mapping
        vehicle_id = self._cache.get(vehicle_external_id)
        
        if vehicle_id:
            self.logger.debug(
                f"Resolved vehicle {vehicle_external_id} -> {vehicle_id}",
                vehicle_external_id=vehicle_external_id,
                vehicle_id=vehicle_id,
            )
            return vehicle_id
        
        # Sprint 1: Log warning but don't fail
        self.logger.warning(
            f"Vehicle ID not found: {vehicle_external_id}",
            vehicle_external_id=vehicle_external_id,
            depot_id=depot_id,
        )
        return None
    
    def add_mapping(self, vehicle_external_id: str, vehicle_id: str) -> None:
        """
        Add a vehicle ID mapping (for testing/development).
        
        Args:
            vehicle_external_id: External vehicle identifier
            vehicle_id: Internal vehicle UUID
        """
        self._cache[vehicle_external_id] = vehicle_id
        self.logger.debug(
            f"Added vehicle mapping: {vehicle_external_id} -> {vehicle_id}",
            vehicle_external_id=vehicle_external_id,
            vehicle_id=vehicle_id,
        )
