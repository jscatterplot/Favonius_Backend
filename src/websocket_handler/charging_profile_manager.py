"""OCPP 2.0.1 Charging Profile Manager with validation and stacking logic."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .cache_manager import CacheManager
from .connection_pool import open_dedicated_connection
from .monitoring import (
    CHARGING_COMMAND_QUEUE_DEPTH,
    PROFILE_PUSH_LATENCY,
    get_logger,
)
from .timescale_client import TimescaleClient


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

    def __init__(
        self, timescale_client: TimescaleClient, cache_manager: Optional[CacheManager] = None
    ):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)

        # Initialize cache manager
        self.cache_manager = cache_manager or CacheManager(
            max_size=1000, default_ttl=timedelta(seconds=300)
        )

        # Legacy profile cache per station
        self.profile_cache: Dict[str, List[ChargingProfile]] = {}

        # Stacking limits per station
        self.stacking_limits = {
            "ChargingStationMaxProfile": 1,
            "TxDefaultProfile": 1,
            "TxProfile": 1,
            "PriorityCharging": 1,
            "LocalGeneration": 1,
            "ChargingStationExternalConstraints": 1,
        }

    async def set_charging_profile(
        self, station_id: str, evse_id: int, charging_profile: Dict[str, Any]
    ) -> Dict[str, Any]:
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
                        "additionalInfo": validation_result["message"],
                    },
                }

            # Check stacking limits
            stacking_result = await self._check_stacking_limits(station_id, evse_id, profile)
            if not stacking_result["valid"]:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": stacking_result["reason_code"],
                        "additionalInfo": stacking_result["message"],
                    },
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
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def clear_charging_profile(
        self,
        station_id: str,
        evse_id: int,
        charging_profile_id: Optional[int] = None,
        charging_profile_purpose: Optional[str] = None,
        stack_level: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Clear charging profile(s)."""
        try:
            # Get profiles to clear
            profiles_to_clear = await self._get_profiles_to_clear(
                station_id, evse_id, charging_profile_id, charging_profile_purpose, stack_level
            )

            if not profiles_to_clear:
                # According to OCPP 2.0.1, clearing non-existent profiles should return Accepted (idempotent)
                return {"status": "Accepted"}

            # Clear profiles
            for profile in profiles_to_clear:
                await self._remove_profile(station_id, evse_id, profile.id)

            # Update cache
            await self._refresh_cache(station_id, evse_id)

            self.logger.info(
                f"Cleared {len(profiles_to_clear)} charging profiles for {station_id}, EVSE {evse_id}"
            )

            return {"status": "Accepted"}

        except Exception as e:
            self.logger.error(f"Error clearing charging profile: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def get_charging_profiles(
        self,
        station_id: str,
        evse_id: int,
        charging_profile_id: Optional[int] = None,
        charging_profile_purpose: Optional[str] = None,
        stack_level: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Get charging profiles."""
        try:
            profiles = await self._get_profiles(
                station_id, evse_id, charging_profile_id, charging_profile_purpose, stack_level
            )

            return {
                "status": "Accepted",
                "chargingProfile": [self._profile_to_dict(p) for p in profiles],
            }

        except Exception as e:
            self.logger.error(f"Error getting charging profiles: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def get_composite_schedule(
        self, station_id: str, evse_id: int, duration: int, charging_rate_unit: Optional[str] = None
    ) -> Dict[str, Any]:
        """Calculate composite schedule from all active profiles."""
        try:
            # Get all active profiles for the EVSE
            profiles = await self._get_active_profiles(station_id, evse_id)

            if not profiles:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "NoActiveChargingProfile",
                        "additionalInfo": "No active charging profiles found",
                    },
                }

            # Calculate composite schedule
            composite_schedule = await self._calculate_composite_schedule(
                profiles, duration, charging_rate_unit
            )

            return {
                "status": "Accepted",
                "connectorId": 1,  # Default connector
                "scheduleStart": composite_schedule["start_time"],
                "chargingSchedule": composite_schedule["schedule"],
            }

        except Exception as e:
            self.logger.error(f"Error calculating composite schedule: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def report_charging_profiles(
        self, station_id: str, evse_id: int, request_id: int, charging_profile: List[Dict[str, Any]]
    ) -> None:
        """Handle ReportChargingProfiles message."""
        try:
            # Store reported profiles
            for profile_data in charging_profile:
                profile = self._parse_charging_profile(profile_data)
                await self._store_reported_profile(station_id, evse_id, request_id, profile)

            self.logger.info(
                f"Stored {len(charging_profile)} reported profiles for {station_id}, EVSE {evse_id}"
            )

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
                v2x_baseline=period_data.get("v2xBaseline"),
            )
            periods.append(period)

        # Parse charging schedule
        charging_schedule = ChargingSchedule(
            id=charging_schedule_data["id"],
            start_schedule=charging_schedule_data.get("startSchedule"),
            duration=charging_schedule_data.get("duration"),
            charging_rate_unit=ChargingRateUnit(
                charging_schedule_data.get("chargingRateUnit", "W")
            ),
            charging_schedule_period=periods,
            min_charging_rate=charging_schedule_data.get("minChargingRate"),
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
            transaction_id=profile_data.get("transactionId"),
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
                        "v2xBaseline": p.v2x_baseline,
                    }
                    for p in profile.charging_schedule.charging_schedule_period
                ],
                "minChargingRate": profile.charging_schedule.min_charging_rate,
            },
            "validFrom": profile.valid_from,
            "validTo": profile.valid_to,
            "recurrencyKind": profile.recurrency_kind,
            "transactionId": profile.transaction_id,
        }

    async def _validate_profile(
        self, station_id: str, evse_id: int, profile: ChargingProfile
    ) -> Dict[str, Any]:
        """Validate charging profile."""
        # Check profile ID uniqueness
        existing_profiles = await self._get_profiles(station_id, evse_id)
        if any(p.id == profile.id for p in existing_profiles):
            return {
                "valid": False,
                "reason_code": "Duplicate",
                "message": f"Profile ID {profile.id} already exists",
            }

        # Validate stack level
        if profile.stack_level < 0 or profile.stack_level > 10:
            return {
                "valid": False,
                "reason_code": "PropertyConstraintViolation",
                "message": "Stack level must be between 0 and 10",
            }

        # Validate charging schedule
        if not profile.charging_schedule.charging_schedule_period:
            return {
                "valid": False,
                "reason_code": "PropertyConstraintViolation",
                "message": "Charging schedule must have at least one period",
            }

        # Validate periods
        for i, period in enumerate(profile.charging_schedule.charging_schedule_period):
            if period.start_period < 0:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": f"Period {i} start time must be >= 0",
                }

            if period.limit < 0:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": f"Period {i} limit must be >= 0",
                }

            # Validate discharging limit (must be negative or None)
            if period.discharging_limit is not None and period.discharging_limit > 0:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": f"Period {i} dischargingLimit must be <= 0",
                }

            # Validate setpoint constraints
            if period.setpoint is not None:
                # Setpoint can be positive (charge) or negative (discharge)
                if period.limit > 0 and period.setpoint > period.limit:
                    return {
                        "valid": False,
                        "reason_code": "PropertyConstraintViolation",
                        "message": f"Period {i} setpoint exceeds charge limit",
                    }
                if (
                    period.discharging_limit is not None
                    and period.setpoint < period.discharging_limit
                ):
                    return {
                        "valid": False,
                        "reason_code": "PropertyConstraintViolation",
                        "message": f"Period {i} setpoint exceeds discharge limit",
                    }

        # Validate time constraints
        if profile.valid_from and profile.valid_to:
            try:
                valid_from = datetime.fromisoformat(profile.valid_from.replace("Z", "+00:00"))
                valid_to = datetime.fromisoformat(profile.valid_to.replace("Z", "+00:00"))

                if valid_from >= valid_to:
                    return {
                        "valid": False,
                        "reason_code": "PropertyConstraintViolation",
                        "message": "validFrom must be before validTo",
                    }
            except ValueError:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": "Invalid datetime format",
                }

        return {"valid": True}

    async def _check_stacking_limits(
        self, station_id: str, evse_id: int, profile: ChargingProfile
    ) -> Dict[str, Any]:
        """Check stacking limits for profile."""
        purpose = profile.charging_profile_purpose.value
        max_profiles = self.stacking_limits.get(purpose, 1)

        # Count existing profiles of same purpose and stack level
        existing_profiles = await self._get_profiles(station_id, evse_id)
        same_purpose_level = [
            p
            for p in existing_profiles
            if p.charging_profile_purpose == profile.charging_profile_purpose
            and p.stack_level == profile.stack_level
        ]

        if len(same_purpose_level) >= max_profiles:
            return {
                "valid": False,
                "reason_code": "PropertyConstraintViolation",
                "message": f"Maximum {max_profiles} profiles allowed for {purpose} at stack level {profile.stack_level}",
            }

        return {"valid": True}

    async def _calculate_composite_schedule(
        self, profiles: List[ChargingProfile], duration: int, charging_rate_unit: Optional[str]
    ) -> Dict[str, Any]:
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
                "priority": 0,
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
                slot_data["operation_mode"],
            )

            if current_state != state_key:
                if current_state is not None:
                    periods.append(
                        self._create_period_from_slot_data(start_period, time_slots[start_period])
                    )

                current_state = state_key
                start_period = time_slot

        # Add final period
        if current_state is not None:
            periods.append(
                self._create_period_from_slot_data(start_period, time_slots[start_period])
            )

        return {
            "start_time": datetime.now(timezone.utc).isoformat(),
            "schedule": {
                "id": int(datetime.now().timestamp()),
                "startSchedule": datetime.now(timezone.utc).isoformat(),
                "duration": duration,
                "chargingRateUnit": charging_rate_unit or "W",
                "chargingSchedulePeriod": periods,
            },
        }

    async def _apply_profile_to_slots(
        self, profile: ChargingProfile, time_slots: Dict[int, Dict[str, Any]], duration: int
    ) -> None:
        """Apply a profile to time slots."""
        for period in profile.charging_schedule.charging_schedule_period:
            start_time = period.start_period
            end_time = min(start_time + 60, duration)  # Assume 1-minute periods

            for slot in range(start_time, end_time, 60):
                if slot in time_slots:
                    # Apply the limit (higher stack level overrides lower)
                    if (
                        time_slots[slot]["source"] is None
                        or profile.stack_level > time_slots[slot]["source"]
                    ):
                        time_slots[slot]["power"] = period.limit
                        time_slots[slot]["source"] = profile.stack_level

    async def _apply_profile_to_slots_v2g(
        self, profile: ChargingProfile, time_slots: Dict[int, Dict[str, Any]], duration: int
    ) -> None:
        """Apply a profile to time slots with V2G support."""
        for period in profile.charging_schedule.charging_schedule_period:
            start_time = period.start_period
            end_time = min(start_time + 60, duration)  # Assume 1-minute periods

            for slot in range(start_time, end_time, 60):
                if slot in time_slots:
                    slot_data = time_slots[slot]

                    # Determine if this profile should override based on V2G stacking rules
                    should_override = self._should_override_slot_v2g(profile, slot_data, period)

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

    def _should_override_slot_v2g(
        self, profile: ChargingProfile, slot_data: Dict[str, Any], period: ChargingSchedulePeriod
    ) -> bool:
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
            if (
                profile.charging_profile_purpose
                == ChargingProfilePurpose.CHARGING_STATION_EXTERNAL_CONSTRAINTS
                and slot_data["priority"] < self._get_profile_priority(profile)
            ):
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
            ChargingProfilePurpose.LOCAL_GENERATION: 50,
        }
        return priority_map.get(profile.charging_profile_purpose, 0)

    def _create_period_from_slot_data(
        self, start_period: int, slot_data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create charging schedule period from slot data."""
        period = {
            "startPeriod": start_period,
            "limit": slot_data["charge_limit"],
            "numberPhases": 3,
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
        await self.timescale_client.store_charging_profile(
            {
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
                "created_at": datetime.now(timezone.utc),
            }
        )

    async def _remove_profile(self, station_id: str, evse_id: int, profile_id: int) -> None:
        """Remove charging profile from database."""
        await self.timescale_client.remove_charging_profile(station_id, evse_id, profile_id)

    async def _get_profiles(
        self,
        station_id: str,
        evse_id: int,
        profile_id: Optional[int] = None,
        purpose: Optional[str] = None,
        stack_level: Optional[int] = None,
    ) -> List[ChargingProfile]:
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

    async def _get_profiles_to_clear(
        self,
        station_id: str,
        evse_id: int,
        profile_id: Optional[int] = None,
        purpose: Optional[str] = None,
        stack_level: Optional[int] = None,
    ) -> List[ChargingProfile]:
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

    async def _store_reported_profile(
        self, station_id: str, evse_id: int, request_id: int, profile: ChargingProfile
    ) -> None:
        """Store reported profile from charger."""
        await self.timescale_client.store_reported_charging_profile(
            {
                "station_id": station_id,
                "evse_id": evse_id,
                "request_id": request_id,
                "profile": self._profile_to_dict(profile),
                "reported_at": datetime.now(timezone.utc),
            }
        )

    async def update_dynamic_schedule(
        self,
        station_id: str,
        charging_profile_id: int,
        limit: Optional[float] = None,
        discharging_limit: Optional[float] = None,
        setpoint: Optional[float] = None,
        setpoint_reactive: Optional[float] = None,
    ) -> Dict[str, Any]:
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
                        "additionalInfo": f"Charging profile {charging_profile_id} not found",
                    },
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

            return {"status": "Accepted"}

        except Exception as e:
            self.logger.error(f"Failed to update dynamic schedule: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }


# ===========================================================================
# Queue-mediated dispatch consumer (session 3)
# ===========================================================================

# Type alias: lookup callable returning the connected charge-point handler
# (FleetChargePoint / OCPP16Session / EnhancedOCPPChargePoint) for a given
# OCPP charge-point id, or None if not connected.
ChargePointLookup = Callable[[str], Optional[Any]]


class ChargingCommandQueueConsumer:
    """Drains ``charging_command_queue`` and pushes to connected chargers.

    The FastAPI optimizer enqueues SetChargingProfile rows (migration 014).
    This consumer runs inside the legacy WebSocket handler — the only
    process that owns live charger sockets — and is responsible for:

      * Picking up newly enqueued ``pending`` rows (LISTEN/NOTIFY when
        possible, plus a 2 s polling fallback).
      * Looking up the charger's in-memory session via ``cp_lookup``.
      * Calling ``send_charging_profile`` on the session and recording
        ``profile_push_latency_seconds`` with outcome ``sent`` / ``failed``
        / ``offline``.
      * Marking the queue row terminal (``sent`` / ``failed``) so the
        next iteration does not re-attempt it.

    Rows whose charger is offline are *left pending*. The boot replay path
    in ``OCPP16Session._on_boot`` (session 2) flushes them on the next
    BootNotification, so we don't need a separate retry loop here.
    """

    POLL_INTERVAL_SECONDS = 2.0
    DEPTH_SAMPLE_INTERVAL_SECONDS = 10.0
    EXPIRY_SCAN_INTERVAL_SECONDS = 60.0
    EXCLUDED_OFFLINE_RETRY_SECONDS = 30.0
    NOTIFY_CHANNEL = "charging_command_queue"

    def __init__(
        self,
        timescale_client: TimescaleClient,
        cp_lookup: ChargePointLookup,
        *,
        poll_interval_seconds: Optional[float] = None,
    ) -> None:
        """Initialise the consumer.

        Args:
            timescale_client: Async TimescaleDB client (owns the asyncpg pool
                we'll use for both polling reads and the LISTEN connection).
            cp_lookup: Callable returning the in-memory charger handler
                for a given OCPP charge_point_id. Returns None when the
                charger is not currently connected.
            poll_interval_seconds: Override for the polling cadence. Tests
                use a small value to keep wall time low.
        """
        self.timescale_client = timescale_client
        self.cp_lookup = cp_lookup
        self.logger = get_logger(__name__)
        self.poll_interval = poll_interval_seconds or self.POLL_INTERVAL_SECONDS

        self._running = False
        self._wake_event = asyncio.Event()
        self._tasks: List[asyncio.Task] = []
        self._listen_conn = None  # asyncpg connection held for LISTEN
        self._offline_charge_points: Dict[str, float] = {}

    async def start(self) -> None:
        """Spawn the consumer + LISTEN + depth-sampler tasks."""
        if self._running:
            return
        self._running = True
        self._wake_event = asyncio.Event()
        self._tasks = [
            asyncio.create_task(self._consume_loop(), name="queue_consumer"),
            asyncio.create_task(self._depth_sampler(), name="queue_depth_sampler"),
        ]
        # LISTEN is best-effort — polling alone is sufficient for correctness.
        listen_task = asyncio.create_task(self._listen_loop(), name="queue_listen")
        listen_task.add_done_callback(self._handle_listen_task_done)
        self._tasks.append(listen_task)
        self.logger.info("ChargingCommandQueueConsumer started (poll=%.1fs)", self.poll_interval)

    def _handle_listen_task_done(self, task: asyncio.Task) -> None:
        """Log LISTEN task crashes so polling-only fallback is explicit."""
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception as callback_exc:
            self.logger.warning("Could not inspect LISTEN task state: %s", callback_exc)
            return
        if exc is not None:
            self.logger.warning("LISTEN task crashed; polling-only mode active: %s", exc)

    async def stop(self) -> None:
        """Cancel all background tasks and release the LISTEN connection."""
        self._running = False
        self._wake_event.set()

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

        if self._listen_conn is not None:
            try:
                await self._listen_conn.close()
            except Exception:
                pass
            self._listen_conn = None
        self.logger.info("ChargingCommandQueueConsumer stopped")

    async def drain_once(self) -> int:
        """Drain one batch. Returns rows attempted against connected chargers.

        Public so the boot path or the admin endpoint can force an
        immediate sweep without waiting for the next poll tick. Offline
        chargers are left ``pending`` and are not counted.
        """
        now = time.monotonic()
        offline_cp_ids = [
            cp_id
            for cp_id, last_seen in self._offline_charge_points.items()
            if now - last_seen < self.EXCLUDED_OFFLINE_RETRY_SECONDS
        ]
        # Drop stale entries so this map stays bounded and we periodically
        # retry stations that may have reconnected without a DB NOTIFY.
        self._offline_charge_points = {
            cp_id: last_seen
            for cp_id, last_seen in self._offline_charge_points.items()
            if now - last_seen < self.EXCLUDED_OFFLINE_RETRY_SECONDS
        }
        try:
            rows = await self.timescale_client.fetch_pending_commands_all(
                limit=200, exclude_charge_point_ids=offline_cp_ids
            )
        except Exception as exc:
            self.logger.error("fetch_pending_commands_all failed: %s", exc)
            return 0

        processed = 0
        for row in rows:
            try:
                if await self._handle_row(row):
                    processed += 1
            except Exception as exc:
                # _handle_row should not raise, but a defensive log keeps a
                # single bad row from killing the whole batch.
                self.logger.error(
                    "handle_row raised for queue_id=%s: %s",
                    row.get("queue_id"),
                    exc,
                )
        return processed

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    async def _consume_loop(self) -> None:
        last_expiry_scan = 0.0
        while self._running:
            try:
                await self.drain_once()

                # Periodically promote expired pending rows so they don't
                # accumulate indefinitely if a charger never reconnects.
                now = time.monotonic()
                if now - last_expiry_scan >= self.EXPIRY_SCAN_INTERVAL_SECONDS:
                    try:
                        n = await self.timescale_client.expire_overdue_commands()
                        if n:
                            self.logger.info("Expired %d overdue queue row(s)", n)
                    except Exception as exc:
                        self.logger.warning("expire_overdue_commands failed: %s", exc)
                    last_expiry_scan = now

                # Wait for either a NOTIFY wakeup or the polling timeout.
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
                self._wake_event.clear()

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.error("consume_loop iteration failed: %s", exc)
                await asyncio.sleep(self.poll_interval)

    async def _listen_loop(self) -> None:
        """Hold an asyncpg LISTEN connection so NOTIFYs wake the consumer.

        Drops back to polling-only on any error — correctness does not
        depend on this path, only latency.

        Uses a dedicated connection (not a pool slot). Parking on a pool
        slot for the lifetime of the process shrinks the pool's effective
        working capacity by one; a dedicated connection costs the same
        cluster slot but keeps the pool's ``max_size`` honest.
        """
        config = getattr(self.timescale_client, "config", None)
        if config is None:
            self.logger.warning("No timescale config exposed; LISTEN disabled")
            return

        while self._running:
            try:
                conn = await open_dedicated_connection(config)
                self._listen_conn = conn
                await conn.add_listener(self.NOTIFY_CHANNEL, self._on_notify)
                self.logger.info("Listening on PostgreSQL channel '%s'", self.NOTIFY_CHANNEL)
                while self._running:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.warning("LISTEN connection failed; will retry in 5s: %s", exc)
                await asyncio.sleep(5)
            finally:
                if self._listen_conn is not None:
                    try:
                        await self._listen_conn.remove_listener(
                            self.NOTIFY_CHANNEL, self._on_notify
                        )
                    except Exception:
                        pass
                    try:
                        await self._listen_conn.close()
                    except Exception:
                        pass
                    self._listen_conn = None

    def _on_notify(self, _conn, _pid, _channel, payload) -> None:
        """asyncpg listener callback. Wakes the consumer immediately."""
        # Payload format from migration trigger: "<queue_id>:<charge_point_id>".
        # Only clear the single station so other offline exclusions remain.
        if payload:
            try:
                _, cp_id = str(payload).split(":", 1)
                cp_id = cp_id.strip()
                if cp_id:
                    self._offline_charge_points.pop(cp_id, None)
            except ValueError:
                self._offline_charge_points.clear()
        else:
            self._offline_charge_points.clear()
        self._wake_event.set()

    async def _depth_sampler(self) -> None:
        """Publish ``charging_command_queue_depth`` every N seconds."""
        while self._running:
            try:
                counts = await self.timescale_client.queue_depth_by_status()
            except Exception as exc:
                self.logger.debug("queue_depth_by_status failed: %s", exc)
                counts = {}

            # Re-publish all known statuses so a status that drops to 0
            # actually shows 0 instead of a stale value.
            for status in ("pending", "sent", "acked", "failed", "expired"):
                CHARGING_COMMAND_QUEUE_DEPTH.labels(status=status).set(counts.get(status, 0))

            await asyncio.sleep(self.DEPTH_SAMPLE_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # Row handling
    # ------------------------------------------------------------------

    async def _handle_row(self, row: Dict[str, Any]) -> bool:
        """Push a single queued command to its charger.

        Outcomes:
          * No connected session → leave row ``pending`` (BootNotification
            replay will pick it up). Returns ``False``.
          * send_charging_profile returns True → mark ``sent``.
          * send_charging_profile returns False or raises → mark ``failed``.

        Returns:
            ``True`` if a connected charger was attempted, ``False`` if the
            row stayed pending because the charger is offline.
        """
        queue_id = int(row["queue_id"])
        cp_id = row["charge_point_id"]
        command_type = row.get("command_type", "set_charging_profile")
        connector_id = int(row["connector_id"])
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)

        cp = self.cp_lookup(cp_id)
        if cp is None:
            self._offline_charge_points[cp_id] = time.monotonic()
            return False

        if command_type == "remote_reset":
            return await self._handle_remote_reset(queue_id, cp_id, cp, payload)

        if command_type == "remote_start_transaction":
            return await self._handle_remote_start_transaction(
                queue_id, cp_id, cp, connector_id, payload
            )

        if command_type == "get_diagnostics":
            return await self._handle_get_diagnostics(queue_id, cp_id, cp, payload)

        if command_type != "set_charging_profile":
            self.logger.warning(
                "Unknown command_type=%s for queue_id=%s cp=%s — marking failed",
                command_type,
                queue_id,
                cp_id,
            )
            try:
                await self.timescale_client.mark_command_failed(
                    queue_id, f"unknown command_type: {command_type}"
                )
            except Exception as mark_exc:
                self.logger.error(
                    "mark_command_failed raised for queue_id=%s: %s",
                    queue_id,
                    mark_exc,
                )
            return True

        # OCPP 1.6 (OCPP16Session) and OCPP 2.0.1 (EnhancedOCPPChargePoint)
        # both expose ``send_charging_profile(evse_id, payload)``. The
        # OCPP16Session variant accepts an ``allow_enqueue`` kwarg — pass
        # False so a transient failure during a consumer cycle does not
        # re-enqueue the row we are already trying to drain.
        send = cp.send_charging_profile
        accepts_allow_enqueue = self._accepts_allow_enqueue(send)
        # OCPP16Session.send_charging_profile already records PROFILE_PUSH_LATENCY
        # with sent/rejected/raised outcomes; avoid double-observing here.
        should_record_latency = not accepts_allow_enqueue

        start = time.perf_counter()
        outcome = "failed"
        try:
            if accepts_allow_enqueue:
                ok = await send(connector_id, payload, allow_enqueue=False)
            else:
                ok = await send(connector_id, payload)
            outcome = "sent" if ok else "failed"
        except Exception as exc:
            self.logger.warning(
                "send_charging_profile raised for cp=%s queue_id=%s: %s",
                cp_id,
                queue_id,
                exc,
            )
            # Race: cp_lookup can return a connected session but the station may
            # disconnect before push completes. Keep pending rows for boot replay.
            if accepts_allow_enqueue and self.cp_lookup(cp_id) is None:
                self._offline_charge_points[cp_id] = time.monotonic()
                if should_record_latency:
                    PROFILE_PUSH_LATENCY.labels(station_id=cp_id, outcome="failed").observe(
                        max(time.perf_counter() - start, 1e-6)
                    )
                return False
            try:
                await self.timescale_client.mark_command_failed(queue_id, str(exc))
            except Exception as mark_exc:
                self.logger.error(
                    "mark_command_failed raised for queue_id=%s: %s",
                    queue_id,
                    mark_exc,
                )
            if should_record_latency:
                PROFILE_PUSH_LATENCY.labels(station_id=cp_id, outcome="failed").observe(
                    max(time.perf_counter() - start, 1e-6)
                )
            return True

        latency = max(time.perf_counter() - start, 1e-6)
        if should_record_latency:
            PROFILE_PUSH_LATENCY.labels(station_id=cp_id, outcome=outcome).observe(latency)

        try:
            if outcome == "sent":
                await self.timescale_client.mark_command_sent(queue_id)
            else:
                await self.timescale_client.mark_command_failed(
                    queue_id, "charger Rejected SetChargingProfile"
                )
        except Exception as exc:
            self.logger.error(
                "Failed to update queue row queue_id=%s outcome=%s: %s",
                queue_id,
                outcome,
                exc,
            )
        return True

    async def _handle_remote_reset(
        self,
        queue_id: int,
        cp_id: str,
        cp: Any,
        payload: Dict[str, Any],
    ) -> bool:
        """Push an OCPP RemoteReset to a connected charger.

        Reset is irreversible at the device, so the row goes terminal
        (``sent`` or ``failed``) on the first attempt regardless of the
        charger's response. The boot-replay path skips ``remote_reset``
        rows so a Rejected reset doesn't get retried on every reconnect.
        """
        send = getattr(cp, "send_reset", None)
        if send is None:
            self.logger.warning("cp_id=%s session has no send_reset; marking failed", cp_id)
            try:
                await self.timescale_client.mark_command_failed(
                    queue_id, "session does not support send_reset"
                )
            except Exception as mark_exc:
                self.logger.error(
                    "mark_command_failed raised for queue_id=%s: %s",
                    queue_id,
                    mark_exc,
                )
            return True

        reset_type = (payload or {}).get("type", "Soft")
        try:
            ok = await send(reset_type)
        except Exception as exc:
            self.logger.warning(
                "send_reset raised for cp=%s queue_id=%s: %s",
                cp_id,
                queue_id,
                exc,
            )
            try:
                await self.timescale_client.mark_command_failed(queue_id, str(exc))
            except Exception as mark_exc:
                self.logger.error(
                    "mark_command_failed raised for queue_id=%s: %s",
                    queue_id,
                    mark_exc,
                )
            return True

        try:
            if ok:
                await self.timescale_client.mark_command_sent(queue_id)
            else:
                await self.timescale_client.mark_command_failed(queue_id, "charger Rejected Reset")
        except Exception as exc:
            self.logger.error(
                "Failed to update queue row queue_id=%s outcome=%s: %s",
                queue_id,
                "sent" if ok else "failed",
                exc,
            )
        return True

    async def _handle_get_diagnostics(
        self,
        queue_id: int,
        cp_id: str,
        cp: Any,
        payload: Dict[str, Any],
    ) -> bool:
        """Push an OCPP ``GetDiagnostics`` to a connected charger.

        Enqueued by ``POST /admin/depots/.../fetch_logs`` together with a
        row in ``charger_log_imports``. The charger asynchronously
        uploads the diagnostic archive to the signed URL in
        ``payload['location']``; the API service receives it on
        ``POST /internal/charger_logs/upload`` and flips the import row
        to ``status='received'``.

        Like ``_handle_remote_reset``, the row goes terminal on the first
        attempt: a Rejected GetDiagnostics shouldn't be retried on every
        BootNotification because the upload URL expires.
        """
        send = getattr(cp, "get_diagnostics", None)
        if send is None:
            self.logger.warning(
                "cp_id=%s session has no get_diagnostics; marking failed",
                cp_id,
            )
            try:
                await self.timescale_client.mark_command_failed(
                    queue_id, "session does not support get_diagnostics"
                )
            except Exception as mark_exc:
                self.logger.error(
                    "mark_command_failed raised for queue_id=%s: %s",
                    queue_id,
                    mark_exc,
                )
            await self._mark_import_failed(
                payload, "session does not support get_diagnostics"
            )
            return True

        location = (payload or {}).get("location")
        if not location:
            self.logger.warning(
                "get_diagnostics queue_id=%s cp=%s missing location — marking failed",
                queue_id,
                cp_id,
            )
            try:
                await self.timescale_client.mark_command_failed(
                    queue_id, "payload missing location"
                )
            except Exception as mark_exc:
                self.logger.error(
                    "mark_command_failed raised for queue_id=%s: %s",
                    queue_id,
                    mark_exc,
                )
            await self._mark_import_failed(payload, "payload missing location")
            return True

        kwargs: Dict[str, Any] = {"location": location}
        for src_key, dst_key in (
            ("retries", "retries"),
            ("retry_interval", "retry_interval"),
            ("start_time", "start_time"),
            ("stop_time", "stop_time"),
        ):
            if (payload or {}).get(src_key) is not None:
                kwargs[dst_key] = payload[src_key]

        try:
            filename = await send(**kwargs)
        except Exception as exc:
            self.logger.warning(
                "get_diagnostics raised for cp=%s queue_id=%s: %s",
                cp_id,
                queue_id,
                exc,
            )
            try:
                await self.timescale_client.mark_command_failed(queue_id, str(exc))
            except Exception as mark_exc:
                self.logger.error(
                    "mark_command_failed raised for queue_id=%s: %s",
                    queue_id,
                    mark_exc,
                )
            await self._mark_import_failed(payload, str(exc))
            return True

        # ``filename is None`` means the charger Rejected or the call
        # raised inside FleetChargePoint.get_diagnostics (it swallows
        # exceptions and returns None). Treat both as failed.
        if filename is None:
            try:
                await self.timescale_client.mark_command_failed(
                    queue_id, "charger Rejected GetDiagnostics or returned no filename"
                )
            except Exception as mark_exc:
                self.logger.error(
                    "mark_command_failed raised for queue_id=%s: %s",
                    queue_id,
                    mark_exc,
                )
            await self._mark_import_failed(payload, "charger Rejected GetDiagnostics")
            return True

        try:
            await self.timescale_client.mark_command_sent(queue_id)
        except Exception as exc:
            self.logger.error(
                "Failed to mark queue row sent queue_id=%s: %s", queue_id, exc
            )

        await self._mark_import_uploading(payload, filename)
        return True

    async def _mark_import_uploading(
        self, payload: Dict[str, Any], filename: str
    ) -> None:
        """Flip ``charger_log_imports`` row to ``uploading`` + record filename.

        Best-effort: a DB failure here doesn't change OCPP delivery
        state (the queue row is already ``sent``). The upload endpoint
        will still flip status to ``received`` when the file arrives.
        """
        import_id = (payload or {}).get("import_id")
        if not import_id:
            return
        pool = getattr(self.timescale_client, "_pool", None) or getattr(
            self.timescale_client, "pool", None
        )
        if pool is None:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE charger_log_imports
                       SET status = 'uploading',
                           file_name = COALESCE(file_name, $2)
                     WHERE id = $1
                       AND status = 'requested'
                    """,
                    import_id,
                    filename,
                )
        except Exception as exc:
            self.logger.warning(
                "Could not mark charger_log_imports=%s uploading: %s",
                import_id,
                exc,
            )

    async def _mark_import_failed(self, payload: Dict[str, Any], reason: str) -> None:
        """Flip ``charger_log_imports`` row to ``failed``. Best-effort."""
        import_id = (payload or {}).get("import_id")
        if not import_id:
            return
        pool = getattr(self.timescale_client, "_pool", None) or getattr(
            self.timescale_client, "pool", None
        )
        if pool is None:
            return
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE charger_log_imports
                       SET status = 'failed',
                           error_message = $2
                     WHERE id = $1
                       AND status IN ('requested', 'uploading')
                    """,
                    import_id,
                    reason[:500],  # error_message can be long; clip
                )
        except Exception as exc:
            self.logger.warning(
                "Could not mark charger_log_imports=%s failed: %s",
                import_id,
                exc,
            )

    async def _handle_remote_start_transaction(
        self,
        queue_id: int,
        cp_id: str,
        cp: Any,
        connector_id: int,
        payload: Dict[str, Any],
    ) -> bool:
        """Push an OCPP 1.6 RemoteStartTransaction with an operator-minted id_tag.

        Enqueued by ``POST /admin/.../manual_authorize`` together with a row
        in ``operator_authorization_overrides``. The synthetic id_tag in
        ``payload`` is consumed exactly once by ``RFIDAuthorizationService``
        when the charger sends Authorize/StartTransaction back to us.

        Like ``_handle_remote_reset``, the row goes terminal on the first
        attempt: a Rejected RemoteStart shouldn't be retried on every
        BootNotification because the override may have already expired.
        """
        send = getattr(cp, "send_remote_start_transaction", None)
        if send is None:
            self.logger.warning(
                "cp_id=%s session has no send_remote_start_transaction; marking failed",
                cp_id,
            )
            try:
                await self.timescale_client.mark_command_failed(
                    queue_id, "session does not support send_remote_start_transaction"
                )
            except Exception as mark_exc:
                self.logger.error(
                    "mark_command_failed raised for queue_id=%s: %s",
                    queue_id,
                    mark_exc,
                )
            return True

        id_tag = (payload or {}).get("id_tag") or (payload or {}).get("idTag")
        if not id_tag:
            self.logger.warning(
                "remote_start_transaction queue_id=%s cp=%s missing id_tag in payload — marking failed",
                queue_id,
                cp_id,
            )
            try:
                await self.timescale_client.mark_command_failed(queue_id, "payload missing id_tag")
            except Exception as mark_exc:
                self.logger.error(
                    "mark_command_failed raised for queue_id=%s: %s",
                    queue_id,
                    mark_exc,
                )
            return True

        try:
            ok = await send(connector_id, id_tag)
        except Exception as exc:
            self.logger.warning(
                "send_remote_start_transaction raised for cp=%s queue_id=%s: %s",
                cp_id,
                queue_id,
                exc,
            )
            try:
                await self.timescale_client.mark_command_failed(queue_id, str(exc))
            except Exception as mark_exc:
                self.logger.error(
                    "mark_command_failed raised for queue_id=%s: %s",
                    queue_id,
                    mark_exc,
                )
            return True

        try:
            if ok:
                await self.timescale_client.mark_command_sent(queue_id)
            else:
                await self.timescale_client.mark_command_failed(
                    queue_id, "charger Rejected RemoteStartTransaction"
                )
        except Exception as exc:
            self.logger.error(
                "Failed to update queue row queue_id=%s outcome=%s: %s",
                queue_id,
                "sent" if ok else "failed",
                exc,
            )
        return True

    @staticmethod
    def _accepts_allow_enqueue(fn: Callable[..., Awaitable[bool]]) -> bool:
        """Best-effort detection of OCPP16Session.send_charging_profile."""
        try:
            import inspect

            sig = inspect.signature(fn)
            return "allow_enqueue" in sig.parameters
        except (TypeError, ValueError):
            return False
