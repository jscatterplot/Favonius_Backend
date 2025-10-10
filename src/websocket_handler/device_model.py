"""OCPP 2.0.1 Device Model implementation for component hierarchy and variable management."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Any, Set
import json

from .monitoring import get_logger
from .timescale_client import TimescaleClient


class ComponentType(Enum):
    """OCPP component types."""
    CHARGING_STATION = "ChargingStation"
    EVSE = "EVSE"
    CONNECTOR = "Connector"
    CABLE = "Cable"
    DISPLAY = "Display"
    LOCAL_AUTH_LIST = "LocalAuthList"
    LOCAL_CONTROLLER = "LocalController"
    MONITORING = "Monitoring"
    PCS = "PCS"  # Power Conversion System
    SECURITY = "Security"
    SMART_CHARGING = "SmartCharging"
    TARIFF_COST = "TariffCost"
    V2X_CONTROLLER = "V2XController"


class VariableType(Enum):
    """OCPP variable types."""
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATETIME = "dateTime"
    ENUM = "enum"


@dataclass
class Component:
    """OCPP component representation."""
    name: str
    instance: Optional[str] = None
    evse: Optional[Dict[str, Any]] = None  # For EVSE components
    
    def __post_init__(self):
        if self.instance is None:
            self.instance = ""


@dataclass
class VariableAttribute:
    """OCPP variable attribute."""
    type: VariableType
    value: Optional[str] = None
    mutability: str = "ReadWrite"  # ReadWrite, ReadOnly, WriteOnly
    persistent: bool = True
    constant: bool = False


@dataclass
class DeviceVariable:
    """OCPP device variable."""
    name: str
    instance: Optional[str] = None
    component: Component = field(default_factory=lambda: Component("ChargingStation"))
    attributes: Dict[str, VariableAttribute] = field(default_factory=dict)
    
    def __post_init__(self):
        if self.instance is None:
            self.instance = ""


class DeviceModel:
    """OCPP 2.0.1 device model manager."""
    
    def __init__(self, timescale_client: TimescaleClient):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        
        # In-memory cache for device components and variables
        self.device_cache: Dict[str, Dict[str, Any]] = {}
        self.variable_cache: Dict[str, Dict[str, DeviceVariable]] = {}
        
        # Standardized OCPP variables per component
        self._initialize_standard_variables()
    
    def _initialize_standard_variables(self) -> None:
        """Initialize standardized OCPP variables."""
        # Charging Station variables
        self._add_standard_variable("ChargingStation", "Model", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "VendorName", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SerialNumber", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "FirmwareVersion", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "Modem", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedFeatures", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedProtocols", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedChargingProfilePurposeTypes", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedChargingProfileTypes", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedMeasurands", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedCableTypes", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedConnectorTypes", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedDisplayMessageTypes", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedIdTokenTypes", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedMessageTypes", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedOcppVersions", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedReservationTypes", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedSecurityProfiles", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedUnitTypes", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedV2GModes", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedV2XChargingCtrlrTypes", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("ChargingStation", "SupportedV2XChargingCtrlrTypes", VariableType.STRING, "ReadOnly")
        
        # EVSE variables
        self._add_standard_variable("EVSE", "AvailabilityState", VariableType.ENUM, "ReadWrite")
        self._add_standard_variable("EVSE", "AvailabilitySchedule", VariableType.STRING, "ReadWrite")
        self._add_standard_variable("EVSE", "Connector", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("EVSE", "ConnectorType", VariableType.ENUM, "ReadOnly")
        self._add_standard_variable("EVSE", "ConnectorTypeId", VariableType.INTEGER, "ReadOnly")
        self._add_standard_variable("EVSE", "MaxEnergy", VariableType.DECIMAL, "ReadOnly")
        self._add_standard_variable("EVSE", "MinEnergy", VariableType.DECIMAL, "ReadOnly")
        self._add_standard_variable("EVSE", "NominalVoltage", VariableType.DECIMAL, "ReadOnly")
        self._add_standard_variable("EVSE", "Power", VariableType.DECIMAL, "ReadOnly")
        self._add_standard_variable("EVSE", "PowerType", VariableType.ENUM, "ReadOnly")
        self._add_standard_variable("EVSE", "ReservationId", VariableType.INTEGER, "ReadWrite")
        self._add_standard_variable("EVSE", "Status", VariableType.ENUM, "ReadOnly")
        self._add_standard_variable("EVSE", "TransactionId", VariableType.INTEGER, "ReadWrite")
        
        # Connector variables
        self._add_standard_variable("Connector", "AvailabilityState", VariableType.ENUM, "ReadWrite")
        self._add_standard_variable("Connector", "AvailabilitySchedule", VariableType.STRING, "ReadWrite")
        self._add_standard_variable("Connector", "Cable", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("Connector", "ConnectorType", VariableType.ENUM, "ReadOnly")
        self._add_standard_variable("Connector", "ConnectorTypeId", VariableType.INTEGER, "ReadOnly")
        self._add_standard_variable("Connector", "MaxEnergy", VariableType.DECIMAL, "ReadOnly")
        self._add_standard_variable("Connector", "MinEnergy", VariableType.DECIMAL, "ReadOnly")
        self._add_standard_variable("Connector", "NominalVoltage", VariableType.DECIMAL, "ReadOnly")
        self._add_standard_variable("Connector", "Power", VariableType.DECIMAL, "ReadOnly")
        self._add_standard_variable("Connector", "PowerType", VariableType.ENUM, "ReadOnly")
        self._add_standard_variable("Connector", "ReservationId", VariableType.INTEGER, "ReadWrite")
        self._add_standard_variable("Connector", "Status", VariableType.ENUM, "ReadOnly")
        self._add_standard_variable("Connector", "TransactionId", VariableType.INTEGER, "ReadWrite")
        
        # Smart Charging variables
        self._add_standard_variable("SmartCharging", "ChargingProfileMaxStackLevel", VariableType.INTEGER, "ReadOnly")
        self._add_standard_variable("SmartCharging", "ChargingScheduleAllowedChargingRateUnit", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("SmartCharging", "ChargingScheduleMaxPeriods", VariableType.INTEGER, "ReadOnly")
        self._add_standard_variable("SmartCharging", "ConnectorSwitch3to1PhaseSupported", VariableType.BOOLEAN, "ReadOnly")
        self._add_standard_variable("SmartCharging", "MaxChargingProfilesInstalled", VariableType.INTEGER, "ReadOnly")
        self._add_standard_variable("SmartCharging", "MaxScheduledChargingProfiles", VariableType.INTEGER, "ReadOnly")
        self._add_standard_variable("SmartCharging", "MaxScheduledChargingProfilesPerEVSE", VariableType.INTEGER, "ReadOnly")
        self._add_standard_variable("SmartCharging", "MaxScheduledChargingProfilesPerEVSEConnector", VariableType.INTEGER, "ReadOnly")
        self._add_standard_variable("SmartCharging", "MaxScheduledChargingProfilesPerEVSEConnector", VariableType.INTEGER, "ReadOnly")
        self._add_standard_variable("SmartCharging", "MaxScheduledChargingProfilesPerEVSEConnector", VariableType.INTEGER, "ReadOnly")
        
        # V2X Controller variables
        self._add_standard_variable("V2XController", "Enabled", VariableType.BOOLEAN, "ReadWrite")
        self._add_standard_variable("V2XController", "SupportedOperationModes", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("V2XController", "TxUpdatedInterval", VariableType.STRING, "ReadWrite")
        self._add_standard_variable("V2XController", "TxUpdatedInterval", VariableType.STRING, "ReadWrite")
        self._add_standard_variable("V2XController", "TxUpdatedInterval", VariableType.STRING, "ReadWrite")
        self._add_standard_variable("V2XController", "TxUpdatedInterval", VariableType.STRING, "ReadWrite")
        
        # Security variables
        self._add_standard_variable("Security", "AdditionalRootCertificateCheck", VariableType.BOOLEAN, "ReadWrite")
        self._add_standard_variable("Security", "CertificateSignedMaxChainSize", VariableType.INTEGER, "ReadOnly")
        self._add_standard_variable("Security", "CertificateStoreMaxLength", VariableType.INTEGER, "ReadOnly")
        self._add_standard_variable("Security", "CpoName", VariableType.STRING, "ReadWrite")
        self._add_standard_variable("Security", "SecurityProfile", VariableType.INTEGER, "ReadOnly")
        self._add_standard_variable("Security", "SupportedFileTransferProtocols", VariableType.STRING, "ReadOnly")
        self._add_standard_variable("Security", "TlsCipherSuite", VariableType.STRING, "ReadWrite")
    
    def _add_standard_variable(self, component: str, name: str, var_type: VariableType, mutability: str) -> None:
        """Add a standard OCPP variable."""
        if component not in self.variable_cache:
            self.variable_cache[component] = {}
        
        variable = DeviceVariable(
            name=name,
            component=Component(name=component),
            attributes={
                "Actual": VariableAttribute(
                    type=var_type,
                    mutability=mutability,
                    persistent=True,
                    constant=False
                )
            }
        )
        
        self.variable_cache[component][name] = variable
    
    async def get_variables(self, station_id: str, get_variable_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Get variables for a station."""
        results = []
        
        for var_data in get_variable_data:
            component = var_data.get("component", {})
            variable = var_data.get("variable", {})
            attribute_type = var_data.get("attributeType", "Actual")
            
            component_name = component.get("name", "ChargingStation")
            component_instance = component.get("instance", "")
            variable_name = variable.get("name", "")
            variable_instance = variable.get("instance", "")
            
            # Get variable from cache or database
            var_value = await self._get_variable_value(
                station_id, component_name, component_instance,
                variable_name, variable_instance, attribute_type
            )
            
            result = {
                "attributeStatus": var_value["status"],
                "component": component,
                "variable": variable,
                "attributeType": attribute_type
            }
            
            if var_value["value"] is not None:
                result["attributeValue"] = var_value["value"]
            
            if var_value["status"] == "Rejected":
                result["attributeStatusInfo"] = {
                    "reasonCode": var_value["reason_code"],
                    "additionalInfo": var_value["additional_info"]
                }
            
            results.append(result)
        
        return results
    
    async def set_variables(self, station_id: str, set_variable_data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Set variables for a station."""
        results = []
        
        for var_data in set_variable_data:
            component = var_data.get("component", {})
            variable = var_data.get("variable", {})
            attribute_type = var_data.get("attributeType", "Actual")
            attribute_value = var_data.get("attributeValue")
            
            component_name = component.get("name", "ChargingStation")
            component_instance = component.get("instance", "")
            variable_name = variable.get("name", "")
            variable_instance = variable.get("instance", "")
            
            # Validate and set variable
            result = await self._set_variable_value(
                station_id, component_name, component_instance,
                variable_name, variable_instance, attribute_type, attribute_value
            )
            
            set_result = {
                "attributeStatus": result["status"],
                "component": component,
                "variable": variable,
                "attributeType": attribute_type
            }
            
            if result["status"] == "Rejected":
                set_result["attributeStatusInfo"] = {
                    "reasonCode": result["reason_code"],
                    "additionalInfo": result["additional_info"]
                }
            
            results.append(set_result)
        
        return results
    
    async def _get_variable_value(
        self, station_id: str, component_name: str, component_instance: str,
        variable_name: str, variable_instance: str, attribute_type: str
    ) -> Dict[str, Any]:
        """Get a variable value from cache or database."""
        try:
            # Check cache first
            cache_key = f"{station_id}:{component_name}:{component_instance}:{variable_name}:{variable_instance}"
            
            if cache_key in self.device_cache:
                cached_value = self.device_cache[cache_key]
                if cached_value.get("attribute_type") == attribute_type:
                    return {
                        "status": "Accepted",
                        "value": cached_value.get("value"),
                        "reason_code": None,
                        "additional_info": None
                    }
            
            # Get from database
            var_data = await self.timescale_client.get_device_variable(
                station_id, component_name, component_instance,
                variable_name, variable_instance, attribute_type
            )
            
            if var_data:
                # Cache the result
                self.device_cache[cache_key] = {
                    "value": var_data["value"],
                    "attribute_type": attribute_type,
                    "timestamp": datetime.now(timezone.utc)
                }
                
                return {
                    "status": "Accepted",
                    "value": var_data["value"],
                    "reason_code": None,
                    "additional_info": None
                }
            else:
                return {
                    "status": "UnknownVariable",
                    "value": None,
                    "reason_code": "UnknownVariable",
                    "additional_info": f"Variable {variable_name} not found"
                }
                
        except Exception as e:
            self.logger.error(f"Error getting variable {variable_name}: {e}")
            return {
                "status": "Rejected",
                "value": None,
                "reason_code": "InternalError",
                "additional_info": str(e)
            }
    
    async def _set_variable_value(
        self, station_id: str, component_name: str, component_instance: str,
        variable_name: str, variable_instance: str, attribute_type: str, value: Any
    ) -> Dict[str, Any]:
        """Set a variable value with validation."""
        try:
            # Validate variable exists and is writable
            if component_name not in self.variable_cache:
                return {
                    "status": "Rejected",
                    "reason_code": "UnknownComponent",
                    "additional_info": f"Component {component_name} not found"
                }
            
            if variable_name not in self.variable_cache[component_name]:
                return {
                    "status": "Rejected",
                    "reason_code": "UnknownVariable",
                    "additional_info": f"Variable {variable_name} not found"
                }
            
            variable = self.variable_cache[component_name][variable_name]
            attribute = variable.attributes.get(attribute_type)
            
            if not attribute:
                return {
                    "status": "Rejected",
                    "reason_code": "UnknownAttribute",
                    "additional_info": f"Attribute {attribute_type} not found"
                }
            
            if attribute.mutability == "ReadOnly":
                return {
                    "status": "Rejected",
                    "reason_code": "WriteDenied",
                    "additional_info": "Variable is read-only"
                }
            
            if attribute.constant:
                return {
                    "status": "Rejected",
                    "reason_code": "WriteDenied",
                    "additional_info": "Variable is constant"
                }
            
            # Validate value type
            if not self._validate_value_type(value, attribute.type):
                return {
                    "status": "Rejected",
                    "reason_code": "TypeConstraintViolation",
                    "additional_info": f"Invalid value type for {attribute.type.value}"
                }
            
            # Store in database
            await self.timescale_client.set_device_variable(
                station_id, component_name, component_instance,
                variable_name, variable_instance, attribute_type, value
            )
            
            # Update cache
            cache_key = f"{station_id}:{component_name}:{component_instance}:{variable_name}:{variable_instance}"
            self.device_cache[cache_key] = {
                "value": value,
                "attribute_type": attribute_type,
                "timestamp": datetime.now(timezone.utc)
            }
            
            return {
                "status": "Accepted",
                "reason_code": None,
                "additional_info": None
            }
            
        except Exception as e:
            self.logger.error(f"Error setting variable {variable_name}: {e}")
            return {
                "status": "Rejected",
                "reason_code": "InternalError",
                "additional_info": str(e)
            }
    
    def _validate_value_type(self, value: Any, var_type: VariableType) -> bool:
        """Validate value matches variable type."""
        if value is None:
            return True
        
        try:
            if var_type == VariableType.STRING:
                return isinstance(value, str)
            elif var_type == VariableType.INTEGER:
                return isinstance(value, int) or (isinstance(value, str) and value.isdigit())
            elif var_type == VariableType.DECIMAL:
                return isinstance(value, (int, float)) or (isinstance(value, str) and value.replace('.', '').isdigit())
            elif var_type == VariableType.BOOLEAN:
                return isinstance(value, bool) or value in ["true", "false", "True", "False"]
            elif var_type == VariableType.DATETIME:
                # Basic datetime validation
                return isinstance(value, str) and len(value) > 10
            elif var_type == VariableType.ENUM:
                return isinstance(value, str)
            else:
                return True
        except Exception:
            return False
    
    async def get_base_report(self, station_id: str, report_base: str) -> Dict[str, Any]:
        """Get base report for device capabilities."""
        try:
            # Get all components and variables for the station
            components = await self.timescale_client.get_device_components(station_id)
            variables = await self.timescale_client.get_device_variables(station_id)
            
            report_data = {
                "reportBase": report_base,
                "reportData": []
            }
            
            for component in components:
                component_data = {
                    "component": {
                        "name": component["name"],
                        "instance": component.get("instance", "")
                    },
                    "variable": []
                }
                
                # Add variables for this component
                for variable in variables:
                    if variable["component_name"] == component["name"]:
                        var_data = {
                            "name": variable["name"],
                            "instance": variable.get("instance", "")
                        }
                        
                        # Add attributes
                        if variable.get("actual_value") is not None:
                            var_data["variableAttribute"] = [{
                                "type": "Actual",
                                "value": variable["actual_value"]
                            }]
                        
                        component_data["variable"].append(var_data)
                
                report_data["reportData"].append(component_data)
            
            return report_data
            
        except Exception as e:
            self.logger.error(f"Error getting base report: {e}")
            return {
                "reportBase": report_base,
                "reportData": []
            }
    
    async def notify_report(self, station_id: str, request_id: int, generated_at: str, 
                          tbc: bool, seq_no: int, report_data: List[Dict[str, Any]]) -> None:
        """Handle NotifyReport message."""
        try:
            # Store report data
            await self.timescale_client.store_device_report(
                station_id, request_id, generated_at, tbc, seq_no, report_data
            )
            
            self.logger.info(f"Stored device report for {station_id}, request {request_id}")
            
        except Exception as e:
            self.logger.error(f"Error storing device report: {e}")
    
    async def initialize_station_device_model(self, station_id: str, station_info: Dict[str, Any]) -> None:
        """Initialize device model for a new station."""
        try:
            # Create default components
            components = [
                {"name": "ChargingStation", "instance": ""},
                {"name": "EVSE", "instance": "1"},
                {"name": "Connector", "instance": "1"},
                {"name": "SmartCharging", "instance": ""},
                {"name": "Security", "instance": ""},
                {"name": "V2XController", "instance": ""}
            ]
            
            # Store components
            for component in components:
                await self.timescale_client.create_device_component(
                    station_id, component["name"], component["instance"]
                )
            
            # Set initial variable values from station info
            initial_variables = {
                "ChargingStation": {
                    "Model": station_info.get("model", "Unknown"),
                    "VendorName": station_info.get("vendor_name", "Unknown"),
                    "SerialNumber": station_info.get("serial_number", "Unknown"),
                    "FirmwareVersion": station_info.get("firmware_version", "Unknown"),
                    "Modem": station_info.get("modem", "Unknown")
                },
                "EVSE": {
                    "AvailabilityState": "Operative",
                    "Status": "Available",
                    "Power": 22.0,  # Default 22kW
                    "NominalVoltage": 400.0
                },
                "Connector": {
                    "AvailabilityState": "Operative",
                    "Status": "Available",
                    "ConnectorType": "IEC_62196_T2"
                },
                "SmartCharging": {
                    "ChargingProfileMaxStackLevel": 10,
                    "MaxChargingProfilesInstalled": 4,
                    "MaxScheduledChargingProfiles": 4
                },
                "Security": {
                    "SecurityProfile": 1,
                    "CertificateStoreMaxLength": 20
                },
                "V2XController": {
                    "Enabled": True,
                    "SupportedOperationModes": "CentralSetpoint"
                }
            }
            
            # Set initial values
            for component_name, variables in initial_variables.items():
                for var_name, var_value in variables.items():
                    await self.timescale_client.set_device_variable(
                        station_id, component_name, "", var_name, "", "Actual", var_value
                    )
            
            self.logger.info(f"Initialized device model for station {station_id}")
            
        except Exception as e:
            self.logger.error(f"Error initializing device model for {station_id}: {e}")
