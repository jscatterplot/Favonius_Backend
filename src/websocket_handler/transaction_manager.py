"""OCPP 2.0.1 Transaction Manager with authorization caching and tariff calculations."""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from .monitoring import get_logger
from .rfid_authorization import (
    RFIDAuthStatus,
    RFIDAuthorizationService,
    map_auth_status_to_ocpp201,
)
from .timescale_client import TimescaleClient


class TransactionEventType(Enum):
    """Transaction event types."""

    STARTED = "Started"
    UPDATED = "Updated"
    ENDED = "Ended"


class ChargingState(Enum):
    """Charging states."""

    CHARGING = "Charging"
    EV_CONNECTED = "EVConnected"
    SUSPENDED_EV = "SuspendedEV"
    SUSPENDED_EVSE = "SuspendedEVSE"
    IDLE = "Idle"


class IdTokenType(Enum):
    """ID token types."""

    CENTRAL = "Central"
    eMAID = "eMAID"
    ISO14443 = "ISO14443"
    ISO15693 = "ISO15693"
    KEY_CODE = "KeyCode"
    LOCAL = "Local"
    NO_AUTHORIZATION = "NoAuthorization"
    OTHER = "Other"


@dataclass
class IdToken:
    """ID token."""

    id_token: str
    type: IdTokenType
    additional_info: Optional[Dict[str, Any]] = None


@dataclass
class IdTokenInfo:
    """ID token information."""

    status: str
    cache_timeout: Optional[int] = None
    charging_priority: Optional[int] = None
    language1: Optional[str] = None
    language2: Optional[str] = None
    group_id_token: Optional[IdToken] = None
    personal_message: Optional[Dict[str, Any]] = None


@dataclass
class TransactionInfo:
    """Transaction information."""

    transaction_id: str
    charging_state: Optional[ChargingState] = None
    time_spent_charging: Optional[int] = None
    stopped_reason: Optional[str] = None
    remote_start_id: Optional[int] = None
    evse_id: Optional[int] = None
    connector_id: Optional[int] = None


@dataclass
class TariffElement:
    """Tariff element."""

    price_component: List[Dict[str, Any]]
    restrictions: Optional[Dict[str, Any]] = None


@dataclass
class Tariff:
    """Tariff information."""

    tariff_id: str
    currency: str
    tariff_element: List[TariffElement]
    start_date_time: Optional[str] = None
    end_date_time: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None


