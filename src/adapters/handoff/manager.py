"""Inter-depot vehicle handoff management.

Implements PRD_v2.md Section 5.4 for inter-depot coordination.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import UUID

# Make httpx import optional to prevent import errors when not installed
try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    httpx = None
    HAS_HTTPX = False

from ...core.models import IncomingVehicle

logger = logging.getLogger(__name__)


@dataclass
class HandoffMessage:
    """Message sent when vehicle departs for another depot."""

    message_id: UUID
    origin_depot_id: UUID
    dest_depot_id: UUID
    vehicle_id: UUID
    external_id: str
    expected_soc: float
    arrival_time: datetime
    battery_kwh: float
    max_charge_kw: float


class HandoffManager:
    """Manages inter-depot vehicle handoffs.

    Per PRD Section 5.4, handles HTTP communication between depots
    for vehicle handoff coordination.

    Example:
        ```python
        from src.adapters.handoff import HandoffManager
        from uuid import UUID

        # Initialize with depot endpoint mapping
        depot_endpoints = {
            UUID('depot-a-id'): 'http://depot-a:8000',
            UUID('depot-b-id'): 'http://depot-b:8000',
        }
        manager = HandoffManager(depot_endpoints)

        # Send handoff
        message_id = await manager.send_handoff(
            origin_depot_id=UUID('depot-a-id'),
            dest_depot_id=UUID('depot-b-id'),
            vehicle_id=UUID('vehicle-123'),
            external_id='bus_101',
            expected_soc=0.35,
            arrival_time=datetime.utcnow() + timedelta(hours=2),
            battery_kwh=324.0,
            max_charge_kw=150.0,
        )
        ```
    """

    def __init__(self, depot_endpoints: dict[UUID, str]):
        """Initialize with mapping of depot_id -> API endpoint URL.

        Args:
            depot_endpoints: Dictionary mapping depot_id to API endpoint URL
        
        Raises:
            ImportError: If httpx is not installed (required for handoff functionality)
        """
        if not HAS_HTTPX:
            raise ImportError(
                "httpx package is required for HandoffManager. "
                "Install with: pip install httpx"
            )
        self.depot_endpoints = depot_endpoints
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client.
        
        Raises:
            ImportError: If httpx is not installed
        """
        if not HAS_HTTPX:
            raise ImportError("httpx package is required for HTTP client")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def send_handoff(
        self,
        origin_depot_id: UUID,
        dest_depot_id: UUID,
        vehicle_id: UUID,
        external_id: str,
        expected_soc: float,
        arrival_time: datetime,
        battery_kwh: float,
        max_charge_kw: float,
    ) -> Optional[UUID]:
        """Send handoff message to destination depot.

        Per PRD Section 5.4, this method:
        1. Calls destination depot's receive endpoint
        2. Returns message_id if successful, None otherwise

        Args:
            origin_depot_id: Origin depot identifier
            dest_depot_id: Destination depot identifier
            vehicle_id: Vehicle identifier
            external_id: Vehicle external ID (e.g., 'bus_101')
            expected_soc: Expected SoC at arrival (0.0-1.0)
            arrival_time: Expected arrival time
            battery_kwh: Vehicle battery capacity (kWh)
            max_charge_kw: Vehicle max charge rate (kW)

        Returns:
            message_id if successful, None otherwise
        """
        endpoint = self.depot_endpoints.get(dest_depot_id)
        if not endpoint:
            logger.error(f"No endpoint configured for depot {dest_depot_id}")
            return None

        try:
            client = await self._get_client()
            receive_url = f"{endpoint}/depots/{dest_depot_id}/handoff/receive"
            receive_payload = {
                "origin_depot_id": str(origin_depot_id),
                "vehicle_id": str(vehicle_id),
                "external_id": external_id,
                "expected_soc": expected_soc,
                "arrival_time": arrival_time.isoformat(),
                "battery_kwh": battery_kwh,
                "max_charge_kw": max_charge_kw,
            }

            response = await client.post(receive_url, json=receive_payload)
            response.raise_for_status()
            ack_data = response.json()

            logger.info(
                f"Handoff sent: vehicle {external_id} to depot {dest_depot_id}, "
                f"acknowledged at {ack_data.get('acknowledged_at')}"
            )
            return UUID(ack_data.get('message_id'))
        except httpx.RequestError as e:
            logger.error(f"Failed to send handoff to {dest_depot_id}: {e}")
            return None
        except Exception as e:
            logger.error(
                f"Unexpected error sending handoff: {e}",
                exc_info=True
            )
            return None

    def create_incoming_vehicle(
        self,
        vehicle_id: UUID,
        external_id: str,
        expected_soc: float,
        arrival_time: datetime,
        battery_kwh: float,
        max_charge_kw: float,
        origin_depot_id: UUID,
    ) -> IncomingVehicle:
        """Create IncomingVehicle for state assembly.

        Args:
            vehicle_id: Vehicle identifier
            external_id: Vehicle external ID
            expected_soc: Expected SoC at arrival
            arrival_time: Expected arrival time
            battery_kwh: Vehicle battery capacity
            max_charge_kw: Vehicle max charge rate
            origin_depot_id: Origin depot identifier

        Returns:
            IncomingVehicle object
        """
        return IncomingVehicle(
            vehicle_id=vehicle_id,
            external_id=external_id,
            expected_soc=expected_soc,
            arrival_time=arrival_time,
            battery_kwh=battery_kwh,
            max_charge_kw=max_charge_kw,
            origin_depot_id=origin_depot_id,
        )

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
