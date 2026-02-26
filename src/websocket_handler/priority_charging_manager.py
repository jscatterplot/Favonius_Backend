"""Priority Charging Manager for V2G operations."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .monitoring import get_logger
from .timescale_client import TimescaleClient


class PriorityChargingStatus(Enum):
    """Priority charging status."""

    INACTIVE = "Inactive"
    ACTIVE = "Active"
    SUSPENDED = "Suspended"
    EXPIRED = "Expired"


@dataclass
class PriorityChargingProfile:
    """Priority charging profile."""

    profile_id: int
    station_id: str
    evse_id: int
    transaction_id: str
    priority_level: int
    max_power_kw: float
    min_power_kw: float
    target_soc_percent: Optional[float] = None
    deadline: Optional[datetime] = None
    status: PriorityChargingStatus = PriorityChargingStatus.INACTIVE
    created_at: datetime = None
    activated_at: Optional[datetime] = None
    deactivated_at: Optional[datetime] = None


class PriorityChargingManager:
    """Manages priority charging profiles and their activation."""

    def __init__(self, timescale_client: TimescaleClient):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)

        # Active priority profiles per station
        self.active_profiles: Dict[str, Dict[int, PriorityChargingProfile]] = {}

        # Priority charging limits
        self.max_priority_profiles_per_station = 5
        self.max_priority_profiles_per_evse = 2

    async def use_priority_charging(
        self, station_id: str, transaction_id: str, activate: bool, **kwargs
    ) -> Dict[str, Any]:
        """Handle UsePriorityCharging request."""
        try:
            self.logger.info(
                f"Priority charging {'activation' if activate else 'deactivation'} for station {station_id}, transaction {transaction_id}"
            )

            if activate:
                return await self._activate_priority_charging(station_id, transaction_id, **kwargs)
            else:
                return await self._deactivate_priority_charging(
                    station_id, transaction_id, **kwargs
                )

        except Exception as e:
            self.logger.error(f"Error handling priority charging: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def notify_priority_charging(
        self, station_id: str, transaction_id: str, activated: bool, **kwargs
    ) -> Dict[str, Any]:
        """Handle NotifyPriorityCharging request."""
        try:
            self.logger.info(
                f"Priority charging notification: {'activated' if activated else 'deactivated'} for station {station_id}, transaction {transaction_id}"
            )

            # Update priority charging status
            await self._update_priority_charging_status(station_id, transaction_id, activated)

            # Store notification
            await self._store_priority_charging_notification(
                station_id, transaction_id, activated, **kwargs
            )

            return {"status": "Accepted", "statusInfo": {"reasonCode": "NoError"}}

        except Exception as e:
            self.logger.error(f"Error handling priority charging notification: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def _activate_priority_charging(
        self, station_id: str, transaction_id: str, **kwargs
    ) -> Dict[str, Any]:
        """Activate priority charging for a transaction."""
        try:
            # Get transaction details
            transaction_data = await self._get_transaction_data(station_id, transaction_id)
            if not transaction_data:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "UnknownTransaction",
                        "additionalInfo": f"Transaction {transaction_id} not found",
                    },
                }

            evse_id = transaction_data.get("evse_id", 1)

            # Check if priority charging is already active
            if self._is_priority_charging_active(station_id, evse_id):
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "PriorityChargingAlreadyActive",
                        "additionalInfo": "Priority charging is already active for this EVSE",
                    },
                }

            # Check limits
            if not await self._check_priority_charging_limits(station_id, evse_id):
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "PriorityChargingLimitExceeded",
                        "additionalInfo": "Maximum priority charging profiles exceeded",
                    },
                }

            # Create priority charging profile
            priority_profile = await self._create_priority_profile(
                station_id, evse_id, transaction_id, **kwargs
            )

            # Activate profile
            await self._activate_profile(priority_profile)

            # Store in database
            await self._store_priority_profile(priority_profile)

            return {
                "status": "Accepted",
                "statusInfo": {"reasonCode": "NoError"},
                "priorityProfile": {
                    "profileId": priority_profile.profile_id,
                    "priorityLevel": priority_profile.priority_level,
                    "maxPowerKw": priority_profile.max_power_kw,
                    "targetSocPercent": priority_profile.target_soc_percent,
                    "deadline": (
                        priority_profile.deadline.isoformat() if priority_profile.deadline else None
                    ),
                },
            }

        except Exception as e:
            self.logger.error(f"Error activating priority charging: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def _deactivate_priority_charging(
        self, station_id: str, transaction_id: str, **kwargs
    ) -> Dict[str, Any]:
        """Deactivate priority charging for a transaction."""
        try:
            # Find active priority profile
            priority_profile = await self._find_active_priority_profile(station_id, transaction_id)
            if not priority_profile:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "PriorityChargingNotActive",
                        "additionalInfo": f"No active priority charging found for transaction {transaction_id}",
                    },
                }

            # Deactivate profile
            await self._deactivate_profile(priority_profile)

            # Update database
            await self._update_priority_profile_status(
                priority_profile, PriorityChargingStatus.INACTIVE
            )

            return {"status": "Accepted", "statusInfo": {"reasonCode": "NoError"}}

        except Exception as e:
            self.logger.error(f"Error deactivating priority charging: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def _create_priority_profile(
        self, station_id: str, evse_id: int, transaction_id: str, **kwargs
    ) -> PriorityChargingProfile:
        """Create a priority charging profile."""
        profile_id = int(datetime.now().timestamp())

        # Extract parameters from kwargs
        priority_level = kwargs.get("priorityLevel", 1)
        max_power_kw = kwargs.get("maxPowerKw", 22.0)
        min_power_kw = kwargs.get("minPowerKw", 3.7)
        target_soc_percent = kwargs.get("targetSocPercent")
        deadline_str = kwargs.get("deadline")

        deadline = None
        if deadline_str:
            deadline = datetime.fromisoformat(deadline_str.replace("Z", "+00:00"))

        return PriorityChargingProfile(
            profile_id=profile_id,
            station_id=station_id,
            evse_id=evse_id,
            transaction_id=transaction_id,
            priority_level=priority_level,
            max_power_kw=max_power_kw,
            min_power_kw=min_power_kw,
            target_soc_percent=target_soc_percent,
            deadline=deadline,
            status=PriorityChargingStatus.INACTIVE,
            created_at=datetime.now(timezone.utc),
        )

    async def _activate_profile(self, profile: PriorityChargingProfile) -> None:
        """Activate a priority charging profile."""
        profile.status = PriorityChargingStatus.ACTIVE
        profile.activated_at = datetime.now(timezone.utc)

        # Add to active profiles
        if profile.station_id not in self.active_profiles:
            self.active_profiles[profile.station_id] = {}

        self.active_profiles[profile.station_id][profile.evse_id] = profile

        self.logger.info(
            f"Activated priority charging profile {profile.profile_id} for station {profile.station_id}, EVSE {profile.evse_id}"
        )

    async def _deactivate_profile(self, profile: PriorityChargingProfile) -> None:
        """Deactivate a priority charging profile."""
        profile.status = PriorityChargingStatus.INACTIVE
        profile.deactivated_at = datetime.now(timezone.utc)

        # Remove from active profiles
        if profile.station_id in self.active_profiles:
            self.active_profiles[profile.station_id].pop(profile.evse_id, None)

        self.logger.info(
            f"Deactivated priority charging profile {profile.profile_id} for station {profile.station_id}, EVSE {profile.evse_id}"
        )

    def _is_priority_charging_active(self, station_id: str, evse_id: int) -> bool:
        """Check if priority charging is active for an EVSE."""
        if station_id not in self.active_profiles:
            return False

        return evse_id in self.active_profiles[station_id]

    async def _check_priority_charging_limits(self, station_id: str, evse_id: int) -> bool:
        """Check priority charging limits."""
        # Check station limit
        station_count = len(self.active_profiles.get(station_id, {}))
        if station_count >= self.max_priority_profiles_per_station:
            return False

        # Check EVSE limit
        evse_count = sum(
            1
            for profile in self.active_profiles.get(station_id, {}).values()
            if profile.evse_id == evse_id
        )
        if evse_count >= self.max_priority_profiles_per_evse:
            return False

        return True

    async def _get_transaction_data(
        self, station_id: str, transaction_id: str
    ) -> Optional[Dict[str, Any]]:
        """Get transaction data from database."""
        try:
            # This would query the transaction events table
            # For now, return mock data
            return {
                "transaction_id": transaction_id,
                "station_id": station_id,
                "evse_id": 1,
                "connector_id": 1,
                "charging_state": "Charging",
            }
        except Exception as e:
            self.logger.error(f"Error getting transaction data: {e}")
            return None

    async def _find_active_priority_profile(
        self, station_id: str, transaction_id: str
    ) -> Optional[PriorityChargingProfile]:
        """Find active priority profile for a transaction."""
        if station_id not in self.active_profiles:
            return None

        for profile in self.active_profiles[station_id].values():
            if (
                profile.transaction_id == transaction_id
                and profile.status == PriorityChargingStatus.ACTIVE
            ):
                return profile

        return None

    async def _store_priority_profile(self, profile: PriorityChargingProfile) -> None:
        """Store priority profile in database."""
        try:
            profile_data = {
                "profile_id": profile.profile_id,
                "station_id": profile.station_id,
                "evse_id": profile.evse_id,
                "transaction_id": profile.transaction_id,
                "priority_level": profile.priority_level,
                "max_power_kw": profile.max_power_kw,
                "min_power_kw": profile.min_power_kw,
                "target_soc_percent": profile.target_soc_percent,
                "deadline": profile.deadline,
                "status": profile.status.value,
                "created_at": profile.created_at,
                "activated_at": profile.activated_at,
                "deactivated_at": profile.deactivated_at,
            }

            await self.timescale_client.store_priority_charging_profile(profile_data)

        except Exception as e:
            self.logger.error(f"Error storing priority profile: {e}")

    async def _update_priority_profile_status(
        self, profile: PriorityChargingProfile, status: PriorityChargingStatus
    ) -> None:
        """Update priority profile status in database."""
        try:
            await self.timescale_client.update_priority_charging_profile_status(
                profile.profile_id, status.value, profile.deactivated_at
            )
        except Exception as e:
            self.logger.error(f"Error updating priority profile status: {e}")

    async def _update_priority_charging_status(
        self, station_id: str, transaction_id: str, activated: bool
    ) -> None:
        """Update priority charging status."""
        try:
            profile = await self._find_active_priority_profile(station_id, transaction_id)
            if profile:
                if activated:
                    profile.status = PriorityChargingStatus.ACTIVE
                    profile.activated_at = datetime.now(timezone.utc)
                else:
                    profile.status = PriorityChargingStatus.INACTIVE
                    profile.deactivated_at = datetime.now(timezone.utc)

                await self._update_priority_profile_status(profile, profile.status)

        except Exception as e:
            self.logger.error(f"Error updating priority charging status: {e}")

    async def _store_priority_charging_notification(
        self, station_id: str, transaction_id: str, activated: bool, **kwargs
    ) -> None:
        """Store priority charging notification."""
        try:
            notification_data = {
                "station_id": station_id,
                "transaction_id": transaction_id,
                "activated": activated,
                "timestamp": datetime.now(timezone.utc),
                "additional_data": json.dumps(kwargs),
            }

            await self.timescale_client.store_priority_charging_notification(notification_data)

        except Exception as e:
            self.logger.error(f"Error storing priority charging notification: {e}")

    async def get_active_priority_profiles(self, station_id: str) -> List[PriorityChargingProfile]:
        """Get all active priority profiles for a station."""
        if station_id not in self.active_profiles:
            return []

        return list(self.active_profiles[station_id].values())

    async def cleanup_expired_profiles(self) -> None:
        """Clean up expired priority profiles."""
        try:
            current_time = datetime.now(timezone.utc)

            for station_id, profiles in self.active_profiles.items():
                expired_profiles = []

                for evse_id, profile in profiles.items():
                    if profile.deadline and current_time > profile.deadline:
                        expired_profiles.append(evse_id)

                # Deactivate expired profiles
                for evse_id in expired_profiles:
                    profile = profiles[evse_id]
                    await self._deactivate_profile(profile)
                    await self._update_priority_profile_status(
                        profile, PriorityChargingStatus.EXPIRED
                    )

                    self.logger.info(
                        f"Expired priority charging profile {profile.profile_id} for station {station_id}, EVSE {evse_id}"
                    )

        except Exception as e:
            self.logger.error(f"Error cleaning up expired profiles: {e}")

    async def get_priority_charging_summary(self, station_id: str) -> Dict[str, Any]:
        """Get priority charging summary for a station."""
        try:
            active_profiles = await self.get_active_priority_profiles(station_id)

            summary = {
                "station_id": station_id,
                "active_profiles_count": len(active_profiles),
                "max_profiles_per_station": self.max_priority_profiles_per_station,
                "max_profiles_per_evse": self.max_priority_profiles_per_evse,
                "active_profiles": [],
            }

            for profile in active_profiles:
                summary["active_profiles"].append(
                    {
                        "profile_id": profile.profile_id,
                        "evse_id": profile.evse_id,
                        "transaction_id": profile.transaction_id,
                        "priority_level": profile.priority_level,
                        "max_power_kw": profile.max_power_kw,
                        "target_soc_percent": profile.target_soc_percent,
                        "deadline": profile.deadline.isoformat() if profile.deadline else None,
                        "activated_at": (
                            profile.activated_at.isoformat() if profile.activated_at else None
                        ),
                    }
                )

            return summary

        except Exception as e:
            self.logger.error(f"Error getting priority charging summary: {e}")
            return {"station_id": station_id, "error": str(e)}
