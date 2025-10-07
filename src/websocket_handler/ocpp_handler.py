"""Enhanced OCPP 2.1 message handler using the official OCPP library."""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ocpp.routing import on
from ocpp.v21 import ChargePoint as OCPPChargePoint, call_result
from ocpp.v21.enums import Action, ConnectorStatusEnumType, TransactionEventEnumType
from ocpp.v21.datatypes import ChargingStationType, StatusInfoType
from ocpp.exceptions import OCPPError, NotImplementedError, NotSupportedError

from .config import Config
from .monitoring import get_logger
from .timescale_client import TimescaleClient
from .connection_manager import ConnectionManager


class EnhancedOCPPChargePoint(OCPPChargePoint):
    """Enhanced OCPP 2.1 charge point with V2G capabilities."""
    
    def __init__(
        self, 
        station_id: str, 
        connection, 
        config: Config,
        timescale_client: TimescaleClient,
        connection_manager: ConnectionManager
    ):
        """Initialize enhanced charge point."""
        super().__init__(station_id, connection)
        self.config = config
        self.timescale_client = timescale_client
        self.connection_manager = connection_manager
        self.logger = get_logger(__name__)
        
        # Station state
        self.station_info: Optional[Dict] = None
        self.connector_states: Dict[int, str] = {}
        self.active_transactions: Dict[int, str] = {}
        self.last_heartbeat = time.time()
        
        # V2G specific state
        self.charging_profiles: Dict[int, Dict] = {}
        self.der_control_active = False
        self.v2x_capabilities = {
            "central_setpoint": True,
            "local_frequency": False,
            "local_load_balancing": False,
            "external_setpoint": False
        }
    
    @on(Action.boot_notification)
    def on_boot_notification(
        self, 
        charging_station: ChargingStationType, 
        reason: str, 
        **kwargs
    ):
        """Handle BootNotification message."""
        self.logger.info(f"Boot notification from {self.id}: {reason}")
        
        # Store station information
        self.station_info = {
            "serial_number": charging_station.serial_number,
            "model": charging_station.model,
            "vendor_name": charging_station.vendor_name,
            "firmware_version": getattr(charging_station, 'firmware_version', None),
            "modem": getattr(charging_station, 'modem', None),
            "boot_reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        # Store in database
        asyncio.create_task(self._store_station_info())
        
        return call_result.BootNotification(
            current_time=datetime.now(timezone.utc).isoformat(),
            interval=self.config.websocket.heartbeat_interval,
            status="Accepted",
            status_info=StatusInfoType(
                reason_code="NoError",
                additional_info="Station registered successfully"
            )
        )
    
    @on(Action.status_notification)
    def on_status_notification(
        self,
        connector_id: int,
        error_code: str,
        status: ConnectorStatusEnumType,
        timestamp: str,
        **kwargs
    ):
        """Handle StatusNotification message."""
        self.logger.debug(f"Status notification from {self.id}: Connector {connector_id} = {status}")
        
        # Update connector state
        self.connector_states[connector_id] = status.value
        
        # Store status in database
        asyncio.create_task(self._store_connector_status(
            connector_id, status.value, error_code, timestamp
        ))
        
        return call_result.StatusNotification()
    
    @on(Action.transaction_event)
    def on_transaction_event(
        self,
        event_type: TransactionEventEnumType,
        timestamp: str,
        transaction_info: Dict,
        **kwargs
    ):
        """Handle TransactionEvent message."""
        self.logger.info(f"Transaction event from {self.id}: {event_type.value}")
        
        transaction_id = transaction_info.get("transactionId")
        evse_id = kwargs.get("evseId", 1)
        connector_id = kwargs.get("connectorId", 1)
        
        # Update active transactions
        if event_type == TransactionEventEnumType.started:
            self.active_transactions[connector_id] = transaction_id
        elif event_type == TransactionEventEnumType.ended:
            self.active_transactions.pop(connector_id, None)
        
        # Store transaction in database
        asyncio.create_task(self._store_transaction_event(
            transaction_id, event_type.value, timestamp, transaction_info, evse_id, connector_id
        ))
        
        return call_result.TransactionEvent()
    
    @on(Action.meter_values)
    def on_meter_values(
        self,
        evse_id: int,
        meter_value: list,
        **kwargs
    ):
        """Handle MeterValues message."""
        self.logger.debug(f"Meter values from {self.id}: EVSE {evse_id}")
        
        # Process meter values
        asyncio.create_task(self._process_meter_values(evse_id, meter_value))
        
        return call_result.MeterValues()
    
    @on(Action.heartbeat)
    def on_heartbeat(self, **kwargs):
        """Handle Heartbeat message."""
        self.last_heartbeat = time.time()
        self.logger.debug(f"Heartbeat from {self.id}")
        
        # Update connection manager
        asyncio.create_task(self.connection_manager.update_heartbeat(self.id))
        
        return call_result.Heartbeat(
            current_time=datetime.now(timezone.utc).isoformat()
        )
    
    @on(Action.authorize)
    def on_authorize(self, id_token: Dict, **kwargs):
        """Handle Authorize message."""
        self.logger.info(f"Authorization request from {self.id}")
        
        # For now, accept all authorizations
        # In production, this would check against user database
        return call_result.Authorize(
            id_token_info={
                "status": "Accepted",
                "expiry_date": None,
                "group_id_token": None,
                "personal_message": {
                    "format": "UTF8",
                    "language": "en",
                    "content": "Authorized"
                }
            }
        )
    
    @on(Action.data_transfer)
    def on_data_transfer(self, vendor_id: str, message_id: str, data: Optional[str] = None, **kwargs):
        """Handle DataTransfer message."""
        self.logger.info(f"Data transfer from {self.id}: {vendor_id}.{message_id}")
        
        # Store custom data if needed
        asyncio.create_task(self._store_data_transfer(vendor_id, message_id, data))
        
        return call_result.DataTransfer(
            status="Accepted",
            data=None
        )
    
    @on(Action.notify_ev_charging_needs)
    def on_notify_ev_charging_needs(
        self,
        evse_id: int,
        charging_needs: Dict,
        **kwargs
    ):
        """Handle NotifyEVChargingNeeds message."""
        self.logger.info(f"EV charging needs from {self.id}: EVSE {evse_id}")
        
        # Store EV charging needs
        asyncio.create_task(self._store_ev_charging_needs(evse_id, charging_needs))
        
        return call_result.NotifyEVChargingNeeds()
    
    @on(Action.notify_ev_charging_schedule)
    def on_notify_ev_charging_schedule(
        self,
        evse_id: int,
        time_base: str,
        charging_schedule: Dict,
        **kwargs
    ):
        """Handle NotifyEVChargingSchedule message."""
        self.logger.info(f"EV charging schedule from {self.id}: EVSE {evse_id}")
        
        # Store EV charging schedule
        asyncio.create_task(self._store_ev_charging_schedule(evse_id, time_base, charging_schedule))
        
        return call_result.NotifyEVChargingSchedule()
    
    async def send_charging_profile(self, evse_id: int, charging_profile: Dict) -> bool:
        """Send SetChargingProfile command to charger."""
        try:
            from ocpp.v21 import call
            
            request = call.SetChargingProfile(
                evse_id=evse_id,
                charging_profile=charging_profile
            )
            
            response = await self.call(request)
            
            if response.status == "Accepted":
                self.logger.info(f"Charging profile accepted for {self.id}, EVSE {evse_id}")
                return True
            else:
                self.logger.warning(f"Charging profile rejected for {self.id}: {response.status}")
                return False
                
        except Exception as e:
            self.logger.error(f"Failed to send charging profile to {self.id}: {e}")
            return False
    
    async def send_der_control(self, der_control: Dict) -> bool:
        """Send SetDERControl command for V2G operations."""
        try:
            from ocpp.v21 import call
            
            request = call.SetDERControl(
                der_control=der_control
            )
            
            response = await self.call(request)
            
            if response.status == "Accepted":
                self.der_control_active = True
                self.logger.info(f"DER control accepted for {self.id}")
                return True
            else:
                self.logger.warning(f"DER control rejected for {self.id}: {response.status}")
                return False
                
        except Exception as e:
            self.logger.error(f"Failed to send DER control to {self.id}: {e}")
            return False
    
    async def clear_der_control(self) -> bool:
        """Clear DER control for V2G operations."""
        try:
            from ocpp.v21 import call
            
            request = call.ClearDERControl()
            
            response = await self.call(request)
            
            if response.status == "Accepted":
                self.der_control_active = False
                self.logger.info(f"DER control cleared for {self.id}")
                return True
            else:
                self.logger.warning(f"Failed to clear DER control for {self.id}: {response.status}")
                return False
                
        except Exception as e:
            self.logger.error(f"Failed to clear DER control for {self.id}: {e}")
            return False
    
    async def _store_station_info(self):
        """Store station information in database."""
        try:
            if self.station_info:
                await self.timescale_client.insert_station_info(self.station_info)
        except Exception as e:
            self.logger.error(f"Failed to store station info: {e}")
    
    async def _store_connector_status(
        self, 
        connector_id: int, 
        status: str, 
        error_code: str, 
        timestamp: str
    ):
        """Store connector status in database."""
        try:
            status_data = {
                "station_id": self.id,
                "connector_id": connector_id,
                "status": status,
                "error_code": error_code,
                "timestamp": datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            }
            await self.timescale_client.insert_connector_status(status_data)
        except Exception as e:
            self.logger.error(f"Failed to store connector status: {e}")
    
    async def _store_transaction_event(
        self,
        transaction_id: str,
        event_type: str,
        timestamp: str,
        transaction_info: Dict,
        evse_id: int,
        connector_id: int
    ):
        """Store transaction event in database."""
        try:
            transaction_data = {
                "transaction_id": transaction_id,
                "event_type": event_type,
                "timestamp": datetime.fromisoformat(timestamp.replace('Z', '+00:00')),
                "station_id": self.id,
                "evse_id": evse_id,
                "connector_id": connector_id,
                "charging_state": transaction_info.get("chargingState"),
                "stopped_reason": transaction_info.get("stoppedReason"),
                "remote_start_id": transaction_info.get("remoteStartId"),
            }
            await self.timescale_client.insert_transaction_event(transaction_data)
        except Exception as e:
            self.logger.error(f"Failed to store transaction event: {e}")
    
    async def _process_meter_values(self, evse_id: int, meter_values: list):
        """Process meter values and store in database."""
        try:
            telemetry_batch = []
            
            for meter_value in meter_values:
                timestamp = meter_value.get("timestamp", datetime.now(timezone.utc).isoformat())
                sampled_values = meter_value.get("sampledValue", [])
                
                telemetry_data = {
                    "time": datetime.fromisoformat(timestamp.replace('Z', '+00:00')),
                    "station_id": self.id,
                    "evse_id": evse_id,
                    "connector_id": meter_value.get("connectorId", 1),
                    "session_id": self.active_transactions.get(1),
                }
                
                # Process each sampled value
                for sampled_value in sampled_values:
                    measurand = sampled_value.get("measurand", "Energy.Active.Import.Register")
                    value = sampled_value.get("value")
                    unit = sampled_value.get("unitOfMeasure", {}).get("unit", "Wh")
                    
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
                
                telemetry_batch.append(telemetry_data)
            
            if telemetry_batch:
                await self.timescale_client.insert_telemetry_batch(telemetry_batch)
                
        except Exception as e:
            self.logger.error(f"Failed to process meter values: {e}")
    
    async def _store_data_transfer(self, vendor_id: str, message_id: str, data: Optional[str]):
        """Store data transfer information."""
        try:
            transfer_data = {
                "station_id": self.id,
                "vendor_id": vendor_id,
                "message_id": message_id,
                "data": data,
                "timestamp": datetime.now(timezone.utc)
            }
            await self.timescale_client.insert_data_transfer(transfer_data)
        except Exception as e:
            self.logger.error(f"Failed to store data transfer: {e}")
    
    async def _store_ev_charging_needs(self, evse_id: int, charging_needs: Dict):
        """Store EV charging needs."""
        try:
            needs_data = {
                "station_id": self.id,
                "evse_id": evse_id,
                "requested_energy_transfer": charging_needs.get("requestedEnergyTransfer"),
                "departure_time": charging_needs.get("departureTime"),
                "ac_charging_parameters": charging_needs.get("acChargingParameters"),
                "dc_charging_parameters": charging_needs.get("dcChargingParameters"),
                "timestamp": datetime.now(timezone.utc)
            }
            await self.timescale_client.insert_ev_charging_needs(needs_data)
        except Exception as e:
            self.logger.error(f"Failed to store EV charging needs: {e}")
    
    async def _store_ev_charging_schedule(self, evse_id: int, time_base: str, charging_schedule: Dict):
        """Store EV charging schedule."""
        try:
            schedule_data = {
                "station_id": self.id,
                "evse_id": evse_id,
                "time_base": time_base,
                "charging_schedule_period": charging_schedule.get("chargingSchedulePeriod", []),
                "duration": charging_schedule.get("duration"),
                "start_schedule": charging_schedule.get("startSchedule"),
                "charging_rate_unit": charging_schedule.get("chargingRateUnit"),
                "timestamp": datetime.now(timezone.utc)
            }
            await self.timescale_client.insert_ev_charging_schedule(schedule_data)
        except Exception as e:
            self.logger.error(f"Failed to store EV charging schedule: {e}")
    
    def get_station_info(self) -> Optional[Dict]:
        """Get station information."""
        return self.station_info
    
    def get_connector_states(self) -> Dict[int, str]:
        """Get connector states."""
        return self.connector_states.copy()
    
    def get_active_transactions(self) -> Dict[int, str]:
        """Get active transactions."""
        return self.active_transactions.copy()
    
    def is_der_control_active(self) -> bool:
        """Check if DER control is active."""
        return self.der_control_active
    
    def get_v2x_capabilities(self) -> Dict[str, bool]:
        """Get V2X capabilities."""
        return self.v2x_capabilities.copy()