"""VDV 463 WebSocket handler for BMS/ITCS communication.

Implements VDV 463 v1.1.0 protocol over WebSocket Secure (WSS).
See docs/PRD_v2_7_Building_Integration.md Section 9.6.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, Optional

import websockets
from websockets import WebSocketServerProtocol

try:
    import structlog

    _HAS_STRUCTLOG = True
except ImportError:
    import logging

    structlog = None  # type: ignore
    _HAS_STRUCTLOG = False

from . import repository as vdv_repo
from .charging_point_resolver import ChargingPointResolver
from .depot_state import get_depot_charging_info
from ...db.pools import DatabasePools
from .messages import (
    ChargingPointInfo,
    ChargingRequest,
    DepotInfo,
    ValidationMode,
    VDV463ValidationError,
    VDVMessageEnvelope,
    build_error,
    build_provide_charging_information_message,
    build_provide_charging_requests_response,
    parse_charging_request_item,
    parse_message,
)
from .vehicle_resolver import VehicleResolver


def _stdlib_log_adapter(logger_instance: Any) -> Any:
    """Wrap stdlib logger to accept structlog-style keyword args."""

    def _log(level: str, msg: str, *args: Any, **kwargs: Any) -> None:
        if kwargs:
            extra = " ".join(f"{k}={v!r}" for k, v in kwargs.items())
            msg = f"{msg} {extra}" if msg else extra
        getattr(logger_instance, level)(msg, *args)

    class Adapter:
        def warning(self, msg: str, *args: Any, **kwargs: Any) -> None:
            _log("warning", msg, *args, **kwargs)

        def info(self, msg: str, *args: Any, **kwargs: Any) -> None:
            _log("info", msg, *args, **kwargs)

        def error(self, msg: str, *args: Any, **kwargs: Any) -> None:
            _log("error", msg, *args, **kwargs)

        def debug(self, msg: str, *args: Any, **kwargs: Any) -> None:
            _log("debug", msg, *args, **kwargs)

    return Adapter()


def get_logger(name: str) -> Any:
    """Get a structured logger instance (structlog if available, else stdlib logging)."""
    if _HAS_STRUCTLOG:
        return structlog.get_logger(name)
    return _stdlib_log_adapter(logging.getLogger(name))


logger = get_logger(__name__)


class VDV463Handler:
    """Handles VDV 463 protocol messages from BMS/ITCS systems."""

    def __init__(
        self,
        presystem_id: str,
        websocket: WebSocketServerProtocol,
        connection_manager: Any,  # ConnectionManager from websocket_handler
        config: Any,  # Config from websocket_handler
        depot_id: Optional[str] = None,
        validation_mode: ValidationMode = ValidationMode.HARD,
        vehicle_resolver: Optional[VehicleResolver] = None,
        charging_point_resolver: Optional[ChargingPointResolver] = None,
        pools: Optional[DatabasePools] = None,
        db_pool: Any = None,
    ):
        """Initialize VDV 463 handler.

        Args:
            presystem_id: Presystem identifier from WebSocket path
            websocket: WebSocket connection
            connection_manager: Connection manager instance
            config: Application configuration
            depot_id: Optional depot ID (defaults to config default)
            validation_mode: Validation mode (HARD or SOFT)
            vehicle_resolver: Optional vehicle resolver (creates default if None)
            charging_point_resolver: Optional charging point resolver (creates default if None)
            pools: DatabasePools with static (Supabase) and ts (TimescaleDB) pools.
                   Preferred over db_pool when provided.
            db_pool: Deprecated single asyncpg Pool for persistence (used when pools is None)
        """
        self.presystem_id = presystem_id
        self.websocket = websocket
        self.connection_manager = connection_manager
        self.config = config
        self.depot_id = depot_id or getattr(config, "default_depot_id", None)
        self.validation_mode = validation_mode
        self._pools = pools
        if pools is not None:
            # Dual-DB mode: vdv_repo.* writes go to TimescaleDB; static lookups use Supabase.
            self.db_pool = pools.ts
            _static_pool = pools.static
        else:
            # Legacy single-pool mode (e.g. websocket_handler with only TimescaleDB).
            self.db_pool = db_pool
            _static_pool = db_pool
        self.vehicle_resolver = vehicle_resolver or VehicleResolver(_static_pool)
        self.charging_point_resolver = charging_point_resolver or ChargingPointResolver(
            _static_pool
        )
        self.logger = get_logger(__name__)

        # Connection state
        self.connection_id: Optional[str] = None
        self.running = False
        self._charging_info_task: Optional[asyncio.Task] = None

        # In-memory fallback when db_pool is None (testing)
        self.charging_requests: Dict[str, ChargingRequest] = {}

    async def run(self) -> None:
        """Run the VDV 463 handler main loop."""
        self.connection_id = f"vdv463_{self.presystem_id}_{id(self)}"
        client_ip = self.websocket.remote_address[0] if self.websocket.remote_address else "unknown"

        # Register connection
        await self.connection_manager.register_connection(
            self.presystem_id,
            self.connection_id,
            client_ip,
            self.websocket,
        )

        self.logger.info(
            "VDV 463 connection established",
            presystem_id=self.presystem_id,
            depot_id=self.depot_id,
            client_ip=client_ip,
            connection_id=self.connection_id,
        )

        # Start periodic ProvideChargingInformation task
        self.running = True
        self._charging_info_task = asyncio.create_task(self._periodic_charging_information())

        try:
            # Main message loop
            while self.running:
                try:
                    message = await asyncio.wait_for(self.websocket.recv(), timeout=1.0)
                    await self._handle_message(message)
                except asyncio.TimeoutError:
                    # Continue loop to check running flag
                    continue
                except websockets.exceptions.ConnectionClosed:
                    self.logger.info(
                        "VDV 463 connection closed",
                        presystem_id=self.presystem_id,
                    )
                    break
        except Exception as e:
            self.logger.error(
                "Error in VDV 463 handler",
                presystem_id=self.presystem_id,
                error=str(e),
                error_type=type(e).__name__,
            )
        finally:
            await self._cleanup()

    async def _handle_message(self, raw_message: str) -> None:
        """Handle incoming WebSocket message."""
        try:
            # Message size limit (PRD: 1 MB)
            max_size = getattr(getattr(self.config, "websocket", None), "max_message_size", 1048576)
            msg_bytes = len(raw_message.encode("utf-8"))
            if msg_bytes > max_size:
                error_msg = [
                    3,
                    "CMS",
                    self.presystem_id,
                    datetime.utcnow().isoformat() + "Z",
                    "error-message-too-large",
                    "ProvideChargingRequests",
                    {
                        "errorCode": "MessageTooLarge",
                        "errorDescription": f"Message size {msg_bytes} exceeds limit {max_size} bytes",
                    },
                ]
                await self.websocket.send(json.dumps(error_msg))
                self._record_error("MessageTooLarge")
                if self.db_pool and self.depot_id:
                    await vdv_repo.log_vdv463_error(
                        self.db_pool,
                        self.depot_id,
                        self.presystem_id,
                        "MessageTooLarge",
                        f"Message size {msg_bytes} exceeds limit {max_size} bytes",
                    )
                return

            # Check rate limiting
            allowed, reason = await self.connection_manager.check_message_rate_limit(
                self.presystem_id
            )
            if not allowed:
                self.logger.warning(
                    "Rate limit exceeded",
                    presystem_id=self.presystem_id,
                    reason=reason,
                )
                # Build error response
                try:
                    envelope = parse_message(raw_message, ValidationMode.SOFT)
                    error_msg = build_error(
                        envelope,
                        "RateLimitExceeded",
                        f"Rate limit exceeded: {reason}",
                    )
                    await self.websocket.send(json.dumps(error_msg))
                except Exception:
                    pass  # Can't send error if we can't parse message
                if self.db_pool and self.depot_id:
                    await vdv_repo.log_vdv463_error(
                        self.db_pool,
                        self.depot_id,
                        self.presystem_id,
                        "RateLimitExceeded",
                        reason or "Rate limit exceeded",
                    )
                return

            # Record message received
            await self.connection_manager.record_message_received(
                self.presystem_id,
                len(raw_message.encode("utf-8")),
            )

            # Parse and validate message
            try:
                envelope = parse_message(raw_message, self.validation_mode)
            except VDV463ValidationError as e:
                self.logger.error(
                    "Message validation failed",
                    presystem_id=self.presystem_id,
                    error_code=e.error_code,
                    error=str(e),
                )
                self._record_error(e.error_code)
                if self.db_pool and self.depot_id:
                    await vdv_repo.log_vdv463_error(
                        self.db_pool,
                        self.depot_id,
                        self.presystem_id,
                        e.error_code,
                        str(e),
                    )
                # Try to build error response
                try:
                    # Parse in soft mode to get envelope structure
                    soft_envelope = parse_message(raw_message, ValidationMode.SOFT)
                    error_msg = build_error(
                        soft_envelope,
                        e.error_code,
                        str(e),
                        e.details,
                    )
                    await self.websocket.send(json.dumps(error_msg))
                except Exception:
                    # If we can't even parse in soft mode, send generic error
                    error_msg = [
                        3,  # Error
                        "CMS",
                        self.presystem_id,
                        datetime.utcnow().isoformat() + "Z",
                        "parse-error",
                        "Unknown",
                        {
                            "errorCode": e.error_code,
                            "errorDescription": str(e),
                        },
                    ]
                    await self.websocket.send(json.dumps(error_msg))
                return

            # Update metrics
            self._record_message_received(envelope.message_action)

            # Route to appropriate handler
            if envelope.message_type == 1:  # Request
                await self._handle_request(envelope)
            elif envelope.message_type == 2:  # Confirmation
                await self._handle_confirmation(envelope)
            elif envelope.message_type == 3:  # Error
                await self._handle_error(envelope)
            else:
                self.logger.warning(
                    "Unknown message type",
                    presystem_id=self.presystem_id,
                    message_type=envelope.message_type,
                )

        except json.JSONDecodeError as e:
            self.logger.error(
                "Invalid JSON message",
                presystem_id=self.presystem_id,
                error=str(e),
            )
            self._record_error("InvalidJSON")
            if self.db_pool and self.depot_id:
                await vdv_repo.log_vdv463_error(
                    self.db_pool,
                    self.depot_id,
                    self.presystem_id,
                    "InvalidJSON",
                    str(e),
                )
        except Exception as e:
            self.logger.error(
                "Unexpected error handling message",
                presystem_id=self.presystem_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            self._record_error("UnexpectedError")

    async def _handle_request(self, envelope: VDVMessageEnvelope) -> None:
        """Handle VDV 463 request message."""
        action = envelope.message_action

        if action == "ProvideChargingRequests":
            await self._handle_provide_charging_requests(envelope)
        elif action == "BootNotification":
            await self._handle_boot_notification(envelope)
        else:
            self.logger.warning(
                "Unsupported request action",
                presystem_id=self.presystem_id,
                action=action,
            )
            error_msg = build_error(
                envelope,
                "UnsupportedAction",
                f"Action not supported: {action}",
            )
            await self.websocket.send(json.dumps(error_msg))
            self._record_error("UnsupportedAction")
            if self.db_pool and self.depot_id:
                await vdv_repo.log_vdv463_error(
                    self.db_pool,
                    self.depot_id,
                    self.presystem_id,
                    "UnsupportedAction",
                    f"Action not supported: {action}",
                )

    async def _handle_provide_charging_requests(self, envelope: VDVMessageEnvelope) -> None:
        """Handle ProvideChargingRequests message: validate, resolve IDs, persist, terminate absent."""
        payload = envelope.payload
        charging_request_list = payload.get("chargingRequestList", [])

        # DuplicateRequestId: same chargingRequestId twice in one message
        seen_ids: set = set()
        for req_data in charging_request_list:
            crid = req_data.get("chargingRequestId")
            if crid and crid in seen_ids:
                error_msg = build_error(
                    envelope,
                    "DuplicateRequestId",
                    f"Duplicate chargingRequestId in message: {crid}",
                    {"chargingRequestId": crid},
                )
                await self.websocket.send(json.dumps(error_msg))
                self._record_error("DuplicateRequestId")
                if self.db_pool and self.depot_id:
                    await vdv_repo.log_vdv463_error(
                        self.db_pool,
                        self.depot_id,
                        self.presystem_id,
                        "DuplicateRequestId",
                        f"Duplicate chargingRequestId: {crid}",
                        crid,
                    )
                return
            if crid:
                seen_ids.add(crid)

        processed_ids: list = []
        for req_data in charging_request_list:
            try:
                # Parse from chargingRequestData (official VDV 463 structure)
                charging_request = parse_charging_request_item(
                    req_data,
                    validation_status=envelope.validation_status or "ok",
                )
            except (KeyError, TypeError) as e:
                self.logger.error(
                    "Missing or invalid field in charging request",
                    presystem_id=self.presystem_id,
                    error=str(e),
                    request_data=req_data,
                )
                continue

            # InvalidTimeWindow: arrival >= departure
            if charging_request.arrival_time and charging_request.departure_time:
                if charging_request.arrival_time >= charging_request.departure_time:
                    error_msg = build_error(
                        envelope,
                        "InvalidTimeWindow",
                        "expectedArrivalTimeAtChargingPoint must be before requestedTimeForDeparture",
                        {"chargingRequestId": charging_request.charging_request_id},
                    )
                    await self.websocket.send(json.dumps(error_msg))
                    self._record_error("InvalidTimeWindow")
                    if self.db_pool and self.depot_id:
                        await vdv_repo.log_vdv463_error(
                            self.db_pool,
                            self.depot_id,
                            self.presystem_id,
                            "InvalidTimeWindow",
                            "Arrival >= departure",
                            charging_request.charging_request_id,
                        )
                    continue

            # Resolve vehicle ID (required for persistence)
            vehicle_id = await self.vehicle_resolver.resolve_vehicle_id(
                charging_request.vehicle_external_id,
                self.depot_id,
            )
            if not vehicle_id:
                error_msg = build_error(
                    envelope,
                    "InvalidVehicleId",
                    f"Vehicle '{charging_request.vehicle_external_id}' not found in depot configuration",
                    {"chargingRequestId": charging_request.charging_request_id},
                )
                await self.websocket.send(json.dumps(error_msg))
                self._record_error("InvalidVehicleId")
                if self.db_pool and self.depot_id:
                    await vdv_repo.log_vdv463_error(
                        self.db_pool,
                        self.depot_id,
                        self.presystem_id,
                        "InvalidVehicleId",
                        f"Vehicle not found: {charging_request.vehicle_external_id}",
                        charging_request.charging_request_id,
                    )
                continue

            # Resolve charging point ID if provided
            charging_point_uuid = None
            if charging_request.charging_point_id and self.depot_id:
                charging_point_uuid = await self.charging_point_resolver.resolve_charging_point_id(
                    charging_request.charging_point_id,
                    self.depot_id,
                )
                if not charging_point_uuid:
                    error_msg = build_error(
                        envelope,
                        "InvalidChargingPointId",
                        f"Charging point '{charging_request.charging_point_id}' not found in depot",
                        {"chargingRequestId": charging_request.charging_request_id},
                    )
                    await self.websocket.send(json.dumps(error_msg))
                    self._record_error("InvalidChargingPointId")
                    if self.db_pool and self.depot_id:
                        await vdv_repo.log_vdv463_error(
                            self.db_pool,
                            self.depot_id,
                            self.presystem_id,
                            "InvalidChargingPointId",
                            f"Charging point not found: {charging_request.charging_point_id}",
                            charging_request.charging_request_id,
                        )
                    continue

            if self.db_pool and self.depot_id:
                try:
                    if charging_request.charging_instruction == "Terminate":
                        await vdv_repo.set_charging_request_terminated(
                            self.db_pool,
                            self.depot_id,
                            self.presystem_id,
                            charging_request.charging_request_id,
                        )
                    else:
                        await vdv_repo.upsert_charging_request(
                            self.db_pool,
                            self.depot_id,
                            self.presystem_id,
                            charging_request,
                            vehicle_id,
                            charging_point_uuid,
                            envelope.message_id or "",
                        )
                    processed_ids.append(charging_request.charging_request_id)
                except Exception as e:
                    self.logger.error(
                        "Failed to persist charging request",
                        presystem_id=self.presystem_id,
                        charging_request_id=charging_request.charging_request_id,
                        error=str(e),
                    )
                    self._record_error("PersistenceError")
                    if self.db_pool and self.depot_id:
                        await vdv_repo.log_vdv463_error(
                            self.db_pool,
                            self.depot_id,
                            self.presystem_id,
                            "PersistenceError",
                            str(e),
                            charging_request.charging_request_id,
                        )
                    continue
            else:
                request_key = (
                    f"{charging_request.charging_request_id}_{charging_request.vehicle_external_id}"
                )
                self.charging_requests[request_key] = charging_request
                processed_ids.append(charging_request.charging_request_id)

            self.logger.info(
                "Processed charging request",
                presystem_id=self.presystem_id,
                vehicle_external_id=charging_request.vehicle_external_id,
                vehicle_id=vehicle_id,
                charging_request_id=charging_request.charging_request_id,
            )

        # Terminate requests that disappeared from this message (per PRD)
        if self.db_pool and self.depot_id:
            try:
                await vdv_repo.terminate_requests_not_in_list(
                    self.db_pool,
                    self.depot_id,
                    self.presystem_id,
                    processed_ids,
                )
                await vdv_repo.update_depot_vdv463_timestamp(self.db_pool, self.depot_id)
            except Exception as e:
                self.logger.warning(
                    "Failed to terminate absent requests or update depot timestamp",
                    presystem_id=self.presystem_id,
                    error=str(e),
                )

        # Send response
        response = build_provide_charging_requests_response(envelope)
        await self.websocket.send(json.dumps(response))
        self._record_message_sent("ProvideChargingRequests")

    async def _handle_boot_notification(self, envelope: VDVMessageEnvelope) -> None:
        """Handle BootNotification message."""
        self.logger.info(
            "BootNotification received",
            presystem_id=self.presystem_id,
            source=envelope.source,
        )
        # Log connection for operator diagnostics (per dev plan Phase 5)
        if self.db_pool and self.depot_id:
            try:
                await vdv_repo.log_vdv463_connection(
                    self.db_pool,
                    self.depot_id,
                    self.presystem_id,
                    envelope.source or "BMS",
                )
            except Exception as e:
                self.logger.warning(
                    "Failed to log VDV 463 connection",
                    presystem_id=self.presystem_id,
                    error=str(e),
                )
        # Send boot notification response (schema: only "status" per BootNotificationResponse.json)
        response = [
            2,  # Confirmation
            "CMS",
            envelope.presystem_id,
            datetime.utcnow().isoformat() + "Z",
            f"boot-response-{envelope.message_id}",
            "BootNotification",
            {"status": "Accepted"},
        ]
        await self.websocket.send(json.dumps(response))
        self._record_message_sent("BootNotification")

    async def _handle_confirmation(self, envelope: VDVMessageEnvelope) -> None:
        """Handle confirmation message."""
        self.logger.debug(
            "Confirmation received",
            presystem_id=self.presystem_id,
            action=envelope.message_action,
        )

    async def _handle_error(self, envelope: VDVMessageEnvelope) -> None:
        """Handle error message."""
        error_payload = envelope.payload
        error_code = error_payload.get("errorCode", "UnknownError")
        error_description = error_payload.get("errorDescription", "Unknown error")

        self.logger.error(
            "Error message received",
            presystem_id=self.presystem_id,
            error_code=error_code,
            error_description=error_description,
        )
        self._record_error(error_code)

    async def _periodic_charging_information(self) -> None:
        """Periodically send ProvideChargingInformation messages (every 15s)."""
        while self.running:
            try:
                await asyncio.sleep(15)  # 15-second interval per US-07

                if not self.running:
                    break

                # Build depot information from DB when available (Sprint 2)
                if self.db_pool and self.depot_id:
                    depot_info_list = await get_depot_charging_info(
                        self.db_pool,
                        self.depot_id,
                        static_pool=self._pools.static if self._pools else None,
                    )
                else:
                    depot_info_list = [
                        DepotInfo(
                            depot_id=self.depot_id or "default_depot",
                            charging_stations=[
                                ChargingPointInfo(
                                    charging_point_id=f"cp_{i}",
                                    charging_point_status="Available",
                                    current_power_kw=0.0,
                                )
                                for i in range(5)
                            ],
                        )
                    ]

                if not depot_info_list:
                    depot_info_list = [
                        DepotInfo(
                            depot_id=self.depot_id or "default_depot",
                            charging_stations=[],
                        )
                    ]

                # Build and send message
                message = build_provide_charging_information_message(
                    self.presystem_id,
                    depot_info_list,
                )

                await self.websocket.send(json.dumps(message))
                self._record_message_sent("ProvideChargingInformation")

                self.logger.debug(
                    "Sent ProvideChargingInformation",
                    presystem_id=self.presystem_id,
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(
                    "Error sending periodic charging information",
                    presystem_id=self.presystem_id,
                    error=str(e),
                )
                await asyncio.sleep(5)  # Wait before retry

    def _record_message_received(self, message_name: str) -> None:
        """Record message received metric.

        When running inside the legacy websocket_handler process we reuse its
        Prometheus metrics. When running under the main API service these
        imports are best-effort and silently skipped if unavailable.
        """
        # Import here to avoid circular dependency
        try:
            import sys
            from pathlib import Path

            # Add src to path for absolute imports
            src_path = Path(__file__).parent.parent.parent
            if str(src_path) not in sys.path:
                sys.path.insert(0, str(src_path))
            try:
                from websocket_handler.monitoring import MESSAGES_RECEIVED_TOTAL
            except Exception as import_error:  # pragma: no cover - defensive
                self.logger.debug(
                    "websocket_handler.monitoring not available; skipping metric",
                    error=str(import_error),
                )
                return

            MESSAGES_RECEIVED_TOTAL.labels(
                station_id=self.presystem_id,
                message_type=f"vdv463_{message_name}",
            ).inc()
        except Exception as e:
            self.logger.debug(f"Could not record metric: {e}")

    def _record_message_sent(self, message_name: str) -> None:
        """Record message sent metric (optional when websocket_handler is present)."""
        try:
            import sys
            from pathlib import Path

            src_path = Path(__file__).parent.parent.parent
            if str(src_path) not in sys.path:
                sys.path.insert(0, str(src_path))
            try:
                from websocket_handler.monitoring import MESSAGES_SENT_TOTAL
            except Exception as import_error:  # pragma: no cover - defensive
                self.logger.debug(
                    "websocket_handler.monitoring not available; skipping metric",
                    error=str(import_error),
                )
                return

            MESSAGES_SENT_TOTAL.labels(
                station_id=self.presystem_id,
                message_type=f"vdv463_{message_name}",
            ).inc()
        except Exception as e:
            self.logger.debug(f"Could not record metric: {e}")

    def _record_error(self, error_code: str) -> None:
        """Record error metric (optional when websocket_handler is present)."""
        try:
            import sys
            from pathlib import Path

            src_path = Path(__file__).parent.parent.parent
            if str(src_path) not in sys.path:
                sys.path.insert(0, str(src_path))
            try:
                from websocket_handler.monitoring import ERRORS_TOTAL
            except Exception as import_error:  # pragma: no cover - defensive
                self.logger.debug(
                    "websocket_handler.monitoring not available; skipping metric",
                    error=str(import_error),
                )
                return

            ERRORS_TOTAL.labels(
                error_type=f"vdv463_{error_code}",
                station_id=self.presystem_id,
            ).inc()
        except Exception as e:
            self.logger.debug(f"Could not record metric: {e}")

    async def _cleanup(self) -> None:
        """Cleanup handler resources."""
        self.running = False

        # Mark VDV 463 connection as disconnected (per dev plan Phase 5)
        if self.db_pool and self.depot_id:
            try:
                await vdv_repo.update_vdv463_connection_disconnect(
                    self.db_pool,
                    self.depot_id,
                    self.presystem_id,
                )
            except Exception as e:
                self.logger.debug(
                    "Failed to update VDV 463 connection disconnect",
                    presystem_id=self.presystem_id,
                    error=str(e),
                )
        # Cancel periodic task
        if self._charging_info_task:
            self._charging_info_task.cancel()
            try:
                await self._charging_info_task
            except asyncio.CancelledError:
                pass

        # Unregister connection
        if self.connection_manager:
            await self.connection_manager.unregister_connection(
                self.presystem_id,
                self.connection_id,
            )

        self.logger.info(
            "VDV 463 handler cleaned up",
            presystem_id=self.presystem_id,
            connection_id=self.connection_id,
        )
