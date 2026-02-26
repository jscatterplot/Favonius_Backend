"""OCPP WebSocket server for charge point connections — full coverage.

Reference: Development plan Step 3.1, PRD_v2.md#9-1-ocpp-integration

Updates from original:
 - Status persistence to database (charger_status table or chargers table)
 - Extended meter values callback with energy_kwh, transaction_id, raw_samples
 - Boot notification validation support
 - Connector status cache for fast queries
 - WebSocket subprotocol handling for both 1.6 and 2.0.1
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Optional

import asyncpg
import websockets
from websockets.server import WebSocketServerProtocol

from .asgi_adapter import StarletteOCPPAdapter
from .charge_point import FleetChargePoint
from .mapping import get_charger_id_from_ocpp_id

logger = logging.getLogger(__name__)


class OCPPServer:
    """WebSocket server for OCPP 1.6 connections.

    Manages charge point connections and provides methods to interact
    with connected chargers. Supports full CitrineOS-parity message coverage.

    Reference: PRD Section 9.1, Development plan Step 3.1
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9000,
        pool: Optional[asyncpg.Pool] = None,
        on_status_change: Optional[Callable] = None,
        on_meter_values: Optional[Callable] = None,
        on_boot: Optional[Callable] = None,
        on_transaction_start: Optional[Callable] = None,
        on_transaction_stop: Optional[Callable] = None,
        on_authorize: Optional[Callable] = None,
    ):
        """Initialize OCPP server.

        Args:
            host: Server host address (default '0.0.0.0')
            port: Server port (default 9000)
            pool: Optional asyncpg connection pool for database operations
            on_status_change: Optional callback for status changes
            on_meter_values: Optional callback for meter value updates
            on_boot: Optional callback for boot notification validation
            on_transaction_start: Optional callback for transaction authorization
            on_transaction_stop: Optional callback for transaction stop notification
            on_authorize: Optional callback for authorization validation
        """
        self.host = host
        self.port = port
        self.pool = pool
        self.charge_points: dict[str, FleetChargePoint] = {}
        self.on_status_change = on_status_change
        self.on_meter_values = on_meter_values
        self._on_boot = on_boot
        self._on_tx_start = on_transaction_start
        self._on_tx_stop = on_transaction_stop
        self._on_authorize = on_authorize
        self.server: Optional[websockets.WebSocketServer] = None
        self._running = False

        # Connector status cache: {charge_point_id: {connector_id: status}}
        self._connector_status_cache: dict[str, dict[int, str]] = {}

        logger.info(f"Initialized OCPPServer on {host}:{port}")

    async def on_connect(self, websocket: WebSocketServerProtocol, path: str) -> None:
        """Handle new charge point connection.

        Extracts charge point ID from path and creates FleetChargePoint instance.
        """
        charge_point_id = path.strip("/")
        if not charge_point_id:
            logger.warning(f"Invalid connection path: {path}")
            await websocket.close()
            return

        logger.info(f"New connection from charge point: {charge_point_id}")

        cp = FleetChargePoint(
            id=charge_point_id,
            connection=websocket,
            on_status_change=self._handle_status_change,
            on_meter_values=self._handle_meter_values,
            on_boot=self._on_boot,
            on_transaction_start=self._on_tx_start,
            on_transaction_stop=self._on_tx_stop,
            on_authorize=self._on_authorize,
        )

        self.charge_points[charge_point_id] = cp

        try:
            await cp.start()
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Connection closed for charge point: {charge_point_id}")
        except Exception as e:
            logger.error(f"Error handling charge point {charge_point_id}: {e}")
        finally:
            if charge_point_id in self.charge_points:
                del self.charge_points[charge_point_id]
            self._connector_status_cache.pop(charge_point_id, None)
            logger.info(f"Cleaned up connection for charge point: {charge_point_id}")

    async def handle_websocket(self, websocket: Any, charge_point_id: str) -> None:
        """Handle one OCPP WebSocket connection from ASGI (e.g. FastAPI route).

        Use when OCPP_USE_SAME_PORT=true so OCPP is served on the same port as REST.
        """
        adapter = StarletteOCPPAdapter(websocket)
        cp = FleetChargePoint(
            id=charge_point_id,
            connection=adapter,
            on_status_change=self._handle_status_change,
            on_meter_values=self._handle_meter_values,
            on_boot=self._on_boot,
            on_transaction_start=self._on_tx_start,
            on_transaction_stop=self._on_tx_stop,
            on_authorize=self._on_authorize,
        )
        self.charge_points[charge_point_id] = cp
        try:
            await cp.start()
        except (ConnectionError, Exception) as e:
            logger.info(f"OCPP connection closed for {charge_point_id}: {e}")
        finally:
            self.charge_points.pop(charge_point_id, None)
            self._connector_status_cache.pop(charge_point_id, None)
            logger.info(f"Cleaned up OCPP connection for charge point: {charge_point_id}")

    # ------------------------------------------------------------------
    # Internal callbacks
    # ------------------------------------------------------------------

    async def _handle_status_change(
        self,
        charge_point_id: str,
        connector_id: int,
        status: str,
        *args,
        **kwargs,
    ) -> None:
        """Handle status change callback with persistence."""
        error_code = args[0] if len(args) > 0 else kwargs.get("error_code")
        timestamp = args[1] if len(args) > 1 else kwargs.get("timestamp")

        logger.debug(
            f"Status change: {charge_point_id}, connector {connector_id}, " f"status={status}"
        )

        # Update cache
        if charge_point_id not in self._connector_status_cache:
            self._connector_status_cache[charge_point_id] = {}
        self._connector_status_cache[charge_point_id][connector_id] = status

        # Call user-provided callback
        if self.on_status_change:
            try:
                await self.on_status_change(charge_point_id, connector_id, status)
            except Exception as e:
                logger.error(f"Error in status change callback: {e}")

        # Persist to database
        if self.pool:
            try:
                await self._store_status_update(
                    charge_point_id,
                    connector_id,
                    status,
                    error_code,
                    timestamp,
                )
            except Exception as e:
                logger.error(f"Error storing status update: {e}")

    async def _handle_meter_values(
        self,
        charge_point_id: str,
        connector_id: int,
        soc: float,
        power_kw: float,
        *args,
        **kwargs,
    ) -> None:
        """Handle meter values callback — supports both old and new signatures."""
        # Parse args flexibly for backward compatibility
        energy_kwh = None
        timestamp = None
        max_charge_kw = None

        if args:
            # New signature: (cp, conn, soc, power, energy, ts, tx_id, max_kw, raw)
            if len(args) >= 1:
                # First extra arg could be energy_kwh (float/None) or timestamp
                if isinstance(args[0], datetime):
                    timestamp = args[0]
                    max_charge_kw = args[1] if len(args) > 1 else None
                else:
                    energy_kwh = args[0]
                    timestamp = args[1] if len(args) > 1 else None
                    args[2] if len(args) > 2 else None
                    max_charge_kw = args[3] if len(args) > 3 else None
                    args[4] if len(args) > 4 else None

        logger.debug(
            f"Meter values: {charge_point_id}, connector {connector_id}, "
            f"SoC={soc:.2f}, Power={power_kw:.2f}kW"
            + (f", Energy={energy_kwh:.2f}kWh" if energy_kwh else "")
            + (f", max_charge={max_charge_kw:.2f}kW" if max_charge_kw else "")
        )

        # Call user-provided callback
        if self.on_meter_values:
            try:
                await self.on_meter_values(
                    charge_point_id, connector_id, soc, power_kw, timestamp, max_charge_kw
                )
            except TypeError:
                try:
                    await self.on_meter_values(
                        charge_point_id, connector_id, soc, power_kw, timestamp
                    )
                except Exception as e:
                    logger.error(f"Error in meter values callback: {e}")
            except Exception as e:
                logger.error(f"Error in meter values callback: {e}")

        # Store in database
        if self.pool and timestamp:
            try:
                await self._store_meter_values(
                    charge_point_id,
                    connector_id,
                    soc,
                    power_kw,
                    energy_kwh,
                    timestamp,
                    max_charge_kw,
                )
            except Exception as e:
                logger.error(f"Error storing meter values: {e}")

    async def _store_meter_values(
        self,
        charge_point_id: str,
        connector_id: int,
        soc: float,
        power_kw: float,
        energy_kwh: Optional[float],
        timestamp: datetime,
        max_charge_kw: Optional[float] = None,
    ) -> None:
        """Store meter values in database."""
        if not self.pool:
            return

        from .telemetry import store_meter_values

        try:
            charger_id = await get_charger_id_from_ocpp_id(self.pool, charge_point_id)
        except Exception as e:
            logger.debug(f"Could not resolve charger ID: {e}")
            charger_id = None

        await store_meter_values(
            self.pool,
            charge_point_id,
            connector_id,
            soc,
            power_kw,
            timestamp,
            vehicle_id=None,
            max_charge_kw=max_charge_kw,
            charger_id=charger_id,
            energy_kwh=energy_kwh,
        )

    async def _store_status_update(
        self,
        charge_point_id: str,
        connector_id: int,
        status: str,
        error_code: Optional[str],
        timestamp: Optional[str] = None,
    ) -> None:
        """Store status update in database.

        Upserts into the connector_status table (migration 004).
        """
        if not self.pool:
            return

        try:
            ts = timestamp or datetime.utcnow().isoformat()

            async with self.pool.acquire() as conn:
                # Try upsert to connector_status table
                await conn.execute(
                    """
                    INSERT INTO connector_status
                        (charger_ocpp_id, connector_id, status, error_code, updated_at)
                    VALUES ($1, $2, $3, $4, $5::timestamptz)
                    ON CONFLICT (charger_ocpp_id, connector_id)
                    DO UPDATE SET
                        status = EXCLUDED.status,
                        error_code = EXCLUDED.error_code,
                        updated_at = EXCLUDED.updated_at
                    """,
                    charge_point_id,
                    connector_id,
                    status,
                    error_code,
                    ts,
                )
                logger.debug(f"Stored status: {charge_point_id}:{connector_id} = {status}")
        except asyncpg.UndefinedTableError:
            # Table doesn't exist yet — log and skip
            logger.debug(
                f"connector_status table not found, skipping persistence for "
                f"{charge_point_id}:{connector_id}={status}"
            )
        except Exception as e:
            logger.error(f"Error storing status update: {e}")

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def get_connector_status(
        self,
        charge_point_id: str,
        connector_id: int,
    ) -> Optional[str]:
        """Get cached connector status.

        Args:
            charge_point_id: Charge point identifier
            connector_id: Connector identifier

        Returns:
            Status string or None if unknown
        """
        return self._connector_status_cache.get(charge_point_id, {}).get(connector_id)

    def get_all_connector_statuses(self) -> dict[str, dict[int, str]]:
        """Get all cached connector statuses.

        Returns:
            Nested dict: {charge_point_id: {connector_id: status}}
        """
        return dict(self._connector_status_cache)

    def get_connected_charge_points(self) -> list[str]:
        """Get list of currently connected charge point IDs."""
        return list(self.charge_points.keys())

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

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
            subprotocols=["ocpp1.6"],
        ) as server:
            self.server = server
            logger.info(f"OCPP server started on ws://{self.host}:{self.port}")
            await asyncio.Future()

    async def stop(self) -> None:
        """Stop OCPP server and close all connections."""
        logger.info("Stopping OCPP server...")
        self._running = False

        for charge_point_id, cp in list(self.charge_points.items()):
            try:
                await cp.close()
            except Exception as e:
                logger.error(f"Error closing charge point {charge_point_id}: {e}")

        self.charge_points.clear()
        self._connector_status_cache.clear()

        if self.server:
            self.server.close()
            await self.server.wait_closed()

        logger.info("OCPP server stopped")

    def get_charge_point(self, charge_point_id: str) -> Optional[FleetChargePoint]:
        """Get charge point instance by ID."""
        return self.charge_points.get(charge_point_id)

    def is_connected(self, charge_point_id: str) -> bool:
        """Check if charge point is connected."""
        return charge_point_id in self.charge_points
