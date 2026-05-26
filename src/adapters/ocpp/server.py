"""Emergency-fallback OCPP WebSocket server for charge point connections.

Reference: Development plan Step 3.1, PRD_v2.md#9-1-ocpp-integration

This adapter path is intentionally *not* the primary production runtime.
The canonical OCPP runtime is ``src/websocket_handler``. Keep this server as an
explicit emergency fallback for controlled failover scenarios.

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
from http import HTTPStatus
from typing import Any, Callable, Optional

import asyncpg
import websockets
from ocpp.v16.enums import AuthorizationStatus
from websockets.server import WebSocketServerProtocol

from .asgi_adapter import StarletteOCPPAdapter
from .charge_point import FleetChargePoint
from .mapping import get_charger_id_from_ocpp_id
from ...db import queries as db_queries
from ...db.pools import DatabasePools
from ...security.ocpp_auth import verify_ocpp_basic_auth

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
        pools: Optional[DatabasePools] = None,
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
            pools: Optional DatabasePools for database operations (static=Supabase, ts=TimescaleDB)
            on_status_change: Optional callback for status changes
            on_meter_values: Optional callback for meter value updates
            on_boot: Optional callback for boot notification validation
            on_transaction_start: Optional callback for transaction authorization
            on_transaction_stop: Optional callback for transaction stop notification
            on_authorize: Optional callback for authorization validation
        """
        self.host = host
        self.port = port
        self.pools = pools
        self.charge_points: dict[str, FleetChargePoint] = {}
        self.on_status_change = on_status_change
        self.on_meter_values = on_meter_values
        self._on_boot = on_boot
        self._on_tx_start = on_transaction_start or self._handle_transaction_start
        self._tx_stop_callback = on_transaction_stop
        self._on_tx_stop = self._handle_transaction_stop
        self._on_authorize = on_authorize or self._handle_authorize
        self.server: Optional[websockets.WebSocketServer] = None
        self._running = False

        # Connector status cache: {charge_point_id: {connector_id: status}}
        self._connector_status_cache: dict[str, dict[int, str]] = {}
        self._pending_starts: dict[str, dict[str, Any]] = {}
        self._connector_identity_cache: dict[tuple[str, int], dict[str, Any]] = {}
        self._transaction_connectors: dict[tuple[str, int], int] = {}

        logger.info(f"Initialized OCPPServer on {host}:{port}")

    async def _process_request(self, path: str, request_headers: Any):
        """websockets pre-handshake hook: enforce OCPP-J Basic Auth.

        Returns None to accept the upgrade, or an HTTP response tuple to reject.
        This runs BEFORE the WebSocket handshake, so unauthenticated clients
        get a 401 with WWW-Authenticate and never reach a charge-point handler.
        """
        cp_id = path.strip("/").strip()
        if not cp_id:
            return HTTPStatus.BAD_REQUEST, [], b""
        if self.pools is None:
            logger.error("OCPP auth refused: no DB pool")
            return HTTPStatus.SERVICE_UNAVAILABLE, [], b""

        auth_header: Optional[str] = None
        try:
            auth_header = request_headers.get("Authorization")  # websockets.Headers
        except AttributeError:
            try:
                auth_header = request_headers["Authorization"]  # mapping fallback
            except (KeyError, TypeError):
                auth_header = None

        if not await verify_ocpp_basic_auth(auth_header, cp_id, self.pools.static):
            logger.warning("OCPP auth rejected at handshake for %s", cp_id)
            return (
                HTTPStatus.UNAUTHORIZED,
                [("WWW-Authenticate", 'Basic realm="ocpp"')],
                b"",
            )
        return None

    async def on_connect(self, websocket: WebSocketServerProtocol, path: str) -> None:
        """Handle new charge point connection.

        Extracts charge point ID from path and creates FleetChargePoint instance.
        Authentication has already been enforced by ``_process_request`` before
        the handshake completed.
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
            tx_id_provider=lambda cp_id=charge_point_id: self._next_transaction_id(cp_id),
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
            self._clear_charge_point_caches(charge_point_id)
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
            tx_id_provider=lambda cp_id=charge_point_id: self._next_transaction_id(cp_id),
        )
        self.charge_points[charge_point_id] = cp
        try:
            await cp.start()
        except (ConnectionError, Exception) as e:
            logger.info(f"OCPP connection closed for {charge_point_id}: {e}")
        finally:
            self.charge_points.pop(charge_point_id, None)
            self._clear_charge_point_caches(charge_point_id)
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
        if self.pools:
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
        soc: Optional[float],
        power_kw: Optional[float],
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

        soc_str = f"{soc:.2f}" if soc is not None else "N/A"
        power_str = f"{power_kw:.2f}" if power_kw is not None else "N/A"
        logger.debug(
            f"Meter values: {charge_point_id}, connector {connector_id}, "
            f"SoC={soc_str}, Power={power_str}kW"
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
        if self.pools and timestamp:
            try:
                identity = self._connector_identity_cache.get((charge_point_id, connector_id), {})
                await self._store_meter_values(
                    charge_point_id,
                    connector_id,
                    soc,
                    power_kw,
                    energy_kwh,
                    timestamp,
                    max_charge_kw,
                    vehicle_id=identity.get("vehicle_id"),
                )
            except Exception as e:
                logger.error(f"Error storing meter values: {e}")

    async def _handle_authorize(self, charge_point_id: str, id_tag: str) -> AuthorizationStatus:
        """Authorize an OCPP idTag against vehicle primary tags and active RFID cards."""
        if not self.pools:
            return AuthorizationStatus.invalid
        try:
            async with self.pools.static.acquire() as conn:
                identity = await db_queries.resolve_id_tag_identity(
                    conn,
                    id_tag,
                    station_id=charge_point_id,
                )
        except Exception as exc:
            logger.error("Authorize lookup failed for %s id_tag=%s: %s", charge_point_id, id_tag, exc)
            return AuthorizationStatus.invalid
        return AuthorizationStatus.accepted if identity else AuthorizationStatus.invalid

    async def _handle_transaction_start(
        self,
        charge_point_id: str,
        connector_id: int,
        id_tag: str,
        meter_start: int,
        timestamp: str,
    ) -> AuthorizationStatus:
        """Validate StartTransaction and stash identity for session persistence."""
        if not self.pools:
            return AuthorizationStatus.invalid
        try:
            async with self.pools.static.acquire() as conn:
                identity = await db_queries.resolve_id_tag_identity(
                    conn,
                    id_tag,
                    station_id=charge_point_id,
                )
        except Exception as exc:
            logger.error(
                "StartTransaction lookup failed for %s id_tag=%s: %s",
                charge_point_id,
                id_tag,
                exc,
            )
            return AuthorizationStatus.invalid
        if not identity:
            return AuthorizationStatus.invalid

        self._connector_identity_cache[(charge_point_id, connector_id)] = identity
        self._pending_starts[charge_point_id] = {
            "connector_id": connector_id,
            "evse_id": connector_id,
            "id_tag": id_tag,
            "timestamp": timestamp,
            "vehicle_id": identity.get("vehicle_id"),
            "driver_id": identity.get("driver_id"),
            "card_id": identity.get("card_id"),
        }
        return AuthorizationStatus.accepted

    async def _handle_transaction_stop(
        self,
        charge_point_id: str,
        transaction_id: int,
        id_tag: str,
        meter_stop: int,
        timestamp: str,
        reason: str,
        transaction_data: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        """Handle StopTransaction and clear connector-scoped identity cache."""
        connector_id: Optional[int] = None
        cp = self.charge_points.get(charge_point_id)
        if cp:
            for known_connector_id, known_transaction_id in cp.transactions.items():
                if known_transaction_id == transaction_id:
                    connector_id = known_connector_id
                    break
            if connector_id is None and cp.current_connector_id is not None:
                connector_id = cp.current_connector_id
        if connector_id is not None:
            self._connector_identity_cache.pop((charge_point_id, connector_id), None)
        else:
            connector_id = self._transaction_connectors.get((charge_point_id, transaction_id))
            if connector_id is not None:
                self._connector_identity_cache.pop((charge_point_id, connector_id), None)
        self._transaction_connectors.pop((charge_point_id, transaction_id), None)

        if self._tx_stop_callback:
            await self._tx_stop_callback(
                charge_point_id,
                transaction_id,
                id_tag,
                meter_stop,
                timestamp,
                reason,
                transaction_data,
            )

    def _clear_charge_point_identity_cache(self, charge_point_id: str) -> None:
        """Drop all connector identity entries for a charge point."""
        stale_keys = [
            key for key in self._connector_identity_cache if key[0] == charge_point_id
        ]
        for key in stale_keys:
            self._connector_identity_cache.pop(key, None)

    def _clear_charge_point_caches(self, charge_point_id: str) -> None:
        """Drop all charge-point scoped caches on disconnect."""
        self._connector_status_cache.pop(charge_point_id, None)
        self._pending_starts.pop(charge_point_id, None)
        self._clear_charge_point_identity_cache(charge_point_id)
        stale_tx_keys = [key for key in self._transaction_connectors if key[0] == charge_point_id]
        for key in stale_tx_keys:
            self._transaction_connectors.pop(key, None)

    async def _next_transaction_id(self, charge_point_id: str) -> int:
        """Generate a DB-backed transactionId and persist open session identity."""
        if not self.pools:
            raise RuntimeError("Database pools unavailable for OCPP transaction id")
        async with self.pools.static.acquire() as conn:
            tx_id = int(await conn.fetchval("SELECT nextval('ocpp_transaction_id')"))
            pending = self._pending_starts.pop(charge_point_id, None)
            if pending:
                self._transaction_connectors[(charge_point_id, tx_id)] = pending["connector_id"]
                await conn.execute(
                    """
                    INSERT INTO charging_sessions (
                        station_id, transaction_id, evse_id, connector_id,
                        id_token, start_time, vehicle_id, driver_id, card_id
                    ) VALUES ($1, $2, $3, $4, $5, $6::timestamptz, $7, $8::uuid, $9::uuid)
                    """,
                    charge_point_id,
                    tx_id,
                    pending["evse_id"],
                    pending["connector_id"],
                    pending["id_tag"],
                    pending["timestamp"],
                    pending.get("vehicle_id"),
                    pending.get("driver_id"),
                    pending.get("card_id"),
                )
            return tx_id

    async def _store_meter_values(
        self,
        charge_point_id: str,
        connector_id: int,
        soc: Optional[float],
        power_kw: Optional[float],
        energy_kwh: Optional[float],
        timestamp: datetime,
        max_charge_kw: Optional[float] = None,
        vehicle_id: Optional[str] = None,
    ) -> None:
        """Store meter values in database."""
        if not self.pools:
            return

        from .telemetry import store_meter_values

        try:
            charger_id = await get_charger_id_from_ocpp_id(self.pools.static, charge_point_id)
        except Exception as e:
            logger.debug(f"Could not resolve charger ID: {e}")
            charger_id = None

        await store_meter_values(
            self.pools,
            charge_point_id,
            connector_id,
            soc,
            power_kw,
            timestamp,
            vehicle_id=vehicle_id,
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
        if not self.pools:
            return

        try:
            ts = timestamp or datetime.utcnow().isoformat()

            async with self.pools.ts.acquire() as conn:
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
            process_request=self._process_request,
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
        self._pending_starts.clear()
        self._connector_identity_cache.clear()
        self._transaction_connectors.clear()

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
