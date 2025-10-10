"""OCPP 2.0.1 Charging Profile Manager with validation and stacking logic."""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Any, Tuple
import json

from .monitoring import get_logger
from .timescale_client import TimescaleClient


class ChargingProfilePurpose(Enum):
    """Charging profile purposes."""
    CHARGING_STATION_EXTERNAL_CONSTRAINTS = "ChargingStationExternalConstraints"
    CHARGING_STATION_MAX_PROFILE = "ChargingStationMaxProfile"
    TX_DEFAULT_PROFILE = "TxDefaultProfile"
    TX_PROFILE = "TxProfile"
    V2X_PROFILE = "V2XProfile"


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
    """Charging schedule period."""
    start_period: int
    limit: float
    number_phases: Optional[int] = None


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
    
    def __init__(self, timescale_client: TimescaleClient):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        
        # Profile cache per station
        self.profile_cache: Dict[str, List[ChargingProfile]] = {}
        
        # Stacking limits per station
        self.stacking_limits = {
            "ChargingStationMaxProfile": 1,
            "TxDefaultProfile": 1,
            "TxProfile": 1,
            "V2XProfile": 4,
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
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "UnknownChargingProfile",
                        "additionalInfo": "No matching profiles found"
                    }
                }
            
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
                number_phases=period_data.get("numberPhases")
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
                        "numberPhases": p.number_phases
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
        """Calculate composite schedule from multiple profiles."""
        # Sort profiles by stack level (highest first)
        sorted_profiles = sorted(profiles, key=lambda p: p.stack_level, reverse=True)
        
        # Create time slots
        time_slots = {}
        for i in range(0, duration, 60):  # 1-minute intervals
            time_slots[i] = {"power": 0.0, "source": None}
        
        # Apply profiles in order
        for profile in sorted_profiles:
            await self._apply_profile_to_slots(profile, time_slots, duration)
        
        # Convert to charging schedule periods
        periods = []
        current_power = None
        start_period = 0
        
        for time_slot in sorted(time_slots.keys()):
            power = time_slots[time_slot]["power"]
            
            if current_power != power:
                if current_power is not None:
                    periods.append({
                        "startPeriod": start_period,
                        "limit": current_power,
                        "numberPhases": 3
                    })
                
                current_power = power
                start_period = time_slot
        
        # Add final period
        if current_power is not None:
            periods.append({
                "startPeriod": start_period,
                "limit": current_power,
                "numberPhases": 3
            })
        
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
        now = datetime.now(timezone.utc)
        
        profiles_data = await self.timescale_client.get_active_charging_profiles(
            station_id, evse_id, now
        )
        
        profiles = []
        for profile_data in profiles_data:
            profile = self._parse_charging_profile(profile_data["schedule"])
            profiles.append(profile)
        
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
