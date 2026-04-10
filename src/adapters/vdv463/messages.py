"""VDV 463 message parsing and validation.

Implements schema-backed validation using official VDV 463 JSON schemas.
See https://github.com/VDVde/VDV463/tree/main/schema for schema definitions.
"""

from __future__ import annotations

import copy
import json
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from jsonschema import ValidationError, validate

try:
    import structlog

    _HAS_STRUCTLOG = True
except ImportError:
    import logging

    structlog = None  # type: ignore
    _HAS_STRUCTLOG = False

# Official VDV 463 schema base URL (Option B: fetch at build/start)
VDV463_SCHEMA_BASE_URL = "https://raw.githubusercontent.com/VDVde/VDV463/main/schema"
SCHEMA_FILENAMES = [
    "MessageStructure.json",
    "BootNotificationRequest.json",
    "BootNotificationResponse.json",
    "ProvideChargingRequestsRequest.json",
    "ProvideChargingRequestsResponse.json",
    "ProvideChargingInformationRequest.json",
    "ProvideChargingInformationResponse.json",
]


def _stdlib_log_adapter(logger_instance: Any) -> Any:
    """Wrap stdlib logger to accept structlog-style keyword args (append to message)."""

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


class ValidationMode(str, Enum):
    """VDV 463 validation mode."""

    HARD = "hard"  # Reject invalid messages
    SOFT = "soft"  # Accept with warnings


class VDV463ValidationError(Exception):
    """Raised when VDV 463 message validation fails."""

    def __init__(
        self,
        message: str,
        error_code: str = "SchemaValidationError",
        details: Optional[Dict[str, Any]] = None,
    ):
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
    """Internal representation of a VDV 463 charging request.

    Per PRD Section 9.6: chargingRequestId, vehicleId, chargingRequestData (expectedArrivalTimeAtChargingPoint,
    requestedTimeForDeparture, minTargetSoc, maxTargetSoc, expectedSocAtArrival), optional chargingPointId,
    priority, manualPreconditioning, automaticPreconditioning, chargingInstruction.
    """

    charging_request_id: str
    vehicle_external_id: str  # vehicleId from VDV 463
    arrival_time: str  # from chargingRequestData.expectedArrivalTimeAtChargingPoint
    departure_time: str  # from chargingRequestData.requestedTimeForDeparture
    min_target_soc: float  # 0.0-1.0
    max_target_soc: float  # 0.0-1.0
    charging_point_id: Optional[str] = None
    expected_soc_at_arrival: Optional[float] = None  # 0-100 or 0-1 depending on spec
    priority: Optional[int] = None  # chargingPriority
    charging_instruction: str = "Normal"  # 'Normal', 'Changed', 'Terminate'
    manual_preconditioning: Optional[Dict[str, Any]] = None
    automatic_preconditioning: Optional[Dict[str, Any]] = None
    validation_status: Literal["ok", "warning", "error"] = "ok"


@dataclass
class VDVProvideChargingRequests:
    """Parsed ProvideChargingRequests message."""

    envelope: VDVMessageEnvelope
    charging_requests: List[ChargingRequest]


@dataclass
class PreconditioningInfo:
    """Preconditioning info per VehicleInfo (VDV 463 schema)."""

    vehicle_preconditioning_time: Optional[int] = None  # minutes
    vehicle_preconditioning_energy: Optional[int] = None  # Wh
    hv_battery_preconditioning_time: Optional[int] = None
    hv_battery_charging_energy: Optional[int] = None


@dataclass
class VehicleInfo:
    """Vehicle info for ChargingPointInfo (VDV 463 schema; required when vehicle present)."""

    vehicle_id: str
    vehicle_status_info: Dict[str, Any] = field(default_factory=dict)  # required but can be {}
    vehicle_charging_status: str = (
        "Unknown"  # ReadyToCharge | Charging | ChargingImpossible | Unknown
    )
    preconditioning_info: Optional[PreconditioningInfo] = (
        None  # required; use default empty if absent
    )
    traction_battery_info: Optional[Dict[str, Any]] = None  # e.g. {"stateOfCharge": 65} 0-100
    preconditioning_status: Optional[str] = None  # Scheduled | Active | Curtailed (AT-09, AT-13)


