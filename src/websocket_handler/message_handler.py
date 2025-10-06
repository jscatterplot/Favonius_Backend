"""OCPP 2.1 message handler implementation."""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .config import Config
from .monitoring import get_logger
from .timescale_client import TimescaleClient


class MessageHandler:
    """Handles OCPP 2.1 message processing and routing."""
    
    def __init__(
        self, 
        connection_manager: 'ConnectionManager',
        config: Config,
        timescale_client: TimescaleClient
    ):
        """Initialize message handler."""
        # Redis removed for simplification
        self.connection_manager = connection_manager
        self.config = config
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        
        # Message handlers mapping
        self.handlers = {
            "BootNotification": self._handle_boot_notification,
            "StatusNotification": self._handle_status_notification,
            "TransactionEvent": self._handle_transaction_event,
            "MeterValues": self._handle_meter_values,
            "NotifyEVChargingNeeds": self._handle_ev_charging_needs,
            "NotifyEVChargingSchedule": self._handle_ev_charging_schedule,
            "Heartbeat": self._handle_heartbeat,
            "Authorize": self._handle_authorize,
            "DataTransfer": self._handle_data_transfer,
        }
    
    async def handle_message(
        self, 
        station_id: str, 
        message_type_id: int, 
        unique_id: str,
        action: str, 
        payload: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Route and handle OCPP message."""
        self.logger.debug(f"Handling {action} from {station_id}")
        
        # Only handle CALL messages (type 2)
        if message_type_id != 2:
            return None
        
        # Get handler
        handler = self.handlers.get(action)
        if not handler:
            self.logger.warning(f"No handler for message type: {action}")
            return {"status": "Rejected"}
        
        try:
            # Process message
            response = await handler(station_id, payload, unique_id)
            
            # Log message processing
            await self._log_message_event(station_id, action, payload, response)
            
            return response
            
        except Exception as e:
            self.logger.error(f"Error handling {action} from {station_id}: {e}")
            return {"status": "Rejected"}
    
    async def _handle_boot_notification(
        self, station_id: str, payload: Dict[str, Any], unique_id: str
    ) -> Dict[str, Any]:
        """Handle BootNotification message."""
        # Redis integration removed for simplification
        
        # Response
        return {
            "status": "Accepted",
            "currentTime": datetime.now(timezone.utc).isoformat(),
            "interval": self.config.websocket.heartbeat_interval,
            "statusInfo": {
                "reasonCode": "NoError",
                "additionalInfo": "Boot notification accepted"
            }
        }
    
    async def _handle_status_notification(
        self, station_id: str, payload: Dict[str, Any], unique_id: str
    ) -> Dict[str, Any]:
        """Handle StatusNotification message."""
        timestamp = payload.get("timestamp", datetime.now(timezone.utc).isoformat())
        connector_status = payload.get("connectorStatus")
        evse_id = payload.get("evseId", 1)
        connector_id = payload.get("connectorId", 1)
        
        # Redis status update removed for simplification
        status_data = {
            "status": connector_status,
            "evse_id": evse_id,
            "connector_id": connector_id,
            "timestamp": timestamp,
            "error_code": payload.get("errorCode", "NoError"),
        }
        
        return {}  # Empty response for StatusNotification
    
    async def _handle_transaction_event(
        self, station_id: str, payload: Dict[str, Any], unique_id: str
    ) -> Dict[str, Any]:
        """Handle TransactionEvent message."""
        timestamp = payload.get("timestamp", datetime.now(timezone.utc).isoformat())
        event_type = payload.get("eventType")  # Started, Updated, Ended
        transaction_info = payload.get("transactionInfo", {})
        evse_id = payload.get("evseId")
        
        transaction_id = transaction_info.get("transactionId")
        
        # Redis transaction update removed for simplification
        transaction_data = {
            "transaction_id": transaction_id,
            "event_type": event_type,
            "timestamp": timestamp,
            "evse_id": evse_id,
            "charging_state": transaction_info.get("chargingState"),
            "stopped_reason": transaction_info.get("stoppedReason"),
            "remote_start_id": transaction_info.get("remoteStartId"),
        }
        
        # Handle meter values if present
        meter_values = payload.get("meterValue", [])
        for meter_value in meter_values:
            await self._process_meter_values(station_id, evse_id, meter_value, transaction_id)
        
        return {}  # Empty response for TransactionEvent
    
    async def _handle_meter_values(
        self, station_id: str, payload: Dict[str, Any], unique_id: str
    ) -> Dict[str, Any]:
        """Handle MeterValues message."""
        meter_values = payload.get("meterValue", [])
        
        for meter_value in meter_values:
            await self._process_meter_values(station_id, 1, meter_value)
        
        return {}  # Empty response for MeterValues
    
    async def _process_meter_values(
        self, station_id: str, evse_id: int, meter_value: Dict[str, Any], 
        transaction_id: Optional[str] = None
    ) -> None:
        """Process individual meter value readings."""
        timestamp = meter_value.get("timestamp", datetime.now(timezone.utc).isoformat())
        sampled_values = meter_value.get("sampledValue", [])
        
        telemetry_data = {
            "timestamp": timestamp,
            "station_id": station_id,
            "evse_id": evse_id,
            "transaction_id": transaction_id,
        }
        
        # Process each sampled value
        for sampled_value in sampled_values:
            measurand = sampled_value.get("measurand", "Energy.Active.Import.Register")
            value = sampled_value.get("value")
            unit = sampled_value.get("unitOfMeasure", {}).get("unit", "Wh")
            phase = sampled_value.get("phase")
            location = sampled_value.get("location", "Outlet")
            
            # Map OCPP measurands to our telemetry format
            if measurand == "Power.Active.Import":
                telemetry_data["power_kw"] = float(value) / 1000 if unit == "W" else float(value)
            elif measurand == "Power.Active.Export":
                telemetry_data["power_kw"] = -float(value) / 1000 if unit == "W" else -float(value)
            elif measurand == "Energy.Active.Import.Register":
                telemetry_data["energy_kwh"] = float(value) / 1000 if unit == "Wh" else float(value)
            elif measurand == "SoC":
                telemetry_data["soc_percent"] = float(value)
            elif measurand == "Voltage":
                telemetry_data["voltage_v"] = float(value)
            elif measurand == "Current.Import":
                telemetry_data["current_a"] = float(value)
            elif measurand == "Current.Export":
                telemetry_data["current_a"] = -float(value)
            elif measurand == "Frequency":
                telemetry_data["frequency_hz"] = float(value)
            elif measurand == "Temperature":
                telemetry_data["temperature_c"] = float(value)
            elif measurand == "Power.Reactive.Import":
                telemetry_data["reactive_power_kvar"] = float(value) / 1000 if unit == "var" else float(value)
            elif measurand == "Power.Factor":
                telemetry_data["power_factor"] = float(value)
        
        # Redis telemetry update removed for simplification
        
        # Write telemetry directly to TimescaleDB
        try:
            await self.timescale_client.insert_telemetry_batch([
                {
                    'time': datetime.fromisoformat(timestamp.replace('Z', '+00:00')),
                    'station_id': station_id,
                    'evse_id': evse_id,
                    'connector_id': meter_value.get('connectorId', 1),
                    'session_id': transaction_id,
                    'power_kw': telemetry_data.get('power_kw'),
                    'energy_kwh': telemetry_data.get('energy_kwh'),
                    'voltage_v': telemetry_data.get('voltage_v'),
                    'current_a': telemetry_data.get('current_a'),
                    'frequency_hz': telemetry_data.get('frequency_hz'),
                    'soc_percent': telemetry_data.get('soc_percent'),
                    'temperature_c': telemetry_data.get('temperature_c'),
                    'grid_frequency_mhz': telemetry_data.get('grid_frequency_mhz'),
                    'reactive_power_kvar': telemetry_data.get('reactive_power_kvar'),
                    'power_factor': telemetry_data.get('power_factor')
                }
            ])
        except Exception as e:
            self.logger.error(f"Failed to write telemetry to TimescaleDB: {e}")
    
    async def _handle_ev_charging_needs(
        self, station_id: str, payload: Dict[str, Any], unique_id: str
    ) -> Dict[str, Any]:
        """Handle NotifyEVChargingNeeds message."""
        evse_id = payload.get("evseId", 1)
        charging_needs = payload.get("chargingNeeds", {})
        
        # Extract EV charging requirements
        ev_needs = {
            "requested_energy_transfer": charging_needs.get("requestedEnergyTransfer"),
            "departure_time": charging_needs.get("departureTime"),
            "ac_charging_parameters": charging_needs.get("acChargingParameters"),
            "dc_charging_parameters": charging_needs.get("dcChargingParameters"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        # Redis EV needs storage removed for simplification
        
        return {"status": "Accepted"}
    
    async def _handle_ev_charging_schedule(
        self, station_id: str, payload: Dict[str, Any], unique_id: str
    ) -> Dict[str, Any]:
        """Handle NotifyEVChargingSchedule message."""
        time_base = payload.get("timeBase")
        evse_id = payload.get("evseId", 1)
        charging_schedule = payload.get("chargingSchedule", {})
        
        # Store EV's proposed schedule
        schedule_data = {
            "time_base": time_base,
            "charging_schedule_period": charging_schedule.get("chargingSchedulePeriod", []),
            "duration": charging_schedule.get("duration"),
            "start_schedule": charging_schedule.get("startSchedule"),
            "charging_rate_unit": charging_schedule.get("chargingRateUnit"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        
        # Redis EV schedule storage removed for simplification
        
        return {"status": "Accepted"}
    
    async def _handle_heartbeat(
        self, station_id: str, payload: Dict[str, Any], unique_id: str
    ) -> Dict[str, Any]:
        """Handle Heartbeat message."""
        current_time = datetime.now(timezone.utc).isoformat()
        
        # Redis heartbeat update removed for simplification
        
        return {"currentTime": current_time}
    
    async def _handle_authorize(
        self, station_id: str, payload: Dict[str, Any], unique_id: str
    ) -> Dict[str, Any]:
        """Handle Authorize message."""
        id_token = payload.get("idToken", {})
        id_token_value = id_token.get("idToken")
        id_token_type = id_token.get("type", "ISO14443")
        
        # For now, accept all authorizations
        # In production, this would check against user database
        return {
            "idTokenInfo": {
                "status": "Accepted",
                "expiryDate": None,
                "groupIdToken": None,
                "language1": "en",
                "language2": None,
                "personalMessage": {
                    "format": "UTF8",
                    "language": "en",
                    "content": "Authorized"
                }
            }
        }
    
    async def _handle_data_transfer(
        self, station_id: str, payload: Dict[str, Any], unique_id: str
    ) -> Dict[str, Any]:
        """Handle DataTransfer message."""
        vendor_id = payload.get("vendorId")
        message_id = payload.get("messageId")
        data = payload.get("data")
        
        # Log data transfer
        self.logger.info(f"DataTransfer from {station_id}: {vendor_id}.{message_id}")
        
        # Store custom data if needed
        if vendor_id and message_id:
            transfer_data = {
                "vendor_id": vendor_id,
                "message_id": message_id,
                "data": data,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            # Redis data transfer storage removed for simplification
        
        return {
            "status": "Accepted",
            "data": None
        }
    
    async def send_charging_profile(
        self, station_id: str, evse_id: int, charging_profile: Dict[str, Any]
    ) -> bool:
        """Send SetChargingProfile command to charger."""
        try:
            # Get connection for station
            connection = await self.connection_manager.get_connection(station_id)
            if not connection:
                self.logger.warning(f"No connection for station {station_id}")
                return False
            
            # Create SetChargingProfile message
            unique_id = str(uuid.uuid4())
            message = [
                2,  # CALL
                unique_id,
                "SetChargingProfile",
                {
                    "evseId": evse_id,
                    "chargingProfile": charging_profile
                }
            ]
            
            # Send message
            raw_message = json.dumps(message, separators=(',', ':'))
            await connection.send(raw_message)
            
            # Redis profile storage removed for simplification
            
            self.logger.info(f"Sent charging profile to {station_id}, EVSE {evse_id}")
            return True
            
        except Exception as e:
            self.logger.error(f"Failed to send charging profile to {station_id}: {e}")
            return False
    
    async def _log_message_event(
        self, station_id: str, action: str, payload: Dict[str, Any], 
        response: Optional[Dict[str, Any]]
    ) -> None:
        """Log message processing event."""
        event = {
            "event_type": "message_processed",
            "station_id": station_id,
            "action": action,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "processing_time_ms": None,  # Would be calculated if needed
            "success": response is not None,
        }
        
        # No external event bus in simplified architecture
