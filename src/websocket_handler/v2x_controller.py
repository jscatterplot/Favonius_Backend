"""Minimal V2X controller implementation used by integration tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional


class V2XOperationMode(str, Enum):
    """Supported V2X operation modes."""

    CENTRAL_SETPOINT = "central_setpoint"
    LOCAL = "local"


@dataclass
class V2XSetpoint:
    """Power setpoint command for a charger/EVSE."""

    station_id: str
    evse_id: int
    power_kw: float
    mode: V2XOperationMode
    timestamp: datetime
    duration_seconds: int
    ramp_rate_kw_per_s: float


@dataclass
class V2XControllerConfig:
    """Configuration for V2X controller behavior."""

    default_duration_seconds: int = 900


class V2XController:
    """Stores active V2X setpoints and dispatches charger commands."""

    def __init__(self, v2x_config: V2XControllerConfig, app_config: Any):
        self.v2x_config = v2x_config
        self.app_config = app_config
        self._active_setpoints: Dict[str, V2XSetpoint] = {}

    async def _send_setpoint_to_charger(self, setpoint: V2XSetpoint) -> None:
        """Send setpoint to charger (implemented by runtime integration)."""
        return None

    async def set_power_setpoint(self, setpoint: V2XSetpoint) -> None:
        """Store and dispatch a setpoint command."""
        if setpoint.timestamp.tzinfo is None:
            setpoint.timestamp = setpoint.timestamp.replace(tzinfo=timezone.utc)
        self._active_setpoints[setpoint.station_id] = setpoint
        await self._send_setpoint_to_charger(setpoint)

    async def get_active_setpoint(self, station_id: str) -> Optional[V2XSetpoint]:
        """Return currently active setpoint for a station."""
        return self._active_setpoints.get(station_id)

    async def clear_setpoint(self, station_id: str) -> None:
        """Clear active setpoint for a station."""
        self._active_setpoints.pop(station_id, None)
