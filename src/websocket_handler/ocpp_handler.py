"""Enhanced OCPP 2.1 message handler using the official OCPP library."""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

from ocpp.routing import on
from ocpp.v201 import ChargePoint as OCPPChargePoint, call_result
from ocpp.v201.enums import Action, ConnectorStatusEnumType, TransactionEventEnumType

# Optional OCPP extension actions (may be missing in ocpp package)
def _opt_action(name: str):
    return getattr(Action, name, None)
from ocpp.v201.datatypes import ChargingStationType, StatusInfoType, IdTokenType
from ocpp.exceptions import OCPPError, NotImplementedError, NotSupportedError

from .config import Config
from .monitoring import get_logger
from .timescale_client import TimescaleClient
from .connection_manager import ConnectionManager
from .task_supervisor import TaskSupervisor
from .device_model import DeviceModel
from .charging_profile_manager import ChargingProfileManager
from .transaction_manager import TransactionManager
from .certificate_manager import CertificateManager, CertificateType
from .security_manager import SecurityManager, SecurityConfig
from .diagnostics_firmware import DiagnosticsManager, FirmwareManager
from .monitoring_manager import MonitoringManager
from .display_manager import DisplayManager
from .tariff_manager import TariffManager
from .privacy_manager import PrivacyManager