class TransactionManager:
    """Manages OCPP transactions with authorization and tariff support."""

    def __init__(
        self,
        timescale_client: TimescaleClient,
        rfid_authorization: Optional[RFIDAuthorizationService] = None,
    ):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        self.rfid_authorization = rfid_authorization or RFIDAuthorizationService(
            timescale_client, self.logger
        )

        # Active transactions cache
        self.active_transactions: Dict[str, TransactionInfo] = {}

        # Per-EVSE locks serialise concurrent RequestStartTransaction calls so
        # the active-transaction check-and-set is atomic. Without this, two
        # concurrent starts for the same EVSE both pass the "is it free?"
        # check (the gap is the await on rfid_authorization.authorize) and
        # the second silently overwrites the first.
        self._evse_locks: Dict[str, asyncio.Lock] = {}

        # Tariff cache
        self.tariff_cache: Dict[str, Tariff] = {}

        # Transaction costs cache
        self.transaction_costs: Dict[str, Dict[str, Any]] = {}

    def _evse_lock(self, station_id: str, evse_id: int) -> asyncio.Lock:
        """Return the per-EVSE Lock, lazily creating it on first use.

        ``dict.setdefault`` is atomic under the GIL so concurrent callers
        observe the same Lock instance — required for the check-and-set in
        ``request_start_transaction`` to serialise correctly.
        """
        key = f"{station_id}:{evse_id}"
        return self._evse_locks.setdefault(key, asyncio.Lock())

    async def request_start_transaction(
        self,
        station_id: str,
        evse_id: int,
        remote_start_id: Optional[int],
        id_token: IdToken,
        charging_profile: Optional[Dict[str, Any]] = None,
        evse_id_token: Optional[IdToken] = None,
    ) -> Dict[str, Any]:
        """Request start transaction."""
        try:
            validation_error = self._validate_id_token_shape(id_token)
            if validation_error is not None:
                return {
                    "status": "Rejected",
                    "statusInfo": validation_error,
                }

            profile_error = self._validate_request_start_profile(charging_profile)
            if profile_error is not None:
                return {
                    "status": "Rejected",
                    "statusInfo": profile_error,
                }

            cache_key = f"{station_id}:{evse_id}"
            # Hold the per-EVSE lock around the slot check, the auth roundtrip,
            # and the slot population so two concurrent starts can't both
            # observe an empty slot before either claims it.
            async with self._evse_lock(station_id, evse_id):
                if cache_key in self.active_transactions:
                    return {
                        "status": "Rejected",
                        "statusInfo": {
                            "reasonCode": "ConcurrentTx",
                            "additionalInfo": "EVSE already has an active transaction",
                        },
                        "idTokenInfo": {"status": "Blocked"},
                    }

                auth_decision = await self.rfid_authorization.authorize(
                    station_id,
                    id_token.id_token,
                    "RequestStartTransaction",
                )
                id_token_info = {
                    "status": map_auth_status_to_ocpp201(auth_decision.status),
                    "cacheTimeout": 300,
                }
                if auth_decision.status != RFIDAuthStatus.ACCEPTED:
                    return {
                        "status": "Rejected",
                        "statusInfo": {
                            "reasonCode": id_token_info["status"],
                            "additionalInfo": auth_decision.reason,
                        },
                        "idTokenInfo": id_token_info,
                    }

                # Check EVSE availability
                evse_available = await self._check_evse_availability(station_id, evse_id)
                if not evse_available:
                    return {
                        "status": "Rejected",
                        "statusInfo": {
                            "reasonCode": "EVSEUnavailable",
                            "additionalInfo": "EVSE is not available",
                        },
                    }

                # Generate transaction ID
                transaction_id = str(uuid.uuid4())

                # Create transaction info
                transaction_info = TransactionInfo(
                    transaction_id=transaction_id,
                    charging_state=ChargingState.EV_CONNECTED,
                    evse_id=evse_id,
                    connector_id=1,  # Default connector
                    remote_start_id=remote_start_id,
                )

                # Store transaction
                await self._store_transaction(station_id, transaction_info, id_token)

                # Cache active transaction (under the lock, atomic with the check above)
                self.active_transactions[cache_key] = transaction_info

            # Apply charging profile if provided
            if charging_profile:
                await self._apply_charging_profile(station_id, evse_id, charging_profile)

            self.logger.info(
                f"Started transaction {transaction_id} for {station_id}, EVSE {evse_id}"
            )

            return {
                "status": "Accepted",
                "transactionId": transaction_id,
                "idTokenInfo": id_token_info,
            }

        except Exception as e:
            self.logger.error(f"Error starting transaction: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    @staticmethod
    def _validate_id_token_shape(id_token: IdToken) -> Optional[Dict[str, str]]:
        """Validate OCPP token type/value before business lookup."""
        if not id_token.id_token or not id_token.id_token.strip():
            return {"reasonCode": "InvalidToken", "additionalInfo": "idToken is required"}
        if id_token.type == IdTokenType.NO_AUTHORIZATION:
            return {
                "reasonCode": "InvalidTokenType",
                "additionalInfo": "NoAuthorization is not valid for RFID start",
            }
        return None

    @staticmethod
    def _validate_request_start_profile(
        charging_profile: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, str]]:
        """Validate CitrineOS-critical RequestStartTransaction profile semantics."""
        if not charging_profile:
            return None

        purpose = charging_profile.get("chargingProfilePurpose") or charging_profile.get(
            "charging_profile_purpose"
        )
        if purpose and purpose != "TxProfile":
            return {
                "reasonCode": "InvalidChargingProfile",
                "additionalInfo": "RequestStartTransaction requires TxProfile purpose",
            }

        if "transactionId" in charging_profile or "transaction_id" in charging_profile:
            return {
                "reasonCode": "InvalidChargingProfile",
                "additionalInfo": "New RequestStartTransaction profiles must not include transactionId",
            }

        return None

    async def request_stop_transaction(
        self, station_id: str, transaction_id: str, reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """Request stop transaction."""
        try:
            # Find transaction
            transaction_info = await self._find_transaction(station_id, transaction_id)
            if not transaction_info:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "UnknownTransaction",
                        "additionalInfo": f"Transaction {transaction_id} not found",
                    },
                }

            # Calculate final cost
            await self._calculate_transaction_cost(station_id, transaction_info)

            # Update transaction
            transaction_info.charging_state = ChargingState.IDLE
            transaction_info.stopped_reason = reason or "Remote"

            # Store updated transaction
            await self._update_transaction(station_id, transaction_info)

            # Remove from active transactions
            cache_key = f"{station_id}:{transaction_info.evse_id}"
            self.active_transactions.pop(cache_key, None)

            self.logger.info(f"Stopped transaction {transaction_id} for {station_id}")

            return {"status": "Accepted", "idTokenInfo": {"status": "Accepted"}}

        except Exception as e:
            self.logger.error(f"Error stopping transaction: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def handle_transaction_event(
        self,
        station_id: str,
        event_type: TransactionEventType,
        timestamp: str,
        transaction_info: TransactionInfo,
        meter_value: Optional[List[Dict[str, Any]]] = None,
        evse_id: Optional[int] = None,
        connector_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Handle transaction event."""
        try:
            # Update transaction info
            transaction_info.evse_id = evse_id or transaction_info.evse_id
            transaction_info.connector_id = connector_id or transaction_info.connector_id

            # Store transaction event
            await self._store_transaction_event(
                station_id, event_type, timestamp, transaction_info, meter_value
            )

            # Update active transaction cache
            if event_type == TransactionEventType.STARTED:
                cache_key = f"{station_id}:{transaction_info.evse_id}"
                self.active_transactions[cache_key] = transaction_info
            elif event_type == TransactionEventType.ENDED:
                cache_key = f"{station_id}:{transaction_info.evse_id}"
                self.active_transactions.pop(cache_key, None)

            # Calculate costs if charging state changed
            if event_type in [TransactionEventType.UPDATED, TransactionEventType.ENDED]:
                await self._update_transaction_costs(station_id, transaction_info)

            self.logger.info(
                f"Processed {event_type.value} event for transaction {transaction_info.transaction_id}"
            )

            return {"status": "Accepted"}

        except Exception as e:
            self.logger.error(f"Error handling transaction event: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def get_transaction_status(
        self, station_id: str, transaction_id: str
    ) -> Optional[TransactionInfo]:
        """Get transaction status."""
        return await self._find_transaction(station_id, transaction_id)

    async def get_active_transactions(self, station_id: str) -> List[TransactionInfo]:
        """Get active transactions for station."""
        active_transactions = []
        for cache_key, transaction in self.active_transactions.items():
            if cache_key.startswith(f"{station_id}:"):
                active_transactions.append(transaction)
        return active_transactions

    async def set_tariff(self, station_id: str, tariff: Tariff) -> None:
        """Set tariff for station."""
        self.tariff_cache[station_id] = tariff
        await self.timescale_client.store_tariff(station_id, tariff)

    async def get_tariff(self, station_id: str) -> Optional[Tariff]:
        """Get tariff for station."""
        if station_id in self.tariff_cache:
            return self.tariff_cache[station_id]

        tariff_data = await self.timescale_client.get_tariff(station_id)
        if tariff_data:
            tariff = self._parse_tariff(tariff_data)
            self.tariff_cache[station_id] = tariff
            return tariff

        return None

    async def _check_evse_availability(self, station_id: str, evse_id: int) -> bool:
        """Check if EVSE is available."""
        try:
            evse_status = await self.timescale_client.get_evse_status(station_id, evse_id)
            return evse_status and evse_status.get("status") == "Available"
        except Exception:
            return False

    async def _store_transaction(
        self, station_id: str, transaction_info: TransactionInfo, id_token: IdToken
    ) -> None:
        """Store transaction in database."""
        await self.timescale_client.store_transaction(
            {
                "station_id": station_id,
                "transaction_id": transaction_info.transaction_id,
                "evse_id": transaction_info.evse_id,
                "connector_id": transaction_info.connector_id,
                "id_token": id_token.id_token,
                "id_token_type": id_token.type,
                "charging_state": (
                    transaction_info.charging_state.value
                    if transaction_info.charging_state
                    else None
                ),
                "remote_start_id": transaction_info.remote_start_id,
                "started_at": datetime.now(timezone.utc),
            }
        )

    async def _update_transaction(self, station_id: str, transaction_info: TransactionInfo) -> None:
        """Update transaction in database."""
        await self.timescale_client.update_transaction(
            {
                "station_id": station_id,
                "transaction_id": transaction_info.transaction_id,
                "charging_state": (
                    transaction_info.charging_state.value
                    if transaction_info.charging_state
                    else None
                ),
                "stopped_reason": transaction_info.stopped_reason,
                "time_spent_charging": transaction_info.time_spent_charging,
                "ended_at": datetime.now(timezone.utc),
            }
        )

    async def _find_transaction(
        self, station_id: str, transaction_id: str
    ) -> Optional[TransactionInfo]:
        """Find transaction by ID."""
        # Check active transactions first
        for cache_key, transaction in self.active_transactions.items():
            if (
                cache_key.startswith(f"{station_id}:")
                and transaction.transaction_id == transaction_id
            ):
                return transaction

        # Check database
        transaction_data = await self.timescale_client.get_transaction(station_id, transaction_id)
        if transaction_data:
            return self._parse_transaction_info(transaction_data)

        return None

    async def _store_transaction_event(
        self,
        station_id: str,
        event_type: TransactionEventType,
        timestamp: str,
        transaction_info: TransactionInfo,
        meter_value: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Store transaction event."""
        await self.timescale_client.store_transaction_event(
            {
                "station_id": station_id,
                "transaction_id": transaction_info.transaction_id,
                "event_type": event_type.value,
                "timestamp": timestamp,
                "charging_state": (
                    transaction_info.charging_state.value
                    if transaction_info.charging_state
                    else None
                ),
                "time_spent_charging": transaction_info.time_spent_charging,
                "stopped_reason": transaction_info.stopped_reason,
                "remote_start_id": transaction_info.remote_start_id,
                "evse_id": transaction_info.evse_id,
                "connector_id": transaction_info.connector_id,
                "meter_value": meter_value,
            }
        )

    async def _calculate_transaction_cost(
        self, station_id: str, transaction_info: TransactionInfo
    ) -> Dict[str, Any]:
        """Calculate transaction cost."""
        try:
            # Get tariff
            tariff = await self.get_tariff(station_id)
            if not tariff:
                return {"total_cost": 0.0, "currency": "USD"}

            # Get energy consumed
            energy_data = await self.timescale_client.get_transaction_energy(
                station_id, transaction_info.transaction_id
            )

            total_cost = 0.0
            cost_breakdown = []

            for element in tariff.tariff_element:
                for component in element.price_component:
                    price = component.get("price", 0.0)
                    step_size = component.get("stepSize", 1)

                    if component.get("type") == "Energy":
                        energy_consumed = energy_data.get("energy_kwh", 0.0)
                        cost = (energy_consumed / step_size) * price
                    elif component.get("type") == "Time":
                        time_spent = transaction_info.time_spent_charging or 0
                        cost = (time_spent / step_size) * price
                    else:
                        cost = price

                    total_cost += cost
                    cost_breakdown.append(
                        {"type": component.get("type"), "price": price, "amount": cost}
                    )

            return {
                "total_cost": round(total_cost, 2),
                "currency": tariff.currency,
                "breakdown": cost_breakdown,
            }

        except Exception as e:
            self.logger.error(f"Error calculating transaction cost: {e}")
            return {"total_cost": 0.0, "currency": "USD"}

    async def _update_transaction_costs(
        self, station_id: str, transaction_info: TransactionInfo
    ) -> None:
        """Update transaction costs."""
        cost_data = await self._calculate_transaction_cost(station_id, transaction_info)

        # Store cost update
        await self.timescale_client.store_transaction_cost(
            {
                "station_id": station_id,
                "transaction_id": transaction_info.transaction_id,
                "total_cost": cost_data["total_cost"],
                "currency": cost_data["currency"],
                "cost_breakdown": cost_data.get("breakdown", []),
                "calculated_at": datetime.now(timezone.utc),
            }
        )

        # Cache cost
        self.transaction_costs[transaction_info.transaction_id] = cost_data

    async def _apply_charging_profile(
        self, station_id: str, evse_id: int, charging_profile: Dict[str, Any]
    ) -> None:
        """Apply charging profile to EVSE."""
        # This would integrate with the charging profile manager
        # For now, just log the profile
        self.logger.info(f"Applied charging profile to {station_id}, EVSE {evse_id}")

    def _parse_transaction_info(self, transaction_data: Dict[str, Any]) -> TransactionInfo:
        """Parse transaction info from database."""
        return TransactionInfo(
            transaction_id=transaction_data["transaction_id"],
            charging_state=(
                ChargingState(transaction_data["charging_state"])
                if transaction_data.get("charging_state")
                else None
            ),
            time_spent_charging=transaction_data.get("time_spent_charging"),
            stopped_reason=transaction_data.get("stopped_reason"),
            remote_start_id=transaction_data.get("remote_start_id"),
            evse_id=transaction_data.get("evse_id"),
            connector_id=transaction_data.get("connector_id"),
        )

    def _parse_tariff(self, tariff_data: Dict[str, Any]) -> Tariff:
        """Parse tariff from database."""
        tariff_elements = []
        for element_data in tariff_data.get("tariff_element", []):
            tariff_elements.append(
                TariffElement(
                    price_component=element_data.get("price_component", []),
                    restrictions=element_data.get("restrictions"),
                )
            )

        return Tariff(
            tariff_id=tariff_data["tariff_id"],
            currency=tariff_data["currency"],
            tariff_element=tariff_elements,
            start_date_time=tariff_data.get("start_date_time"),
            end_date_time=tariff_data.get("end_date_time"),
            min_price=tariff_data.get("min_price"),
            max_price=tariff_data.get("max_price"),
        )
