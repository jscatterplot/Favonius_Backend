"""OCPP 2.0.1 Charging Profile Manager with validation and stacking logic."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple
import json

from .monitoring import get_logger
from .timescale_client import TimescaleClient
from .cache_manager import CacheManager


class ChargingProfilePurpose(Enum):
    """Charging profile purposes."""
    CHARGING_STATION_EXTERNAL_CONSTRAINTS = "ChargingStationExternalConstraints"
    CHARGING_STATION_MAX_PROFILE = "ChargingStationMaxProfile"
    TX_DEFAULT_PROFILE = "TxDefaultProfile"
    TX_PROFILE = "TxProfile"
    PRIORITY_CHARGING = "PriorityCharging"
    LOCAL_GENERATION = "LocalGeneration"


class OperationModeEnumType(Enum):
    """V2G operation modes."""
    CHARGING_ONLY = "ChargingOnly"
    CENTRAL_SETPOINT = "CentralSetpoint"
    CENTRAL_FREQUENCY = "CentralFrequency"
    LOCAL_FREQUENCY = "LocalFrequency"
    EXTERNAL_SETPOINT = "ExternalSetpoint"
    EXTERNAL_LIMITS = "ExternalLimits"
    LOCAL_LOAD_BALANCING = "LocalLoadBalancing"
    IDLE = "Idle"


class EnergyTransferModeEnumType(Enum):
    """Energy transfer modes."""
    AC_SINGLE_PHASE = "AC_single_phase"
    AC_TWO_PHASE = "AC_two_phase"
    AC_THREE_PHASE = "AC_three_phase"
    DC = "DC"
    DC_ACDP = "DC_ACDP"
    DC_BPT = "DC_BPT"
    DC_ACDP_BPT = "DC_ACDP_BPT"
    AC_BPT = "AC_BPT"
    AC_BPT_DER = "AC_BPT_DER"


class ChargingProfileKind(Enum):
    """Charging profile kinds."""
    ABSOLUTE = "Absolute"
    RECURRING = "Recurring"
    RELATIVE = "Relative"


class ChargingRateUnit(Enum):
    """Charging rate units."""
    W = "W"
    A = "A"


@dataclass
class ChargingSchedulePeriod:
    """Charging schedule period with V2G extensions."""
    start_period: int
    limit: float  # positive only, max charge limit
    discharging_limit: Optional[float] = None  # negative only, max discharge limit
    setpoint: Optional[float] = None  # positive=charge, negative=discharge
    setpoint_reactive: Optional[float] = None  # reactive power setpoint
    number_phases: Optional[int] = None
    phase_to_use: Optional[int] = None
    operation_mode: Optional[str] = None  # OperationModeEnumType
    v2x_freq_watt_curve: Optional[List[Dict[str, float]]] = None
    v2x_signal_watt_curve: Optional[List[Dict[str, float]]] = None
    v2x_baseline: Optional[float] = None


@dataclass
class ChargingSchedule:
    """Charging schedule."""
    id: int
    start_schedule: Optional[str] = None
    duration: Optional[int] = None
    charging_rate_unit: ChargingRateUnit = ChargingRateUnit.W
    charging_schedule_period: List[ChargingSchedulePeriod] = field(default_factory=list)
    min_charging_rate: Optional[float] = None


@dataclass
class ChargingProfile:
    """OCPP charging profile."""
    id: int
    stack_level: int
    charging_profile_purpose: ChargingProfilePurpose
    charging_profile_kind: ChargingProfileKind
    charging_schedule: ChargingSchedule
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    recurrency_kind: Optional[str] = None
    transaction_id: Optional[int] = None


class ChargingProfileManager:
    """Manages OCPP charging profiles with validation and stacking."""
    
    def __init__(self, timescale_client: TimescaleClient, cache_manager: Optional[CacheManager] = None):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        
        # Initialize cache manager
        self.cache_manager = cache_manager or CacheManager(max_size=1000, default_ttl=timedelta(seconds=300))
        
        # Legacy profile cache per station
        self.profile_cache: Dict[str, List[ChargingProfile]] = {}
        
        # Stacking limits per station
        self.stacking_limits = {
            "ChargingStationMaxProfile": 1,
            "TxDefaultProfile": 1,
            "TxProfile": 1,
            "PriorityCharging": 1,
            "LocalGeneration": 1,
            "ChargingStationExternalConstraints": 1
        }
    
    async def set_charging_profile(self, station_id: str, evse_id: int, 
                                 charging_profile: Dict[str, Any]) -> Dict[str, Any]:
        """Set charging profile with validation."""
        try:
            # Parse charging profile
            profile = self._parse_charging_profile(charging_profile)
            
            # Validate profile
            validation_result = await self._validate_profile(station_id, evse_id, profile)
            if not validation_result["valid"]:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": validation_result["reason_code"],
                        "additionalInfo": validation_result["message"]
                    }
                }
            
            # Check stacking limits
            stacking_result = await self._check_stacking_limits(station_id, evse_id, profile)
            if not stacking_result["valid"]:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": stacking_result["reason_code"],
                        "additionalInfo": stacking_result["message"]
                    }
                }
            
            # Store profile
            await self._store_profile(station_id, evse_id, profile)
            
            # Update cache
            await self._update_cache(station_id, evse_id, profile)
            
            self.logger.info(f"Set charging profile {profile.id} for {station_id}, EVSE {evse_id}")
            
            return {"status": "Accepted"}
            
        except Exception as e:
            self.logger.error(f"Error setting charging profile: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def clear_charging_profile(self, station_id: str, evse_id: int, 
                                   charging_profile_id: Optional[int] = None,
                                   charging_profile_purpose: Optional[str] = None,
                                   stack_level: Optional[int] = None) -> Dict[str, Any]:
        """Clear charging profile(s)."""
        try:
            # Get profiles to clear
            profiles_to_clear = await self._get_profiles_to_clear(
                station_id, evse_id, charging_profile_id, 
                charging_profile_purpose, stack_level
            )
            
            if not profiles_to_clear:
                # According to OCPP 2.0.1, clearing non-existent profiles should return Accepted (idempotent)
                return {"status": "Accepted"}
            
            # Clear profiles
            for profile in profiles_to_clear:
                await self._remove_profile(station_id, evse_id, profile.id)
            
            # Update cache
            await self._refresh_cache(station_id, evse_id)
            
            self.logger.info(f"Cleared {len(profiles_to_clear)} charging profiles for {station_id}, EVSE {evse_id}")
            
            return {"status": "Accepted"}
            
        except Exception as e:
            self.logger.error(f"Error clearing charging profile: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def get_charging_profiles(self, station_id: str, evse_id: int,
                                  charging_profile_id: Optional[int] = None,
                                  charging_profile_purpose: Optional[str] = None,
                                  stack_level: Optional[int] = None) -> Dict[str, Any]:
        """Get charging profiles."""
        try:
            profiles = await self._get_profiles(
                station_id, evse_id, charging_profile_id,
                charging_profile_purpose, stack_level
            )
            
            return {
                "status": "Accepted",
                "chargingProfile": [self._profile_to_dict(p) for p in profiles]
            }
            
        except Exception as e:
            self.logger.error(f"Error getting charging profiles: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def get_composite_schedule(self, station_id: str, evse_id: int, 
                                   duration: int, charging_rate_unit: Optional[str] = None) -> Dict[str, Any]:
        """Calculate composite schedule from all active profiles."""
        try:
            # Get all active profiles for the EVSE
            profiles = await self._get_active_profiles(station_id, evse_id)
            
            if not profiles:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "NoActiveChargingProfile",
                        "additionalInfo": "No active charging profiles found"
                    }
                }
            
            # Calculate composite schedule
            composite_schedule = await self._calculate_composite_schedule(
                profiles, duration, charging_rate_unit
            )
            
            return {
                "status": "Accepted",
                "connectorId": 1,  # Default connector
                "scheduleStart": composite_schedule["start_time"],
                "chargingSchedule": composite_schedule["schedule"]
            }
            
        except Exception as e:
            self.logger.error(f"Error calculating composite schedule: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def report_charging_profiles(self, station_id: str, evse_id: int,
                                    request_id: int, charging_profile: List[Dict[str, Any]]) -> None:
        """Handle ReportChargingProfiles message."""
        try:
            # Store reported profiles
            for profile_data in charging_profile:
                profile = self._parse_charging_profile(profile_data)
                await self._store_reported_profile(station_id, evse_id, request_id, profile)
            
            self.logger.info(f"Stored {len(charging_profile)} reported profiles for {station_id}, EVSE {evse_id}")
            
        except Exception as e:
            self.logger.error(f"Error storing reported profiles: {e}")
    
    def _parse_charging_profile(self, profile_data: Dict[str, Any]) -> ChargingProfile:
        """Parse charging profile from OCPP message."""
        charging_schedule_data = profile_data.get("chargingSchedule", {})
        
        # Parse charging schedule periods
        periods = []
        for period_data in charging_schedule_data.get("chargingSchedulePeriod", []):
            period = ChargingSchedulePeriod(
                start_period=period_data["startPeriod"],
                limit=period_data["limit"],
                discharging_limit=period_data.get("dischargingLimit"),
                setpoint=period_data.get("setpoint"),
                setpoint_reactive=period_data.get("setpointReactive"),
                number_phases=period_data.get("numberPhases"),
                phase_to_use=period_data.get("phaseToUse"),
                operation_mode=period_data.get("operationMode"),
                v2x_freq_watt_curve=period_data.get("v2xFreqWattCurve"),
                v2x_signal_watt_curve=period_data.get("v2xSignalWattCurve"),
                v2x_baseline=period_data.get("v2xBaseline")
            )
            periods.append(period)
        
        # Parse charging schedule
        charging_schedule = ChargingSchedule(
            id=charging_schedule_data["id"],
            start_schedule=charging_schedule_data.get("startSchedule"),
            duration=charging_schedule_data.get("duration"),
            charging_rate_unit=ChargingRateUnit(charging_schedule_data.get("chargingRateUnit", "W")),
            charging_schedule_period=periods,
            min_charging_rate=charging_schedule_data.get("minChargingRate")
        )
        
        # Parse charging profile
        profile = ChargingProfile(
            id=profile_data["id"],
            stack_level=profile_data["stackLevel"],
            charging_profile_purpose=ChargingProfilePurpose(profile_data["chargingProfilePurpose"]),
            charging_profile_kind=ChargingProfileKind(profile_data["chargingProfileKind"]),
            charging_schedule=charging_schedule,
            valid_from=profile_data.get("validFrom"),
            valid_to=profile_data.get("validTo"),
            recurrency_kind=profile_data.get("recurrencyKind"),
            transaction_id=profile_data.get("transactionId")
        )
        
        return profile
    
    def _profile_to_dict(self, profile: ChargingProfile) -> Dict[str, Any]:
        """Convert ChargingProfile to dictionary."""
        return {
            "id": profile.id,
            "stackLevel": profile.stack_level,
            "chargingProfilePurpose": profile.charging_profile_purpose.value,
            "chargingProfileKind": profile.charging_profile_kind.value,
            "chargingSchedule": {
                "id": profile.charging_schedule.id,
                "startSchedule": profile.charging_schedule.start_schedule,
                "duration": profile.charging_schedule.duration,
                "chargingRateUnit": profile.charging_schedule.charging_rate_unit.value,
                "chargingSchedulePeriod": [
                    {
                        "startPeriod": p.start_period,
                        "limit": p.limit,
                        "dischargingLimit": p.discharging_limit,
                        "setpoint": p.setpoint,
                        "setpointReactive": p.setpoint_reactive,
                        "numberPhases": p.number_phases,
                        "phaseToUse": p.phase_to_use,
                        "operationMode": p.operation_mode,
                        "v2xFreqWattCurve": p.v2x_freq_watt_curve,
                        "v2xSignalWattCurve": p.v2x_signal_watt_curve,
                        "v2xBaseline": p.v2x_baseline
                    }
                    for p in profile.charging_schedule.charging_schedule_period
                ],
                "minChargingRate": profile.charging_schedule.min_charging_rate
            },
            "validFrom": profile.valid_from,
            "validTo": profile.valid_to,
            "recurrencyKind": profile.recurrency_kind,
            "transactionId": profile.transaction_id
        }
    
    async def _validate_profile(self, station_id: str, evse_id: int, 
                              profile: ChargingProfile) -> Dict[str, Any]:
        """Validate charging profile."""
        # Check profile ID uniqueness
        existing_profiles = await self._get_profiles(station_id, evse_id)
        if any(p.id == profile.id for p in existing_profiles):
            return {
                "valid": False,
                "reason_code": "Duplicate",
                "message": f"Profile ID {profile.id} already exists"
            }
        
        # Validate stack level
        if profile.stack_level < 0 or profile.stack_level > 10:
            return {
                "valid": False,
                "reason_code": "PropertyConstraintViolation",
                "message": "Stack level must be between 0 and 10"
            }
        
        # Validate charging schedule
        if not profile.charging_schedule.charging_schedule_period:
            return {
                "valid": False,
                "reason_code": "PropertyConstraintViolation",
                "message": "Charging schedule must have at least one period"
            }
        
        # Validate periods
        for i, period in enumerate(profile.charging_schedule.charging_schedule_period):
            if period.start_period < 0:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": f"Period {i} start time must be >= 0"
                }
            
            if period.limit < 0:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": f"Period {i} limit must be >= 0"
                }
            
            # Validate discharging limit (must be negative or None)
            if period.discharging_limit is not None and period.discharging_limit > 0:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": f"Period {i} dischargingLimit must be <= 0"
                }
            
            # Validate setpoint constraints
            if period.setpoint is not None:
                # Setpoint can be positive (charge) or negative (discharge)
                if period.limit > 0 and period.setpoint > period.limit:
                    return {
                        "valid": False,
                        "reason_code": "PropertyConstraintViolation",
                        "message": f"Period {i} setpoint exceeds charge limit"
                    }
                if period.discharging_limit is not None and period.setpoint < period.discharging_limit:
                    return {
                        "valid": False,
                        "reason_code": "PropertyConstraintViolation",
                        "message": f"Period {i} setpoint exceeds discharge limit"
                    }
        
        # Validate time constraints
        if profile.valid_from and profile.valid_to:
            try:
                valid_from = datetime.fromisoformat(profile.valid_from.replace('Z', '+00:00'))
                valid_to = datetime.fromisoformat(profile.valid_to.replace('Z', '+00:00'))
                
                if valid_from >= valid_to:
                    return {
                        "valid": False,
                        "reason_code": "PropertyConstraintViolation",
                        "message": "validFrom must be before validTo"
                    }
            except ValueError:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": "Invalid datetime format"
                }
        
        return {"valid": True}
    
    async def _check_stacking_limits(self, station_id: str, evse_id: int, 
                                   profile: ChargingProfile) -> Dict[str, Any]:
        """Check stacking limits for profile."""
        purpose = profile.charging_profile_purpose.value
        max_profiles = self.stacking_limits.get(purpose, 1)
        
        # Count existing profiles of same purpose and stack level
        existing_profiles = await self._get_profiles(station_id, evse_id)
        same_purpose_level = [
            p for p in existing_profiles 
            if p.charging_profile_purpose == profile.charging_profile_purpose
            and p.stack_level == profile.stack_level
        ]
        
        if len(same_purpose_level) >= max_profiles:
            return {
                "valid": False,
                "reason_code": "PropertyConstraintViolation",
                "message": f"Maximum {max_profiles} profiles allowed for {purpose} at stack level {profile.stack_level}"
            }
        
        return {"valid": True}
    
    async def _calculate_composite_schedule(self, profiles: List[ChargingProfile], 
                                         duration: int, charging_rate_unit: Optional[str]) -> Dict[str, Any]:
        """Calculate composite schedule from multiple profiles with V2G support."""
        # Sort profiles by stack level (highest first)
        sorted_profiles = sorted(profiles, key=lambda p: p.stack_level, reverse=True)
        
        # Create time slots with V2G support
        time_slots = {}
        for i in range(0, duration, 60):  # 1-minute intervals
            time_slots[i] = {
                "charge_limit": 0.0,
                "discharge_limit": 0.0,
                "setpoint": None,
                "setpoint_reactive": None,
                "operation_mode": None,
                "v2x_freq_watt_curve": None,
                "v2x_signal_watt_curve": None,
                "v2x_baseline": None,
                "source": None,
                "priority": 0
            }
        
        # Apply profiles in order with V2G stacking rules
        for profile in sorted_profiles:
            await self._apply_profile_to_slots_v2g(profile, time_slots, duration)
        
        # Convert to charging schedule periods with V2G fields
        periods = []
        current_state = None
        start_period = 0
        
        for time_slot in sorted(time_slots.keys()):
            slot_data = time_slots[time_slot]
            
            # Create state key for comparison
            state_key = (
                slot_data["charge_limit"],
                slot_data["discharge_limit"],
                slot_data["setpoint"],
                slot_data["operation_mode"]
            )
            
            if current_state != state_key:
                if current_state is not None:
                    periods.append(self._create_period_from_slot_data(
                        start_period, time_slots[start_period]
                    ))
                
                current_state = state_key
                start_period = time_slot
        
        # Add final period
        if current_state is not None:
            periods.append(self._create_period_from_slot_data(
                start_period, time_slots[start_period]
            ))
        
        return {
            "start_time": datetime.now(timezone.utc).isoformat(),
            "schedule": {
                "id": int(datetime.now().timestamp()),
                "startSchedule": datetime.now(timezone.utc).isoformat(),
                "duration": duration,
                "chargingRateUnit": charging_rate_unit or "W",
                "chargingSchedulePeriod": periods
            }
        }
    
    async def _apply_profile_to_slots(self, profile: ChargingProfile, 
                                    time_slots: Dict[int, Dict[str, Any]], duration: int) -> None:
        """Apply a profile to time slots."""
        for period in profile.charging_schedule.charging_schedule_period:
            start_time = period.start_period
            end_time = min(start_time + 60, duration)  # Assume 1-minute periods
            
            for slot in range(start_time, end_time, 60):
                if slot in time_slots:
                    # Apply the limit (higher stack level overrides lower)
                    if time_slots[slot]["source"] is None or profile.stack_level > time_slots[slot]["source"]:
                        time_slots[slot]["power"] = period.limit
                        time_slots[slot]["source"] = profile.stack_level
    
    async def _apply_profile_to_slots_v2g(self, profile: ChargingProfile, 
                                        time_slots: Dict[int, Dict[str, Any]], duration: int) -> None:
        """Apply a profile to time slots with V2G support."""
        for period in profile.charging_schedule.charging_schedule_period:
            start_time = period.start_period
            end_time = min(start_time + 60, duration)  # Assume 1-minute periods
            
            for slot in range(start_time, end_time, 60):
                if slot in time_slots:
                    slot_data = time_slots[slot]
                    
                    # Determine if this profile should override based on V2G stacking rules
                    should_override = self._should_override_slot_v2g(
                        profile, slot_data, period
                    )
                    
                    if should_override:
                        # Apply V2G-specific fields
                        slot_data["charge_limit"] = period.limit
                        slot_data["discharge_limit"] = period.discharging_limit
                        slot_data["setpoint"] = period.setpoint
                        slot_data["setpoint_reactive"] = period.setpoint_reactive
                        slot_data["operation_mode"] = period.operation_mode
                        slot_data["v2x_freq_watt_curve"] = period.v2x_freq_watt_curve
                        slot_data["v2x_signal_watt_curve"] = period.v2x_signal_watt_curve
                        slot_data["v2x_baseline"] = period.v2x_baseline
                        slot_data["source"] = profile.stack_level
                        slot_data["priority"] = self._get_profile_priority(profile)
    
    def _should_override_slot_v2g(self, profile: ChargingProfile, 
                                 slot_data: Dict[str, Any], period: ChargingSchedulePeriod) -> bool:
        """Determine if profile should override slot based on V2G stacking rules."""
        # Higher stack level always overrides lower
        if slot_data["source"] is None or profile.stack_level > slot_data["source"]:
            return True
        
        # Same stack level - check V2G-specific rules
        if profile.stack_level == slot_data["source"]:
            # Priority charging overrides other profiles
            if profile.charging_profile_purpose == ChargingProfilePurpose.PRIORITY_CHARGING:
                return True
            
            # Local generation adds capacity (doesn't override limits)
            if profile.charging_profile_purpose == ChargingProfilePurpose.LOCAL_GENERATION:
                return False
            
            # External constraints have priority over transaction profiles
            if (profile.charging_profile_purpose == ChargingProfilePurpose.CHARGING_STATION_EXTERNAL_CONSTRAINTS and
                slot_data["priority"] < self._get_profile_priority(profile)):
                return True
        
        return False
    
    def _get_profile_priority(self, profile: ChargingProfile) -> int:
        """Get profile priority for V2G stacking."""
        priority_map = {
            ChargingProfilePurpose.CHARGING_STATION_EXTERNAL_CONSTRAINTS: 100,
            ChargingProfilePurpose.CHARGING_STATION_MAX_PROFILE: 90,
            ChargingProfilePurpose.PRIORITY_CHARGING: 80,
            ChargingProfilePurpose.TX_PROFILE: 70,
            ChargingProfilePurpose.TX_DEFAULT_PROFILE: 60,
            ChargingProfilePurpose.LOCAL_GENERATION: 50
        }
        return priority_map.get(profile.charging_profile_purpose, 0)
    
    def _create_period_from_slot_data(self, start_period: int, slot_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create charging schedule period from slot data."""
        period = {
            "startPeriod": start_period,
            "limit": slot_data["charge_limit"],
            "numberPhases": 3
        }
        
        # Add V2G-specific fields if present
        if slot_data["discharge_limit"] is not None:
            period["dischargingLimit"] = slot_data["discharge_limit"]
        
        if slot_data["setpoint"] is not None:
            period["setpoint"] = slot_data["setpoint"]
        
        if slot_data["setpoint_reactive"] is not None:
            period["setpointReactive"] = slot_data["setpoint_reactive"]
        
        if slot_data["operation_mode"] is not None:
            period["operationMode"] = slot_data["operation_mode"]
        
        if slot_data["v2x_freq_watt_curve"] is not None:
            period["v2xFreqWattCurve"] = slot_data["v2x_freq_watt_curve"]
        
        if slot_data["v2x_signal_watt_curve"] is not None:
            period["v2xSignalWattCurve"] = slot_data["v2x_signal_watt_curve"]
        
        if slot_data["v2x_baseline"] is not None:
            period["v2xBaseline"] = slot_data["v2x_baseline"]
        
        return period
    
    async def _store_profile(self, station_id: str, evse_id: int, profile: ChargingProfile) -> None:
        """Store charging profile in database."""
        await self.timescale_client.store_charging_profile({
            "station_id": station_id,
            "evse_id": evse_id,
            "profile_id": profile.id,
            "stack_level": profile.stack_level,
            "purpose": profile.charging_profile_purpose.value,
            "kind": profile.charging_profile_kind.value,
            "schedule": self._profile_to_dict(profile),
            "valid_from": profile.valid_from,
            "valid_to": profile.valid_to,
            "transaction_id": profile.transaction_id,
            "created_at": datetime.now(timezone.utc)
        })
    
    async def _remove_profile(self, station_id: str, evse_id: int, profile_id: int) -> None:
        """Remove charging profile from database."""
        await self.timescale_client.remove_charging_profile(station_id, evse_id, profile_id)
    
    async def _get_profiles(self, station_id: str, evse_id: int,
                          profile_id: Optional[int] = None,
                          purpose: Optional[str] = None,
                          stack_level: Optional[int] = None) -> List[ChargingProfile]:
        """Get charging profiles with filters."""
        profiles_data = await self.timescale_client.get_charging_profiles(
            station_id, evse_id, profile_id, purpose, stack_level
        )
        
        profiles = []
        for profile_data in profiles_data:
            profile = self._parse_charging_profile(profile_data["schedule"])
            profiles.append(profile)
        
        return profiles
    
    async def _get_active_profiles(self, station_id: str, evse_id: int) -> List[ChargingProfile]:
        """Get active charging profiles."""
        cache_key = f"active_profiles:{station_id}:{evse_id}"
        
        # Check new cache manager first
        cached_profiles = await self.cache_manager.get(cache_key)
        if cached_profiles:
            self.logger.debug(f"Active profiles for {station_id}:{evse_id} found in cache")
            return cached_profiles
        
        # Check legacy cache
        legacy_cache_key = f"{station_id}:{evse_id}"
        if legacy_cache_key in self.profile_cache:
            return self.profile_cache[legacy_cache_key]
        
        now = datetime.now(timezone.utc)
        
        profiles_data = await self.timescale_client.get_active_charging_profiles(
            station_id, evse_id, now
        )
        
        profiles = []
        for profile_data in profiles_data:
            profile = self._parse_charging_profile(profile_data["schedule"])
            profiles.append(profile)
        
        # Cache profiles in both caches
        await self.cache_manager.set(cache_key, profiles, ttl=timedelta(seconds=300))
        self.profile_cache[legacy_cache_key] = profiles
        
        return profiles
    
    async def _get_profiles_to_clear(self, station_id: str, evse_id: int,
                                   profile_id: Optional[int] = None,
                                   purpose: Optional[str] = None,
                                   stack_level: Optional[int] = None) -> List[ChargingProfile]:
        """Get profiles to clear based on criteria."""
        return await self._get_profiles(station_id, evse_id, profile_id, purpose, stack_level)
    
    async def _update_cache(self, station_id: str, evse_id: int, profile: ChargingProfile) -> None:
        """Update profile cache."""
        cache_key = f"{station_id}:{evse_id}"
        if cache_key not in self.profile_cache:
            self.profile_cache[cache_key] = []
        
        # Remove existing profile with same ID
        self.profile_cache[cache_key] = [
            p for p in self.profile_cache[cache_key] if p.id != profile.id
        ]
        
        # Add new profile
        self.profile_cache[cache_key].append(profile)
    
    async def _refresh_cache(self, station_id: str, evse_id: int) -> None:
        """Refresh profile cache from database."""
        cache_key = f"{station_id}:{evse_id}"
        self.profile_cache[cache_key] = await self._get_profiles(station_id, evse_id)
    
    async def _store_reported_profile(self, station_id: str, evse_id: int, 
                                    request_id: int, profile: ChargingProfile) -> None:
        """Store reported profile from charger."""
        await self.timescale_client.store_reported_charging_profile({
            "station_id": station_id,
            "evse_id": evse_id,
            "request_id": request_id,
            "profile": self._profile_to_dict(profile),
            "reported_at": datetime.now(timezone.utc)
        })

    async def update_dynamic_schedule(self, station_id: str, charging_profile_id: int,
                                    limit: Optional[float] = None,
                                    discharging_limit: Optional[float] = None,
                                    setpoint: Optional[float] = None,
                                    setpoint_reactive: Optional[float] = None) -> Dict[str, Any]:
        """Update dynamic schedule for a charging profile."""
        try:
            # Get existing profile
            profiles = await self.timescale_client.get_charging_profiles(
                station_id, None, None, None
            )
            
            # Find the profile to update
            profile_to_update = None
            for profile_data in profiles:
                if profile_data["profile_id"] == charging_profile_id:
                    profile_to_update = profile_data
                    break
            
            if not profile_to_update:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "NotFound",
                        "additionalInfo": f"Charging profile {charging_profile_id} not found"
                    }
                }
            
            # Parse existing profile
            profile_dict = json.loads(profile_to_update["schedule"])
            profile = self._parse_charging_profile(profile_dict)
            
            # Update the first period with new values
            if profile.charging_schedule.charging_schedule_period:
                period = profile.charging_schedule.charging_schedule_period[0]
                if limit is not None:
                    period.limit = limit
                if discharging_limit is not None:
                    period.discharging_limit = discharging_limit
                if setpoint is not None:
                    period.setpoint = setpoint
                if setpoint_reactive is not None:
                    period.setpoint_reactive = setpoint_reactive
            
            # Store updated profile
            await self.timescale_client.store_charging_profile(
                station_id, profile_to_update["evse_id"], profile
            )
            
            return {
                "status": "Accepted"
            }
            
        except Exception as e:
            self.logger.error(f"Failed to update dynamic schedule: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
