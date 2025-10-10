"""Enhanced OCPP 2.1 message handler using the official OCPP library."""

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List

from ocpp.routing import on
from ocpp.v21 import ChargePoint as OCPPChargePoint, call_result
from ocpp.v21.enums import Action, ConnectorStatusEnumType, TransactionEventEnumType
from ocpp.v21.datatypes import ChargingStationType, StatusInfoType
from ocpp.exceptions import OCPPError, NotImplementedError, NotSupportedError

from .config import Config
from .monitoring import get_logger
from .timescale_client import TimescaleClient
from .connection_manager import ConnectionManager
from .device_model import DeviceModel
from .charging_profile_manager import ChargingProfileManager
from .transaction_manager import TransactionManager
from .certificate_manager import CertificateManager, CertificateType
from .security_manager import SecurityManager, SecurityConfig


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
        
        # Initialize managers
        self.device_model = DeviceModel(timescale_client)
        self.charging_profile_manager = ChargingProfileManager(timescale_client)
        self.transaction_manager = TransactionManager(timescale_client)
        self.certificate_manager = CertificateManager(timescale_client)
        
        # Initialize security manager
        security_config = SecurityConfig()
        self.security_manager = SecurityManager(timescale_client, security_config)
    
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
        
        # Initialize device model
        asyncio.create_task(self._initialize_device_model())
        
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
    
    # ===== NEW OCPP 2.0.1 MESSAGE HANDLERS =====
    
    @on(Action.get_variables)
    def on_get_variables(self, get_variable_data: list, **kwargs):
        """Handle GetVariables message."""
        self.logger.info(f"GetVariables from {self.id}")
        
        # Process variables request
        asyncio.create_task(self._handle_get_variables(get_variable_data))
        
        return call_result.GetVariables()
    
    @on(Action.set_variables)
    def on_set_variables(self, set_variable_data: list, **kwargs):
        """Handle SetVariables message."""
        self.logger.info(f"SetVariables from {self.id}")
        
        # Process variables set request
        asyncio.create_task(self._handle_set_variables(set_variable_data))
        
        return call_result.SetVariables()
    
    @on(Action.get_base_report)
    def on_get_base_report(self, request_id: int, report_base: str, **kwargs):
        """Handle GetBaseReport message."""
        self.logger.info(f"GetBaseReport from {self.id}")
        
        # Process base report request
        asyncio.create_task(self._handle_get_base_report(request_id, report_base))
        
        return call_result.GetBaseReport()
    
    @on(Action.notify_report)
    def on_notify_report(self, request_id: int, generated_at: str, tbc: bool, 
                        seq_no: int, report_data: list, **kwargs):
        """Handle NotifyReport message."""
        self.logger.info(f"NotifyReport from {self.id}")
        
        # Process report notification
        asyncio.create_task(self._handle_notify_report(
            request_id, generated_at, tbc, seq_no, report_data
        ))
        
        return call_result.NotifyReport()
    
    @on(Action.get_charging_profiles)
    def on_get_charging_profiles(self, request_id: int, evse_id: int, 
                                charging_profile_id: Optional[int] = None,
                                charging_profile_purpose: Optional[str] = None,
                                stack_level: Optional[int] = None, **kwargs):
        """Handle GetChargingProfiles message."""
        self.logger.info(f"GetChargingProfiles from {self.id}")
        
        # Process charging profiles request
        asyncio.create_task(self._handle_get_charging_profiles(
            request_id, evse_id, charging_profile_id, 
            charging_profile_purpose, stack_level
        ))
        
        return call_result.GetChargingProfiles()
    
    @on(Action.clear_charging_profile)
    def on_clear_charging_profile(self, charging_profile_id: Optional[int] = None,
                                 charging_profile_purpose: Optional[str] = None,
                                 stack_level: Optional[int] = None, **kwargs):
        """Handle ClearChargingProfile message."""
        self.logger.info(f"ClearChargingProfile from {self.id}")
        
        # Process clear charging profile request
        asyncio.create_task(self._handle_clear_charging_profile(
            charging_profile_id, charging_profile_purpose, stack_level
        ))
        
        return call_result.ClearChargingProfile()
    
    @on(Action.get_composite_schedule)
    def on_get_composite_schedule(self, request_id: int, evse_id: int, duration: int,
                                 charging_rate_unit: Optional[str] = None, **kwargs):
        """Handle GetCompositeSchedule message."""
        self.logger.info(f"GetCompositeSchedule from {self.id}")
        
        # Process composite schedule request
        asyncio.create_task(self._handle_get_composite_schedule(
            request_id, evse_id, duration, charging_rate_unit
        ))
        
        return call_result.GetCompositeSchedule()
    
    @on(Action.report_charging_profiles)
    def on_report_charging_profiles(self, request_id: int, evse_id: int,
                                   charging_profile: list, **kwargs):
        """Handle ReportChargingProfiles message."""
        self.logger.info(f"ReportChargingProfiles from {self.id}")
        
        # Process charging profiles report
        asyncio.create_task(self._handle_report_charging_profiles(
            request_id, evse_id, charging_profile
        ))
        
        return call_result.ReportChargingProfiles()
    
    @on(Action.request_start_transaction)
    def on_request_start_transaction(self, evse_id: int, id_token: dict,
                                   remote_start_id: Optional[int] = None,
                                   charging_profile: Optional[dict] = None,
                                   evse_id_token: Optional[dict] = None, **kwargs):
        """Handle RequestStartTransaction message."""
        self.logger.info(f"RequestStartTransaction from {self.id}")
        
        # Process start transaction request
        asyncio.create_task(self._handle_request_start_transaction(
            evse_id, id_token, remote_start_id, charging_profile, evse_id_token
        ))
        
        return call_result.RequestStartTransaction()
    
    @on(Action.request_stop_transaction)
    def on_request_stop_transaction(self, transaction_id: str, reason: Optional[str] = None, **kwargs):
        """Handle RequestStopTransaction message."""
        self.logger.info(f"RequestStopTransaction from {self.id}")
        
        # Process stop transaction request
        asyncio.create_task(self._handle_request_stop_transaction(transaction_id, reason))
        
        return call_result.RequestStopTransaction()
    
    @on(Action.reset)
    def on_reset(self, type: str, evse_id: Optional[int] = None, **kwargs):
        """Handle Reset message."""
        self.logger.info(f"Reset from {self.id}: {type}")
        
        # Process reset request
        asyncio.create_task(self._handle_reset(type, evse_id))
        
        return call_result.Reset()
    
    @on(Action.change_availability)
    def on_change_availability(self, operational_status: str, evse: Optional[dict] = None, **kwargs):
        """Handle ChangeAvailability message."""
        self.logger.info(f"ChangeAvailability from {self.id}: {operational_status}")
        
        # Process change availability request
        asyncio.create_task(self._handle_change_availability(operational_status, evse))
        
        return call_result.ChangeAvailability()
    
    @on(Action.trigger_message)
    def on_trigger_message(self, requested_message: str, evse: Optional[dict] = None, **kwargs):
        """Handle TriggerMessage message."""
        self.logger.info(f"TriggerMessage from {self.id}: {requested_message}")
        
        # Process trigger message request
        asyncio.create_task(self._handle_trigger_message(requested_message, evse))
        
        return call_result.TriggerMessage()
    
    @on(Action.unlock_connector)
    def on_unlock_connector(self, evse_id: int, connector_id: int, **kwargs):
        """Handle UnlockConnector message."""
        self.logger.info(f"UnlockConnector from {self.id}: EVSE {evse_id}, Connector {connector_id}")
        
        # Process unlock connector request
        asyncio.create_task(self._handle_unlock_connector(evse_id, connector_id))
        
        return call_result.UnlockConnector()
    
    # ===== ISO 15118 CERTIFICATE HANDLERS =====
    
    @on(Action.get15118_ev_certificate)
    def on_get_15118_ev_certificate(self, certificate_type: str, exi_request: Optional[str] = None, **kwargs):
        """Handle Get15118EVCertificate message."""
        self.logger.info(f"Get15118EVCertificate from {self.id}: {certificate_type}")
        
        # Process certificate request
        asyncio.create_task(self._handle_get_15118_ev_certificate(certificate_type, exi_request))
        
        return call_result.Get15118EVCertificate()
    
    @on(Action.certificate_signed)
    def on_certificate_signed(self, certificate_type: str, certificate_chain: List[str], 
                             exi_response: str, **kwargs):
        """Handle CertificateSigned message."""
        self.logger.info(f"CertificateSigned from {self.id}: {certificate_type}")
        
        # Process certificate signed
        asyncio.create_task(self._handle_certificate_signed(certificate_type, certificate_chain, exi_response))
        
        return call_result.CertificateSigned()
    
    @on(Action.install_certificate)
    def on_install_certificate(self, certificate_type: str, certificate: str, **kwargs):
        """Handle InstallCertificate message."""
        self.logger.info(f"InstallCertificate from {self.id}: {certificate_type}")
        
        # Process certificate installation
        asyncio.create_task(self._handle_install_certificate(certificate_type, certificate))
        
        return call_result.InstallCertificate()
    
    @on(Action.delete_certificate)
    def on_delete_certificate(self, certificate_hash_data: List[Dict[str, Any]], **kwargs):
        """Handle DeleteCertificate message."""
        self.logger.info(f"DeleteCertificate from {self.id}")
        
        # Process certificate deletion
        asyncio.create_task(self._handle_delete_certificate(certificate_hash_data))
        
        return call_result.DeleteCertificate()
    
    @on(Action.get_installed_certificate_ids)
    def on_get_installed_certificate_ids(self, certificate_type: Optional[str] = None, **kwargs):
        """Handle GetInstalledCertificateIds message."""
        self.logger.info(f"GetInstalledCertificateIds from {self.id}")
        
        # Process certificate IDs request
        asyncio.create_task(self._handle_get_installed_certificate_ids(certificate_type))
        
        return call_result.GetInstalledCertificateIds()
    
    @on(Action.sign_certificate)
    def on_sign_certificate(self, certificate_type: str, certificate_signing_request: str, **kwargs):
        """Handle SignCertificate message."""
        self.logger.info(f"SignCertificate from {self.id}: {certificate_type}")
        
        # Process certificate signing
        asyncio.create_task(self._handle_sign_certificate(certificate_type, certificate_signing_request))
        
        return call_result.SignCertificate()
    
    @on(Action.security_event_notification)
    def on_security_event_notification(self, event_type: str, timestamp: str,
                                     tech_info: Optional[str] = None,
                                     additional_info: Optional[Dict[str, Any]] = None, **kwargs):
        """Handle SecurityEventNotification message."""
        self.logger.info(f"SecurityEventNotification from {self.id}: {event_type}")
        
        # Process security event notification
        asyncio.create_task(self._handle_security_event_notification(
            event_type, timestamp, tech_info, additional_info
        ))
        
        return call_result.SecurityEventNotification()
    
    # ===== ASYNC HANDLERS =====
    
    async def _handle_get_variables(self, get_variable_data: list):
        """Handle GetVariables request."""
        try:
            results = await self.device_model.get_variables(self.id, get_variable_data)
            
            # Send GetVariablesResponse
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
            request = call.UnlockConnectorResponse(
                status="Accepted",
                status_info={"reason_code": "NoError"}
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling UnlockConnector: {e}")
    
    async def _initialize_device_model(self):
        """Initialize device model for new station."""
        try:
            await self.device_model.initialize_station_device_model(self.id, self.station_info)
            self.logger.info(f"Initialized device model for station {self.id}")
        except Exception as e:
            self.logger.error(f"Error initializing device model for {self.id}: {e}")
    
    async def _handle_get_15118_ev_certificate(self, certificate_type: str, exi_request: Optional[str]):
        """Handle Get15118EVCertificate request."""
        try:
            cert_type = CertificateType(certificate_type)
            result = await self.certificate_manager.get_15118_ev_certificate(self.id, cert_type, exi_request)
            
            # Send Get15118EVCertificateResponse
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
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
            from ocpp.v21 import call
            request = call.SecurityEventNotificationResponse(
                status=result["status"],
                status_info=result.get("statusInfo")
            )
            await self.call(request)
            
        except Exception as e:
            self.logger.error(f"Error handling SecurityEventNotification: {e}")
