"""OCPP WebSocket server for charge point connections.

Reference: Development plan Step 3.1, PRD_v2.md#9-1-ocpp-integration
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Callable, Optional

import asyncpg
import websockets
from websockets.server import WebSocketServerProtocol

from .charge_point import FleetChargePoint
from .mapping import get_charger_id_from_ocpp_id

logger = logging.getLogger(__name__)


class OCPPServer:
    """WebSocket server for OCPP 1.6 connections.

    Manages charge point connections and provides methods to interact
    with connected chargers.

    Reference: PRD Section 9.1, Development plan Step 3.1
    """

    def __init__(
        self,
        host: str = '0.0.0.0',
        port: int = 9000,
        pool: Optional[asyncpg.Pool] = None,
        on_status_change: Optional[
            Callable[[str, int, str], None]
        ] = None,
        on_meter_values: Optional[
            Callable[[str, int, float, float, datetime], None]
        ] = None,
    ):
        """Initialize OCPP server.

        Args:
            host: Server host address (default '0.0.0.0')
            port: Server port (default 9000)
            pool: Optional asyncpg connection pool for database operations
            on_status_change: Optional callback for status changes
            on_meter_values: Optional callback for meter value updates
        """
        self.host = host
        self.port = port
        self.pool = pool
        self.charge_points: dict[str, FleetChargePoint] = {}
        self.on_status_change = on_status_change
        self.on_meter_values = on_meter_values
        self.server: Optional[websockets.WebSocketServer] = None
        self._running = False
        logger.info(f"Initialized OCPPServer on {host}:{port}")

    async def on_connect(
        self, websocket: WebSocketServerProtocol, path: str
    ) -> None:
        """Handle new charge point connection.

        Extracts charge point ID from path and creates FleetChargePoint instance.

        Args:
            websocket: WebSocket connection
            path: Connection path (format: /{charge_point_id})
        """
        # Extract charge point ID from path
        charge_point_id = path.strip('/')
        if not charge_point_id:
            logger.warning(f"Invalid connection path: {path}")
            await websocket.close()
            return

        logger.info(f"New connection from charge point: {charge_point_id}")

        # Create charge point instance with callbacks
        cp = FleetChargePoint(
            id=charge_point_id,
            connection=websocket,
            on_status_change=self._handle_status_change,
            on_meter_values=self._handle_meter_values,
        )

        # Register charge point
        self.charge_points[charge_point_id] = cp

        try:
            # Start charge point message loop
            await cp.start()
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Connection closed for charge point: {charge_point_id}")
        except Exception as e:
            logger.error(f"Error handling charge point {charge_point_id}: {e}")
        finally:
            # Clean up on disconnect
            if charge_point_id in self.charge_points:
                del self.charge_points[charge_point_id]
            logger.info(f"Cleaned up connection for charge point: {charge_point_id}")

    async def _handle_status_change(
        self, charge_point_id: str, connector_id: int, status: str
    ) -> None:
        """Handle status change callback.

        Args:
            charge_point_id: Charge point identifier
            connector_id: Connector identifier
            status: New connector status
        """
        logger.debug(
            f"Status change: {charge_point_id}, connector {connector_id}, status={status}"
        )

        # Call user-provided callback if available
        if self.on_status_change:
            try:
                await self.on_status_change(charge_point_id, connector_id, status)
            except Exception as e:
                logger.error(f"Error in status change callback: {e}")

        # Optionally store in database
        if self.pool:
            try:
                await self._store_status_update(
                    charge_point_id, connector_id, status, None
                )
            except Exception as e:
                logger.error(f"Error storing status update: {e}")

    async def _handle_meter_values(
        self,
        charge_point_id: str,
        connector_id: int,
        soc: float,
        power_kw: float,
        timestamp: datetime,
        max_charge_kw: Optional[float] = None,
    ) -> None:
        """Handle meter values callback.

        Args:
            charge_point_id: Charge point identifier
            connector_id: Connector identifier
            soc: State of charge (0.0-1.0)
            power_kw: Charging power in kW
            timestamp: Meter reading timestamp
            max_charge_kw: Optional max charge rate from OCPP (kW) - per PRD Section 8.4
        """
        logger.debug(
            f"Meter values: {charge_point_id}, connector {connector_id}, "
            f"SoC={soc:.2f}, Power={power_kw:.2f}kW"
            + (f", max_charge_kw={max_charge_kw:.2f}kW" if max_charge_kw else "")
        )

        # Call user-provided callback if available
        if self.on_meter_values:
            try:
                # Support both old signature (4 args) and new signature (5 args with max_charge_kw)
                import inspect
                sig = inspect.signature(self.on_meter_values)
                if len(sig.parameters) >= 5:
                    await self.on_meter_values(
                        charge_point_id, connector_id, soc, power_kw, timestamp, max_charge_kw
                    )
                else:
                    await self.on_meter_values(
                        charge_point_id, connector_id, soc, power_kw, timestamp
                    )
            except Exception as e:
                logger.error(f"Error in meter values callback: {e}")

        # Store in database
        if self.pool:
            try:
                await self._store_meter_values(
                    charge_point_id, connector_id, soc, power_kw, timestamp, max_charge_kw
                )
            except Exception as e:
                logger.error(f"Error storing meter values: {e}")

    async def _store_meter_values(
        self,
        charge_point_id: str,
        connector_id: int,
        soc: float,
        power_kw: float,
        timestamp: datetime,
        max_charge_kw: Optional[float] = None,
    ) -> None:
        """Store meter values in database.
        
        Per PRD Section 8.4, if max_charge_kw is provided, it will be
        dynamically updated in the vehicles table.

        Args:
            charge_point_id: Charge point identifier
            connector_id: Connector identifier
            soc: State of charge (0.0-1.0)
            power_kw: Charging power in kW
            timestamp: Meter reading timestamp
            max_charge_kw: Optional max charge rate from OCPP (kW)
        """
        if not self.pool:
            return

        # Import telemetry storage function
        from .telemetry import store_meter_values
        
        # Look up vehicle_id and charger_id
        try:
            # Get charger_id using mapping helper
            charger_id = await get_charger_id_from_ocpp_id(self.pool, charge_point_id)
            
            # Vehicle ID will be resolved in store_meter_values
            vehicle_id = None
                
        except Exception as e:
            logger.debug(f"Could not resolve charger/vehicle IDs: {e}")
            charger_id = None
            vehicle_id = None
        
        # Use centralized telemetry storage function
        await store_meter_values(
            self.pool,
            charge_point_id,
            connector_id,
            soc,
            power_kw,
            timestamp,
            vehicle_id=vehicle_id,
            max_charge_kw=max_charge_kw,
            charger_id=charger_id,
        )
        is_plugged = power_kw > 0.1  # Consider plugged if charging

        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    query, timestamp, vehicle_id, soc, power_kw, is_plugged
                )
        except Exception as e:
            logger.error(f"Database error storing meter values: {e}")
            raise

    async def _store_status_update(
        self,
        charge_point_id: str,
        connector_id: int,
        status: str,
        error_code: Optional[str],
    ) -> None:
        """Store status update in database.

        Args:
            charge_point_id: Charge point identifier
            connector_id: Connector identifier
            status: Connector status
            error_code: Optional error code
        """
        # Status updates could be stored in a charger_status table
        # For now, we'll just log it
        logger.debug(
            f"Status update: {charge_point_id}, connector {connector_id}, "
            f"status={status}, error_code={error_code}"
        )

    async def start(self) -> None:
        """Start OCPP WebSocket server."""
        if self._running:
            logger.warning("Server already running")
            return

        logger.info(f"Starting OCPP server on {self.host}:{self.port}")
        self._running = True

        async with websockets.serve(
            self.on_connect,
            self.host,
            self.port,
            subprotocols=['ocpp1.6'],
        ) as server:
            self.server = server
            logger.info(f"OCPP server started on ws://{self.host}:{self.port}")
            await asyncio.Future()  # Run forever

    async def stop(self) -> None:
        """Stop OCPP server and close all connections."""
        logger.info("Stopping OCPP server...")
        self._running = False

        # Close all charge point connections
        for charge_point_id, cp in list(self.charge_points.items()):
            try:
                await cp.close()
            except Exception as e:
                logger.error(f"Error closing charge point {charge_point_id}: {e}")

        self.charge_points.clear()

        if self.server:
            self.server.close()
            await self.server.wait_closed()

        logger.info("OCPP server stopped")

    def get_charge_point(self, charge_point_id: str) -> Optional[FleetChargePoint]:
        """Get charge point instance by ID.

        Args:
            charge_point_id: Charge point identifier

        Returns:
            FleetChargePoint instance or None if not connected
        """
        return self.charge_points.get(charge_point_id)

    def is_connected(self, charge_point_id: str) -> bool:
        """Check if charge point is connected.

        Args:
            charge_point_id: Charge point identifier

        Returns:
            True if connected, False otherwise
        """
        return charge_point_id in self.charge_points

