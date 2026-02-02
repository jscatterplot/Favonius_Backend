"""VDV 463 transit operations integration adapter.

Implements VDV 463 v1.1.0 protocol for BMS/ITCS communication.
See docs/PRD_v2_7_Building_Integration.md Section 9.6 for specifications.
"""

__version__ = "1.0.0"

from .messages import (
    ValidationMode,
    VDVMessageEnvelope,
    VDVProvideChargingRequests,
    VDVProvideChargingInformation,
    VDVError,
    parse_message,
    build_error,
    build_provide_charging_requests_response,
    build_provide_charging_information_message,
    get_validation_mode,
    get_schema_registry,
    DepotInfo,
    ChargingStationInfo,
    ChargingPointInfo,
    VehicleInfo,
    ChargingProcessInfo,
    PreconditioningInfo,
)
from .vehicle_resolver import VehicleResolver

__all__ = [
    "VDV463Handler",
    "ValidationMode",
    "VDVMessageEnvelope",
    "VDVProvideChargingRequests",
    "VDVProvideChargingInformation",
    "VDVError",
    "parse_message",
    "build_error",
    "build_provide_charging_requests_response",
    "build_provide_charging_information_message",
    "get_validation_mode",
    "get_schema_registry",
    "DepotInfo",
    "ChargingStationInfo",
    "ChargingPointInfo",
    "VehicleInfo",
    "ChargingProcessInfo",
    "PreconditioningInfo",
    "VehicleResolver",
]


def __getattr__(name: str):
    """Lazy import VDV463Handler so message-only tests run without websockets."""
    if name == "VDV463Handler":
        from .handler import VDV463Handler
        return VDV463Handler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