class EnhancedOCPPChargePoint(OCPPChargePoint):
    """Enhanced OCPP 2.1 charge point with V2G capabilities."""
    
    def __init__(
        self, 
        station_id: str, 
        connection, 
        config: Config,
        timescale_client: TimescaleClient,
        connection_manager: ConnectionManager,
        charging_profile_manager: Optional[ChargingProfileManager] = None,
        der_control_manager: Optional[Any] = None,
        priority_charging_manager: Optional[Any] = None,
        external_control_manager: Optional[Any] = None,
        certificate_manager: Optional[CertificateManager] = None,
        # v2x_controller removed - out of scope for MVP per PRD Section 1.2
    ):
        """Initialize enhanced charge point with V2G capabilities."""
        super().__init__(station_id, connection)
        self.config = config
        self.timescale_client = timescale_client
        self.connection_manager = connection_manager
        self.logger = get_logger(__name__)
        
        # V2G managers
        self.charging_profile_manager = charging_profile_manager
        self.der_control_manager = der_control_manager
        self.priority_charging_manager = priority_charging_manager
        self.external_control_manager = external_control_manager
        self.certificate_manager = certificate_manager
        # V2X removed - out of scope for MVP
        
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
        
        # Initialize managers
        self.device_model = DeviceModel(timescale_client)
        self.charging_profile_manager = ChargingProfileManager(timescale_client)
        self.transaction_manager = TransactionManager(timescale_client)
        self.certificate_manager = CertificateManager(timescale_client)
        
        # Initialize security manager
        security_config = SecurityConfig()
        self.security_manager = SecurityManager(timescale_client, security_config)
        
        # Initialize diagnostics and firmware managers
        self.diagnostics_manager = DiagnosticsManager(timescale_client)
        self.firmware_manager = FirmwareManager(timescale_client)
        
        # Initialize monitoring manager
        self.monitoring_manager = MonitoringManager(timescale_client)
        
        # Initialize display manager
        self.display_manager = DisplayManager(timescale_client)
        
        # Initialize tariff manager
        self.tariff_manager = TariffManager(timescale_client)
        
        # Initialize privacy manager
        self.privacy_manager = PrivacyManager(timescale_client)

        # Task supervisor for fire-and-forget tasks with error handling
        self._task_supervisor = TaskSupervisor(f"ocpp:{station_id}", max_concurrent=50)
    
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
            "station_id": self.id,
            "serial_number": charging_station.serial_number,
            "model": charging_station.model,
            "vendor_name": charging_station.vendor_name,
            "firmware_version": getattr(charging_station, 'firmware_version', None),
            "modem": getattr(charging_station, 'modem', None),
            "boot_reason": reason,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        # Store in database and initialize device model concurrently
        # Use task supervisor to track tasks and handle errors
        self._task_supervisor.create_task(self._store_station_info(), "store_station_info")
        self._task_supervisor.create_task(self._initialize_complete_device_model(), "init_device_model")
        
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
        self._task_supervisor.create_task(
            self._store_connector_status(connector_id, status.value, error_code, timestamp),
            f"store_connector_status:{connector_id}"
        )
        
        return call_result.StatusNotification()
    
    @on(Action.transaction_event)
    def on_transaction_event(
        self,
        event_type: TransactionEventEnumType,
        timestamp: str,
        transaction_info: Dict,
        **kwargs
    ):
        """Handle TransactionEvent message with V2G extensions."""
        self.logger.info(f"Transaction event from {self.id}: {event_type.value}")
        
        transaction_id = transaction_info.get("transactionId")
        evse_id = kwargs.get("evseId", 1)
        connector_id = kwargs.get("connectorId", 1)
        
        # Update active transactions
        if event_type == TransactionEventEnumType.started:
            self.active_transactions[connector_id] = transaction_id
        elif event_type == TransactionEventEnumType.ended:
            self.active_transactions.pop(connector_id, None)

        # Store transaction in database with V2G extensions
        self._task_supervisor.create_task(
            self._store_transaction_event_v2g(
                transaction_id, event_type.value, timestamp, transaction_info, evse_id, connector_id, kwargs
            ),
            f"store_transaction:{transaction_id}"
        )
        
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
        self._task_supervisor.create_task(
            self._process_meter_values(evse_id, meter_value),
            f"process_meter_values:{evse_id}"
        )
        
        return call_result.MeterValues()
    
    @on(Action.heartbeat)
    def on_heartbeat(self, **kwargs):
        """Handle Heartbeat message."""
        self.last_heartbeat = time.time()
        self.logger.debug(f"Heartbeat from {self.id}")

        # Update connection manager
        self._task_supervisor.create_task(
            self.connection_manager.update_heartbeat(self.id),
            "update_heartbeat"
        )
        
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
        self._task_supervisor.create_task(
            self._store_data_transfer(vendor_id, message_id, data),
            f"store_data_transfer:{message_id}"
        )
        
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
        self._task_supervisor.create_task(
            self._store_ev_charging_needs(evse_id, charging_needs),
            f"store_ev_charging_needs:{evse_id}"
        )
        
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
        self._task_supervisor.create_task(
            self._store_ev_charging_schedule(evse_id, time_base, charging_schedule),
            f"store_ev_charging_schedule:{evse_id}"
        )
        
        return call_result.NotifyEVChargingSchedule()
    
    async def send_charging_profile(self, evse_id: int, charging_profile: Dict) -> bool:
        """Send SetChargingProfile command to charger."""
        try:
            from ocpp.v201 import call
            
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
            from ocpp.v201 import call
            
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
            from ocpp.v201 import call
            
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
    
    async def _store_transaction_event_v2g(
        self,
        transaction_id: str,
        event_type: str,
        timestamp: str,
        transaction_info: Dict,
        evse_id: int,
        connector_id: int,
        kwargs: Dict
    ):
        """Store transaction event in database with V2G extensions."""
        try:
            # Extract V2G-specific fields
            charging_state = transaction_info.get("chargingState")
            trigger_reason = kwargs.get("triggerReason")
            meter_values = kwargs.get("meterValue", [])
            
            # Handle V2G-specific trigger reasons
            v2g_trigger_reasons = [
                "OperationModeChanged",
                "EnergyLimitReached",
                "DischargingStarted",
                "DischargingStopped",
                "V2XModeChanged"
            ]
            
            is_v2g_event = trigger_reason in v2g_trigger_reasons if trigger_reason else False
            
            # Extract operation mode if present
            operation_mode = None
            if "operationMode" in transaction_info:
                operation_mode = transaction_info["operationMode"]
            elif trigger_reason == "OperationModeChanged":
                # Extract from meter values or other sources
                operation_mode = self._extract_operation_mode_from_meter_values(meter_values)
            
            # Process V2G-specific meter values
            v2g_meter_data = {}
            if meter_values:
                v2g_meter_data = await self._process_v2g_meter_values(meter_values)
            
            transaction_data = {
                "transaction_id": transaction_id,
                "event_type": event_type,
                "timestamp": datetime.fromisoformat(timestamp.replace('Z', '+00:00')),
                "station_id": self.id,
                "evse_id": evse_id,
                "connector_id": connector_id,
                "charging_state": charging_state,
                "stopped_reason": transaction_info.get("stoppedReason"),
                "remote_start_id": transaction_info.get("remoteStartId"),
                "trigger_reason": trigger_reason,
                "is_v2g_event": is_v2g_event,
                "operation_mode": operation_mode,
                "v2g_meter_data": json.dumps(v2g_meter_data) if v2g_meter_data else None,
                "offline": kwargs.get("offline", False),
                "number_of_phases_used": kwargs.get("numberOfPhasesUsed"),
                "cable_max_current": kwargs.get("cableMaxCurrent"),
                "reservation_id": kwargs.get("reservationId"),
                "evse": json.dumps(kwargs.get("evse", {})),
                "id_token": json.dumps(kwargs.get("idToken", {})),
                "certificate": kwargs.get("certificate"),
                "iso15118_certificate_hash_data": json.dumps(kwargs.get("iso15118CertificateHashData", []))
            }
            
            await self.timescale_client.insert_transaction_event_v2g(transaction_data)
            
            # Log V2G-specific events
            if is_v2g_event:
                self.logger.info(f"V2G transaction event: {trigger_reason} for transaction {transaction_id}")
                if operation_mode:
                    self.logger.info(f"Operation mode changed to: {operation_mode}")
            
        except Exception as e:
            self.logger.error(f"Failed to store V2G transaction event: {e}")
    
    def _extract_operation_mode_from_meter_values(self, meter_values: List[Dict]) -> Optional[str]:
        """Extract operation mode from meter values."""
        try:
            for meter_value in meter_values:
                sampled_values = meter_value.get("sampledValue", [])
                for sampled_value in sampled_values:
                    measurand = sampled_value.get("measurand", "")
                    if "OperationMode" in measurand:
                        return sampled_value.get("value")
            return None
        except Exception:
            return None
    
    async def _process_v2g_meter_values(self, meter_values: List[Dict]) -> Dict[str, Any]:
        """Process V2G-specific meter values."""
        try:
            v2g_data = {
                "power_active_export": None,
                "energy_active_export": None,
                "power_active_setpoint": None,
                "power_active_residual": None,
                "power_reactive_export": None,
                "power_reactive_import": None,
                "operation_mode": None,
                "frequency": None,
                "voltage": None,
                "current": None
            }
            
            for meter_value in meter_values:
                timestamp = meter_value.get("timestamp")
                sampled_values = meter_value.get("sampledValue", [])
                
                for sampled_value in sampled_values:
                    measurand = sampled_value.get("measurand", "")
                    value = sampled_value.get("value")
                    unit = sampled_value.get("unitOfMeasure", {}).get("unit", "")
                    
                    # Map V2G-specific measurands
                    if measurand == "Power.Active.Export":
                        v2g_data["power_active_export"] = float(value) if value else None
                    elif measurand == "Energy.Active.Export.Register":
                        v2g_data["energy_active_export"] = float(value) if value else None
                    elif measurand == "Power.Active.Setpoint":
                        v2g_data["power_active_setpoint"] = float(value) if value else None
                    elif measurand == "Power.Active.Residual":
                        v2g_data["power_active_residual"] = float(value) if value else None
                    elif measurand == "Power.Reactive.Export":
                        v2g_data["power_reactive_export"] = float(value) if value else None
                    elif measurand == "Power.Reactive.Import":
                        v2g_data["power_reactive_import"] = float(value) if value else None
                    elif "OperationMode" in measurand:
                        v2g_data["operation_mode"] = value
                    elif measurand == "Frequency":
                        v2g_data["frequency"] = float(value) if value else None
                    elif measurand == "Voltage":
                        v2g_data["voltage"] = float(value) if value else None
                    elif measurand == "Current.Import" or measurand == "Current.Export":
                        v2g_data["current"] = float(value) if value else None
            
            return v2g_data
            
        except Exception as e:
            self.logger.error(f"Error processing V2G meter values: {e}")
            return {}
    
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
                    
                    # Map OCPP measurands to our telemetry format (including V2G)
                    if measurand == "Power.Active.Import":
                        telemetry_data["power_kw"] = float(value) / 1000 if unit == "W" else float(value)
                    elif measurand == "Power.Active.Export":
                        telemetry_data["power_discharge_kw"] = float(value) / 1000 if unit == "W" else float(value)
                    elif measurand == "Power.Active.Setpoint":
                        telemetry_data["power_setpoint_kw"] = float(value) / 1000 if unit == "W" else float(value)
                    elif measurand == "Power.Active.Residual":
                        telemetry_data["power_residual_kw"] = float(value) / 1000 if unit == "W" else float(value)
                    elif measurand == "Energy.Active.Import.Register":
                        telemetry_data["energy_kwh"] = float(value) / 1000 if unit == "Wh" else float(value)
                    elif measurand == "Energy.Active.Export.Register":
                        telemetry_data["energy_discharged_kwh"] = float(value) / 1000 if unit == "Wh" else float(value)
                    elif measurand == "SoC":
                        telemetry_data["soc_percent"] = float(value)
                    elif measurand == "Voltage":
                        telemetry_data["voltage_v"] = float(value)
                    elif measurand == "Current.Import":
                        telemetry_data["current_a"] = float(value)
                    elif measurand == "Current.Export":
                        telemetry_data["current_discharge_a"] = float(value)
                    elif measurand == "Frequency":
                        telemetry_data["frequency_hz"] = float(value)
                    elif measurand == "Temperature":
                        telemetry_data["temperature_c"] = float(value)
                    elif measurand == "Power.Reactive.Import":
                        telemetry_data["reactive_power_import_kvar"] = float(value) / 1000 if unit == "var" else float(value)
                    elif measurand == "Power.Reactive.Export":
                        telemetry_data["reactive_power_export_kvar"] = float(value) / 1000 if unit == "var" else float(value)
                    elif measurand == "Power.Factor":
                        telemetry_data["power_factor"] = float(value)
                    elif "OperationMode" in measurand:
                        telemetry_data["operation_mode"] = value
                
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
        """Store EV charging needs with V2G parameter extraction."""
        try:
            # Extract V2G-specific parameters
            v2x_charging_parameters = charging_needs.get("v2xChargingParameters")
            der_charging_parameters = charging_needs.get("derChargingParameters")
            
            # Extract V2X charging parameters
            v2x_params = {}
            if v2x_charging_parameters:
                v2x_params = {
                    "ev_maximum_discharge_power": v2x_charging_parameters.get("evMaximumDischargePower"),
                    "ev_minimum_discharge_power": v2x_charging_parameters.get("evMinimumDischargePower"),
                    "ev_maximum_charge_power": v2x_charging_parameters.get("evMaximumChargePower"),
                    "ev_minimum_charge_power": v2x_charging_parameters.get("evMinimumChargePower"),
                    "ev_target_energy_request": v2x_charging_parameters.get("evTargetEnergyRequest"),
                    "ev_maximum_energy_request": v2x_charging_parameters.get("evMaximumEnergyRequest"),
                    "ev_minimum_energy_request": v2x_charging_parameters.get("evMinimumEnergyRequest"),
                    "ev_present_active_power": v2x_charging_parameters.get("evPresentActivePower"),
                    "ev_present_reactive_power": v2x_charging_parameters.get("evPresentReactivePower"),
                    "soc": v2x_charging_parameters.get("soc"),
                    "capacity": v2x_charging_parameters.get("capacity"),
                    "control_mode": v2x_charging_parameters.get("controlMode")  # ScheduledControl | DynamicControl
                }
            
            # Extract DER charging parameters
            der_params = {}
            if der_charging_parameters:
                der_params = {
                    "max_w": der_charging_parameters.get("maxW"),
                    "max_var": der_charging_parameters.get("maxVar"),
                    "inverter_manufacturer": der_charging_parameters.get("inverterManufacturer"),
                    "inverter_model": der_charging_parameters.get("inverterModel"),
                    "supported_controls": der_charging_parameters.get("supportedControls")  # Array of DERControlEnumType
                }
            
            needs_data = {
                "station_id": self.id,
                "evse_id": evse_id,
                "requested_energy_transfer": charging_needs.get("requestedEnergyTransfer"),
                "departure_time": charging_needs.get("departureTime"),
                "ac_charging_parameters": charging_needs.get("acChargingParameters"),
                "dc_charging_parameters": charging_needs.get("dcChargingParameters"),
                "v2x_charging_parameters": v2x_params,
                "der_charging_parameters": der_params,
                "control_mode": v2x_params.get("control_mode"),
                "timestamp": datetime.now(timezone.utc)
            }
            
            await self.timescale_client.insert_ev_charging_needs(needs_data)
            
            # Log V2G parameters if present
            if v2x_params or der_params:
                self.logger.info(f"V2G charging needs detected for {self.id}, EVSE {evse_id}: "
                               f"V2X params: {bool(v2x_params)}, DER params: {bool(der_params)}")
                
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
    
    # ===== NEW OCPP 2.0.1 MESSAGE HANDLERS =====
    
    @on(Action.get_variables)
    def on_get_variables(self, get_variable_data: list, **kwargs):
        """Handle GetVariables message."""
        self.logger.info(f"GetVariables from {self.id}")
        
        # Process variables request
        self._task_supervisor.create_task(self._handle_get_variables(get_variable_data), "get_variables")
        
        return call_result.GetVariables(get_variable_result=[])
    
    @on(Action.set_variables)
    def on_set_variables(self, set_variable_data: list, **kwargs):
        """Handle SetVariables message."""
        self.logger.info(f"SetVariables from {self.id}")
        
        # Process variables set request
        self._task_supervisor.create_task(self._handle_set_variables(set_variable_data), "set_variables")
        
        return call_result.SetVariables(set_variable_result=[])
    
    @on(Action.get_base_report)
    def on_get_base_report(self, request_id: int, report_base: str, **kwargs):
        """Handle GetBaseReport message."""
        self.logger.info(f"GetBaseReport from {self.id}")
        
        # Process base report request
        self._task_supervisor.create_task(self._handle_get_base_report(request_id, report_base), "get_base_report")
        
        return call_result.GetBaseReport()
    
    @on(Action.notify_report)
    def on_notify_report(self, request_id: int, generated_at: str, tbc: bool, 
                        seq_no: int, report_data: list, **kwargs):
        """Handle NotifyReport message."""
        self.logger.info(f"NotifyReport from {self.id}")
        
        # Process report notification
        self._task_supervisor.create_task(
            self._handle_notify_report(request_id, generated_at, tbc, seq_no, report_data),
            "notify_report"
        )
        
        return call_result.NotifyReport()
    
    @on(Action.get_charging_profiles)
    def on_get_charging_profiles(self, request_id: int, evse_id: int, 
                                charging_profile_id: Optional[int] = None,
                                charging_profile_purpose: Optional[str] = None,
                                stack_level: Optional[int] = None, **kwargs):
        """Handle GetChargingProfiles message."""
        self.logger.info(f"GetChargingProfiles from {self.id}")
        
        # Process charging profiles request
        self._task_supervisor.create_task(
            self._handle_get_charging_profiles(request_id, evse_id, charging_profile_id, charging_profile_purpose, stack_level),
            "get_charging_profiles"
        )
        
        return call_result.GetChargingProfiles(status="Accepted")
    
    @on(Action.clear_charging_profile)
    def on_clear_charging_profile(self, charging_profile_id: Optional[int] = None,
                                 charging_profile_purpose: Optional[str] = None,
                                 stack_level: Optional[int] = None, **kwargs):
        """Handle ClearChargingProfile message."""
        self.logger.info(f"ClearChargingProfile from {self.id}")
        
        # Process clear charging profile request
        self._task_supervisor.create_task(
            self._handle_clear_charging_profile(charging_profile_id, charging_profile_purpose, stack_level),
            "clear_charging_profile"
        )
        
        return call_result.ClearChargingProfile(status="Accepted")
    
    @on(Action.get_composite_schedule)
    def on_get_composite_schedule(self, request_id: int, evse_id: int, duration: int,
                                 charging_rate_unit: Optional[str] = None, **kwargs):
        """Handle GetCompositeSchedule message."""
        self.logger.info(f"GetCompositeSchedule from {self.id}")
        
        # Process composite schedule request
        self._task_supervisor.create_task(
            self._handle_get_composite_schedule(request_id, evse_id, duration, charging_rate_unit),
            "get_composite_schedule"
        )
        
        return call_result.GetCompositeSchedule()
    
    @on(Action.set_charging_profile)
    def on_set_charging_profile(self, evse_id: int, charging_profile: Dict[str, Any], **kwargs):
        """Handle SetChargingProfile message."""
        self.logger.info(f"SetChargingProfile from {self.id}")
        
        # Process charging profile set request
        self._task_supervisor.create_task(self._handle_set_charging_profile(evse_id, charging_profile), "set_charging_profile")
        
        return call_result.SetChargingProfile(status="Accepted")
    
    @on(Action.request_start_transaction)
    def on_request_start_transaction(self, evse_id: int, id_token: dict,
                                   remote_start_id: Optional[int] = None,
                                   charging_profile: Optional[dict] = None,
                                   evse_id_token: Optional[dict] = None, **kwargs):
        """Handle RequestStartTransaction message."""
        self.logger.info(f"RequestStartTransaction from {self.id}")
        
        # Process start transaction request
        self._task_supervisor.create_task(
            self._handle_request_start_transaction(evse_id, id_token, remote_start_id, charging_profile, evse_id_token),
            "request_start_transaction"
        )
        
        return call_result.RequestStartTransaction(status="Accepted")
    
    @on(Action.request_stop_transaction)
    def on_request_stop_transaction(self, transaction_id: str, reason: Optional[str] = None, **kwargs):
        """Handle RequestStopTransaction message."""
        self.logger.info(f"RequestStopTransaction from {self.id}")
        
        # Process stop transaction request
        self._task_supervisor.create_task(self._handle_request_stop_transaction(transaction_id, reason), "request_stop_transaction")
        
        return call_result.RequestStopTransaction()
    
    @on(Action.reset)
    def on_reset(self, type: str, evse_id: Optional[int] = None, **kwargs):
        """Handle Reset message."""
        self.logger.info(f"Reset from {self.id}: {type}")
        
        # Process reset request
        self._task_supervisor.create_task(self._handle_reset(type, evse_id), "reset")
        
        return call_result.Reset(status="Accepted")
    
    @on(Action.change_availability)
    def on_change_availability(self, operational_status: str, evse: Optional[dict] = None, **kwargs):
        """Handle ChangeAvailability message."""
        self.logger.info(f"ChangeAvailability from {self.id}: {operational_status}")
        
        # Process change availability request
        self._task_supervisor.create_task(self._handle_change_availability(operational_status, evse), "change_availability")
        
        return call_result.ChangeAvailability(status="Accepted")
    
    @on(Action.trigger_message)
    def on_trigger_message(self, requested_message: str, evse: Optional[dict] = None, **kwargs):
        """Handle TriggerMessage message."""
        self.logger.info(f"TriggerMessage from {self.id}: {requested_message}")
        
        # Process trigger message request
        self._task_supervisor.create_task(self._handle_trigger_message(requested_message, evse), "trigger_message")
        
        return call_result.TriggerMessage()
    
    @on(Action.unlock_connector)
    def on_unlock_connector(self, evse_id: int, connector_id: int, **kwargs):
        """Handle UnlockConnector message."""
        self.logger.info(f"UnlockConnector from {self.id}: EVSE {evse_id}, Connector {connector_id}")
        
        # Process unlock connector request
        self._task_supervisor.create_task(self._handle_unlock_connector(evse_id, connector_id), "unlock_connector")
        
        return call_result.UnlockConnector()
    
    # ===== ISO 15118 CERTIFICATE HANDLERS =====
    
    @on(Action.get_15118_ev_certificate)
    def on_get_15118_ev_certificate(self, certificate_type: str, exi_request: Optional[str] = None, **kwargs):
        """Handle Get15118EVCertificate message."""
        self.logger.info(f"Get15118EVCertificate from {self.id}: {certificate_type}")
        
        # Process certificate request
        self._task_supervisor.create_task(self._handle_get_15118_ev_certificate(certificate_type, exi_request), "get_15118_ev_certificate")
        
        return call_result.Get15118EVCertificate()
    
    @on(Action.certificate_signed)
    def on_certificate_signed(self, certificate_type: str, certificate_chain: List[str], 
                             exi_response: str, **kwargs):
        """Handle CertificateSigned message."""
        self.logger.info(f"CertificateSigned from {self.id}: {certificate_type}")
        
        # Process certificate signed
        self._task_supervisor.create_task(self._handle_certificate_signed(certificate_type, certificate_chain, exi_response), "certificate_signed")
        
        return call_result.CertificateSigned()
    
    @on(Action.install_certificate)
    def on_install_certificate(self, certificate_type: str, certificate: str, **kwargs):
        """Handle InstallCertificate message."""
        self.logger.info(f"InstallCertificate from {self.id}: {certificate_type}")
        
        # Process certificate installation
        self._task_supervisor.create_task(self._handle_install_certificate(certificate_type, certificate), "install_certificate")
        
        return call_result.InstallCertificate()
    
    @on(Action.delete_certificate)
    def on_delete_certificate(self, certificate_hash_data: List[Dict[str, Any]], **kwargs):
        """Handle DeleteCertificate message."""
        self.logger.info(f"DeleteCertificate from {self.id}")
        
        # Process certificate deletion
        self._task_supervisor.create_task(self._handle_delete_certificate(certificate_hash_data), "delete_certificate")
        
        return call_result.DeleteCertificate()
    
    @on(Action.get_installed_certificate_ids)
    def on_get_installed_certificate_ids(self, certificate_type: Optional[str] = None, **kwargs):
        """Handle GetInstalledCertificateIds message."""
        self.logger.info(f"GetInstalledCertificateIds from {self.id}")
        
        # Process certificate IDs request
        self._task_supervisor.create_task(self._handle_get_installed_certificate_ids(certificate_type), "get_installed_certificate_ids")
        
        return call_result.GetInstalledCertificateIds()
    
    @on(Action.sign_certificate)
    def on_sign_certificate(self, certificate_type: str, certificate_signing_request: str, **kwargs):
        """Handle SignCertificate message."""
        self.logger.info(f"SignCertificate from {self.id}: {certificate_type}")
        
        # Process certificate signing
        self._task_supervisor.create_task(self._handle_sign_certificate(certificate_type, certificate_signing_request), "sign_certificate")
        
        return call_result.SignCertificate()
    
    @on(Action.security_event_notification)
    def on_security_event_notification(self, event_type: str, timestamp: str,
                                     tech_info: Optional[str] = None,
                                     additional_info: Optional[Dict[str, Any]] = None, **kwargs):
        """Handle SecurityEventNotification message."""
        self.logger.info(f"SecurityEventNotification from {self.id}: {event_type}")
        
        # Process security event notification
        self._task_supervisor.create_task(
            self._handle_security_event_notification(event_type, timestamp, tech_info, additional_info),
            "security_event_notification"
        )
        
        return call_result.SecurityEventNotification()
    
    @on(Action.get_log)
    def on_get_log(self, log_type: str, request_id: int, retry_count: Optional[int] = None,
                  retry_interval: Optional[int] = None, **kwargs):
        """Handle GetLog request."""
        self.logger.info(f"GetLog from {self.id}: {log_type}")
        
        # Process log request
        self._task_supervisor.create_task(self._handle_get_log(log_type, request_id, retry_count, retry_interval), "get_log")
        
        return call_result.GetLog()
    
    @on(Action.log_status_notification)
    def on_log_status_notification(self, status: str, request_id: int, **kwargs):
        """Handle LogStatusNotification."""
        self.logger.info(f"LogStatusNotification from {self.id}: {status}")
        
        # Process log status notification
        self._task_supervisor.create_task(self._handle_log_status_notification(status, request_id), "log_status_notification")
        
        return call_result.LogStatusNotification()
    
    @on(Action.notify_event)
    def on_notify_event(self, event_type: str, timestamp: str, tech_info: Optional[str] = None,
                       additional_info: Optional[Dict[str, Any]] = None, **kwargs):
        """Handle NotifyEvent."""
        self.logger.info(f"NotifyEvent from {self.id}: {event_type}")
        
        # Process notify event
        self._task_supervisor.create_task(self._handle_notify_event(event_type, timestamp, tech_info, additional_info), "notify_event")
        
        return call_result.NotifyEvent()
    
    @on(Action.publish_firmware)
    def on_publish_firmware(self, location: str, retrieve_date_time: str, request_id: int,
                           retry_interval: Optional[int] = None, retries: Optional[int] = None,
                           retry_back_off_random_range: Optional[int] = None,
                           checksum: Optional[str] = None, checksum_algorithm: Optional[str] = None,
                           signing_certificate: Optional[str] = None, signature: Optional[str] = None,
                           signing_certificate_chain: Optional[List[str]] = None,
                           request_start_time: Optional[str] = None,
                           request_stop_time: Optional[str] = None, **kwargs):
        """Handle PublishFirmware request."""
        self.logger.info(f"PublishFirmware from {self.id}: {location}")
        
        # Process publish firmware request
        self._task_supervisor.create_task(
            self._handle_publish_firmware(
                location, retrieve_date_time, request_id, retry_interval, retries,
                retry_back_off_random_range, checksum, checksum_algorithm,
                signing_certificate, signature, signing_certificate_chain,
                request_start_time, request_stop_time
            ),
            "publish_firmware"
        )
        
        return call_result.PublishFirmware()
    
    @on(Action.unpublish_firmware)
    def on_unpublish_firmware(self, checksum: str, **kwargs):
        """Handle UnpublishFirmware request."""
        self.logger.info(f"UnpublishFirmware from {self.id}: {checksum}")
        
        # Process unpublish firmware request
        self._task_supervisor.create_task(self._handle_unpublish_firmware(checksum), "unpublish_firmware")
        
        return call_result.UnpublishFirmware()
    
    @on(Action.update_firmware)
    def on_update_firmware(self, location: str, retrieve_date_time: str, request_id: int,
                          retry_interval: Optional[int] = None, retries: Optional[int] = None,
                          retry_back_off_random_range: Optional[int] = None,
                          checksum: Optional[str] = None, checksum_algorithm: Optional[str] = None,
                          signing_certificate: Optional[str] = None, signature: Optional[str] = None,
                          signing_certificate_chain: Optional[List[str]] = None,
                          request_start_time: Optional[str] = None,
                          request_stop_time: Optional[str] = None, **kwargs):
        """Handle UpdateFirmware request."""
        self.logger.info(f"UpdateFirmware from {self.id}: {location}")
        
        # Process update firmware request
        self._task_supervisor.create_task(
            self._handle_update_firmware(
                location, retrieve_date_time, request_id, retry_interval, retries,
                retry_back_off_random_range, checksum, checksum_algorithm,
                signing_certificate, signature, signing_certificate_chain,
                request_start_time, request_stop_time
            ),
            "update_firmware"
        )
        
        return call_result.UpdateFirmware()
    
    @on(Action.firmware_status_notification)
    def on_firmware_status_notification(self, status: str, request_id: Optional[int] = None,
                                      location: Optional[str] = None, **kwargs):
        """Handle FirmwareStatusNotification."""
        self.logger.info(f"FirmwareStatusNotification from {self.id}: {status}")
        
        # Process firmware status notification
        self._task_supervisor.create_task(self._handle_firmware_status_notification(status, request_id, location), "firmware_status_notification")
        
        return call_result.FirmwareStatusNotification()

    @on(Action.get_monitoring_report)
    def on_get_monitoring_report(self, request_id: int, monitoring_base: str,
                               monitoring_criterion: Optional[str] = None,
                               component_name: Optional[str] = None,
                               variable_name: Optional[str] = None, **kwargs):
        """Handle GetMonitoringReport request."""
        self.logger.info(f"GetMonitoringReport from {self.id}: {monitoring_base}")
        
        # Process monitoring report request
        self._task_supervisor.create_task(
            self._handle_get_monitoring_report(request_id, monitoring_base, monitoring_criterion, component_name, variable_name),
            "get_monitoring_report"
        )
        
        return call_result.GetMonitoringReport(status="Accepted")

    @on(Action.set_variable_monitoring)
    def on_set_variable_monitoring(self, component_name: str, variable_name: str,
                                 monitoring_criterion: str, threshold: Optional[float] = None, **kwargs):
        """Handle SetVariableMonitoring request."""
        self.logger.info(f"SetVariableMonitoring from {self.id}: {component_name}.{variable_name}")
        
        # Process variable monitoring request
        self._task_supervisor.create_task(
            self._handle_set_variable_monitoring(component_name, variable_name, monitoring_criterion, threshold),
            "set_variable_monitoring"
        )
        
        return call_result.SetVariableMonitoring()

    @on(Action.clear_variable_monitoring)
    def on_clear_variable_monitoring(self, component_name: str, variable_name: str,
                                   monitoring_criterion: str, **kwargs):
        """Handle ClearVariableMonitoring request."""
        self.logger.info(f"ClearVariableMonitoring from {self.id}: {component_name}.{variable_name}")
        
        # Process clear variable monitoring request
        self._task_supervisor.create_task(
            self._handle_clear_variable_monitoring(component_name, variable_name, monitoring_criterion),
            "clear_variable_monitoring"
        )
        
        return call_result.ClearVariableMonitoring()

    @on(Action.notify_monitoring_report)
    def on_notify_monitoring_report(self, request_id: int, monitoring_base: str,
                                  monitoring_criterion: Optional[str] = None,
                                  component_name: Optional[str] = None,
                                  variable_name: Optional[str] = None, **kwargs):
        """Handle NotifyMonitoringReport notification."""
        self.logger.info(f"NotifyMonitoringReport from {self.id}: {monitoring_base}")
        
        # Process monitoring report notification
        self._task_supervisor.create_task(
            self._handle_notify_monitoring_report(request_id, monitoring_base, monitoring_criterion, component_name, variable_name),
            "notify_monitoring_report"
        )
        
        return call_result.NotifyMonitoringReport()

    @on(Action.set_display_message)
    def on_set_display_message(self, message_info, evse_id: Optional[int] = None,
                             connector_id: Optional[int] = None, **kwargs):
        """Handle SetDisplayMessage request."""
        self.logger.info(f"SetDisplayMessage from {self.id}: {message_info.id}")
        
        # Process display message request
        self._task_supervisor.create_task(
            self._handle_set_display_message(message_info, evse_id, connector_id),
            "set_display_message"
        )
        
        return call_result.SetDisplayMessage(status="Accepted")

    @on(Action.clear_display_message)
    def on_clear_display_message(self, message_id: Optional[str] = None,
                               evse_id: Optional[int] = None,
                               connector_id: Optional[int] = None, **kwargs):
        """Handle ClearDisplayMessage request."""
        self.logger.info(f"ClearDisplayMessage from {self.id}: {message_id}")
        
        # Process clear display message request
        self._task_supervisor.create_task(
            self._handle_clear_display_message(message_id, evse_id, connector_id),
            "clear_display_message"
        )
        
        return call_result.ClearDisplayMessage()

    @on(Action.customer_information)
    def on_customer_information(self, request_id: int,
                              customer_certificate_id: Optional[str] = None,
                              id_token: Optional[IdTokenType] = None,
                              customer_identifier: Optional[str] = None, **kwargs):
        """Handle CustomerInformation request."""
        self.logger.info(f"CustomerInformation from {self.id}: {request_id}")
        
        # Process customer information request
        self._task_supervisor.create_task(
            self._handle_customer_information(request_id, customer_certificate_id, id_token, customer_identifier),
            "customer_information"
        )
        
        return call_result.CustomerInformation(status="Accepted")

    # Note: delete_customer_information is not supported in OCPP 2.1
    
    # V2G-specific message handlers (optional actions may be missing in ocpp package)
    _act_uds = _opt_action("update_dynamic_schedule")
    if _act_uds is not None:
        @on(_act_uds)
        def on_update_dynamic_schedule(self, charging_profile_id: int,
                                      limit: Optional[float] = None,
                                      discharging_limit: Optional[float] = None,
                                      setpoint: Optional[float] = None,
                                      setpoint_reactive: Optional[float] = None, **kwargs):
            self._task_supervisor.create_task(
                self._handle_update_dynamic_schedule(charging_profile_id, limit, discharging_limit, setpoint, setpoint_reactive),
                "update_dynamic_schedule"
            )
            return call_result.UpdateDynamicSchedule(status="Accepted")
    else:
        def on_update_dynamic_schedule(self, charging_profile_id: int, limit=None, discharging_limit=None, setpoint=None, setpoint_reactive=None, **kwargs):
            return {"status": "Accepted"}

    _act_pds = _opt_action("pull_dynamic_schedule_update")
    if _act_pds is not None:
        @on(_act_pds)
        def on_pull_dynamic_schedule_update(self, charging_profile_id: int, **kwargs):
            self._task_supervisor.create_task(self._handle_pull_dynamic_schedule_update(charging_profile_id), "pull_dynamic_schedule_update")
            return call_result.PullDynamicScheduleUpdate(status="Accepted")
    else:
        def on_pull_dynamic_schedule_update(self, charging_profile_id: int, **kwargs):
            return {"status": "Accepted"}

    @on(Action.notify_charging_limit)
    def on_notify_charging_limit(self, charging_schedule: Optional[List] = None,
                                evse_id: Optional[int] = None,
                                charging_limit: Dict = None, **kwargs):
        """Handle NotifyChargingLimit request."""
        self._task_supervisor.create_task(self._handle_notify_charging_limit(charging_schedule, evse_id, charging_limit), "notify_charging_limit")
        return call_result.NotifyChargingLimit(status="Accepted")
    
    @on(Action.cleared_charging_limit)
    def on_cleared_charging_limit(self, charging_limit_source: str,
                                 evse_id: Optional[int] = None, **kwargs):
        """Handle ClearedChargingLimit request."""
        self._task_supervisor.create_task(self._handle_cleared_charging_limit(charging_limit_source, evse_id), "cleared_charging_limit")
        return call_result.ClearedChargingLimit(status="Accepted")
    
    _act_upc = _opt_action("use_priority_charging")
    if _act_upc is not None:
        @on(_act_upc)
        def on_use_priority_charging(self, transaction_id: str, activate: bool, **kwargs):
            self._task_supervisor.create_task(self._handle_use_priority_charging(transaction_id, activate), "use_priority_charging")
            return call_result.UsePriorityCharging(status="Accepted")
    else:
        def on_use_priority_charging(self, transaction_id: str, activate: bool, **kwargs):
            return {"status": "Accepted"}

    _act_npc = _opt_action("notify_priority_charging")
    if _act_npc is not None:
        @on(_act_npc)
        def on_notify_priority_charging(self, transaction_id: str, activated: bool, **kwargs):
            self._task_supervisor.create_task(self._handle_notify_priority_charging(transaction_id, activated), "notify_priority_charging")
            return call_result.NotifyPriorityCharging(status="Accepted")
    else:
        def on_notify_priority_charging(self, transaction_id: str, activated: bool, **kwargs):
            return {"status": "Accepted"}

    _act_naet = _opt_action("notify_allowed_energy_transfer")
    if _act_naet is not None:
        @on(_act_naet)
        def on_notify_allowed_energy_transfer(self, allowed_energy_transfer: List[str], **kwargs):
            self._task_supervisor.create_task(self._handle_notify_allowed_energy_transfer(allowed_energy_transfer), "notify_allowed_energy_transfer")
            return call_result.NotifyAllowedEnergyTransfer(status="Accepted")
    else:
        def on_notify_allowed_energy_transfer(self, allowed_energy_transfer: List[str], **kwargs):
            return {"status": "Accepted"}

    # DER Control message handlers (optional)
    _act_sdc = _opt_action("set_der_control")
    if _act_sdc is not None:
        @on(_act_sdc)
        def on_set_der_control(self, der_control: Dict, **kwargs):
            self._task_supervisor.create_task(self._handle_set_der_control(der_control), "set_der_control")
            return call_result.SetDERControl(status="Accepted")
    else:
        def on_set_der_control(self, der_control: Dict, **kwargs):
            return {"status": "Accepted"}

    _act_gdc = _opt_action("get_der_control")
    if _act_gdc is not None:
        @on(_act_gdc)
        def on_get_der_control(self, control_id: Optional[int] = None, **kwargs):
            self._task_supervisor.create_task(self._handle_get_der_control(control_id), "get_der_control")
            return call_result.GetDERControl(status="Accepted")
    else:
        def on_get_der_control(self, control_id: Optional[int] = None, **kwargs):
            return {"status": "Accepted"}

    _act_rdc = _opt_action("report_der_control")
    if _act_rdc is not None:
        @on(_act_rdc)
        def on_report_der_control(self, der_control: List[Dict], **kwargs):
            self._task_supervisor.create_task(self._handle_report_der_control(der_control), "report_der_control")
            return call_result.ReportDERControl(status="Accepted")
    else:
        def on_report_der_control(self, der_control: List[Dict], **kwargs):
            return {"status": "Accepted"}

    _act_cdc = _opt_action("clear_der_control")
    if _act_cdc is not None:
        @on(_act_cdc)
        def on_clear_der_control(self, control_id: Optional[int] = None, **kwargs):
            self._task_supervisor.create_task(self._handle_clear_der_control(control_id), "clear_der_control")
            return call_result.ClearDERControl(status="Accepted")
    else:
        def on_clear_der_control(self, control_id: Optional[int] = None, **kwargs):
            return {"status": "Accepted"}

    _act_nda = _opt_action("notify_der_alarm")
    if _act_nda is not None:
        @on(_act_nda)
        def on_notify_der_alarm(self, control_type: str, alarm_ended: bool, grid_event_fault: Optional[str] = None, timestamp: str = None, **kwargs):
            self._task_supervisor.create_task(self._handle_notify_der_alarm(control_type, alarm_ended, grid_event_fault, timestamp), "notify_der_alarm")
            return call_result.NotifyDERAlarm(status="Accepted")
    else:
        def on_notify_der_alarm(self, control_type: str, alarm_ended: bool, grid_event_fault=None, timestamp=None, **kwargs):
            return {"status": "Accepted"}

    _act_ndss = _opt_action("notify_der_start_stop")
    if _act_ndss is not None:
        @on(_act_ndss)
        def on_notify_der_start_stop(self, control_id: int, started: bool, superseded_id: Optional[int] = None, timestamp: str = None, **kwargs):
            self._task_supervisor.create_task(self._handle_notify_der_start_stop(control_id, started, superseded_id, timestamp), "notify_der_start_stop")
            return call_result.NotifyDERStartStop(status="Accepted")
    else:
        def on_notify_der_start_stop(self, control_id: int, started: bool, superseded_id=None, timestamp=None, **kwargs):
            return {"status": "Accepted"}

    _act_afrr = _opt_action("afrr_signal")
    if _act_afrr is not None:
        @on(_act_afrr)
        def on_afrr_signal(self, signal: float, timestamp: str, **kwargs):
            self._task_supervisor.create_task(self._handle_afrr_signal(signal, timestamp), "afrr_signal")
            return call_result.AFRRSignal(status="Accepted")
    else:
        def on_afrr_signal(self, signal: float, timestamp: str, **kwargs):
            return {"status": "Accepted"}
    
    # ===== ASYNC HANDLERS =====
    
    async def _handle_get_variables(self, get_variable_data: list):
        """Handle GetVariables request."""
        try:
            results = await self.device_model.get_variables(self.id, get_variable_data)
            
            # Send GetVariablesResponse
            from ocpp.v201 import call
            request = call.GetVariablesResponse(
                get_variable_result=results
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling GetVariables: {e}")
    
    async def _handle_set_variables(self, set_variable_data: list):
        """Handle SetVariables request."""
        try:
            results = await self.device_model.set_variables(self.id, set_variable_data)
            
            # Send SetVariablesResponse
            from ocpp.v201 import call
            request = call.SetVariablesResponse(
                set_variable_result=results
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling SetVariables: {e}")
    
    async def _handle_get_base_report(self, request_id: int, report_base: str):
        """Handle GetBaseReport request."""
        try:
            report_data = await self.device_model.get_base_report(self.id, report_base)
            
            # Send GetBaseReportResponse
            from ocpp.v201 import call
            request = call.GetBaseReportResponse(
                status="Accepted",
                status_info={"reason_code": "NoError"}
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling GetBaseReport: {e}")
    
    async def _handle_notify_report(self, request_id: int, generated_at: str, 
                                  tbc: bool, seq_no: int, report_data: list):
        """Handle NotifyReport notification."""
        try:
            await self.device_model.notify_report(
                self.id, request_id, generated_at, tbc, seq_no, report_data
            )
            
        except Exception as e:
            self.logger.error(f"Error handling NotifyReport: {e}")
    
    async def _handle_get_charging_profiles(self, request_id: int, evse_id: int,
                                          charging_profile_id: Optional[int],
                                          charging_profile_purpose: Optional[str],
                                          stack_level: Optional[int]):
        """Handle GetChargingProfiles request."""
        try:
            result = await self.charging_profile_manager.get_charging_profiles(
                self.id, evse_id, charging_profile_id, 
                charging_profile_purpose, stack_level
            )
            
            # Send GetChargingProfilesResponse
            from ocpp.v201 import call
            request = call.GetChargingProfilesResponse(
                status=result["status"],
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling GetChargingProfiles: {e}")
    
    async def _handle_clear_charging_profile(self, charging_profile_id: Optional[int],
                                           charging_profile_purpose: Optional[str],
                                           stack_level: Optional[int]):
        """Handle ClearChargingProfile request."""
        try:
            result = await self.charging_profile_manager.clear_charging_profile(
                self.id, 1, charging_profile_id, charging_profile_purpose, stack_level
            )
            
            # Send ClearChargingProfileResponse
            from ocpp.v201 import call
            request = call.ClearChargingProfileResponse(
                status=result["status"],
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling ClearChargingProfile: {e}")
    
    async def _handle_get_composite_schedule(self, request_id: int, evse_id: int,
                                           duration: int, charging_rate_unit: Optional[str]):
        """Handle GetCompositeSchedule request."""
        try:
            result = await self.charging_profile_manager.get_composite_schedule(
                self.id, evse_id, duration, charging_rate_unit
            )
            
            # Send GetCompositeScheduleResponse
            from ocpp.v201 import call
            request = call.GetCompositeScheduleResponse(
                status=result["status"],
                status_info=result.get("statusInfo"),
                connector_id=result.get("connectorId"),
                schedule_start=result.get("scheduleStart"),
                charging_schedule=result.get("chargingSchedule")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling GetCompositeSchedule: {e}")
    
    async def _handle_report_charging_profiles(self, request_id: int, evse_id: int,
                                             charging_profile: list):
        """Handle ReportChargingProfiles notification."""
        try:
            await self.charging_profile_manager.report_charging_profiles(
                self.id, evse_id, request_id, charging_profile
            )
            
        except Exception as e:
            self.logger.error(f"Error handling ReportChargingProfiles: {e}")
    
    async def _handle_set_charging_profile(self, evse_id: int, charging_profile: Dict[str, Any]):
        """Handle SetChargingProfile request."""
        try:
            result = await self.charging_profile_manager.set_charging_profile(
                self.id, evse_id, charging_profile
            )
            
            # Send SetChargingProfileResponse
            from ocpp.v201 import call
            request = call.SetChargingProfileResponse(
                status=result["status"],
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling SetChargingProfile: {e}")
    
    async def _handle_request_start_transaction(self, evse_id: int, id_token: dict,
                                              remote_start_id: Optional[int],
                                              charging_profile: Optional[dict],
                                              evse_id_token: Optional[dict]):
        """Handle RequestStartTransaction request."""
        try:
            from .transaction_manager import IdToken, IdTokenType
            
            # Parse ID token
            parsed_id_token = IdToken(
                id_token=id_token["idToken"],
                type=IdTokenType(id_token.get("type", "ISO14443")),
                additional_info=id_token.get("additionalInfo")
            )
            
            result = await self.transaction_manager.request_start_transaction(
                self.id, evse_id, remote_start_id, parsed_id_token, charging_profile
            )
            
            # Send RequestStartTransactionResponse
            from ocpp.v201 import call
            request = call.RequestStartTransactionResponse(
                status=result["status"],
                status_info=result.get("statusInfo"),
                transaction_id=result.get("transactionId"),
                id_token_info=result.get("idTokenInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling RequestStartTransaction: {e}")
    
    async def _handle_request_stop_transaction(self, transaction_id: str, reason: Optional[str]):
        """Handle RequestStopTransaction request."""
        try:
            result = await self.transaction_manager.request_stop_transaction(
                self.id, transaction_id, reason
            )
            
            # Send RequestStopTransactionResponse
            from ocpp.v201 import call
            request = call.RequestStopTransactionResponse(
                status=result["status"],
                status_info=result.get("statusInfo"),
                id_token_info=result.get("idTokenInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling RequestStopTransaction: {e}")
    
    async def _handle_reset(self, reset_type: str, evse_id: Optional[int]):
        """Handle Reset request."""
        try:
            # Store reset request
            await self.timescale_client.store_reset_request({
                "station_id": self.id,
                "reset_type": reset_type,
                "evse_id": evse_id,
                "requested_at": datetime.now(timezone.utc)
            })
            
            # Send ResetResponse
            from ocpp.v201 import call
            request = call.ResetResponse(
                status="Accepted",
                status_info={"reason_code": "NoError"}
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling Reset: {e}")
    
    async def _handle_change_availability(self, operational_status: str, evse: Optional[dict]):
        """Handle ChangeAvailability request."""
        try:
            evse_id = evse.get("id") if evse else None
            
            # Store availability change
            await self.timescale_client.store_availability_change({
                "station_id": self.id,
                "evse_id": evse_id,
                "operational_status": operational_status,
                "changed_at": datetime.now(timezone.utc)
            })
            
            # Send ChangeAvailabilityResponse
            from ocpp.v201 import call
            request = call.ChangeAvailabilityResponse(
                status="Accepted",
                status_info={"reason_code": "NoError"}
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling ChangeAvailability: {e}")
    
    async def _handle_trigger_message(self, requested_message: str, evse: Optional[dict]):
        """Handle TriggerMessage request."""
        try:
            evse_id = evse.get("id") if evse else None
            
            # Store trigger message request
            await self.timescale_client.store_trigger_message({
                "station_id": self.id,
                "evse_id": evse_id,
                "requested_message": requested_message,
                "triggered_at": datetime.now(timezone.utc)
            })
            
            # Send TriggerMessageResponse
            from ocpp.v201 import call
            request = call.TriggerMessageResponse(
                status="Accepted",
                status_info={"reason_code": "NoError"}
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling TriggerMessage: {e}")
    
    async def _handle_unlock_connector(self, evse_id: int, connector_id: int):
        """Handle UnlockConnector request."""
        try:
            # Store unlock connector request
            await self.timescale_client.store_unlock_connector({
                "station_id": self.id,
                "evse_id": evse_id,
                "connector_id": connector_id,
                "unlocked_at": datetime.now(timezone.utc)
            })
            
            # Send UnlockConnectorResponse
            from ocpp.v201 import call
            request = call.UnlockConnectorResponse(
                status="Accepted",
                status_info={"reason_code": "NoError"}
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling UnlockConnector: {e}")
    
    async def _initialize_complete_device_model(self):
        """Initialize device model for new station."""
        try:
            await self.device_model.initialize_complete_device_model(self.id, self.station_info)
            self.logger.info(f"Initialized complete device model for station {self.id}")
        except Exception as e:
            self.logger.error(f"Error initializing device model for {self.id}: {e}")
    
    async def _handle_get_15118_ev_certificate(self, certificate_type: str, exi_request: Optional[str]):
        """Handle Get15118EVCertificate request."""
        try:
            cert_type = CertificateType(certificate_type)
            result = await self.certificate_manager.get_15118_ev_certificate(self.id, cert_type, exi_request)
            
            # Send Get15118EVCertificateResponse
            from ocpp.v201 import call
            request = call.Get15118EVCertificateResponse(
                status=result["status"],
                exi_response=result.get("exiResponse"),
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling Get15118EVCertificate: {e}")
    
    async def _handle_certificate_signed(self, certificate_type: str, certificate_chain: List[str], exi_response: str):
        """Handle CertificateSigned notification."""
        try:
            cert_type = CertificateType(certificate_type)
            result = await self.certificate_manager.certificate_signed(self.id, cert_type, certificate_chain, exi_response)
            
            # Send CertificateSignedResponse
            from ocpp.v201 import call
            request = call.CertificateSignedResponse(
                status=result["status"],
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling CertificateSigned: {e}")
    
    async def _handle_install_certificate(self, certificate_type: str, certificate: str):
        """Handle InstallCertificate request."""
        try:
            cert_type = CertificateType(certificate_type)
            result = await self.certificate_manager.install_certificate(self.id, cert_type, certificate)
            
            # Send InstallCertificateResponse
            from ocpp.v201 import call
            request = call.InstallCertificateResponse(
                status=result["status"],
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling InstallCertificate: {e}")
    
    async def _handle_delete_certificate(self, certificate_hash_data: List[Dict[str, Any]]):
        """Handle DeleteCertificate request."""
        try:
            result = await self.certificate_manager.delete_certificate(self.id, certificate_hash_data)
            
            # Send DeleteCertificateResponse
            from ocpp.v201 import call
            request = call.DeleteCertificateResponse(
                status=result["status"],
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling DeleteCertificate: {e}")
    
    async def _handle_get_installed_certificate_ids(self, certificate_type: Optional[str]):
        """Handle GetInstalledCertificateIds request."""
        try:
            cert_type = CertificateType(certificate_type) if certificate_type else None
            result = await self.certificate_manager.get_installed_certificate_ids(self.id, cert_type)
            
            # Send GetInstalledCertificateIdsResponse
            from ocpp.v201 import call
            request = call.GetInstalledCertificateIdsResponse(
                status=result["status"],
                certificate_hash_data=result.get("certificateHashData", []),
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling GetInstalledCertificateIds: {e}")
    
    async def _handle_sign_certificate(self, certificate_type: str, certificate_signing_request: str):
        """Handle SignCertificate request."""
        try:
            cert_type = CertificateType(certificate_type)
            result = await self.certificate_manager.sign_certificate(self.id, cert_type, certificate_signing_request)
            
            # Send SignCertificateResponse
            from ocpp.v201 import call
            request = call.SignCertificateResponse(
                status=result["status"],
                certificate=result.get("certificate"),
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling SignCertificate: {e}")
    
    async def _handle_security_event_notification(self, event_type: str, timestamp: str,
                                                tech_info: Optional[str],
                                                additional_info: Optional[Dict[str, Any]]):
        """Handle SecurityEventNotification notification."""
        try:
            result = await self.security_manager.handle_security_event_notification(
                self.id, event_type, timestamp, tech_info, additional_info
            )
            
            # Send SecurityEventNotificationResponse
            from ocpp.v201 import call
            request = call.SecurityEventNotificationResponse(
                status=result["status"],
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling SecurityEventNotification: {e}")
    
    async def _handle_get_log(self, log_type: str, request_id: int, retry_count: Optional[int],
                            retry_interval: Optional[int]):
        """Handle GetLog request."""
        try:
            from .diagnostics_firmware import LogType
            
            # Convert string to LogType enum
            try:
                log_type_enum = LogType(log_type)
            except ValueError:
                log_type_enum = LogType.CUSTOM_LOG
            
            result = await self.diagnostics_manager.get_log(
                self.id, log_type_enum, request_id, retry_count, retry_interval
            )
            
            # Send GetLogResponse
            from ocpp.v201 import call
            request = call.GetLogResponse(
                status=result["status"],
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling GetLog: {e}")
    
    async def _handle_log_status_notification(self, status: str, request_id: int):
        """Handle LogStatusNotification."""
        try:
            await self.diagnostics_manager.handle_log_status_notification(
                self.id, request_id, status
            )
            
        except Exception as e:
            self.logger.error(f"Error handling LogStatusNotification: {e}")
    
    async def _handle_notify_event(self, event_type: str, timestamp: str, tech_info: Optional[str],
                                 additional_info: Optional[Dict[str, Any]]):
        """Handle NotifyEvent."""
        try:
            await self.diagnostics_manager.handle_notify_event(
                self.id, event_type, timestamp, tech_info, additional_info
            )
            
        except Exception as e:
            self.logger.error(f"Error handling NotifyEvent: {e}")
    
    async def _handle_publish_firmware(self, location: str, retrieve_date_time: str, request_id: int,
                                     retry_interval: Optional[int], retries: Optional[int],
                                     retry_back_off_random_range: Optional[int],
                                     checksum: Optional[str], checksum_algorithm: Optional[str],
                                     signing_certificate: Optional[str], signature: Optional[str],
                                     signing_certificate_chain: Optional[List[str]],
                                     request_start_time: Optional[str], request_stop_time: Optional[str]):
        """Handle PublishFirmware request."""
        try:
            result = await self.firmware_manager.publish_firmware(
                self.id, location, retrieve_date_time, request_id, retry_interval, retries,
                retry_back_off_random_range, checksum, checksum_algorithm,
                signing_certificate, signature, signing_certificate_chain,
                request_start_time, request_stop_time
            )
            
            # Send PublishFirmwareResponse
            from ocpp.v201 import call
            request = call.PublishFirmwareResponse(
                status=result["status"],
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling PublishFirmware: {e}")
    
    async def _handle_unpublish_firmware(self, checksum: str):
        """Handle UnpublishFirmware request."""
        try:
            result = await self.firmware_manager.unpublish_firmware(self.id, checksum)
            
            # Send UnpublishFirmwareResponse
            from ocpp.v201 import call
            request = call.UnpublishFirmwareResponse(
                status=result["status"],
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling UnpublishFirmware: {e}")
    
    async def _handle_update_firmware(self, location: str, retrieve_date_time: str, request_id: int,
                                    retry_interval: Optional[int], retries: Optional[int],
                                    retry_back_off_random_range: Optional[int],
                                    checksum: Optional[str], checksum_algorithm: Optional[str],
                                    signing_certificate: Optional[str], signature: Optional[str],
                                    signing_certificate_chain: Optional[List[str]],
                                    request_start_time: Optional[str], request_stop_time: Optional[str]):
        """Handle UpdateFirmware request."""
        try:
            result = await self.firmware_manager.update_firmware(
                self.id, location, retrieve_date_time, request_id, retry_interval, retries,
                retry_back_off_random_range, checksum, checksum_algorithm,
                signing_certificate, signature, signing_certificate_chain,
                request_start_time, request_stop_time
            )
            
            # Send UpdateFirmwareResponse
            from ocpp.v201 import call
            request = call.UpdateFirmwareResponse(
                status=result["status"],
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling UpdateFirmware: {e}")
    
    async def _handle_firmware_status_notification(self, status: str, request_id: Optional[int],
                                                 location: Optional[str]):
        """Handle FirmwareStatusNotification."""
        try:
            await self.firmware_manager.handle_firmware_status_notification(
                self.id, status, request_id, location
            )
            
        except Exception as e:
            self.logger.error(f"Error handling FirmwareStatusNotification: {e}")

    async def _handle_get_monitoring_report(self, request_id: int, monitoring_base: str,
                                          monitoring_criterion: Optional[str],
                                          component_name: Optional[str],
                                          variable_name: Optional[str]):
        """Handle GetMonitoringReport request."""
        try:
            result = await self.monitoring_manager.get_monitoring_report(
                self.id, request_id, monitoring_base, monitoring_criterion,
                component_name, variable_name
            )
            
            # Send GetMonitoringReportResponse
            from ocpp.v201 import call
            request = call.GetMonitoringReportResponse(
                status=result["status"],
                statusInfo=result.get("statusInfo"),
                monitoringData=result.get("monitoringData", [])
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling GetMonitoringReport: {e}")

    async def _handle_set_variable_monitoring(self, component_name: str, variable_name: str,
                                            monitoring_criterion: str, threshold: Optional[float]):
        """Handle SetVariableMonitoring request."""
        try:
            result = await self.monitoring_manager.set_variable_monitoring(
                self.id, component_name, variable_name, monitoring_criterion, threshold
            )
            
            # Send SetVariableMonitoringResponse
            from ocpp.v201 import call
            request = call.SetVariableMonitoringResponse(
                status=result["status"],
                statusInfo=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling SetVariableMonitoring: {e}")

    async def _handle_clear_variable_monitoring(self, component_name: str, variable_name: str,
                                              monitoring_criterion: str):
        """Handle ClearVariableMonitoring request."""
        try:
            result = await self.monitoring_manager.clear_variable_monitoring(
                self.id, component_name, variable_name, monitoring_criterion
            )
            
            # Send ClearVariableMonitoringResponse
            from ocpp.v201 import call
            request = call.ClearVariableMonitoringResponse(
                status=result["status"],
                statusInfo=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling ClearVariableMonitoring: {e}")

    async def _handle_notify_monitoring_report(self, request_id: int, monitoring_base: str,
                                             monitoring_criterion: Optional[str],
                                             component_name: Optional[str],
                                             variable_name: Optional[str]):
        """Handle NotifyMonitoringReport notification."""
        try:
            await self.monitoring_manager.notify_monitoring_report(
                self.id, request_id, monitoring_base, monitoring_criterion,
                component_name, variable_name
            )
            
        except Exception as e:
            self.logger.error(f"Error handling NotifyMonitoringReport: {e}")

    async def _handle_set_display_message(self, message_info, evse_id: Optional[int],
                                        connector_id: Optional[int]):
        """Handle SetDisplayMessage request."""
        try:
            result = await self.display_manager.set_display_message(
                self.id, message_info, evse_id, connector_id
            )
            
            # Send SetDisplayMessageResponse
            from ocpp.v201 import call
            request = call.SetDisplayMessageResponse(
                status=result["status"],
                statusInfo=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling SetDisplayMessage: {e}")

    async def _handle_clear_display_message(self, message_id: Optional[str],
                                          evse_id: Optional[int],
                                          connector_id: Optional[int]):
        """Handle ClearDisplayMessage request."""
        try:
            result = await self.display_manager.clear_display_message(
                self.id, message_id, evse_id, connector_id
            )
            
            # Send ClearDisplayMessageResponse
            from ocpp.v201 import call
            request = call.ClearDisplayMessageResponse(
                status=result["status"],
                statusInfo=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling ClearDisplayMessage: {e}")

    async def _handle_customer_information(self, request_id: int,
                                         customer_certificate_id: Optional[str],
                                         id_token: Optional[IdTokenType],
                                         customer_identifier: Optional[str]):
        """Handle CustomerInformation request."""
        try:
            result = await self.privacy_manager.handle_customer_information_request(
                self.id, request_id, customer_certificate_id, id_token, customer_identifier
            )
            
            # Send CustomerInformationResponse
            from ocpp.v201 import call
            request = call.CustomerInformationResponse(
                status=result["status"],
                statusInfo=result.get("statusInfo"),
                customer_information=result.get("customer_information")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling CustomerInformation: {e}")

    async def _handle_delete_customer_information(self, request_id: int,
                                                customer_certificate_id: Optional[str],
                                                id_token: Optional[IdTokenType],
                                                customer_identifier: Optional[str]):
        """Handle DeleteCustomerInformation request."""
        try:
            result = await self.privacy_manager.handle_delete_customer_information_request(
                self.id, request_id, customer_certificate_id, id_token, customer_identifier
            )
            
            # Send DeleteCustomerInformationResponse
            from ocpp.v201 import call
            request = call.DeleteCustomerInformationResponse(
                status=result["status"],
                statusInfo=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling DeleteCustomerInformation: {e}")
    
    # V2G-specific async handlers
    async def _handle_update_dynamic_schedule(self, charging_profile_id: int, 
                                            limit: Optional[float] = None,
                                            discharging_limit: Optional[float] = None,
                                            setpoint: Optional[float] = None,
                                            setpoint_reactive: Optional[float] = None):
        """Handle UpdateDynamicSchedule request."""
        try:
            self.logger.info(f"UpdateDynamicSchedule from {self.id}: profile_id={charging_profile_id}, "
                           f"limit={limit}, discharging_limit={discharging_limit}, "
                           f"setpoint={setpoint}, setpoint_reactive={setpoint_reactive}")
            
            # Update the charging profile with new dynamic values
            if self.charging_profile_manager:
                result = await self.charging_profile_manager.update_dynamic_schedule(
                    self.id, charging_profile_id, limit, discharging_limit, 
                    setpoint, setpoint_reactive
                )
                self.logger.info(f"Updated dynamic schedule for profile {charging_profile_id}: {result}")
            else:
                self.logger.warning("Charging profile manager not available")
            
            # Send UpdateDynamicScheduleResponse
            from ocpp.v201 import call
            request = call.UpdateDynamicScheduleResponse(
                status=result["status"],
                statusInfo=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling UpdateDynamicSchedule: {e}")
    
    async def _handle_pull_dynamic_schedule_update(self, charging_profile_id: int):
        """Handle PullDynamicScheduleUpdate request."""
        try:
            self.logger.info(f"PullDynamicScheduleUpdate from {self.id}: profile_id={charging_profile_id}")
            
            # Get the current dynamic schedule
            result = await self.charging_profile_manager.pull_dynamic_schedule_update(
                self.id, charging_profile_id
            )
            
            # Send PullDynamicScheduleUpdateResponse
            from ocpp.v201 import call
            request = call.PullDynamicScheduleUpdateResponse(
                status=result["status"],
                statusInfo=result.get("statusInfo"),
                charging_schedule=result.get("charging_schedule")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling PullDynamicScheduleUpdate: {e}")
    
    async def _handle_notify_charging_limit(self, charging_schedule: Optional[List] = None,
                                          evse_id: Optional[int] = None,
                                          charging_limit: Dict = None):
        """Handle NotifyChargingLimit request."""
        try:
            self.logger.info(f"NotifyChargingLimit from {self.id}: evse_id={evse_id}, "
                           f"charging_limit={charging_limit}")
            
            # Store the external charging limit
            result = await self.charging_profile_manager.notify_charging_limit(
                self.id, evse_id, charging_limit, charging_schedule
            )
            
            # Send NotifyChargingLimitResponse
            from ocpp.v201 import call
            request = call.NotifyChargingLimitResponse(
                status=result["status"],
                statusInfo=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling NotifyChargingLimit: {e}")
    
    async def _handle_cleared_charging_limit(self, charging_limit_source: str,
                                           evse_id: Optional[int] = None):
        """Handle ClearedChargingLimit request."""
        try:
            self.logger.info(f"ClearedChargingLimit from {self.id}: source={charging_limit_source}, "
                           f"evse_id={evse_id}")
            
            # Clear the external charging limit
            result = await self.charging_profile_manager.cleared_charging_limit(
                self.id, charging_limit_source, evse_id
            )
            
            # Send ClearedChargingLimitResponse
            from ocpp.v201 import call
            request = call.ClearedChargingLimitResponse(
                status=result["status"],
                statusInfo=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling ClearedChargingLimit: {e}")
    
    async def _handle_use_priority_charging(self, transaction_id: str, activate: bool):
        """Handle UsePriorityCharging request."""
        try:
            self.logger.info(f"UsePriorityCharging from {self.id}: transaction_id={transaction_id}, "
                           f"activate={activate}")
            
            # Process priority charging request
            if self.priority_charging_manager:
                result = await self.priority_charging_manager.use_priority_charging(
                    self.id, transaction_id, activate
                )
                self.logger.info(f"Priority charging {'activated' if activate else 'deactivated'} for transaction {transaction_id}: {result}")
            else:
                self.logger.warning("Priority charging manager not available")
            
            # Send UsePriorityChargingResponse
            from ocpp.v201 import call
            request = call.UsePriorityChargingResponse(
                status=result["status"],
                statusInfo=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling UsePriorityCharging: {e}")
    
    async def _handle_notify_priority_charging(self, transaction_id: str, activated: bool):
        """Handle NotifyPriorityCharging request."""
        try:
            self.logger.info(f"NotifyPriorityCharging from {self.id}: transaction_id={transaction_id}, "
                           f"activated={activated}")
            
            # Store priority charging status
            result = await self.charging_profile_manager.notify_priority_charging(
                self.id, transaction_id, activated
            )
            
            # Send NotifyPriorityChargingResponse
            from ocpp.v201 import call
            request = call.NotifyPriorityChargingResponse(
                status=result["status"],
                statusInfo=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling NotifyPriorityCharging: {e}")
    
    # V2X/V2G handlers removed - out of scope for MVP per PRD Section 1.2
    # These methods would handle NotifyAllowedEnergyTransfer, SetDERControl, etc.
    # but V2G is explicitly out of scope for MVP
