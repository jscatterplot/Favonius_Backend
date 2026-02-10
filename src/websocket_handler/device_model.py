"""OCPP 2.0.1 Device Model implementation for component hierarchy and variable management."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Dict, List, Optional, Any, Set
import json

from ocpp.v201.enums import AttributeEnumType

from .monitoring import get_logger
from .timescale_client import TimescaleClient
from .cache_manager import CacheManager


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
    # V2X_CONTROLLER = "V2XController"  # Removed - out of scope for MVP per PRD Section 1.2
    METER = "Meter"
    NETWORK = "Network"
    FIRMWARE = "Firmware"
    DIAGNOSTICS = "Diagnostics"
    ALARM = "Alarm"
    CUSTOM = "Custom"


class VariableType(Enum):
    """OCPP variable types."""
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"
    DATETIME = "dateTime"
    ENUM = "enum"


class VariableAccess(Enum):
    """Variable access types."""
    READ_ONLY = "ReadOnly"
    READ_WRITE = "ReadWrite"
    WRITE_ONLY = "WriteOnly"


@dataclass
class StandardOCPPVariables:
    """Standardized OCPP variables by component."""
    
    @staticmethod
    def get_charging_station_variables() -> List[Dict[str, Any]]:
        """Get standardized ChargingStation variables."""
        return [
            {
                "name": "VendorName",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Manufacturer name",
                "required": True
            },
            {
                "name": "Model",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Charging station model",
                "required": True
            },
            {
                "name": "SerialNumber",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Charging station serial number",
                "required": True
            },
            {
                "name": "FirmwareVersion",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Current firmware version",
                "required": False
            },
            {
                "name": "Modem",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Modem configuration",
                "required": False
            },
            {
                "name": "MeterType",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Type of energy meter",
                "required": False
            },
            {
                "name": "MeterSerialNumber",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Energy meter serial number",
                "required": False
            },
            {
                "name": "SupportedFeatureProfiles",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Comma-separated list of supported feature profiles",
                "required": True
            },
            {
                "name": "SupportedProtocolVersions",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Comma-separated list of supported OCPP versions",
                "required": True
            },
            {
                "name": "HeartbeatInterval",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Heartbeat interval in seconds",
                "default_value": "300",
                "min_value": "30",
                "max_value": "86400",
                "required": False
            },
            {
                "name": "MessageTimeout",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Message timeout in seconds",
                "default_value": "30",
                "min_value": "5",
                "max_value": "300",
                "required": False
            },
            {
                "name": "RetryBackOffRandomRange",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Random range for retry backoff in seconds",
                "default_value": "10",
                "min_value": "1",
                "max_value": "60",
                "required": False
            },
            {
                "name": "RetryBackOffRepeatTimes",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Number of retry attempts",
                "default_value": "3",
                "min_value": "1",
                "max_value": "10",
                "required": False
            },
            {
                "name": "RetryBackOffWaitMinimum",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Minimum wait time between retries in seconds",
                "default_value": "10",
                "min_value": "1",
                "max_value": "300",
                "required": False
            },
            {
                "name": "RetryBackOffWaitMaximum",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Maximum wait time between retries in seconds",
                "default_value": "60",
                "min_value": "1",
                "max_value": "600",
                "required": False
            },
            {
                "name": "WebSocketPingInterval",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "WebSocket ping interval in seconds",
                "default_value": "60",
                "min_value": "10",
                "max_value": "300",
                "required": False
            },
            {
                "name": "AuthorizeRemoteTxRequests",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Allow remote transaction start requests",
                "default_value": "true",
                "required": False
            },
            {
                "name": "LocalAuthListEnabled",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Enable local authorization list",
                "default_value": "false",
                "required": False
            },
            {
                "name": "LocalAuthListMaxLength",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Maximum length of local authorization list",
                "default_value": "100",
                "min_value": "1",
                "max_value": "1000",
                "required": False
            },
            {
                "name": "LocalPreAuthorize",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Enable local pre-authorization",
                "default_value": "false",
                "required": False
            },
            {
                "name": "StopTransactionOnEVSideDisconnect",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Stop transaction when EV disconnects",
                "default_value": "true",
                "required": False
            },
            {
                "name": "StopTransactionOnInvalidId",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Stop transaction on invalid ID token",
                "default_value": "true",
                "required": False
            },
            {
                "name": "StopTxnAlignedData",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Align stop transaction data to clock",
                "default_value": "false",
                "required": False
            },
            {
                "name": "StopTxnAlignedDataMaxLength",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Maximum length of aligned data",
                "default_value": "4",
                "min_value": "1",
                "max_value": "10",
                "required": False
            },
            {
                "name": "StopTxnSampledData",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Comma-separated list of sampled data for stop transaction",
                "required": False
            },
            {
                "name": "StopTxnSampledDataMaxLength",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Maximum length of sampled data",
                "default_value": "4",
                "min_value": "1",
                "max_value": "10",
                "required": False
            },
            {
                "name": "MeterValueSampleInterval",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Meter value sampling interval in seconds",
                "default_value": "60",
                "min_value": "1",
                "max_value": "3600",
                "required": False
            },
            {
                "name": "MeterValuesSampledData",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Comma-separated list of sampled data for meter values",
                "required": False
            },
            {
                "name": "MeterValuesAlignedData",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Comma-separated list of aligned data for meter values",
                "required": False
            },
            {
                "name": "MeterValuesAlignedDataMaxLength",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Maximum length of aligned data for meter values",
                "default_value": "4",
                "min_value": "1",
                "max_value": "10",
                "required": False
            },
            {
                "name": "MeterValuesSampledDataMaxLength",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Maximum length of sampled data for meter values",
                "default_value": "4",
                "min_value": "1",
                "max_value": "10",
                "required": False
            },
            {
                "name": "ResetRetries",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Number of reset retry attempts",
                "default_value": "3",
                "min_value": "1",
                "max_value": "10",
                "required": False
            },
            {
                "name": "ConnectorPhaseRotation",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Phase rotation configuration for connectors",
                "required": False
            },
            {
                "name": "ConnectorPhaseRotationMaxLength",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Maximum length of phase rotation configuration",
                "default_value": "3",
                "min_value": "1",
                "max_value": "10",
                "required": False
            },
            {
                "name": "MaxEnergyOnInvalidId",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Maximum energy allowed with invalid ID token in kWh",
                "default_value": "0.0",
                "min_value": "0.0",
                "max_value": "100.0",
                "required": False
            }
        ]
    
    @staticmethod
    def get_evse_variables() -> List[Dict[str, Any]]:
        """Get standardized EVSE variables."""
        return [
            {
                "name": "AvailabilityState",
                "type": VariableType.ENUM.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Current availability state of the EVSE",
                "allowed_values": ["Inoperative", "Operative"],
                "required": True
            },
            {
                "name": "Enabled",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Whether the EVSE is enabled",
                "default_value": "true",
                "required": True
            },
            {
                "name": "Power",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Maximum power of the EVSE in kW",
                "required": True
            },
            {
                "name": "PowerRampUp",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Power ramp-up rate in kW/s",
                "default_value": "1.0",
                "min_value": "0.1",
                "max_value": "10.0",
                "required": False
            },
            {
                "name": "PowerRampDown",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Power ramp-down rate in kW/s",
                "default_value": "1.0",
                "min_value": "0.1",
                "max_value": "10.0",
                "required": False
            },
            {
                "name": "ReservationUpdateInterval",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Reservation update interval in seconds",
                "default_value": "60",
                "min_value": "10",
                "max_value": "3600",
                "required": False
            },
            {
                "name": "UnavailableWhenEvDisconnected",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Set EVSE unavailable when EV disconnects",
                "default_value": "false",
                "required": False
            },
            {
                "name": "V2XCapability",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Whether the EVSE supports V2X",
                "default_value": "false",
                "required": False
            },
            {
                "name": "V2XCapabilityMaxPower",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Maximum V2X power in kW",
                "default_value": "0.0",
                "required": False
            },
            {
                "name": "V2XCapabilityMinPower",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Minimum V2X power in kW",
                "default_value": "0.0",
                "required": False
            }
        ]
    
    @staticmethod
    def get_connector_variables() -> List[Dict[str, Any]]:
        """Get standardized Connector variables."""
        return [
            {
                "name": "AvailabilityState",
                "type": VariableType.ENUM.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Current availability state of the connector",
                "allowed_values": ["Inoperative", "Operative"],
                "required": True
            },
            {
                "name": "Enabled",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Whether the connector is enabled",
                "default_value": "true",
                "required": True
            },
            {
                "name": "ConnectorType",
                "type": VariableType.ENUM.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Type of connector",
                "allowed_values": ["cCCS1", "cCCS2", "cG105", "cTesla", "cType1", "cType2", "s309-1P-16A", "s309-1P-32A", "s309-3P-16A", "s309-3P-32A", "sBS1361", "sCEE-7-7", "sType2", "sType3", "Other1PhMax16A", "Other1PhOver16A", "Other3Ph", "Pan", "wInductive", "wResonant", "Undetermined", "Unknown"],
                "required": True
            },
            {
                "name": "ConnectorFormat",
                "type": VariableType.ENUM.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Format of the connector",
                "allowed_values": ["Socket", "Cable"],
                "required": True
            },
            {
                "name": "ConnectorPowerType",
                "type": VariableType.ENUM.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Power type of the connector",
                "allowed_values": ["AC1", "AC3", "DC"],
                "required": True
            },
            {
                "name": "MaxVoltage",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Maximum voltage in V",
                "required": True
            },
            {
                "name": "MaxAmperage",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Maximum amperage in A",
                "required": True
            },
            {
                "name": "MaxElectricPower",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Maximum electric power in W",
                "required": True
            },
            {
                "name": "MaxOfferedCurrent",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Maximum offered current in A",
                "required": False
            },
            {
                "name": "MaxOfferedPower",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Maximum offered power in W",
                "required": False
            },
            {
                "name": "MinOfferedCurrent",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Minimum offered current in A",
                "default_value": "0",
                "required": False
            },
            {
                "name": "MinOfferedPower",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Minimum offered power in W",
                "default_value": "0",
                "required": False
            }
        ]
    
    @staticmethod
    def get_smart_charging_variables() -> List[Dict[str, Any]]:
        """Get standardized SmartCharging variables."""
        return [
            {
                "name": "ChargingScheduleAllowedChargingRateUnit",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Allowed charging rate units",
                "default_value": "W,A",
                "required": False
            },
            {
                "name": "ChargingScheduleMaxPeriods",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Maximum number of periods in charging schedule",
                "default_value": "1024",
                "min_value": "1",
                "max_value": "1024",
                "required": False
            },
            {
                "name": "ConnectorSwitch3to1PhaseSupported",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Support for switching from 3-phase to 1-phase",
                "default_value": "false",
                "required": False
            },
            {
                "name": "MaxChargingProfilesInstalled",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Maximum number of charging profiles that can be installed",
                "default_value": "10",
                "min_value": "1",
                "max_value": "100",
                "required": False
            }
        ]
    
    @staticmethod
    def get_v2x_charging_ctrlr_variables() -> List[Dict[str, Any]]:
        """Get standardized V2XChargingCtrlr variables."""
        return [
            {
                "name": "Enabled",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Activate/deactivate V2X functionality",
                "default_value": "false",
                "required": True
            },
            {
                "name": "SupportedEnergyTransferModes",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Lists supported energy transfer services (AC_BPT, DC_BPT, etc.)",
                "default_value": "AC_BPT,DC_BPT",
                "required": True
            },
            {
                "name": "SupportedOperationModes",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Supported operation modes",
                "default_value": "ChargingOnly,CentralSetpoint,LocalFrequency,LocalLoadBalancing",
                "required": True
            },
            {
                "name": "LocalFrequencyUpdateThreshold",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Net frequency change threshold to trigger setpoint recalculation in mHz",
                "default_value": "50",
                "min_value": "1",
                "max_value": "1000",
                "required": True
            },
            {
                "name": "TxStartedMeasurands",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Measurands for transaction started events",
                "default_value": "Power.Active.Import,Power.Active.Export,SoC",
                "required": False
            },
            {
                "name": "TxEndedMeasurands",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Measurands for transaction ended events",
                "default_value": "Energy.Active.Import.Register,Energy.Active.Export.Register",
                "required": False
            },
            {
                "name": "TxUpdatedMeasurands",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Measurands for transaction updated events",
                "default_value": "Power.Active.Import,Power.Active.Export,SoC,Frequency",
                "required": False
            },
            {
                "name": "TxEndedInterval",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Interval for transaction ended events in seconds",
                "default_value": "60",
                "min_value": "1",
                "max_value": "3600",
                "required": False
            },
            {
                "name": "TxUpdatedInterval",
                "type": VariableType.INTEGER.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Interval for transaction updated events in seconds",
                "default_value": "30",
                "min_value": "1",
                "max_value": "3600",
                "required": False
            },
            {
                "name": "LocalLoadBalancing.UpperThreshold",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Upper threshold for local load balancing in Watts",
                "default_value": "10000.0",
                "required": True
            },
            {
                "name": "LocalLoadBalancing.LowerThreshold",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Lower threshold for local load balancing in Watts",
                "default_value": "5000.0",
                "required": True
            },
            {
                "name": "LocalLoadBalancing.UpperOffset",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Upper offset for local load balancing in Watts",
                "default_value": "1000.0",
                "required": True
            },
            {
                "name": "LocalLoadBalancing.LowerOffset",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Lower offset for local load balancing in Watts",
                "default_value": "1000.0",
                "required": True
            }
        ]
    
    @staticmethod
    def get_dc_der_ctrlr_variables() -> List[Dict[str, Any]]:
        """Get standardized DCDERCtrlr variables."""
        return [
            # Power Ratings
            {
                "name": "MaxW",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Active power rating at unity power factor in Watts",
                "required": True
            },
            {
                "name": "OverExcitedW",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Active power at specified over-excited power factor in Watts",
                "required": True
            },
            {
                "name": "OverExcitedPF",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Over-excited power factor",
                "required": True
            },
            {
                "name": "UnderExcitedW",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Active power at specified under-excited power factor in Watts",
                "required": True
            },
            {
                "name": "UnderExcitedPF",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Under-excited power factor",
                "required": True
            },
            {
                "name": "MaxVA",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Maximum apparent power rating in VA",
                "required": True
            },
            {
                "name": "MaxVar",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Maximum injected reactive power in var",
                "required": True
            },
            {
                "name": "MaxVarNeg",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Maximum absorbed reactive power in var",
                "required": True
            },
            {
                "name": "MaxChargeRateW",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Maximum active power charge rating in Watts",
                "required": True
            },
            {
                "name": "MaxChargeRateVA",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Maximum apparent power charge rating in VA",
                "required": True
            },
            # Voltage Ratings
            {
                "name": "VNom",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Nominal AC voltage rating in Volts RMS",
                "required": False
            },
            {
                "name": "MaxV",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Maximum AC voltage rating in Volts RMS",
                "required": False
            },
            {
                "name": "MinV",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Minimum AC voltage rating in Volts RMS",
                "required": False
            },
            # Control Support
            {
                "name": "ModesSupported",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Supported control mode functions",
                "default_value": "FixedPFInject,VoltVar,WattVar,FixedVar,VoltWatt,FreqDroop",
                "required": True
            },
            # Hardware Information
            {
                "name": "InverterManufacturer",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Inverter manufacturer",
                "required": True
            },
            {
                "name": "InverterModel",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Inverter model",
                "required": True
            },
            {
                "name": "InverterSerialNumber",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Inverter serial number",
                "required": False
            },
            {
                "name": "InverterSwVersion",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Inverter software version",
                "required": True
            },
            {
                "name": "InverterHwVersion",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Inverter hardware version",
                "required": True
            },
            # Grid Protection
            {
                "name": "IslandingDetectionMethod",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Islanding detection method",
                "default_value": "Passive",
                "required": False
            },
            {
                "name": "IslandingDetectionTripTime",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Islanding detection trip time in seconds",
                "default_value": "2.0",
                "required": False
            },
            {
                "name": "ReactiveSusceptance",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "Reactive susceptance in cease to energize state",
                "required": True
            }
        ]
    
    @staticmethod
    def get_ac_der_ctrlr_variables() -> List[Dict[str, Any]]:
        """Get standardized ACDERCtrlr variables."""
        return [
            {
                "name": "modesSupported",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_ONLY.value,
                "description": "DER controls that can be locally emulated via ChargeLoop",
                "default_value": "FixedPFInject,VoltVar,WattVar,FixedVar,VoltWatt",
                "required": True
            }
        ]
    
    @staticmethod
    def get_data_collector_variables() -> List[Dict[str, Any]]:
        """Get standardized DataCollector variables."""
        return [
            {
                "name": "Enabled",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Activate/deactivate data collection",
                "default_value": "false",
                "required": False
            },
            {
                "name": "DateTime.Start",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Collection start time",
                "required": False
            },
            {
                "name": "DateTime.End",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Collection end time",
                "required": False
            },
            {
                "name": "SampledMeasurands",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Measurands to collect",
                "default_value": "Power.Active.Import,Power.Active.Export,Frequency,Voltage",
                "required": False
            },
            {
                "name": "SamplingInterval",
                "type": VariableType.DECIMAL.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Sampling frequency in seconds",
                "default_value": "0.1",
                "min_value": "0.01",
                "max_value": "1.0",
                "required": False
            }
        ]
    
    @staticmethod
    def get_frequency_simulator_variables() -> List[Dict[str, Any]]:
        """Get standardized FrequencySimulator variables."""
        return [
            {
                "name": "Enabled",
                "type": VariableType.BOOLEAN.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Enable frequency simulation",
                "default_value": "false",
                "required": False
            },
            {
                "name": "DateTime.Start",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Simulation start time",
                "required": False
            },
            {
                "name": "DateTime.End",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Simulation end time",
                "required": False
            },
            {
                "name": "FrequencySchedule",
                "type": VariableType.STRING.value,
                "access": VariableAccess.READ_WRITE.value,
                "description": "Simulated frequency profile as JSON",
                "default_value": '[{"time": 0, "freq": 50.0}, {"time": 60, "freq": 49.2}]',
                "required": False
            }
        ]
    
    # V2X controller variables removed - out of scope for MVP per PRD Section 1.2
    @staticmethod
    def get_v2x_controller_variables() -> List[Dict[str, Any]]:
        """Get standardized V2XController variables.
        
        Note: V2X/V2G is out of scope for MVP per PRD Section 1.2.
        This method is kept for future reference but returns empty list.
        """
        return []


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
    
    def __init__(self, timescale_client: TimescaleClient, cache_manager: Optional[CacheManager] = None):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        
        # Initialize cache manager
        self.cache_manager = cache_manager or CacheManager(max_size=2000, default_ttl=timedelta(seconds=300))
        
        # Legacy in-memory cache for device components and variables
        self.device_cache: Dict[str, Dict[str, Any]] = {}
        self.variable_cache: Dict[str, Dict[str, DeviceVariable]] = {}
        
        # Standardized OCPP variables per component
        self._initialize_standard_variables()
    
    def _initialize_standard_variables(self) -> None:
        """Initialize standardized OCPP variables."""
        # Charging Station variables
        self._add_standard_variable("ChargingStation", "Model", VariableType.STRING, "ReadWrite")
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
        self._add_standard_variable("ChargingStation", "HeartbeatInterval", VariableType.INTEGER, "ReadWrite")
        
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
        
        # Monitoring variables
        self._add_standard_variable("Monitoring", "HeartbeatInterval", VariableType.INTEGER, "ReadWrite")
        self._add_standard_variable("Monitoring", "ClockAlignedDataInterval", VariableType.INTEGER, "ReadWrite")
        
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
    
    def _add_component(self, station_id: str, component: str, evse_id: str = "") -> None:
        """Add a component to the device model."""
        component_key = f"{station_id}:{component}:{evse_id}"
        if station_id not in self.device_cache:
            self.device_cache[station_id] = {}
        if component not in self.device_cache[station_id]:
            self.device_cache[station_id][component] = {}
        if evse_id not in self.device_cache[station_id][component]:
            self.device_cache[station_id][component][evse_id] = {}
        
        self.logger.debug(f"Added component {component} for station {station_id}, evse {evse_id}")

    async def initialize_complete_device_model(self, station_id: str, station_info: Dict[str, Any]) -> None:
        """Initialize complete device model with full component hierarchy."""
        try:
            self.logger.info(f"Initializing complete device model for station {station_id}")
            
            # Initialize ChargingStation component
            await self._initialize_charging_station_component(station_id, station_info)
            
            # Initialize EVSE components
            await self._initialize_evse_components(station_id, station_info)
            
            # Initialize Connector components
            await self._initialize_connector_components(station_id, station_info)
            
            # Initialize SmartCharging component
            await self._initialize_smart_charging_component(station_id)
            
            # V2XController component removed - out of scope for MVP per PRD Section 1.2
            
            # Initialize V2G-specific components
            await self._initialize_v2x_charging_ctrlr_component(station_id)
            await self._initialize_dc_der_ctrlr_component(station_id)
            await self._initialize_ac_der_ctrlr_component(station_id)
            
            # Initialize per-EVSE V2G components
            num_evses = station_info.get("num_evses", 1)
            for evse_id in range(1, num_evses + 1):
                await self._initialize_data_collector_component(station_id, evse_id)
                await self._initialize_frequency_simulator_component(station_id, evse_id)
            
            # Initialize Security component
            await self._initialize_security_component(station_id)
            
            # Initialize ISO15118Ctrlr component
            await self._initialize_iso15118_ctrlr_component(station_id)
            
            # Initialize Display component
            await self._initialize_display_component(station_id)
            
            # Initialize Meter component
            await self._initialize_meter_component(station_id)
            
            # Initialize Network component
            await self._initialize_network_component(station_id)
            
            # Initialize Firmware component
            await self._initialize_firmware_component(station_id)
            
            # Initialize Diagnostics component
            await self._initialize_diagnostics_component(station_id)
            
            self.logger.info(f"Complete device model initialized for station {station_id}")
            
        except Exception as e:
            self.logger.error(f"Error initializing complete device model: {e}")
            raise
    
    async def _initialize_charging_station_component(self, station_id: str, station_info: Dict[str, Any]) -> None:
        """Initialize ChargingStation component."""
        self._add_component(station_id, "ChargingStation", "")
        
        # Add all ChargingStation variables
        variables = StandardOCPPVariables.get_charging_station_variables()
        for var_def in variables:
            await self._set_variable_value(
                station_id, "ChargingStation", "", var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
        
        # Set station-specific values
        await self._set_variable_value(station_id, "ChargingStation", "", "VendorName", "", 
                               AttributeEnumType.actual, station_info.get("vendor_name", "Unknown"))
        await self._set_variable_value(station_id, "ChargingStation", "", "Model", "", 
                               AttributeEnumType.actual, station_info.get("model", "Unknown"))
        await self._set_variable_value(station_id, "ChargingStation", "", "SerialNumber", "", 
                               AttributeEnumType.actual, station_info.get("serial_number", "Unknown"))
        await self._set_variable_value(station_id, "ChargingStation", "", "FirmwareVersion", "", 
                               AttributeEnumType.actual, station_info.get("firmware_version", "1.0.0"))
        await self._set_variable_value(station_id, "ChargingStation", "", "SupportedFeatureProfiles", "", 
                               AttributeEnumType.actual, "Core,SmartCharging,RemoteTrigger,Reservation,LocalAuthListManagement,SoC,RemoteControl,DisplayMessages,ISO15118Pnc,ISO15118Common,DeviceData,Monitoring,LocalListManagement,ExtendedTriggerMessage,ISO15118PnC,ISO15118Common,DeviceData,Monitoring,LocalListManagement,ExtendedTriggerMessage")
        await self._set_variable_value(station_id, "ChargingStation", "", "SupportedProtocolVersions", "", 
                               AttributeEnumType.actual, "2.0.1")
    
    async def _initialize_evse_components(self, station_id: str, station_info: Dict[str, Any]) -> None:
        """Initialize EVSE components."""
        # Get number of EVSEs from station info or default to 1
        num_evses = station_info.get("num_evses", 1)
        
        for evse_id in range(1, num_evses + 1):
            self._add_component(station_id, "EVSE", str(evse_id))
            
            # Add all EVSE variables
            variables = StandardOCPPVariables.get_evse_variables()
            for var_def in variables:
                await self._set_variable_value(
                    station_id, "EVSE", str(evse_id), var_def["name"], "",
                    AttributeEnumType.actual, var_def.get("default_value", "")
                )
            
            # Set EVSE-specific values
            await self._set_variable_value(station_id, "EVSE", str(evse_id), "AvailabilityState", "", 
                                   AttributeEnumType.actual, "Operative")
            await self._set_variable_value(station_id, "EVSE", str(evse_id), "Enabled", "", 
                                   AttributeEnumType.actual, "true")
            await self._set_variable_value(station_id, "EVSE", str(evse_id), "Power", "", 
                                   AttributeEnumType.actual, str(station_info.get("max_power", 22.0)))
            await self._set_variable_value(station_id, "EVSE", str(evse_id), "V2XCapability", "", 
                                   AttributeEnumType.actual, str(station_info.get("v2x_capable", False)).lower())
    
    async def _initialize_connector_components(self, station_id: str, station_info: Dict[str, Any]) -> None:
        """Initialize Connector components."""
        # Get number of connectors from station info or default to 1
        num_connectors = station_info.get("num_connectors", 1)
        
        for connector_id in range(1, num_connectors + 1):
            self._add_component(station_id, "Connector", str(connector_id))
            
            # Add all Connector variables
            variables = StandardOCPPVariables.get_connector_variables()
            for var_def in variables:
                await self._set_variable_value(
                    station_id, "Connector", str(connector_id), var_def["name"], "",
                    AttributeEnumType.actual, var_def.get("default_value", "")
                )
            
            # Set Connector-specific values
            await self._set_variable_value(station_id, "Connector", str(connector_id), "AvailabilityState", "", 
                                   AttributeEnumType.actual, "Operative")
            await self._set_variable_value(station_id, "Connector", str(connector_id), "Enabled", "", 
                                   AttributeEnumType.actual, "true")
            await self._set_variable_value(station_id, "Connector", str(connector_id), "ConnectorType", "", 
                                   AttributeEnumType.actual, station_info.get("connector_type", "cType2"))
            await self._set_variable_value(station_id, "Connector", str(connector_id), "ConnectorFormat", "", 
                                   AttributeEnumType.actual, "Socket")
            await self._set_variable_value(station_id, "Connector", str(connector_id), "ConnectorPowerType", "", 
                                   AttributeEnumType.actual, "AC3")
            await self._set_variable_value(station_id, "Connector", str(connector_id), "MaxVoltage", "", 
                                   AttributeEnumType.actual, str(station_info.get("max_voltage", 400)))
            await self._set_variable_value(station_id, "Connector", str(connector_id), "MaxAmperage", "", 
                                   AttributeEnumType.actual, str(station_info.get("max_amperage", 32)))
            await self._set_variable_value(station_id, "Connector", str(connector_id), "MaxElectricPower", "", 
                                   AttributeEnumType.actual, str(station_info.get("max_power", 22000)))
    
    async def _initialize_smart_charging_component(self, station_id: str) -> None:
        """Initialize SmartCharging component."""
        self._add_component(station_id, "SmartCharging", "")
        
        # Add all SmartCharging variables
        variables = StandardOCPPVariables.get_smart_charging_variables()
        for var_def in variables:
            await self._set_variable_value(
                station_id, "SmartCharging", "", var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
        
        # Add V2G-specific SmartCharging variables
        v2g_smart_charging_variables = [
            {"name": "ExternalControlSignalsEnabled", "type": VariableType.BOOLEAN.value, "default_value": "false"},
            {"name": "ExternalConstraintsProfileDisallowed", "type": VariableType.BOOLEAN.value, "default_value": "false"},
            {"name": "NotifyChargingLimitWithSchedules", "type": VariableType.BOOLEAN.value, "default_value": "true"},
            {"name": "SetpointPriority", "type": VariableType.STRING.value, "default_value": "ExternalSystem"},
            {"name": "MaxExternalConstraintsId", "type": VariableType.INTEGER.value, "default_value": "100"},
            {"name": "LimitChangeSignificance", "type": VariableType.DECIMAL.value, "default_value": "5.0"}
        ]
        
        for var_def in v2g_smart_charging_variables:
            await self._set_variable_value(
                station_id, "SmartCharging", "", var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
    
    # V2XController initialization removed - out of scope for MVP per PRD Section 1.2
    # async def _initialize_v2x_controller_component(self, station_id: str) -> None:
    #     """Initialize V2XController component."""
    #     ...
    
    async def _initialize_v2x_charging_ctrlr_component(self, station_id: str) -> None:
        """Initialize V2XChargingCtrlr component."""
        self._add_component(station_id, "V2XChargingCtrlr", "")
        
        # Add all V2XChargingCtrlr variables
        variables = StandardOCPPVariables.get_v2x_charging_ctrlr_variables()
        for var_def in variables:
            await self._set_variable_value(
                station_id, "V2XChargingCtrlr", "", var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
    
    async def _initialize_dc_der_ctrlr_component(self, station_id: str) -> None:
        """Initialize DCDERCtrlr component."""
        self._add_component(station_id, "DCDERCtrlr", "")
        
        # Add all DCDERCtrlr variables
        variables = StandardOCPPVariables.get_dc_der_ctrlr_variables()
        for var_def in variables:
            await self._set_variable_value(
                station_id, "DCDERCtrlr", "", var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
    
    async def _initialize_ac_der_ctrlr_component(self, station_id: str) -> None:
        """Initialize ACDERCtrlr component."""
        self._add_component(station_id, "ACDERCtrlr", "")
        
        # Add all ACDERCtrlr variables
        variables = StandardOCPPVariables.get_ac_der_ctrlr_variables()
        for var_def in variables:
            await self._set_variable_value(
                station_id, "ACDERCtrlr", "", var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
    
    async def _initialize_data_collector_component(self, station_id: str, evse_id: int) -> None:
        """Initialize DataCollector component."""
        self._add_component(station_id, "DataCollector", str(evse_id))
        
        # Add all DataCollector variables
        variables = StandardOCPPVariables.get_data_collector_variables()
        for var_def in variables:
            await self._set_variable_value(
                station_id, "DataCollector", str(evse_id), var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
    
    async def _initialize_frequency_simulator_component(self, station_id: str, evse_id: int) -> None:
        """Initialize FrequencySimulator component."""
        self._add_component(station_id, "FrequencySimulator", str(evse_id))
        
        # Add all FrequencySimulator variables
        variables = StandardOCPPVariables.get_frequency_simulator_variables()
        for var_def in variables:
            await self._set_variable_value(
                station_id, "FrequencySimulator", str(evse_id), var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
    
    async def _initialize_security_component(self, station_id: str) -> None:
        """Initialize Security component."""
        self._add_component(station_id, "Security", "")
        
        # Add Security variables
        security_variables = [
            {"name": "SecurityProfile", "type": VariableType.INTEGER.value, "default_value": "3"},
            {"name": "AdditionalRootCertificateCheck", "type": VariableType.BOOLEAN.value, "default_value": "true"},
            {"name": "CertificateSignedMaxChainSize", "type": VariableType.INTEGER.value, "default_value": "5"},
            {"name": "CertificateStoreMaxLength", "type": VariableType.INTEGER.value, "default_value": "100"},
            {"name": "CpoName", "type": VariableType.STRING.value, "default_value": "FavoniusEnergy"},
            {"name": "SupportedFileTransferProtocols", "type": VariableType.STRING.value, "default_value": "HTTPS"},
            {"name": "TlsCipherSuite", "type": VariableType.STRING.value, "default_value": "TLS_AES_256_GCM_SHA384"}
        ]
        
        for var_def in security_variables:
            await self._set_variable_value(
                station_id, "Security", "", var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
    
    async def _initialize_iso15118_ctrlr_component(self, station_id: str) -> None:
        """Initialize ISO15118Ctrlr component."""
        self._add_component(station_id, "ISO15118Ctrlr", "")
        
        # Add ISO15118Ctrlr variables
        iso15118_variables = [
            {"name": "SeccId", "type": VariableType.STRING.value, "default_value": "SECC001"},
            {"name": "ISO15118PnCEnabled", "type": VariableType.BOOLEAN.value, "default_value": "true"},
            {"name": "CentralContractValidationAllowed", "type": VariableType.BOOLEAN.value, "default_value": "true"},
            {"name": "ContractValidationOffline", "type": VariableType.BOOLEAN.value, "default_value": "false"},
            {"name": "SupportedFeatures", "type": VariableType.STRING.value, "default_value": "PnC,ContractCertificate,Payment"},
            {"name": "SupportedPaymentMethods", "type": VariableType.STRING.value, "default_value": "Contract,ExternalPayment"},
            {"name": "MaxContractIdLength", "type": VariableType.INTEGER.value, "default_value": "36"},
            {"name": "MaxCertificateChainSize", "type": VariableType.INTEGER.value, "default_value": "5"},
            {"name": "CertificateInstallationTimeout", "type": VariableType.INTEGER.value, "default_value": "300"},
            {"name": "CertificateUpdateTimeout", "type": VariableType.INTEGER.value, "default_value": "60"}
        ]
        
        for var_def in iso15118_variables:
            await self._set_variable_value(
                station_id, "ISO15118Ctrlr", "", var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
    
    async def _initialize_display_component(self, station_id: str) -> None:
        """Initialize Display component."""
        self._add_component(station_id, "Display", "")
        
        # Add Display variables
        display_variables = [
            {"name": "SupportedDisplayMessageTypes", "type": VariableType.STRING.value, "default_value": "Normal,Info,Warning,Error"},
            {"name": "SupportedLanguages", "type": VariableType.STRING.value, "default_value": "en,de,fr,es"},
            {"name": "MaxDisplayMessageLength", "type": VariableType.INTEGER.value, "default_value": "160"},
            {"name": "NumberOfDisplays", "type": VariableType.INTEGER.value, "default_value": "1"}
        ]
        
        for var_def in display_variables:
            await self._set_variable_value(
                station_id, "Display", "", var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
    
    async def _initialize_meter_component(self, station_id: str) -> None:
        """Initialize Meter component."""
        self._add_component(station_id, "Meter", "")
        
        # Add Meter variables
        meter_variables = [
            {"name": "MeterType", "type": VariableType.STRING.value, "default_value": "EnergyMeter"},
            {"name": "MeterSerialNumber", "type": VariableType.STRING.value, "default_value": "METER001"},
            {"name": "MeterCalibration", "type": VariableType.STRING.value, "default_value": "Class1"},
            {"name": "MeterAccuracy", "type": VariableType.DECIMAL.value, "default_value": "0.1"},
            {"name": "MeterSamplingInterval", "type": VariableType.INTEGER.value, "default_value": "60"}
        ]
        
        for var_def in meter_variables:
            await self._set_variable_value(
                station_id, "Meter", "", var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
    
    async def _initialize_network_component(self, station_id: str) -> None:
        """Initialize Network component."""
        self._add_component(station_id, "Network", "")
        
        # Add Network variables
        network_variables = [
            {"name": "NetworkInterface", "type": VariableType.STRING.value, "default_value": "Ethernet,WiFi,4G"},
            {"name": "NetworkSecurity", "type": VariableType.STRING.value, "default_value": "WPA2,TLS"},
            {"name": "NetworkTimeout", "type": VariableType.INTEGER.value, "default_value": "30"},
            {"name": "NetworkRetryCount", "type": VariableType.INTEGER.value, "default_value": "3"}
        ]
        
        for var_def in network_variables:
            await self._set_variable_value(
                station_id, "Network", "", var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
    
    async def _initialize_firmware_component(self, station_id: str) -> None:
        """Initialize Firmware component."""
        self._add_component(station_id, "Firmware", "")
        
        # Add Firmware variables
        firmware_variables = [
            {"name": "FirmwareVersion", "type": VariableType.STRING.value, "default_value": "1.0.0"},
            {"name": "FirmwareUpdateStatus", "type": VariableType.ENUM.value, "default_value": "Idle"},
            {"name": "FirmwareUpdateProgress", "type": VariableType.INTEGER.value, "default_value": "0"},
            {"name": "FirmwareUpdateRetryCount", "type": VariableType.INTEGER.value, "default_value": "3"},
            {"name": "FirmwareUpdateTimeout", "type": VariableType.INTEGER.value, "default_value": "3600"}
        ]
        
        for var_def in firmware_variables:
            await self._set_variable_value(
                station_id, "Firmware", "", var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
    
    async def _initialize_diagnostics_component(self, station_id: str) -> None:
        """Initialize Diagnostics component."""
        self._add_component(station_id, "Diagnostics", "")
        
        # Add Diagnostics variables
        diagnostics_variables = [
            {"name": "LogLevel", "type": VariableType.ENUM.value, "default_value": "Info"},
            {"name": "LogMaxEntries", "type": VariableType.INTEGER.value, "default_value": "1000"},
            {"name": "LogRetentionDays", "type": VariableType.INTEGER.value, "default_value": "30"},
            {"name": "DiagnosticStatus", "type": VariableType.ENUM.value, "default_value": "Idle"},
            {"name": "DiagnosticProgress", "type": VariableType.INTEGER.value, "default_value": "0"}
        ]
        
        for var_def in diagnostics_variables:
            await self._set_variable_value(
                station_id, "Diagnostics", "", var_def["name"], "",
                AttributeEnumType.actual, var_def.get("default_value", "")
            )
    
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
            cache_key = f"{station_id}:{component_name}:{component_instance}:{variable_name}:{variable_instance}:{attribute_type}"
            
            # Check new cache manager first
            cached_value = await self.cache_manager.get(cache_key)
            if cached_value:
                self.logger.debug(f"Variable {cache_key} found in cache")
                return {
                    "status": "Accepted",
                    "value": cached_value.get("value"),
                    "reason_code": None,
                    "additional_info": None
                }
            
            # Check legacy cache
            legacy_cache_key = f"{station_id}:{component_name}:{component_instance}:{variable_name}:{variable_instance}"
            if legacy_cache_key in self.device_cache:
                cached_value = self.device_cache[legacy_cache_key]
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
                # Cache the result in both caches
                cache_data = {
                    "value": var_data["value"],
                    "attribute_type": attribute_type,
                    "timestamp": datetime.now(timezone.utc)
                }
                await self.cache_manager.set(cache_key, cache_data, ttl=timedelta(seconds=300))
                self.device_cache[legacy_cache_key] = cache_data
                
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
                {"name": "V2XController", "instance": ""},
                {"name": "Monitoring", "instance": ""}
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
                },
                "Monitoring": {
                    "HeartbeatInterval": 300,
                    "ClockAlignedDataInterval": 900
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
