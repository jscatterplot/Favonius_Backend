"""VDV 463 message parsing and validation.

Implements schema-backed validation using official VDV 463 JSON schemas.
See https://github.com/VDVde/VDV463/tree/main/schema for schema definitions.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal
import jsonschema
from jsonschema import validate, ValidationError
import structlog


def get_logger(name: str) -> structlog.BoundLogger:
    """Get a structured logger instance."""
    return structlog.get_logger(name)


logger = get_logger(__name__)


class ValidationMode(str, Enum):
    """VDV 463 validation mode."""
    HARD = "hard"  # Reject invalid messages
    SOFT = "soft"  # Accept with warnings


class VDV463ValidationError(Exception):
    """Raised when VDV 463 message validation fails."""
    
    def __init__(self, message: str, error_code: str = "SchemaValidationError", 
                 details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.error_code = error_code
        self.details = details or {}


@dataclass
class VDVMessageEnvelope:
    """Base VDV 463 message envelope structure.
    
    Per MessageStructure.json:
    [0] MessageType (1=Request, 2=Confirmation, 3=Error)
    [1] Source ("BMS" | "ITCS" | "CMS")
    [2] PresystemId (string)
    [3] Timestamp (date-time)
    [4] MessageId (UUID string)
    [5] MessageAction (string)
    [6] Payload (object)
    """
    message_type: int  # 1=Request, 2=Confirmation, 3=Error
    source: str  # "BMS" | "ITCS" | "CMS"
    presystem_id: str
    timestamp: str  # ISO 8601 datetime
    message_id: str  # UUID string
    message_action: str
    payload: Dict[str, Any] = field(default_factory=dict)
    validation_status: Literal["ok", "warning", "error"] = "ok"
    validation_warnings: List[str] = field(default_factory=list)


@dataclass
class ChargingRequest:
    """Internal representation of a VDV 463 charging request."""
    vehicle_external_id: str  # vehicleId from VDV 463
    charging_point_id: Optional[str] = None
    arrival_time: str  # ISO 8601 datetime
    departure_time: str  # ISO 8601 datetime
    min_target_soc: float  # 0.0-1.0
    max_target_soc: float  # 0.0-1.0
    priority: Optional[int] = None  # chargingPriority
    manual_preconditioning: Optional[Dict[str, Any]] = None
    automatic_preconditioning: Optional[Dict[str, Any]] = None
    validation_status: Literal["ok", "warning", "error"] = "ok"


@dataclass
class VDVProvideChargingRequests:
    """Parsed ProvideChargingRequests message."""
    envelope: VDVMessageEnvelope
    charging_requests: List[ChargingRequest]


@dataclass
class ChargingPointInfo:
    """Charging point information for ProvideChargingInformation."""
    charging_point_id: str
    status: str  # "Available" | "Occupied" | "Faulted" | "Unavailable"
    current_power_kw: Optional[float] = None
    vehicle_id: Optional[str] = None


@dataclass
class DepotInfo:
    """Depot information for ProvideChargingInformation."""
    depot_id: str
    charging_stations: List[ChargingPointInfo] = field(default_factory=list)


@dataclass
class VDVProvideChargingInformation:
    """Parsed ProvideChargingInformation message."""
    envelope: VDVMessageEnvelope
    depot_info_list: List[DepotInfo]


@dataclass
class VDVError:
    """VDV 463 error message."""
    envelope: VDVMessageEnvelope
    error_code: str
    error_description: str
    error_details: Optional[Dict[str, Any]] = None


class SchemaRegistry:
    """Registry for VDV 463 JSON schemas."""
    
    def __init__(self, schema_dir: Optional[str] = None):
        """Initialize schema registry.
        
        Args:
            schema_dir: Directory containing VDV 463 JSON schemas.
                       Defaults to VDV463_SCHEMA_DIR env var or ./schemas/vdv463/
        """
        self.schemas: Dict[str, Dict[str, Any]] = {}
        self.schema_dir = schema_dir or os.getenv(
            "VDV463_SCHEMA_DIR",
            str(Path(__file__).parent.parent.parent / "schemas" / "vdv463")
        )
        self.logger = get_logger(__name__)
        self._load_schemas()
    
    def _load_schemas(self) -> None:
        """Load VDV 463 JSON schemas from schema directory."""
        schema_path = Path(self.schema_dir)
        
        if not schema_path.exists():
            self.logger.warning(
                f"VDV 463 schema directory not found: {self.schema_dir}",
                schema_dir=self.schema_dir,
            )
            # In Sprint 1, we'll create minimal schemas inline if files don't exist
            self._create_minimal_schemas()
            return
        
        # Load required schemas
        schema_files = {
            "MessageStructure": "MessageStructure.json",
            "ProvideChargingRequestsRequest": "ProvideChargingRequestsRequest.json",
            "ProvideChargingRequestsResponse": "ProvideChargingRequestsResponse.json",
            "ProvideChargingInformationRequest": "ProvideChargingInformationRequest.json",
            "ProvideChargingInformationResponse": "ProvideChargingInformationResponse.json",
        }
        
        for schema_name, filename in schema_files.items():
            file_path = schema_path / filename
            if file_path.exists():
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        self.schemas[schema_name] = json.load(f)
                    self.logger.info(f"Loaded schema: {schema_name}")
                except Exception as e:
                    self.logger.error(
                        f"Failed to load schema {filename}: {e}",
                        schema_file=filename,
                        error=str(e),
                    )
            else:
                self.logger.warning(
                    f"Schema file not found: {filename}",
                    schema_file=filename,
                )
        
        # If no schemas loaded, create minimal ones
        if not self.schemas:
            self._create_minimal_schemas()
    
    def _create_minimal_schemas(self) -> None:
        """Create minimal inline schemas for Sprint 1 testing.
        
        These are simplified versions. In production, use official schemas.
        """
        self.schemas["MessageStructure"] = {
            "type": "array",
            "minItems": 7,
            "maxItems": 7,
            "items": [
                {"type": "integer", "enum": [1, 2, 3]},  # MessageType
                {"type": "string", "enum": ["BMS", "ITCS", "CMS"]},  # Source
                {"type": "string"},  # PresystemId
                {"type": "string", "format": "date-time"},  # Timestamp
                {"type": "string"},  # MessageId
                {"type": "string"},  # MessageAction
                {"type": "object"},  # Payload
            ],
        }
        
        self.schemas["ProvideChargingRequestsRequest"] = {
            "type": "object",
            "properties": {
                "chargingRequestList": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["vehicleId", "arrivalTime", "departureTime"],
                        "properties": {
                            "vehicleId": {"type": "string"},
                            "chargingPointId": {"type": "string"},
                            "arrivalTime": {"type": "string", "format": "date-time"},
                            "departureTime": {"type": "string", "format": "date-time"},
                            "minTargetSoc": {"type": "number", "minimum": 0, "maximum": 1},
                            "maxTargetSoc": {"type": "number", "minimum": 0, "maximum": 1},
                            "chargingPriority": {"type": "integer"},
                            "manualPreconditioning": {"type": "object"},
                            "automaticPreconditioning": {"type": "object"},
                        },
                    },
                },
            },
            "required": ["chargingRequestList"],
        }
        
        self.schemas["ProvideChargingInformationRequest"] = {
            "type": "object",
            "properties": {
                "depotInfoList": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["depotId"],
                        "properties": {
                            "depotId": {"type": "string"},
                            "chargingStationInfoList": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "chargingPointInfoList": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "required": ["chargingPointId", "status"],
                                                "properties": {
                                                    "chargingPointId": {"type": "string"},
                                                    "status": {
                                                        "type": "string",
                                                        "enum": ["Available", "Occupied", "Faulted", "Unavailable"],
                                                    },
                                                    "currentPowerKw": {"type": "number"},
                                                    "vehicleId": {"type": "string"},
                                                },
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        }
        
        self.logger.info("Created minimal inline schemas for Sprint 1")
    
    def validate_message_structure(self, message: List[Any]) -> None:
        """Validate message against MessageStructure schema."""
        schema = self.schemas.get("MessageStructure")
        if not schema:
            raise VDV463ValidationError(
                "MessageStructure schema not loaded",
                error_code="SchemaNotLoaded",
            )
        
        try:
            validate(instance=message, schema=schema)
        except ValidationError as e:
            raise VDV463ValidationError(
                f"Message structure validation failed: {e.message}",
                error_code="SchemaValidationError",
                details={"validation_error": str(e), "path": list(e.path)},
            )
    
    def validate_payload(
        self, 
        payload: Dict[str, Any], 
        action: str,
        mode: ValidationMode = ValidationMode.HARD
    ) -> List[str]:
        """
        Validate payload against action-specific schema.
        
        Returns:
            List of validation warnings (empty if valid or HARD mode)
        """
        schema_name = f"{action}Request"
        schema = self.schemas.get(schema_name)
        
        if not schema:
            if mode == ValidationMode.HARD:
                raise VDV463ValidationError(
                    f"Schema not found for action: {action}",
                    error_code="SchemaNotLoaded",
                )
            return [f"Schema not found for action: {action}"]
        
        try:
            validate(instance=payload, schema=schema)
            return []
        except ValidationError as e:
            if mode == ValidationMode.HARD:
                raise VDV463ValidationError(
                    f"Payload validation failed: {e.message}",
                    error_code="SchemaValidationError",
                    details={"validation_error": str(e), "path": list(e.path)},
                )
            return [f"Payload validation warning: {e.message}"]


# Global schema registry instance
_schema_registry: Optional[SchemaRegistry] = None


def get_schema_registry() -> SchemaRegistry:
    """Get or create global schema registry."""
    global _schema_registry
    if _schema_registry is None:
        _schema_registry = SchemaRegistry()
    return _schema_registry


def parse_message(
    raw_text: str,
    validation_mode: ValidationMode = ValidationMode.HARD
) -> VDVMessageEnvelope:
    """
    Parse and validate VDV 463 message from raw JSON text.
    
    Args:
        raw_text: Raw JSON message string
        validation_mode: Validation mode (HARD or SOFT)
    
    Returns:
        Parsed message envelope
    
    Raises:
        VDV463ValidationError: If validation fails in HARD mode
    """
    try:
        message = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise VDV463ValidationError(
            f"Invalid JSON: {e}",
            error_code="InvalidJSON",
        )
    
    if not isinstance(message, list) or len(message) < 7:
        raise VDV463ValidationError(
            "Message must be an array with at least 7 elements",
            error_code="InvalidMessageStructure",
        )
    
    registry = get_schema_registry()
    
    # Validate message structure
    try:
        registry.validate_message_structure(message)
    except VDV463ValidationError as e:
        if validation_mode == ValidationMode.HARD:
            raise
        logger.warning(
            "Message structure validation warning",
            error=str(e),
            validation_mode=validation_mode.value,
        )
    
    # Extract envelope fields
    envelope = VDVMessageEnvelope(
        message_type=message[0],
        source=message[1],
        presystem_id=message[2],
        timestamp=message[3],
        message_id=message[4],
        message_action=message[5],
        payload=message[6] if len(message) > 6 else {},
    )
    
    # Validate payload
    warnings = registry.validate_payload(
        envelope.payload,
        envelope.message_action,
        validation_mode,
    )
    
    if warnings:
        envelope.validation_status = "warning"
        envelope.validation_warnings = warnings
        logger.warning(
            "Payload validation warnings",
            action=envelope.message_action,
            warnings=warnings,
            validation_mode=validation_mode.value,
        )
    
    return envelope


def build_error(
    envelope: VDVMessageEnvelope,
    error_code: str,
    error_description: str,
    error_details: Optional[Dict[str, Any]] = None
) -> List[Any]:
    """
    Build VDV 463 Error message (MessageType 3).
    
    Args:
        envelope: Original message envelope
        error_code: Error code (e.g., "SchemaValidationError", "InvalidVehicleId")
        error_description: Human-readable error description
        error_details: Optional additional error details
    
    Returns:
        VDV 463 Error message array
    """
    error_payload = {
        "errorCode": error_code,
        "errorDescription": error_description,
    }
    
    if error_details:
        error_payload.update(error_details)
    
    return [
        3,  # MessageType: Error
        "CMS",  # Source
        envelope.presystem_id,
        datetime.utcnow().isoformat() + "Z",
        f"error-{envelope.message_id}",  # MessageId
        envelope.message_action,
        error_payload,
    ]


def build_provide_charging_requests_response(
    envelope: VDVMessageEnvelope
) -> List[Any]:
    """
    Build ProvideChargingRequestsResponse (empty payload per spec).
    
    Args:
        envelope: Original request envelope
    
    Returns:
        VDV 463 Response message array
    """
    return [
        2,  # MessageType: Confirmation
        "CMS",  # Source
        envelope.presystem_id,
        datetime.utcnow().isoformat() + "Z",
        f"response-{envelope.message_id}",
        "ProvideChargingRequests",
        {},  # Empty payload per spec
    ]


def build_provide_charging_information_message(
    presystem_id: str,
    depot_info_list: List[DepotInfo],
    message_id: Optional[str] = None
) -> List[Any]:
    """
    Build ProvideChargingInformation message (CMS → BMS/ITCS).
    
    Args:
        presystem_id: Presystem identifier
        depot_info_list: List of depot information
        message_id: Optional message ID (generated if not provided)
    
    Returns:
        VDV 463 message array
    """
    import uuid
    
    if not message_id:
        message_id = str(uuid.uuid4())
    
    # Convert DepotInfo to dict structure
    depot_info_dicts = []
    for depot_info in depot_info_list:
        depot_dict = {
            "depotId": depot_info.depot_id,
            "chargingStationInfoList": [
                {
                    "chargingPointInfoList": [
                        {
                            "chargingPointId": cp.charging_point_id,
                            "status": cp.status,
                            **({"currentPowerKw": cp.current_power_kw} if cp.current_power_kw is not None else {}),
                            **({"vehicleId": cp.vehicle_id} if cp.vehicle_id else {}),
                        }
                        for cp in depot_info.charging_stations
                    ]
                }
            ],
        }
        depot_info_dicts.append(depot_dict)
    
    payload = {
        "depotInfoList": depot_info_dicts,
    }
    
    # Validate before sending
    registry = get_schema_registry()
    warnings = registry.validate_payload(
        payload,
        "ProvideChargingInformation",
        ValidationMode.HARD,  # Always hard validate outbound
    )
    
    if warnings:
        logger.warning(
            "ProvideChargingInformation validation warnings",
            warnings=warnings,
        )
    
    return [
        1,  # MessageType: Request (CMS sends requests to BMS/ITCS)
        "CMS",
        presystem_id,
        datetime.utcnow().isoformat() + "Z",
        message_id,
        "ProvideChargingInformation",
        payload,
    ]
