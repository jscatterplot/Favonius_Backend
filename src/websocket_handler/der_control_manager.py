"""
DER Control Manager for OCPP 2.1 V2G operations.

This module manages Distributed Energy Resource (DER) controls including:
- DERControlType structures with all fields
- Control priority and superseding logic
- Validation against station capabilities
- Storage and retrieval from database
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Any, Union
import json

from .monitoring import get_logger
from .timescale_client import TimescaleClient


class DERControlEnumType(Enum):
    """DER control types."""
    FIXED_PF_INJECT = "FixedPFInject"
    FIXED_PF_ABSORB = "FixedPFAbsorb"
    VOLT_VAR = "VoltVar"
    WATT_VAR = "WattVar"
    FIXED_VAR = "FixedVar"
    VOLT_WATT = "VoltWatt"
    FREQ_DROOP = "FreqDroop"
    LIMIT_MAX_DISCHARGE = "LimitMaxDischarge"
    LIMIT_MAX_CHARGE = "LimitMaxCharge"
    LIMIT_VAR = "LimitVar"
    LIMIT_WATT = "LimitWatt"


class DERCurveType(Enum):
    """DER curve types."""
    FREQ_DROOP = "FreqDroop"
    VOLT_VAR = "VoltVar"
    WATT_VAR = "WattVar"
    VOLT_WATT = "VoltWatt"


@dataclass
class DERCurvePoint:
    """DER curve point."""
    x: float
    y: float


@dataclass
class DERCurve:
    """DER curve definition."""
    curve_type: DERCurveType
    points: List[DERCurvePoint]
    curve_unit_x: str
    curve_unit_y: str


@dataclass
class DERControlType:
    """DER control structure with all fields."""
    control_id: int
    is_default: bool = False
    control_type: DERControlEnumType = DERControlEnumType.FIXED_PF_INJECT
    priority: int = 0
    start_time: Optional[str] = None
    duration: Optional[int] = None
    is_superseded: bool = False
    
    # FreqDroop-specific fields
    over_freq: Optional[float] = None
    under_freq: Optional[float] = None
    over_droop: Optional[float] = None
    under_droop: Optional[float] = None
    response_time: Optional[int] = None
    
    # Curve-based controls
    curve: Optional[DERCurve] = None
    
    # LimitMaxDischarge-specific
    pct_max_discharge_power: Optional[float] = None
    
    # Additional fields
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DERControlManager:
    """Manages DER controls for V2G operations."""
    
    def __init__(self, timescale_client: TimescaleClient):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        
        # Active controls cache per station
        self.active_controls: Dict[str, List[DERControlType]] = {}
        
        # Control ID counter per station
        self.control_id_counters: Dict[str, int] = {}
    
    async def set_der_control(self, station_id: str, der_control_data: Dict[str, Any]) -> Dict[str, Any]:
        """Set DER control with validation and priority handling."""
        try:
            self.logger.info(f"Setting DER control for station {station_id}: {der_control_data}")
            
            # Parse DER control data
            der_control = self._parse_der_control(der_control_data)
            
            # Validate control type against station capabilities
            validation_result = await self._validate_der_control(station_id, der_control)
            if not validation_result["valid"]:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": validation_result["reason_code"],
                        "additionalInfo": validation_result["message"]
                    }
                }
            
            # Handle priority and superseding logic
            await self._handle_control_priority(station_id, der_control)
            
            # Generate control ID if not provided
            if der_control.control_id is None:
                der_control.control_id = await self._get_next_control_id(station_id)
            
            # Set timestamps
            now = datetime.now(timezone.utc)
            der_control.created_at = now
            der_control.updated_at = now
            
            # Store in database
            await self._store_der_control(station_id, der_control)
            
            # Update cache
            await self._update_control_cache(station_id)
            
            self.logger.info(f"DER control {der_control.control_id} set successfully for station {station_id}")
            
            return {
                "status": "Accepted",
                "statusInfo": {
                    "reasonCode": "Success",
                    "additionalInfo": f"DER control {der_control.control_id} set successfully"
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error setting DER control: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def get_der_control(self, station_id: str, control_id: Optional[int] = None) -> Dict[str, Any]:
        """Get DER control(s) for station."""
        try:
            if control_id is not None:
                # Get specific control
                der_control = await self._get_der_control_by_id(station_id, control_id)
                if der_control:
                    return {
                        "status": "Accepted",
                        "der_control": self._der_control_to_dict(der_control)
                    }
                else:
                    return {
                        "status": "Rejected",
                        "statusInfo": {
                            "reasonCode": "NotFound",
                            "additionalInfo": f"DER control {control_id} not found"
                        }
                    }
            else:
                # Get all active controls
                active_controls = await self._get_active_der_controls(station_id)
                return {
                    "status": "Accepted",
                    "der_control": [self._der_control_to_dict(control) for control in active_controls]
                }
                
        except Exception as e:
            self.logger.error(f"Error getting DER control: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def report_der_control(self, station_id: str, der_controls: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Report DER control status."""
        try:
            self.logger.info(f"Reporting DER controls for station {station_id}: {len(der_controls)} controls")
            
            # Parse and validate each control
            parsed_controls = []
            for control_data in der_controls:
                try:
                    control = self._parse_der_control(control_data)
                    parsed_controls.append(control)
                except Exception as e:
                    self.logger.warning(f"Failed to parse DER control: {e}")
                    continue
            
            # Store reported controls
            await self._store_reported_der_controls(station_id, parsed_controls)
            
            return {
                "status": "Accepted",
                "statusInfo": {
                    "reasonCode": "Success",
                    "additionalInfo": f"Reported {len(parsed_controls)} DER controls"
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error reporting DER control: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def clear_der_control(self, station_id: str, control_id: Optional[int] = None) -> Dict[str, Any]:
        """Clear DER control(s)."""
        try:
            if control_id is not None:
                # Clear specific control
                success = await self._clear_der_control_by_id(station_id, control_id)
                if success:
                    return {
                        "status": "Accepted",
                        "statusInfo": {
                            "reasonCode": "Success",
                            "additionalInfo": f"DER control {control_id} cleared"
                        }
                    }
                else:
                    return {
                        "status": "Rejected",
                        "statusInfo": {
                            "reasonCode": "NotFound",
                            "additionalInfo": f"DER control {control_id} not found"
                        }
                    }
            else:
                # Clear all controls
                cleared_count = await self._clear_all_der_controls(station_id)
                return {
                    "status": "Accepted",
                    "statusInfo": {
                        "reasonCode": "Success",
                        "additionalInfo": f"Cleared {cleared_count} DER controls"
                    }
                }
                
        except Exception as e:
            self.logger.error(f"Error clearing DER control: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def notify_der_alarm(self, station_id: str, control_type: str, alarm_ended: bool,
                             grid_event_fault: Optional[str] = None, timestamp: Optional[str] = None) -> Dict[str, Any]:
        """Handle DER alarm notification."""
        try:
            self.logger.info(f"DER alarm notification for station {station_id}: "
                           f"control_type={control_type}, alarm_ended={alarm_ended}, "
                           f"grid_event_fault={grid_event_fault}")
            
            # Store alarm event
            await self._store_der_alarm_event(station_id, control_type, alarm_ended, 
                                            grid_event_fault, timestamp)
            
            return {
                "status": "Accepted",
                "statusInfo": {
                    "reasonCode": "Success",
                    "additionalInfo": "DER alarm notification processed"
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error handling DER alarm notification: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def notify_der_start_stop(self, station_id: str, control_id: int, started: bool,
                                  superseded_id: Optional[int] = None, timestamp: Optional[str] = None) -> Dict[str, Any]:
        """Handle DER start/stop notification."""
        try:
            self.logger.info(f"DER start/stop notification for station {station_id}: "
                           f"control_id={control_id}, started={started}, superseded_id={superseded_id}")
            
            # Store start/stop event
            await self._store_der_start_stop_event(station_id, control_id, started, 
                                                 superseded_id, timestamp)
            
            # Update control status if needed
            if started:
                await self._activate_der_control(station_id, control_id)
            else:
                await self._deactivate_der_control(station_id, control_id)
            
            return {
                "status": "Accepted",
                "statusInfo": {
                    "reasonCode": "Success",
                    "additionalInfo": "DER start/stop notification processed"
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error handling DER start/stop notification: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    def _parse_der_control(self, control_data: Dict[str, Any]) -> DERControlType:
        """Parse DER control from dictionary."""
        control = DERControlType(
            control_id=control_data.get("controlId"),
            is_default=control_data.get("isDefault", False),
            control_type=DERControlEnumType(control_data.get("controlType", "FixedPFInject")),
            priority=control_data.get("priority", 0),
            start_time=control_data.get("startTime"),
            duration=control_data.get("duration"),
            is_superseded=control_data.get("isSuperseded", False)
        )
        
        # Parse FreqDroop-specific fields
        if control.control_type == DERControlEnumType.FREQ_DROOP:
            control.over_freq = control_data.get("overFreq")
            control.under_freq = control_data.get("underFreq")
            control.over_droop = control_data.get("overDroop")
            control.under_droop = control_data.get("underDroop")
            control.response_time = control_data.get("responseTime")
        
        # Parse curve-based controls
        curve_data = control_data.get("curve")
        if curve_data:
            control.curve = self._parse_der_curve(curve_data)
        
        # Parse LimitMaxDischarge-specific fields
        if control.control_type == DERControlEnumType.LIMIT_MAX_DISCHARGE:
            control.pct_max_discharge_power = control_data.get("pctMaxDischargePower")
        
        return control
    
    def _parse_der_curve(self, curve_data: Dict[str, Any]) -> DERCurve:
        """Parse DER curve from dictionary."""
        points = []
        for point_data in curve_data.get("curvePoints", []):
            points.append(DERCurvePoint(
                x=point_data["x"],
                y=point_data["y"]
            ))
        
        return DERCurve(
            curve_type=DERCurveType(curve_data.get("curveType", "FreqDroop")),
            points=points,
            curve_unit_x=curve_data.get("curveUnitX", "Hz"),
            curve_unit_y=curve_data.get("curveUnitY", "W")
        )
    
    async def _validate_der_control(self, station_id: str, der_control: DERControlType) -> Dict[str, Any]:
        """Validate DER control against station capabilities."""
        try:
            # Get station DER capabilities
            capabilities = await self._get_station_der_capabilities(station_id)
            
            # Check if control type is supported
            supported_modes = capabilities.get("modesSupported", [])
            if der_control.control_type.value not in supported_modes:
                return {
                    "valid": False,
                    "reason_code": "NotSupported",
                    "message": f"Control type {der_control.control_type.value} not supported"
                }
            
            # Validate control-specific parameters
            if der_control.control_type == DERControlEnumType.FREQ_DROOP:
                if der_control.over_freq is None or der_control.under_freq is None:
                    return {
                        "valid": False,
                        "reason_code": "PropertyConstraintViolation",
                        "message": "FreqDroop control requires overFreq and underFreq parameters"
                    }
            
            if der_control.control_type == DERControlEnumType.LIMIT_MAX_DISCHARGE:
                if der_control.pct_max_discharge_power is None:
                    return {
                        "valid": False,
                        "reason_code": "PropertyConstraintViolation",
                        "message": "LimitMaxDischarge control requires pctMaxDischargePower parameter"
                    }
            
            return {"valid": True}
            
        except Exception as e:
            self.logger.error(f"Error validating DER control: {e}")
            return {
                "valid": False,
                "reason_code": "InternalError",
                "message": str(e)
            }
    
    async def _handle_control_priority(self, station_id: str, der_control: DERControlType) -> None:
        """Handle control priority and superseding logic."""
        try:
            # Get existing controls with same or higher priority
            existing_controls = await self._get_active_der_controls(station_id)
            
            # Mark lower priority controls as superseded
            for existing_control in existing_controls:
                if existing_control.priority <= der_control.priority and not existing_control.is_superseded:
                    existing_control.is_superseded = True
                    existing_control.updated_at = datetime.now(timezone.utc)
                    await self._update_der_control(station_id, existing_control)
            
        except Exception as e:
            self.logger.error(f"Error handling control priority: {e}")
    
    async def _get_next_control_id(self, station_id: str) -> int:
        """Get next available control ID for station."""
        if station_id not in self.control_id_counters:
            self.control_id_counters[station_id] = 0
        
        self.control_id_counters[station_id] += 1
        return self.control_id_counters[station_id]
    
    def _der_control_to_dict(self, der_control: DERControlType) -> Dict[str, Any]:
        """Convert DER control to dictionary."""
        control_dict = {
            "controlId": der_control.control_id,
            "isDefault": der_control.is_default,
            "controlType": der_control.control_type.value,
            "priority": der_control.priority,
            "startTime": der_control.start_time,
            "duration": der_control.duration,
            "isSuperseded": der_control.is_superseded
        }
        
        # Add FreqDroop-specific fields
        if der_control.control_type == DERControlEnumType.FREQ_DROOP:
            control_dict.update({
                "overFreq": der_control.over_freq,
                "underFreq": der_control.under_freq,
                "overDroop": der_control.over_droop,
                "underDroop": der_control.under_droop,
                "responseTime": der_control.response_time
            })
        
        # Add curve data
        if der_control.curve:
            control_dict["curve"] = {
                "curveType": der_control.curve.curve_type.value,
                "curvePoints": [{"x": p.x, "y": p.y} for p in der_control.curve.points],
                "curveUnitX": der_control.curve.curve_unit_x,
                "curveUnitY": der_control.curve.curve_unit_y
            }
        
        # Add LimitMaxDischarge-specific fields
        if der_control.control_type == DERControlEnumType.LIMIT_MAX_DISCHARGE:
            control_dict["pctMaxDischargePower"] = der_control.pct_max_discharge_power
        
        return control_dict
    
    # Database methods (to be implemented with actual TimescaleDB queries)
    async def _store_der_control(self, station_id: str, der_control: DERControlType) -> None:
        """Store DER control in database."""
        # TODO: Implement actual database storage
        self.logger.debug(f"Storing DER control {der_control.control_id} for station {station_id}")
    
    async def _get_der_control_by_id(self, station_id: str, control_id: int) -> Optional[DERControlType]:
        """Get DER control by ID."""
        # TODO: Implement actual database query
        self.logger.debug(f"Getting DER control {control_id} for station {station_id}")
        return None
    
    async def _get_active_der_controls(self, station_id: str) -> List[DERControlType]:
        """Get active DER controls for station."""
        # TODO: Implement actual database query
        self.logger.debug(f"Getting active DER controls for station {station_id}")
        return []
    
    async def _update_der_control(self, station_id: str, der_control: DERControlType) -> None:
        """Update DER control in database."""
        # TODO: Implement actual database update
        self.logger.debug(f"Updating DER control {der_control.control_id} for station {station_id}")
    
    async def _clear_der_control_by_id(self, station_id: str, control_id: int) -> bool:
        """Clear DER control by ID."""
        # TODO: Implement actual database deletion
        self.logger.debug(f"Clearing DER control {control_id} for station {station_id}")
        return True
    
    async def _clear_all_der_controls(self, station_id: str) -> int:
        """Clear all DER controls for station."""
        # TODO: Implement actual database deletion
        self.logger.debug(f"Clearing all DER controls for station {station_id}")
        return 0
    
    async def _update_control_cache(self, station_id: str) -> None:
        """Update control cache for station."""
        # TODO: Implement cache update logic
        self.logger.debug(f"Updating control cache for station {station_id}")
    
    async def _get_station_der_capabilities(self, station_id: str) -> Dict[str, Any]:
        """Get station DER capabilities."""
        # TODO: Implement actual capability query
        return {
            "modesSupported": ["FixedPFInject", "VoltVar", "WattVar", "FixedVar", "VoltWatt", "FreqDroop"]
        }
    
    async def _store_reported_der_controls(self, station_id: str, controls: List[DERControlType]) -> None:
        """Store reported DER controls."""
        # TODO: Implement actual database storage
        self.logger.debug(f"Storing {len(controls)} reported DER controls for station {station_id}")
    
    async def _store_der_alarm_event(self, station_id: str, control_type: str, alarm_ended: bool,
                                   grid_event_fault: Optional[str], timestamp: Optional[str]) -> None:
        """Store DER alarm event."""
        # TODO: Implement actual database storage
        self.logger.debug(f"Storing DER alarm event for station {station_id}")
    
    async def _store_der_start_stop_event(self, station_id: str, control_id: int, started: bool,
                                        superseded_id: Optional[int], timestamp: Optional[str]) -> None:
        """Store DER start/stop event."""
        # TODO: Implement actual database storage
        self.logger.debug(f"Storing DER start/stop event for station {station_id}")
    
    async def _activate_der_control(self, station_id: str, control_id: int) -> None:
        """Activate DER control."""
        # TODO: Implement actual activation logic
        self.logger.debug(f"Activating DER control {control_id} for station {station_id}")
    
    async def _deactivate_der_control(self, station_id: str, control_id: int) -> None:
        """Deactivate DER control."""
        # TODO: Implement actual deactivation logic
        self.logger.debug(f"Deactivating DER control {control_id} for station {station_id}")
