"""VDV 463 WebSocket handler for BMS/ITCS communication.

Implements VDV 463 v1.1.0 protocol over WebSocket Secure (WSS).
See docs/PRD_v2_7_Building_Integration.md Section 9.6.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Optional, Dict, Any, Callable
from websockets import WebSocketServerProtocol
import websockets
import structlog

from .messages import (
    ValidationMode,
    VDVMessageEnvelope,
    VDVProvideChargingRequests,
    VDVError,
    parse_message,
    build_error,
    build_provide_charging_requests_response,
    build_provide_charging_information_message,
    ChargingRequest,
    DepotInfo,
    ChargingPointInfo,
    VDV463ValidationError,
)
from .vehicle_resolver import VehicleResolver


def get_logger(name: str) -> structlog.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)


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
        """
        self.presystem_id = presystem_id
        self.websocket = websocket
        self.connection_manager = connection_manager
        self.config = config
        self.depot_id = depot_id or getattr(config, "default_depot_id", None)
        self.validation_mode = validation_mode
        self.vehicle_resolver = vehicle_resolver or VehicleResolver()
        self.logger = get_logger(__name__)
        
        # Connection state
        self.connection_id: Optional[str] = None
        self.running = False
        self._charging_info_task: Optional[asyncio.Task] = None
        
        # In-memory storage for Sprint 1 (Sprint 2 will use database)
        self.charging_requests: Dict[str, ChargingRequest] = {}
    
    async def run(self) -> None:
        """Run the VDV 463 handler main loop."""
        self.connection_id = f"vdv463_{self.presystem_id}_{id(self)}"
        client_ip = (
            self.websocket.remote_address[0] 
            if self.websocket.remote_address 
            else "unknown"
        )
        
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
        self._charging_info_task = asyncio.create_task(
            self._periodic_charging_information()
        )
        
        try:
            # Main message loop
            while self.running:
                try:
                    message = await asyncio.wait_for(
                        self.websocket.recv(),
                        timeout=1.0
                    )
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
    
    async def _handle_provide_charging_requests(
        self, envelope: VDVMessageEnvelope
    ) -> None:
        """Handle ProvideChargingRequests message."""
        payload = envelope.payload
        charging_request_list = payload.get("chargingRequestList", [])
        
        parsed_requests = []
        
        for req_data in charging_request_list:
            try:
                # Parse charging request
                charging_request = ChargingRequest(
                    vehicle_external_id=req_data["vehicleId"],
                    charging_point_id=req_data.get("chargingPointId"),
                    arrival_time=req_data["arrivalTime"],
                    departure_time=req_data["departureTime"],
                    min_target_soc=req_data.get("minTargetSoc", 0.0),
                    max_target_soc=req_data.get("maxTargetSoc", 1.0),
                    priority=req_data.get("chargingPriority"),
                    manual_preconditioning=req_data.get("manualPreconditioning"),
                    automatic_preconditioning=req_data.get("automaticPreconditioning"),
                    validation_status=envelope.validation_status,
                )
                
                # Resolve vehicle ID (Sprint 1: may return None)
                vehicle_id = await self.vehicle_resolver.resolve_vehicle_id(
                    charging_request.vehicle_external_id,
                    self.depot_id,
                )
                
                # Store request (Sprint 1: in-memory; Sprint 2: database)
                request_key = f"{charging_request.vehicle_external_id}_{charging_request.arrival_time}"
                self.charging_requests[request_key] = charging_request
                
                parsed_requests.append(charging_request)
                
                self.logger.info(
                    "Processed charging request",
                    presystem_id=self.presystem_id,
                    vehicle_external_id=charging_request.vehicle_external_id,
                    vehicle_id=vehicle_id,
                    arrival_time=charging_request.arrival_time,
                    departure_time=charging_request.departure_time,
                )
            
            except KeyError as e:
                self.logger.error(
                    "Missing required field in charging request",
                    presystem_id=self.presystem_id,
                    missing_field=str(e),
                    request_data=req_data,
                )
                # Continue processing other requests
                continue
            except Exception as e:
                self.logger.error(
                    "Error processing charging request",
                    presystem_id=self.presystem_id,
                    error=str(e),
                    error_type=type(e).__name__,
                )
                continue
        
        # Send response
        response = build_provide_charging_requests_response(envelope)
        await self.websocket.send(json.dumps(response))
        self._record_message_sent("ProvideChargingRequests")
    
    async def _handle_boot_notification(
        self, envelope: VDVMessageEnvelope
    ) -> None:
        """Handle BootNotification message."""
        self.logger.info(
            "BootNotification received",
            presystem_id=self.presystem_id,
            source=envelope.source,
        )
        
        # Send boot notification response
        response = [
            2,  # Confirmation
            "CMS",
            envelope.presystem_id,
            datetime.utcnow().isoformat() + "Z",
            f"boot-response-{envelope.message_id}",
            "BootNotification",
            {
                "status": "Accepted",
                "currentTime": datetime.utcnow().isoformat() + "Z",
            },
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
                
                # Build depot information (Sprint 1: placeholder data)
                # Sprint 2: Query real depot/charger state
                depot_info = DepotInfo(
                    depot_id=self.depot_id or "default_depot",
                    charging_stations=[
                        ChargingPointInfo(
                            charging_point_id=f"cp_{i}",
                            status="Available",
                            current_power_kw=0.0,
                        )
                        for i in range(5)  # Placeholder: 5 charging points
                    ],
                )
                
                # Build and send message
                message = build_provide_charging_information_message(
                    self.presystem_id,
                    [depot_info],
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
        """Record message received metric."""
        # Import here to avoid circular dependency
        try:
            import sys
            from pathlib import Path
            # Add src to path for absolute imports
            src_path = Path(__file__).parent.parent.parent
            if str(src_path) not in sys.path:
                sys.path.insert(0, str(src_path))
            from websocket_handler.monitoring import MESSAGES_RECEIVED_TOTAL
            MESSAGES_RECEIVED_TOTAL.labels(
                station_id=self.presystem_id,
                message_type=f"vdv463_{message_name}",
            ).inc()
        except Exception as e:
            self.logger.debug(f"Could not record metric: {e}")
    
    def _record_message_sent(self, message_name: str) -> None:
        """Record message sent metric."""
        try:
            import sys
            from pathlib import Path
            src_path = Path(__file__).parent.parent.parent
            if str(src_path) not in sys.path:
                sys.path.insert(0, str(src_path))
            from websocket_handler.monitoring import MESSAGES_SENT_TOTAL
            MESSAGES_SENT_TOTAL.labels(
                station_id=self.presystem_id,
                message_type=f"vdv463_{message_name}",
            ).inc()
        except Exception as e:
            self.logger.debug(f"Could not record metric: {e}")
    
    def _record_error(self, error_code: str) -> None:
        """Record error metric."""
        try:
            import sys
            from pathlib import Path
            src_path = Path(__file__).parent.parent.parent
            if str(src_path) not in sys.path:
                sys.path.insert(0, str(src_path))
            from websocket_handler.monitoring import ERRORS_TOTAL
            ERRORS_TOTAL.labels(
                error_type=f"vdv463_{error_code}",
                station_id=self.presystem_id,
            ).inc()
        except Exception as e:
            self.logger.debug(f"Could not record metric: {e}")
    
    async def _cleanup(self) -> None:
        """Cleanup handler resources."""
        self.running = False
        
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
