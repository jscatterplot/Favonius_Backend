"""OCPP 1.6 Charge Point management for fleet optimization — full coverage.

Reference: Development plan Step 3.1, PRD_v2.md#9-1-ocpp-integration
Per PRD Section 8.4, max_charge_kw from OCPP MeterValues is extracted
and dynamically updated in the vehicles table.

Covers all 28 OCPP 1.6 CallActions, matching CitrineOS handler parity:
 - Incoming (CP → CSMS): BootNotification, Heartbeat, StatusNotification,
   MeterValues, StartTransaction, StopTransaction, Authorize, DataTransfer,
   DiagnosticsStatusNotification, FirmwareStatusNotification
 - Outgoing (CSMS → CP): SetChargingProfile, ClearChargingProfile,
   GetCompositeSchedule, RemoteStartTransaction, RemoteStopTransaction,
   Reset, ChangeAvailability, TriggerMessage, UnlockConnector,
   ChangeConfiguration, GetConfiguration, ClearCache, SendLocalList,
   GetLocalListVersion, ReserveNow, CancelReservation, UpdateFirmware,
   GetDiagnostics, DataTransfer
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from ocpp.routing import on
from ocpp.v16 import ChargePoint as CP16
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    AuthorizationStatus,
    RegistrationStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transaction ID Generator
# ---------------------------------------------------------------------------


class TransactionIdGenerator:
    """Thread-safe, monotonically increasing transaction ID generator.

    Uses a time-based seed so IDs are unique across server restarts.
    In production, back this with a database sequence.
    """

    def __init__(self) -> None:
        self._counter = int(time.time()) % 1_000_000_000

    def next_id(self) -> int:
        self._counter += 1
        return self._counter


_tx_id_gen = TransactionIdGenerator()


# ---------------------------------------------------------------------------
# OCPP 1.6 spec helpers
# ---------------------------------------------------------------------------


def _now_iso_z() -> str:
    """OCPP 1.6 timestamp: UTC, milliseconds, trailing Z.

    Some chargers (notably ABB Terra AC firmware ≤1.8.21) reject naive
    ISO-8601 strings. Always emit ``2026-04-26T12:34:56.789Z`` style.
    """
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# DataTransfer vendor allowlist. Vendor IDs outside this set get
# ``UnknownVendorId`` per OCPP 1.6-J Section 6.4. Adding a vendor here is
# how we acknowledge we know how to interpret their proprietary payloads.
_KNOWN_VENDORS: frozenset[str] = frozenset(
    {
        "FavoniusEnergy",
        "ABB",
        "Etrel",
    }
)


# Measurands safe to push to ABB Terra AC chargers via
# ChangeConfiguration("MeterValuesSampledData"). Pushing any measurand
# outside this set on firmware ≤1.8.21 puts the charger into a reboot loop.
_ABB_SAFE_MEASURANDS: frozenset[str] = frozenset(
    {
        "Energy.Active.Import.Register",
        "Current.Import",
        "Voltage",
        "Power.Active.Import",
        "Current.Offered",
    }
)

# OCPP 1.6 spec: SendLocalList max entries per ABB integration guide is 16.
_LOCAL_LIST_MAX_ENTRIES = 16


# ---------------------------------------------------------------------------
# FleetChargePoint
# ---------------------------------------------------------------------------


class FleetChargePoint(CP16):
    """Custom ChargePoint handler for fleet optimization.

    Handles *all* OCPP 1.6-J incoming messages from EV chargers and provides
    methods to send every outgoing command defined in the spec.

    Reference: PRD Section 9.1, Development plan Step 3.1
    """

    def __init__(
        self,
        id: str,
        connection,
        on_status_change: Optional[Callable] = None,
        on_meter_values: Optional[Callable] = None,
        on_boot: Optional[Callable] = None,
        on_transaction_start: Optional[Callable] = None,
        on_transaction_stop: Optional[Callable] = None,
        on_authorize: Optional[Callable] = None,
        on_diagnostics_status: Optional[Callable] = None,
        on_firmware_status: Optional[Callable] = None,
        on_data_transfer: Optional[Callable] = None,
        tx_id_provider: Optional[Callable[[], Awaitable[int]]] = None,
    ):
        """Initialize FleetChargePoint.

        Args:
            id: Charge point identifier (OCPP station ID)
            connection: WebSocket connection
            on_status_change: Callback for status changes
            on_meter_values: Callback for meter value updates
            on_boot: Callback for boot notifications
            on_transaction_start: Callback for transaction start
            on_transaction_stop: Callback for transaction stop
            on_authorize: Callback for authorization requests
            on_diagnostics_status: Callback for diagnostics status
            on_firmware_status: Callback for firmware status
            on_data_transfer: Callback for data transfer messages
            tx_id_provider: Async callable returning the next transactionId
                (back this with a DB sequence in production so IDs survive
                restarts). Falls back to an in-memory monotonic counter.
        """
        super().__init__(id, connection)
        self._cb_status_change = on_status_change
        self._cb_meter_values = on_meter_values
        self._cb_boot = on_boot
        self._cb_tx_start = on_transaction_start
        self._cb_tx_stop = on_transaction_stop
        self._cb_authorize = on_authorize
        self._cb_diagnostics = on_diagnostics_status
        self._cb_firmware = on_firmware_status
        self._cb_data_transfer = on_data_transfer
        self._tx_id_provider = tx_id_provider

        # Backward-compatible attribute names (used by OCPPServer)
        self.on_status_change_callback = on_status_change
        self.on_meter_values_callback = on_meter_values
        self.on_status_change = on_status_change

        # Transaction tracking — keyed by connector_id
        self.transactions: dict[int, int] = {}
        self.current_transaction_id: Optional[int] = None  # backward-compat

        # Boot info cache
        self.vendor: Optional[str] = None
        self.model: Optional[str] = None
        self.serial_number: Optional[str] = None
        self.firmware_version: Optional[str] = None

        # Connector state cache
        self.connector_status: dict[int, str] = {}

        logger.info(f"Initialized FleetChargePoint: {id}")

    # ===================================================================
    # Incoming message handlers (CP → CSMS)
    # ===================================================================

    @on("BootNotification")
    async def on_boot_notification(
        self, charge_point_vendor: str, charge_point_model: str, **kwargs
    ):
        """Handle BootNotification from charger.

        Stores station metadata. Optionally delegates accept/reject to callback.
        """
        self.vendor = charge_point_vendor
        self.model = charge_point_model
        self.serial_number = kwargs.get("charge_point_serial_number")
        self.firmware_version = kwargs.get("firmware_version")

        logger.info(
            f"BootNotification from {self.id}: {charge_point_vendor} "
            f"{charge_point_model} (serial={self.serial_number}, "
            f"fw={self.firmware_version})"
        )

        status = RegistrationStatus.accepted
        if self._cb_boot:
            try:
                result = await self._cb_boot(
                    self.id,
                    charge_point_vendor,
                    charge_point_model,
                    self.serial_number,
                    self.firmware_version,
                    **kwargs,
                )
                if result is not None:
                    status = result
            except Exception as e:
                logger.error(f"Error in boot callback: {e}")

        return call_result.BootNotification(
            current_time=_now_iso_z(),
            interval=300,
            status=status,
        )

    @on("Heartbeat")
    async def on_heartbeat(self, **kwargs):
        """Handle Heartbeat from charger. Returns current server time."""
        return call_result.Heartbeat(current_time=_now_iso_z())

    @on("StatusNotification")
    async def on_status_notification(
        self, connector_id: int, error_code: str, status: str, **kwargs
    ):
        """Handle StatusNotification from charger.

        Caches connector state and notifies via callback.
        """
        self.connector_status[connector_id] = status

        logger.debug(
            f"StatusNotification from {self.id}, connector {connector_id}: "
            f"{status} (error={error_code})"
        )

        if self._cb_status_change:
            try:
                await self._cb_status_change(
                    self.id,
                    connector_id,
                    status,
                    error_code,
                    kwargs.get("timestamp"),
                    kwargs.get("vendor_id"),
                    kwargs.get("vendor_error_code"),
                )
            except TypeError:
                # Backward-compat: old callbacks only accept (cp_id, connector, status)
                try:
                    await self._cb_status_change(self.id, connector_id, status)
                except Exception as e:
                    logger.error(f"Error in status change callback: {e}")
            except Exception as e:
                logger.error(f"Error in status change callback: {e}")

        return call_result.StatusNotification()

    @on("MeterValues")
    async def on_meter_values(self, connector_id: int, meter_value: list, **kwargs):
        """Handle MeterValues from charger — comprehensive parsing.

        Extracts all standard OCPP 1.6 measurands:
         - SoC (%)
         - Power.Active.Import (W/kW)
         - Energy.Active.Import.Register (Wh/kWh)
         - Power.Offered (max power charger is offering)
         - Current.Import, Voltage
         - Vendor-specific maxChargingRate

        Also captures transaction_id and context for proper correlation.
        """
        transaction_id = kwargs.get("transaction_id")

        soc: Optional[float] = None
        power_kw: Optional[float] = None
        energy_kwh: Optional[float] = None
        max_charge_kw: Optional[float] = None
        timestamp: Optional[datetime] = None
        raw_samples: list[dict] = []

        for mv in meter_value:
            ts_raw = mv.get("timestamp")
            if ts_raw:
                if isinstance(ts_raw, str):
                    try:
                        timestamp = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                    except ValueError:
                        logger.warning(f"Invalid timestamp from {self.id}: {ts_raw}")
                elif isinstance(ts_raw, datetime):
                    timestamp = ts_raw

            for sv in mv.get("sampledValue", mv.get("sampled_value", [])):
                measurand = sv.get("measurand", "Energy.Active.Import.Register")
                value_str = sv.get("value", "0")
                unit = sv.get("unit", "")
                context = sv.get("context", "")
                phase = sv.get("phase")

                try:
                    value = float(value_str)
                except (ValueError, TypeError):
                    logger.warning(f"Invalid meter value from {self.id}: {value_str}")
                    continue

                raw_samples.append(
                    {
                        "measurand": measurand,
                        "value": value,
                        "unit": unit,
                        "context": context,
                        "phase": phase,
                    }
                )

                if measurand == "SoC":
                    soc = value / 100.0

                elif measurand == "Power.Active.Import":
                    if unit in ("kW", "kw"):
                        power_kw = value
                    else:
                        power_kw = value / 1000.0

                elif measurand == "Energy.Active.Import.Register":
                    if unit in ("kWh", "kwh"):
                        energy_kwh = value
                    else:
                        energy_kwh = value / 1000.0

                elif measurand == "Power.Offered":
                    if unit in ("kW", "kw"):
                        max_charge_kw = value
                    else:
                        max_charge_kw = value / 1000.0

                elif measurand == "Current.Import":
                    pass

                elif measurand == "Voltage":
                    pass

                elif measurand in ("maxChargingRate", "MaxChargingRate"):
                    if max_charge_kw is None:
                        if unit in ("kW", "kw"):
                            max_charge_kw = value
                        else:
                            max_charge_kw = value / 1000.0

        if soc is not None or power_kw is not None or energy_kwh is not None:
            logger.debug(
                f"MeterValues from {self.id}, connector {connector_id}: "
                f"SoC={soc}, Power={power_kw}kW, Energy={energy_kwh}kWh"
                + (f", max_charge={max_charge_kw}kW" if max_charge_kw else "")
            )

            if self._cb_meter_values and timestamp:
                try:
                    await self._cb_meter_values(
                        self.id,
                        connector_id,
                        soc or 0.0,
                        power_kw or 0.0,
                        energy_kwh,
                        timestamp,
                        transaction_id,
                        max_charge_kw,
                        raw_samples,
                    )
                except TypeError:
                    # Backward-compat: old callback (cp, conn, soc, power, ts, max_kw)
                    try:
                        await self._cb_meter_values(
                            self.id,
                            connector_id,
                            soc or 0.0,
                            power_kw or 0.0,
                            timestamp,
                            max_charge_kw,
                        )
                    except Exception as e:
                        logger.error(f"Error in meter values callback: {e}")
                except Exception as e:
                    logger.error(f"Error in meter values callback: {e}")

        return call_result.MeterValues()

    @on("StartTransaction")
    async def on_start_transaction(
        self,
        connector_id: int,
        id_tag: str,
        meter_start: int,
        timestamp: str,
        **kwargs,
    ):
        """Handle StartTransaction from charger.

        Uses monotonic ID generator (no random collisions).
        Validates id_tag via callback if provided.
        """
        auth_status = AuthorizationStatus.accepted

        if self._cb_tx_start:
            try:
                result = await self._cb_tx_start(
                    self.id,
                    connector_id,
                    id_tag,
                    meter_start,
                    timestamp,
                )
                if result is not None:
                    auth_status = result
            except Exception as e:
                logger.error(f"Error in transaction start callback: {e}")

        if auth_status == AuthorizationStatus.accepted:
            if self._tx_id_provider is not None:
                try:
                    tx_id = await self._tx_id_provider()
                except Exception as e:
                    # Fall back to in-memory counter rather than rejecting a
                    # session that the charger has already physically begun.
                    logger.error(
                        f"tx_id_provider failed for {self.id}; "
                        f"falling back to in-memory counter: {e}"
                    )
                    tx_id = _tx_id_gen.next_id()
            else:
                tx_id = _tx_id_gen.next_id()
            self.transactions[connector_id] = tx_id
            self.current_transaction_id = tx_id
        else:
            tx_id = 0

        logger.info(
            f"StartTransaction from {self.id}, connector {connector_id}, "
            f"id_tag={id_tag}, tx_id={tx_id}, status={auth_status}"
        )

        return call_result.StartTransaction(
            transaction_id=tx_id,
            id_tag_info={"status": auth_status},
        )

    @on("StopTransaction")
    async def on_stop_transaction(
        self,
        transaction_id: int,
        meter_stop: int,
        timestamp: str,
        **kwargs,
    ):
        """Handle StopTransaction from charger.

        Cleans up transaction tracking and notifies callback with stop reason.
        """
        id_tag = kwargs.get("id_tag", "")
        reason = kwargs.get("reason", "Local")

        logger.info(
            f"StopTransaction from {self.id}, tx_id={transaction_id}, "
            f"reason={reason}, meter_stop={meter_stop}"
        )

        for conn_id, tid in list(self.transactions.items()):
            if tid == transaction_id:
                del self.transactions[conn_id]
                break
        if self.current_transaction_id == transaction_id:
            self.current_transaction_id = None

        if self._cb_tx_stop:
            try:
                await self._cb_tx_stop(
                    self.id,
                    transaction_id,
                    id_tag,
                    meter_stop,
                    timestamp,
                    reason,
                )
            except Exception as e:
                logger.error(f"Error in transaction stop callback: {e}")

        return call_result.StopTransaction(id_tag_info={"status": AuthorizationStatus.accepted})

    @on("Authorize")
    async def on_authorize_request(self, id_tag: str, **kwargs):
        """Handle Authorize from charger.

        For fleet ops, default-accept all tags. Callback can override.
        """
        auth_status = AuthorizationStatus.accepted

        if self._cb_authorize:
            try:
                result = await self._cb_authorize(self.id, id_tag)
                if result is not None:
                    auth_status = result
            except Exception as e:
                logger.error(f"Error in authorize callback: {e}")

        logger.info(f"Authorize from {self.id}: id_tag={id_tag}, status={auth_status}")
        return call_result.Authorize(id_tag_info={"status": auth_status})

    @on("DataTransfer")
    async def on_data_transfer_request(self, vendor_id: str, **kwargs):
        """Handle DataTransfer from charger — vendor-specific messages.

        Returns ``UnknownVendorId`` when ``vendor_id`` is not in the
        allowlist (OCPP 1.6-J Section 6.4). The callback can override the
        decision when a vendor's payload should be processed even when not
        listed.
        """
        message_id = kwargs.get("message_id", "")
        data = kwargs.get("data", "")

        logger.info(
            f"DataTransfer from {self.id}: vendor={vendor_id}, "
            f"msg_id={message_id}, data={data!r}"
        )

        status = "Accepted" if vendor_id in _KNOWN_VENDORS else "UnknownVendorId"
        response_data = None

        if self._cb_data_transfer:
            try:
                result = await self._cb_data_transfer(
                    self.id,
                    vendor_id,
                    message_id,
                    data,
                )
                if result is not None:
                    status, response_data = result
            except Exception as e:
                logger.error(f"Error in data transfer callback: {e}")

        return call_result.DataTransfer(status=status, data=response_data)

    @on("DiagnosticsStatusNotification")
    async def on_diagnostics_status_notification(self, status: str, **kwargs):
        """Handle DiagnosticsStatusNotification from charger."""
        logger.info(f"DiagnosticsStatus from {self.id}: {status}")
        if self._cb_diagnostics:
            try:
                await self._cb_diagnostics(self.id, status)
            except Exception as e:
                logger.error(f"Error in diagnostics callback: {e}")
        return call_result.DiagnosticsStatusNotification()

    @on("FirmwareStatusNotification")
    async def on_firmware_status_notification(self, status: str, **kwargs):
        """Handle FirmwareStatusNotification from charger."""
        logger.info(f"FirmwareStatus from {self.id}: {status}")
        if self._cb_firmware:
            try:
                await self._cb_firmware(self.id, status)
            except Exception as e:
                logger.error(f"Error in firmware callback: {e}")
        return call_result.FirmwareStatusNotification()

    # ===================================================================
    # Outgoing commands (CSMS → Charge Point)
    # ===================================================================

    async def set_charging_profile(
        self,
        connector_id: int,
        charging_schedule: list[dict],
        profile_purpose: str = "TxProfile",
        profile_kind: str = "Absolute",
        charging_rate_unit: str = "W",
        stack_level: int = 0,
        profile_id: int = 1,
        valid_from: Optional[str] = None,
        valid_to: Optional[str] = None,
        recurrency_kind: Optional[str] = None,
        max_retries: int = 3,
    ) -> bool:
        """Send SetChargingProfile to charger with retry logic.

        Supports all profile purposes (ChargePointMaxProfile, TxDefaultProfile,
        TxProfile), all kinds (Absolute, Recurring, Relative), and both rate
        units (W, A).

        Args:
            connector_id: Connector identifier (0 = whole station)
            charging_schedule: List of ChargingSchedulePeriod dicts
            profile_purpose: 'ChargePointMaxProfile' | 'TxDefaultProfile' | 'TxProfile'
            profile_kind: 'Absolute' | 'Recurring' | 'Relative'
            charging_rate_unit: 'W' | 'A'
            stack_level: Priority (0 = lowest). Higher overrides lower.
            profile_id: Unique profile identifier
            valid_from: ISO 8601 start of validity (optional)
            valid_to: ISO 8601 end of validity (optional)
            recurrency_kind: 'Daily' | 'Weekly' (required if kind='Recurring')
            max_retries: Maximum retry attempts

        Returns:
            True if accepted, False otherwise
        """
        profile_dict: dict[str, Any] = {
            "charging_profile_id": profile_id,
            "stack_level": stack_level,
            "charging_profile_purpose": profile_purpose,
            "charging_profile_kind": profile_kind,
            "charging_schedule": {
                "charging_rate_unit": charging_rate_unit,
                "charging_schedule_period": charging_schedule,
            },
        }
        if valid_from:
            profile_dict["valid_from"] = valid_from
        if valid_to:
            profile_dict["valid_to"] = valid_to
        if recurrency_kind:
            profile_dict["recurrency_kind"] = recurrency_kind

        for attempt in range(max_retries):
            try:
                payload = call.SetChargingProfile(
                    connector_id=connector_id,
                    cs_charging_profiles=profile_dict,
                )
                response = await self.call(payload)
                accepted = response.status == "Accepted"

                if accepted:
                    logger.info(
                        f"SetChargingProfile to {self.id}, connector {connector_id}: "
                        f"Accepted ({profile_purpose}, attempt {attempt + 1})"
                    )
                    return True
                else:
                    logger.warning(
                        f"SetChargingProfile to {self.id}: {response.status} "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.0)

            except Exception as e:
                logger.error(
                    f"Error SetChargingProfile to {self.id} "
                    f"(attempt {attempt + 1}/{max_retries}): {e}"
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0)

        return False

    async def clear_charging_profile(
        self,
        profile_id: Optional[int] = None,
        connector_id: Optional[int] = None,
        charging_profile_purpose: Optional[str] = None,
        stack_level: Optional[int] = None,
    ) -> str:
        """Send ClearChargingProfile to remove profiles from charger.

        Args:
            profile_id: Specific profile ID to clear (None = all matching)
            connector_id: Clear profiles on this connector (None = all)
            charging_profile_purpose: Filter by purpose
            stack_level: Filter by stack level

        Returns:
            'Accepted' or 'Unknown'
        """
        kwargs: dict[str, Any] = {}
        if profile_id is not None:
            kwargs["id"] = profile_id
        if connector_id is not None:
            kwargs["connector_id"] = connector_id
        if charging_profile_purpose is not None:
            kwargs["charging_profile_purpose"] = charging_profile_purpose
        if stack_level is not None:
            kwargs["stack_level"] = stack_level

        try:
            payload = call.ClearChargingProfile(**kwargs)
            response = await self.call(payload)
            logger.info(f"ClearChargingProfile to {self.id}: {response.status}")
            return response.status
        except Exception as e:
            logger.error(f"Error ClearChargingProfile to {self.id}: {e}")
            return "Unknown"

    async def get_composite_schedule(
        self,
        connector_id: int,
        duration: int,
        charging_rate_unit: Optional[str] = None,
    ) -> Optional[dict]:
        """Send GetCompositeSchedule to query what the charger is executing.

        Args:
            connector_id: Connector to query
            duration: Duration in seconds to cover
            charging_rate_unit: 'W' or 'A' (optional)

        Returns:
            Composite schedule dict or None if rejected
        """
        kwargs: dict[str, Any] = {
            "connector_id": connector_id,
            "duration": duration,
        }
        if charging_rate_unit:
            kwargs["charging_rate_unit"] = charging_rate_unit

        try:
            payload = call.GetCompositeSchedule(**kwargs)
            response = await self.call(payload)
            if response.status == "Accepted":
                logger.info(f"GetCompositeSchedule from {self.id}: Accepted")
                return {
                    "status": response.status,
                    "connector_id": getattr(response, "connector_id", connector_id),
                    "schedule_start": getattr(response, "schedule_start", None),
                    "charging_schedule": getattr(response, "charging_schedule", None),
                }
            else:
                logger.warning(f"GetCompositeSchedule from {self.id}: {response.status}")
                return None
        except Exception as e:
            logger.error(f"Error GetCompositeSchedule to {self.id}: {e}")
            return None

    async def remote_start_transaction(
        self,
        connector_id: int,
        id_tag: str,
        charging_profile: Optional[dict] = None,
    ) -> bool:
        """Start charging session remotely with optional profile.

        Args:
            connector_id: Connector identifier
            id_tag: ID tag for authorization
            charging_profile: Optional charging profile to apply

        Returns:
            True if accepted, False otherwise
        """
        try:
            kwargs: dict[str, Any] = {
                "connector_id": connector_id,
                "id_tag": id_tag,
            }
            if charging_profile:
                kwargs["charging_profile"] = charging_profile

            payload = call.RemoteStartTransaction(**kwargs)
            response = await self.call(payload)
            accepted = response.status == "Accepted"
            logger.info(
                f"RemoteStartTransaction to {self.id}, connector {connector_id}: "
                f"{'Accepted' if accepted else 'Rejected'}"
            )
            return accepted
        except Exception as e:
            logger.error(f"Error RemoteStartTransaction to {self.id}: {e}")
            return False

    async def remote_stop_transaction(self, transaction_id: int) -> bool:
        """Stop charging session remotely."""
        try:
            payload = call.RemoteStopTransaction(transaction_id=transaction_id)
            response = await self.call(payload)
            accepted = response.status == "Accepted"
            logger.info(
                f"RemoteStopTransaction to {self.id}, tx={transaction_id}: "
                f"{'Accepted' if accepted else 'Rejected'}"
            )
            return accepted
        except Exception as e:
            logger.error(f"Error RemoteStopTransaction to {self.id}: {e}")
            return False

    async def reset(self, reset_type: str = "Soft") -> str:
        """Send Reset command. Returns 'Accepted' or 'Rejected'."""
        try:
            payload = call.Reset(type=reset_type)
            response = await self.call(payload)
            logger.info(f"Reset to {self.id} ({reset_type}): {response.status}")
            return response.status
        except Exception as e:
            logger.error(f"Error Reset to {self.id}: {e}")
            return "Rejected"

    async def change_availability(
        self,
        connector_id: int,
        availability_type: str = "Operative",
    ) -> str:
        """Change charger/connector availability.

        Returns 'Accepted', 'Rejected', or 'Scheduled'.
        """
        try:
            payload = call.ChangeAvailability(
                connector_id=connector_id,
                type=availability_type,
            )
            response = await self.call(payload)
            logger.info(
                f"ChangeAvailability to {self.id}, connector {connector_id} "
                f"-> {availability_type}: {response.status}"
            )
            return response.status
        except Exception as e:
            logger.error(f"Error ChangeAvailability to {self.id}: {e}")
            return "Rejected"

    async def trigger_message(
        self,
        requested_message: str,
        connector_id: Optional[int] = None,
    ) -> str:
        """Request charger to send a specific message on demand.

        requested_message: 'BootNotification' | 'Heartbeat' | 'MeterValues' |
            'StatusNotification' | 'DiagnosticsStatusNotification' |
            'FirmwareStatusNotification'

        Returns 'Accepted', 'Rejected', or 'NotImplemented'.
        """
        try:
            kwargs: dict[str, Any] = {"requested_message": requested_message}
            if connector_id is not None:
                kwargs["connector_id"] = connector_id
            payload = call.TriggerMessage(**kwargs)
            response = await self.call(payload)
            logger.info(f"TriggerMessage to {self.id} ({requested_message}): {response.status}")
            return response.status
        except Exception as e:
            logger.error(f"Error TriggerMessage to {self.id}: {e}")
            return "Rejected"

    async def unlock_connector(self, connector_id: int) -> str:
        """Unlock a connector. Returns 'Unlocked', 'UnlockFailed', or 'NotSupported'."""
        try:
            payload = call.UnlockConnector(connector_id=connector_id)
            response = await self.call(payload)
            logger.info(
                f"UnlockConnector to {self.id}, connector {connector_id}: {response.status}"
            )
            return response.status
        except Exception as e:
            logger.error(f"Error UnlockConnector to {self.id}: {e}")
            return "UnlockFailed"

    async def change_configuration(self, key: str, value: str) -> str:
        """Change a configuration key on the charger.

        Returns 'Accepted', 'Rejected', 'RebootRequired', or 'NotSupported'.

        Raises ValueError if the caller asks to push measurands outside the
        ABB-safe set on ``MeterValuesSampledData`` / ``MeterValuesAlignedData``.
        ABB Terra AC firmware ≤1.8.21 reboot-loops when the sampled-data list
        contains unsupported measurands.
        """
        if key in {"MeterValuesSampledData", "MeterValuesAlignedData"}:
            requested = {m.strip() for m in value.split(",") if m.strip()}
            unsupported = requested - _ABB_SAFE_MEASURANDS
            if unsupported:
                raise ValueError(
                    f"Refusing ChangeConfiguration({key}) to {self.id}: "
                    f"measurands outside ABB-safe set: {sorted(unsupported)}. "
                    f"Allowed: {sorted(_ABB_SAFE_MEASURANDS)}"
                )

        try:
            payload = call.ChangeConfiguration(key=key, value=value)
            response = await self.call(payload)
            logger.info(f"ChangeConfiguration to {self.id}: {key}={value} -> {response.status}")
            return response.status
        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Error ChangeConfiguration to {self.id}: {e}")
            return "Rejected"

    async def get_configuration(self, keys: Optional[list[str]] = None) -> dict:
        """Get configuration values from charger.

        Returns dict with 'configuration_key' and 'unknown_key' lists.
        """
        try:
            kwargs: dict[str, Any] = {}
            if keys:
                kwargs["key"] = keys
            payload = call.GetConfiguration(**kwargs)
            response = await self.call(payload)
            return {
                "configuration_key": getattr(response, "configuration_key", []) or [],
                "unknown_key": getattr(response, "unknown_key", []) or [],
            }
        except Exception as e:
            logger.error(f"Error GetConfiguration to {self.id}: {e}")
            return {"configuration_key": [], "unknown_key": []}

    async def clear_cache(self) -> str:
        """Clear authorization cache. Returns 'Accepted' or 'Rejected'."""
        try:
            payload = call.ClearCache()
            response = await self.call(payload)
            logger.info(f"ClearCache to {self.id}: {response.status}")
            return response.status
        except Exception as e:
            logger.error(f"Error ClearCache to {self.id}: {e}")
            return "Rejected"

    async def send_local_list(
        self,
        list_version: int,
        update_type: str = "Full",
        local_authorization_list: Optional[list[dict]] = None,
    ) -> str:
        """Send/update local authorization list.

        Returns 'Accepted', 'Failed', 'NotSupported', or 'VersionMismatch'.

        ABB Terra AC chargers cap LocalAuthList at 16 entries. If the caller
        passes more, we refuse the push and return ``NotSupported`` so the
        operator falls back to central authorization. Truncating silently
        would create a security gap (some idTags would never authorize).
        """
        if (
            local_authorization_list is not None
            and len(local_authorization_list) > _LOCAL_LIST_MAX_ENTRIES
        ):
            logger.warning(
                "SendLocalList to %s: %d entries exceeds %d-entry cap; "
                "refusing — caller should fall back to central Authorize.",
                self.id,
                len(local_authorization_list),
                _LOCAL_LIST_MAX_ENTRIES,
            )
            return "NotSupported"

        try:
            kwargs: dict[str, Any] = {
                "list_version": list_version,
                "update_type": update_type,
            }
            if local_authorization_list:
                kwargs["local_authorization_list"] = local_authorization_list
            payload = call.SendLocalList(**kwargs)
            response = await self.call(payload)
            logger.info(f"SendLocalList to {self.id}: {response.status}")
            return response.status
        except Exception as e:
            logger.error(f"Error SendLocalList to {self.id}: {e}")
            return "Failed"

    async def get_local_list_version(self) -> int:
        """Get current local list version. Returns version number (-1 on error)."""
        try:
            payload = call.GetLocalListVersion()
            response = await self.call(payload)
            return response.list_version
        except Exception as e:
            logger.error(f"Error GetLocalListVersion to {self.id}: {e}")
            return -1

    async def reserve_now(
        self,
        connector_id: int,
        expiry_date: str,
        id_tag: str,
        reservation_id: int,
        parent_id_tag: Optional[str] = None,
    ) -> str:
        """Reserve a connector.

        Returns 'Accepted', 'Faulted', 'Occupied', 'Rejected', or 'Unavailable'.
        """
        try:
            kwargs: dict[str, Any] = {
                "connector_id": connector_id,
                "expiry_date": expiry_date,
                "id_tag": id_tag,
                "reservation_id": reservation_id,
            }
            if parent_id_tag:
                kwargs["parent_id_tag"] = parent_id_tag
            payload = call.ReserveNow(**kwargs)
            response = await self.call(payload)
            logger.info(f"ReserveNow to {self.id}, connector {connector_id}: {response.status}")
            return response.status
        except Exception as e:
            logger.error(f"Error ReserveNow to {self.id}: {e}")
            return "Rejected"

    async def cancel_reservation(self, reservation_id: int) -> str:
        """Cancel a reservation. Returns 'Accepted' or 'Rejected'."""
        try:
            payload = call.CancelReservation(reservation_id=reservation_id)
            response = await self.call(payload)
            logger.info(f"CancelReservation to {self.id}: {response.status}")
            return response.status
        except Exception as e:
            logger.error(f"Error CancelReservation to {self.id}: {e}")
            return "Rejected"

    async def update_firmware(
        self,
        location: str,
        retrieve_date: str,
        retries: Optional[int] = None,
        retry_interval: Optional[int] = None,
    ) -> None:
        """Request charger to download and install firmware."""
        try:
            kwargs: dict[str, Any] = {
                "location": location,
                "retrieve_date": retrieve_date,
            }
            if retries is not None:
                kwargs["retries"] = retries
            if retry_interval is not None:
                kwargs["retry_interval"] = retry_interval
            payload = call.UpdateFirmware(**kwargs)
            await self.call(payload)
            logger.info(f"UpdateFirmware to {self.id}: {location}")
        except Exception as e:
            logger.error(f"Error UpdateFirmware to {self.id}: {e}")

    async def get_diagnostics(
        self,
        location: str,
        start_time: Optional[str] = None,
        stop_time: Optional[str] = None,
        retries: Optional[int] = None,
        retry_interval: Optional[int] = None,
    ) -> Optional[str]:
        """Request charger to upload diagnostics. Returns filename or None."""
        try:
            kwargs: dict[str, Any] = {"location": location}
            if start_time:
                kwargs["start_time"] = start_time
            if stop_time:
                kwargs["stop_time"] = stop_time
            if retries is not None:
                kwargs["retries"] = retries
            if retry_interval is not None:
                kwargs["retry_interval"] = retry_interval
            payload = call.GetDiagnostics(**kwargs)
            response = await self.call(payload)
            filename = getattr(response, "file_name", None)
            logger.info(f"GetDiagnostics from {self.id}: {filename}")
            return filename
        except Exception as e:
            logger.error(f"Error GetDiagnostics to {self.id}: {e}")
            return None

    async def data_transfer(
        self,
        vendor_id: str,
        message_id: Optional[str] = None,
        data: Optional[str] = None,
    ) -> tuple[str, Optional[str]]:
        """Send DataTransfer to charger (vendor-specific).

        Returns (status, response_data).
        """
        try:
            kwargs: dict[str, Any] = {"vendor_id": vendor_id}
            if message_id:
                kwargs["message_id"] = message_id
            if data:
                kwargs["data"] = data
            payload = call.DataTransfer(**kwargs)
            response = await self.call(payload)
            return response.status, getattr(response, "data", None)
        except Exception as e:
            logger.error(f"Error DataTransfer to {self.id}: {e}")
            return "Rejected", None


# ---------------------------------------------------------------------------
# Schedule conversion utility
# ---------------------------------------------------------------------------


def convert_schedule_to_ocpp_profile(
    schedule: list[tuple[int, float]],
    delta_t: float = 0.25,
    number_phases: int = 3,
    charging_rate_unit: str = "W",
) -> list[dict]:
    """Convert optimization schedule to OCPP charging profile format.

    Args:
        schedule: List of (timestep, power_kw) tuples from optimization
        delta_t: Time step duration in hours (default 0.25 = 15 minutes)
        number_phases: Number of phases for AC charging (default 3)
        charging_rate_unit: 'W' or 'A'

    Returns:
        List of ChargingSchedulePeriod dicts
    """
    periods = []
    for timestep, power_kw in schedule:
        start_period = int(timestep * delta_t * 3600)

        if charging_rate_unit == "A":
            voltage = 230.0
            limit = (power_kw * 1000.0) / (number_phases * voltage)
        else:
            limit = int(power_kw * 1000)

        periods.append(
            {
                "startPeriod": start_period,
                "limit": limit,
                "numberPhases": number_phases,
            }
        )
    return periods