@dataclass
class ChargingProcessInfo:
    """Charging process info for ChargingPointInfo (VDV 463 schema)."""

    charging_process_id: str
    process_status: str  # Preparing | Charging | SuspendedEVSE | SuspendedEV | Finishing | Queued | ChargingRejectedTechnically
    start_time: str  # ISO date-time
    electric_data_charging_power: float  # kW
    presystem_id: Optional[str] = None
    charging_request_id: Optional[str] = None
    charging_prediction_data: Optional[Dict[str, Any]] = (
        None  # e.g. chargingPredictionDataDepartureTime
    )


@dataclass
class ChargingPointInfo:
    """Charging point information for ProvideChargingInformation (VDV 463 schema)."""

    charging_point_id: str
    charging_point_status: Optional[str] = (
        None  # Available | Occupied | Reserved | Unavailable | Faulted
    )
    present_power: Optional[float] = None  # kW (schema: presentPower)
    vehicle_info: Optional[VehicleInfo] = None
    charging_process_info: Optional[ChargingProcessInfo] = None
    # Backward compatibility: status/current_power_kw/vehicle_id for simple usage
    status: Optional[str] = None  # alias for charging_point_status when set
    current_power_kw: Optional[float] = None  # alias for present_power
    vehicle_id: Optional[str] = None  # legacy; use vehicle_info.vehicle_id


@dataclass
class ChargingStationInfo:
    """Charging station info for DepotInfo (VDV 463 schema)."""

    charging_station_id: str
    charging_station_status: str  # Available | Unavailable | Faulted
    charging_point_info_list: List[ChargingPointInfo] = field(default_factory=list)


@dataclass
class DepotInfo:
    """Depot information for ProvideChargingInformation (VDV 463 schema)."""

    depot_id: str
    charging_station_info_list: List[ChargingStationInfo] = field(default_factory=list)
    name: Optional[str] = None
    # Backward compatibility: charging_stations builds one ChargingStationInfo
    charging_stations: Optional[List[ChargingPointInfo]] = None


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


def _get_bundled_schema_dir() -> Path:
    """Return path to bundled VDV 463 schemas (repo)."""
    # src/adapters/vdv463/messages.py -> project root = parent.parent.parent.parent
    return Path(__file__).resolve().parent.parent.parent.parent / "schemas" / "vdv463"


def _get_cache_schema_dir() -> Path:
    """Return path for schema cache (VDV463_SCHEMA_DIR or .cache/vdv463/schemas)."""
    env_dir = os.getenv("VDV463_SCHEMA_DIR")
    if env_dir:
        return Path(env_dir)
    root = Path(__file__).resolve().parent.parent.parent.parent
    return root / ".cache" / "vdv463" / "schemas"


def _load_schemas_from_dir(schema_dir: Path) -> Dict[str, Dict[str, Any]]:
    """Load schema JSON files from a directory. Returns name -> schema dict."""
    result: Dict[str, Dict[str, Any]] = {}
    name_from_file = {
        "MessageStructure.json": "MessageStructure",
        "BootNotificationRequest.json": "BootNotificationRequest",
        "BootNotificationResponse.json": "BootNotificationResponse",
        "ProvideChargingRequestsRequest.json": "ProvideChargingRequestsRequest",
        "ProvideChargingRequestsResponse.json": "ProvideChargingRequestsResponse",
        "ProvideChargingInformationRequest.json": "ProvideChargingInformationRequest",
        "ProvideChargingInformationResponse.json": "ProvideChargingInformationResponse",
    }
    for filename, schema_name in name_from_file.items():
        path = schema_dir / filename
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    result[schema_name] = json.load(f)
            except Exception:
                pass
    return result


def _fetch_schemas_to_cache(cache_dir: Path) -> bool:
    """Fetch official schemas from GitHub and write to cache_dir. Returns True on success."""
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        for filename in SCHEMA_FILENAMES:
            url = f"{VDV463_SCHEMA_BASE_URL}/{filename}"
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = resp.read().decode("utf-8")
            (cache_dir / filename).write_text(data, encoding="utf-8")
        return True
    except Exception as e:
        logger.warning("VDV 463 schema fetch from GitHub failed", error=str(e))
        return False


class SchemaRegistry:
    """Registry for VDV 463 JSON schemas (Option B: fetch, cache, bundled fallback)."""

    def __init__(self, schema_dir: Optional[str] = None):
        """Initialize schema registry.

        Load order: 1) cache dir (VDV463_SCHEMA_DIR or .cache/vdv463/schemas),
        2) if empty, fetch from GitHub and write to cache,
        3) if fetch fails, load from bundled schemas/vdv463/,
        4) if still empty, use minimal inline schemas.
        """
        self.schemas: Dict[str, Dict[str, Any]] = {}
        self._schema_source: str = "none"
        self.logger = get_logger(__name__)
        cache_dir = _get_cache_schema_dir() if not schema_dir else Path(schema_dir)
        bundled_dir = _get_bundled_schema_dir()

        # 1) Try loading from cache dir
        if cache_dir.exists():
            self.schemas = _load_schemas_from_dir(cache_dir)
            if self.schemas:
                self._schema_source = "cache"
                self.logger.info(
                    "VDV 463 schemas loaded from cache", dir=str(cache_dir), count=len(self.schemas)
                )

        # 2) If cache empty, try fetch and write to cache
        if not self.schemas:
            if _fetch_schemas_to_cache(cache_dir):
                self.schemas = _load_schemas_from_dir(cache_dir)
                if self.schemas:
                    self._schema_source = "fetched"
                    self.logger.info(
                        "VDV 463 schemas fetched from GitHub and cached", count=len(self.schemas)
                    )

        # 3) If still empty, load from bundled
        if not self.schemas and bundled_dir.exists():
            self.schemas = _load_schemas_from_dir(bundled_dir)
            if self.schemas:
                self._schema_source = "bundled"
                self.logger.info(
                    "VDV 463 schemas loaded from bundled",
                    dir=str(bundled_dir),
                    count=len(self.schemas),
                )

        # 4) Last resort: minimal inline schemas
        if not self.schemas:
            self._create_minimal_schemas()
            self._schema_source = "minimal"
            self.logger.warning(
                "VDV 463 using minimal inline schemas; add bundled schemas or enable network for full validation"
            )

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
                {"type": "string", "format": "uuid"},  # MessageId
                {
                    "type": "string",
                    "enum": [
                        "BootNotification",
                        "ProvideChargingRequests",
                        "ProvideChargingInformation",
                    ],
                },  # MessageAction
                {"type": "object"},  # Payload
            ],
        }
        self.schemas["BootNotificationRequest"] = {
            "type": "object",
            "required": ["presystem"],
            "properties": {"presystem": {"type": "string", "enum": ["BMS", "ITCS"]}},
            "additionalProperties": False,
        }
        self.schemas["BootNotificationResponse"] = {
            "type": "object",
            "required": ["status"],
            "properties": {"status": {"type": "string", "enum": ["Accepted", "Rejected"]}},
            "additionalProperties": False,
        }

        # Per PRD Section 9.6: chargingRequestData nested (expectedArrivalTimeAtChargingPoint, requestedTimeForDeparture, minTargetSoc, maxTargetSoc)
        self.schemas["ProvideChargingRequestsRequest"] = {
            "type": "object",
            "properties": {
                "chargingRequestList": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["vehicleId", "chargingRequestId", "chargingRequestData"],
                        "properties": {
                            "vehicleId": {"type": "string"},
                            "chargingRequestId": {"type": "string"},
                            "chargingPointId": {"type": "string"},
                            "chargingProcessId": {"type": "string"},
                            "priority": {"type": "integer"},
                            "chargingPriority": {"type": "integer"},
                            "chargingInstruction": {
                                "type": "string",
                                "enum": ["Normal", "Changed", "Terminate"],
                            },
                            "chargingRequestData": {
                                "type": "object",
                                "required": ["minTargetSoc", "maxTargetSoc"],
                                "properties": {
                                    "expectedArrivalTimeAtChargingPoint": {
                                        "type": "string",
                                        "format": "date-time",
                                    },
                                    "expectedSocAtArrival": {
                                        "type": "number",
                                        "minimum": 0,
                                        "maximum": 100,
                                    },
                                    "minTargetSoc": {
                                        "type": "number",
                                        "minimum": 0,
                                        "maximum": 100,
                                    },
                                    "maxTargetSoc": {
                                        "type": "number",
                                        "minimum": 0,
                                        "maximum": 100,
                                    },
                                    "requestedTimeForDeparture": {
                                        "type": "string",
                                        "format": "date-time",
                                    },
                                    "adHocCharging": {"type": "boolean"},
                                },
                            },
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
                                                        "enum": [
                                                            "Available",
                                                            "Occupied",
                                                            "Faulted",
                                                            "Unavailable",
                                                        ],
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

        self.logger.info(
            "Created minimal inline schemas (fallback when fetch and bundled unavailable)"
        )

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
                f"Message structure validation failed: {getattr(e, 'message', str(e))}",
                error_code="SchemaValidationError",
                details={"validation_error": str(e), "path": list(e.path)},
            )

    def validate_payload(
        self, payload: Dict[str, Any], action: str, mode: ValidationMode = ValidationMode.HARD
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

        # Normalize payload for validation: official schema has "priority" not "chargingPriority"
        to_validate = payload
        if action == "ProvideChargingRequests":
            to_validate = copy.deepcopy(payload)
            for item in to_validate.get("chargingRequestList", []):
                if isinstance(item, dict) and "chargingPriority" in item:
                    if "priority" not in item:
                        item["priority"] = item["chargingPriority"]
                    item.pop("chargingPriority", None)

        try:
            validate(instance=to_validate, schema=schema)
            return []
        except ValidationError as e:
            err_msg = getattr(e, "message", str(e))
            if mode == ValidationMode.HARD:
                raise VDV463ValidationError(
                    f"Payload validation failed: {err_msg}",
                    error_code="SchemaValidationError",
                    details={"validation_error": str(e), "path": list(e.path)},
                )
            return [f"Payload validation warning: {err_msg}"]


def parse_charging_request_item(
    req_data: Dict[str, Any],
    validation_status: Literal["ok", "warning", "error"] = "ok",
) -> ChargingRequest:
    """
    Parse one item from chargingRequestList into ChargingRequest.

    Per PRD Section 9.6: uses chargingRequestData.expectedArrivalTimeAtChargingPoint,
    requestedTimeForDeparture, minTargetSoc, maxTargetSoc, expectedSocAtArrival.
    SoC values in VDV 463 are 0-100; we store as 0-1 for optimizer.
    """
    data = req_data.get("chargingRequestData") or {}
    min_soc = data.get("minTargetSoc", 0)
    max_soc = data.get("maxTargetSoc", 1)
    expected_soc = data.get("expectedSocAtArrival")
    # Normalize 0-100 to 0-1 if needed
    if min_soc is not None and min_soc > 1:
        min_soc = min_soc / 100.0
    if max_soc is not None and max_soc > 1:
        max_soc = max_soc / 100.0
    if expected_soc is not None and expected_soc > 1:
        expected_soc = expected_soc / 100.0

    arrival = data.get("expectedArrivalTimeAtChargingPoint") or ""
    departure = data.get("requestedTimeForDeparture") or ""

    return ChargingRequest(
        charging_request_id=req_data.get("chargingRequestId", ""),
        vehicle_external_id=req_data.get("vehicleId", ""),
        charging_point_id=req_data.get("chargingPointId"),
        arrival_time=arrival,
        departure_time=departure,
        min_target_soc=float(min_soc) if min_soc is not None else 0.0,
        max_target_soc=float(max_soc) if max_soc is not None else 1.0,
        expected_soc_at_arrival=float(expected_soc) if expected_soc is not None else None,
        priority=req_data.get("priority") or req_data.get("chargingPriority"),
        charging_instruction=req_data.get("chargingInstruction", "Normal"),
        manual_preconditioning=req_data.get("manualPreconditioning"),
        automatic_preconditioning=req_data.get("automaticPreconditioning"),
        validation_status=validation_status,
    )


# Global schema registry instance
_schema_registry: Optional[SchemaRegistry] = None


def get_schema_registry() -> SchemaRegistry:
    """Get or create global schema registry."""
    global _schema_registry
    if _schema_registry is None:
        _schema_registry = SchemaRegistry()
    return _schema_registry


def get_validation_mode() -> ValidationMode:
    """Return validation mode from env VDV463_VALIDATION_MODE (default: SOFT)."""
    mode = (os.getenv("VDV463_VALIDATION_MODE") or "soft").strip().lower()
    if mode == "hard":
        return ValidationMode.HARD
    return ValidationMode.SOFT


def parse_message(
    raw_text: str,
    validation_mode: Optional[ValidationMode] = None,
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
    if validation_mode is None:
        validation_mode = get_validation_mode()
    # Default to hard validation in production to prevent data poisoning
    _env = os.getenv("ENVIRONMENT", "development")
    if _env == "production" and validation_mode == ValidationMode.SOFT:
        validation_mode = ValidationMode.HARD
    try:
        message = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise VDV463ValidationError(
            f"Invalid JSON: {e}",
            error_code="InvalidJSON",
        )

    if not isinstance(message, list) or len(message) < 7:
        if validation_mode == ValidationMode.HARD:
            raise VDV463ValidationError(
                "Message must be an array with at least 7 elements",
                error_code="InvalidMessageStructure",
            )
        # SOFT: return minimal envelope with warning
        envelope = VDVMessageEnvelope(
            message_type=message[0] if len(message) > 0 else 1,
            source=message[1] if len(message) > 1 else "",
            presystem_id=message[2] if len(message) > 2 else "",
            timestamp=message[3] if len(message) > 3 else "",
            message_id=message[4] if len(message) > 4 else "",
            message_action=message[5] if len(message) > 5 else "",
            payload=message[6] if len(message) > 6 else {},
            validation_status="warning",
            validation_warnings=["Message must be an array with at least 7 elements"],
        )
        return envelope

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
    error_details: Optional[Dict[str, Any]] = None,
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


def build_provide_charging_requests_response(envelope: VDVMessageEnvelope) -> List[Any]:
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


def _charging_point_info_to_dict(cp: ChargingPointInfo) -> Dict[str, Any]:
    """Convert ChargingPointInfo to VDV 463 schema dict (chargingPointStatus, presentPower, vehicleInfo, chargingProcessInfo)."""
    d: Dict[str, Any] = {
        "chargingPointId": cp.charging_point_id,
        "chargingPointStatus": cp.charging_point_status or cp.status or "Available",
    }
    if cp.present_power is not None or cp.current_power_kw is not None:
        d["presentPower"] = (
            cp.present_power if cp.present_power is not None else cp.current_power_kw
        )
    if cp.vehicle_info:
        vi = cp.vehicle_info
        prec = vi.preconditioning_info
        prec_dict: Dict[str, Any] = {}
        if prec:
            if prec.vehicle_preconditioning_time is not None:
                prec_dict["vehiclePreconditioningTime"] = prec.vehicle_preconditioning_time
            if prec.vehicle_preconditioning_energy is not None:
                prec_dict["vehiclePreconditioningEnergy"] = prec.vehicle_preconditioning_energy
        status_info = dict(vi.vehicle_status_info or {})
        if vi.preconditioning_status == "Active":
            status_info["hvacPreconditioningActive"] = True
        elif vi.preconditioning_status in ("Scheduled", "Curtailed", None):
            status_info.setdefault("hvacPreconditioningActive", False)
        d["vehicleInfo"] = {
            "vehicleId": vi.vehicle_id,
            "vehicleStatusInfo": status_info,
            "vehicleChargingStatus": vi.vehicle_charging_status,
            "preconditioningInfo": prec_dict,
        }
        if vi.traction_battery_info:
            d["vehicleInfo"]["tractionBatteryInfo"] = vi.traction_battery_info
    if cp.charging_process_info:
        cpi = cp.charging_process_info
        d["chargingProcessInfo"] = {
            "chargingProcessId": cpi.charging_process_id,
            "processStatus": cpi.process_status,
            "startTime": cpi.start_time,
            "chargingPredictionData": cpi.charging_prediction_data or {},
            "electricData": {"chargingPower": cpi.electric_data_charging_power},
        }
        if cpi.presystem_id:
            d["chargingProcessInfo"]["presystemId"] = cpi.presystem_id
        if cpi.charging_request_id:
            d["chargingProcessInfo"]["chargingRequestId"] = cpi.charging_request_id
    return d


def build_provide_charging_information_message(
    presystem_id: str,
    depot_info_list: List[DepotInfo],
    message_id: Optional[str] = None,
    validation_mode: Optional[ValidationMode] = None,
) -> List[Any]:
    """
    Build ProvideChargingInformation message (CMS → BMS/ITCS).

    Output conforms to ProvideChargingInformationRequest.json (chargingPointStatus,
    presentPower, vehicleInfo, chargingProcessInfo). Validation mode defaults to SOFT.
    """
    import uuid

    if not message_id:
        message_id = str(uuid.uuid4())
    if validation_mode is None:
        validation_mode = get_validation_mode()

    depot_info_dicts: List[Dict[str, Any]] = []
    for depot_info in depot_info_list:
        if depot_info.charging_station_info_list:
            station_list = [
                {
                    "chargingStationId": st.charging_station_id,
                    "chargingStationStatus": st.charging_station_status,
                    "chargingPointInfoList": [
                        _charging_point_info_to_dict(cp) for cp in st.charging_point_info_list
                    ],
                }
                for st in depot_info.charging_station_info_list
            ]
        elif depot_info.charging_stations:
            points = [_charging_point_info_to_dict(cp) for cp in depot_info.charging_stations]
            station_list = [
                {
                    "chargingStationId": depot_info.depot_id,
                    "chargingStationStatus": "Available",
                    "chargingPointInfoList": points,
                }
            ]
        else:
            station_list = []
        depot_dict: Dict[str, Any] = {
            "depotId": depot_info.depot_id,
            "chargingStationInfoList": station_list,
        }
        if depot_info.name is not None:
            depot_dict["name"] = depot_info.name
        depot_info_dicts.append(depot_dict)

    payload = {"depotInfoList": depot_info_dicts}

    registry = get_schema_registry()
    warnings = registry.validate_payload(
        payload,
        "ProvideChargingInformation",
        validation_mode,
    )
    if warnings:
        logger.warning(
            "ProvideChargingInformation validation warnings",
            warnings=warnings,
        )

    return [
        1,
        "CMS",
        presystem_id,
        datetime.utcnow().isoformat() + "Z",
        message_id,
        "ProvideChargingInformation",
        payload,
    ]
