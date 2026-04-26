"""OCPP 1.6 session adapter for the legacy WebSocket handler.

Routes chargers that negotiate the ``ocpp1.6`` subprotocol through the
correct ocpp.v16 library instead of the 2.0.1 handler.  This eliminates
the schema-validation log storm caused by passing 1.6 messages through
``EnhancedOCPPChargePoint`` (which uses ocpp.v201).

Each ``OCPP16Session`` wraps ``FleetChargePoint`` (adapters/ocpp/charge_point.py)
and wires its callbacks to:
  - ``timescale_client.insert_telemetry_batch``  — telemetry persistence
  - ``message_handler._push_to_main_api``        — real-time event push
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import datetime, timezone
from itertools import count
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ocpp.v16.enums import AuthorizationStatus

from src.adapters.ocpp.charge_point import FleetChargePoint

if TYPE_CHECKING:
    from .message_handler import MessageHandler
    from .timescale_client import TimescaleClient

logger = logging.getLogger(__name__)
_PROFILE_ID_FALLBACK_START = 9_000_000_000_000_000_000
_PROFILE_ID_FALLBACK_SPAN = 100_000_000


def _new_profile_id_fallback_counter() -> count:
    """Create a non-constant fallback chargingProfileId stream.

    The DB sequence is the durable source of truth. This fallback only runs
    during sequence outages, so seed it from process-start entropy instead of
    a fixed constant to avoid deterministic reuse after a crash/restart.
    """
    seed_offset = (time.time_ns() + secrets.randbelow(_PROFILE_ID_FALLBACK_SPAN)) % (
        _PROFILE_ID_FALLBACK_SPAN
    )
    return count(seed_offset)


_profile_id_fallback_counter = _new_profile_id_fallback_counter()


class OCPP16Session:
    """Manages a single OCPP 1.6 charger connection.

    Provides the same external interface as ``EnhancedOCPPChargePoint``
    (``start``, ``send_charging_profile``, ``send_der_control``,
    ``clear_der_control``) so ``OCPPWebSocketServer`` can store both types
    in the same ``charge_points`` dict without special-casing.
    """

    def __init__(
        self,
        station_id: str,
        websocket: Any,
        timescale_client: "TimescaleClient",
        message_handler: "MessageHandler",
    ) -> None:
        """Initialise the session and wire all FleetChargePoint callbacks."""
        self._station_id = station_id
        self._timescale = timescale_client
        self._message_handler = message_handler

        self._cp = FleetChargePoint(
            id=station_id,
            connection=websocket,
            on_boot=self._on_boot,
            on_meter_values=self._on_meter_values,
            on_status_change=self._on_status_change,
            on_transaction_start=self._on_transaction_start,
            on_transaction_stop=self._on_transaction_stop,
            on_authorize=self._on_authorize,
            tx_id_provider=self._next_transaction_id,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start processing messages from the charger (blocks until disconnect)."""
        await self._cp.start()

    # ------------------------------------------------------------------
    # Outgoing commands (matches EnhancedOCPPChargePoint's interface)
    # ------------------------------------------------------------------

    async def send_charging_profile(self, evse_id: int, charging_profile: Dict) -> bool:
        """Send SetChargingProfile to the charger using OCPP 1.6 semantics.

        ``evse_id`` is treated as OCPP 1.6 ``connector_id`` (equivalent concept).
        The ``charging_profile`` dict is expected to carry the standard OCPP
        fields; the schedule periods are forwarded verbatim.

        If the caller does not pass ``chargingProfileId``, we draw a unique
        id from the ``ocpp_charging_profile_id`` sequence so concurrent
        pushes do not stack-collide on the charger.
        """
        schedule_periods: List[Dict] = []
        cp_schedule = charging_profile.get("chargingSchedule", {})
        if cp_schedule:
            schedule_periods = cp_schedule.get("chargingSchedulePeriod", [])
        else:
            # Flat format: list of period dicts at top level
            schedule_periods = charging_profile.get("chargingSchedulePeriod", [])

        profile_id = charging_profile.get("chargingProfileId")
        if profile_id is None:
            try:
                profile_id = await self._timescale.next_charging_profile_id()
            except Exception as exc:
                profile_id = _PROFILE_ID_FALLBACK_START + (
                    next(_profile_id_fallback_counter) % _PROFILE_ID_FALLBACK_SPAN
                )
                logger.warning(
                    "next_charging_profile_id failed for station=%s; using local fallback id=%s: %s",
                    self._station_id,
                    profile_id,
                    exc,
                )

        charging_rate_unit = (cp_schedule or {}).get(
            "chargingRateUnit", charging_profile.get("chargingRateUnit", "W")
        )

        return await self._cp.set_charging_profile(
            connector_id=evse_id,
            charging_schedule=schedule_periods,
            profile_purpose=charging_profile.get(
                "chargingProfilePurpose", "TxDefaultProfile"
            ),
            profile_kind=charging_profile.get("chargingProfileKind", "Absolute"),
            charging_rate_unit=charging_rate_unit,
            stack_level=charging_profile.get("stackLevel", 0),
            profile_id=profile_id,
        )

    # ------------------------------------------------------------------
    # Database-backed providers wired into FleetChargePoint
    # ------------------------------------------------------------------

    async def _next_transaction_id(self) -> int:
        """Provide a restart-safe transactionId from the DB sequence."""
        return await self._timescale.next_transaction_id()

    async def _validate_id_tag(self, cp_id: str, id_tag: str, source: str) -> AuthorizationStatus:
        """Validate an OCPP idTag against ``vehicles.id_tag``.

        Returns ``Accepted`` for tags registered in the fleet, ``Invalid``
        for unknown tags. We deliberately do not use ``Blocked`` /
        ``Expired`` here — those require richer tag metadata which is out
        of scope for the pilot.
        """
        try:
            row = await self._timescale.lookup_id_tag(id_tag)
        except Exception as exc:
            logger.error(
                "%s lookup failed for station=%s id_tag=%s: %s",
                source,
                cp_id,
                id_tag,
                exc,
            )
            return AuthorizationStatus.invalid
        return AuthorizationStatus.accepted if row else AuthorizationStatus.invalid

    async def _on_authorize(self, cp_id: str, id_tag: str) -> AuthorizationStatus:
        """Handle Authorize by failing closed on unknown fleet idTags."""
        try:
            return await self._validate_id_tag(cp_id, id_tag, "Authorize")
        except Exception as exc:
            logger.error(
                "Authorize validation raised unexpectedly for station=%s id_tag=%s: %s",
                cp_id,
                id_tag,
                exc,
            )
            return AuthorizationStatus.invalid

    async def send_der_control(self, der_control: Dict) -> bool:  # noqa: ARG002
        """DER control is an OCPP 2.x feature; no-op for OCPP 1.6 chargers."""
        logger.debug("send_der_control called on OCPP 1.6 session — skipping")
        return False

    async def clear_der_control(self) -> bool:
        """DER control is an OCPP 2.x feature; no-op for OCPP 1.6 chargers."""
        logger.debug("clear_der_control called on OCPP 1.6 session — skipping")
        return False

    # ------------------------------------------------------------------
    # FleetChargePoint callbacks
    # ------------------------------------------------------------------

    async def _on_boot(
        self,
        cp_id: str,
        vendor: str,
        model: str,
        serial_number: Optional[str],
        firmware_version: Optional[str],
        **kwargs: Any,
    ) -> None:
        logger.info(
            "OCPP 1.6 boot: station=%s vendor=%s model=%s serial=%s fw=%s",
            cp_id,
            vendor,
            model,
            serial_number,
            firmware_version,
        )

    async def _on_meter_values(
        self,
        cp_id: str,
        connector_id: int,
        soc: float,  # 0.0–1.0 fraction from FleetChargePoint
        power_kw: float,
        energy_kwh: Optional[float],
        timestamp: datetime,
        transaction_id: Optional[int],
        max_charge_kw: Optional[float],
        raw_samples: list,  # noqa: ARG002
    ) -> None:
        """Write telemetry row and push a meter_values event to the main API."""
        # TimescaleClient expects soc_percent (0–100)
        soc_percent = soc * 100.0 if soc is not None else None

        try:
            await self._timescale.insert_telemetry_batch(
                [
                    {
                        "time": timestamp,
                        "station_id": cp_id,
                        "connector_id": connector_id,
                        "session_id": str(transaction_id) if transaction_id else None,
                        "power_kw": power_kw,
                        "energy_kwh": energy_kwh,
                        "soc_percent": soc_percent,
                        "max_charge_power_kw": max_charge_kw,
                    }
                ]
            )
        except Exception as exc:
            logger.warning("Telemetry write failed: station=%s error=%s", cp_id, exc)

        asyncio.create_task(
            self._message_handler._push_to_main_api(
                cp_id,
                "meter_values",
                {"soc_percent": soc_percent, "power_kw": power_kw},
            )
        )

    async def _on_status_change(
        self,
        cp_id: str,
        connector_id: int,
        status: str,
        error_code: str,  # noqa: ARG002
        timestamp: Optional[Any] = None,  # noqa: ARG002
        vendor_id: Optional[str] = None,  # noqa: ARG002
        vendor_error_code: Optional[str] = None,  # noqa: ARG002
    ) -> None:
        """Push a status_notification event to the main API."""
        asyncio.create_task(
            self._message_handler._push_to_main_api(
                cp_id,
                "status_notification",
                {"status": status, "evse_id": connector_id, "connector_id": connector_id},
            )
        )

    async def _on_transaction_start(
        self,
        cp_id: str,
        connector_id: int,
        id_tag: str,
        meter_start: int,
        timestamp: str,
    ) -> AuthorizationStatus:
        auth_status = await self._validate_id_tag(cp_id, id_tag, "StartTransaction")
        if auth_status != AuthorizationStatus.accepted:
            return auth_status

        asyncio.create_task(
            self._message_handler._push_to_main_api(
                cp_id,
                "transaction_start",
                {
                    "connector_id": connector_id,
                    "id_tag": id_tag,
                    "meter_start": meter_start,
                    "timestamp": timestamp,
                },
            )
        )
        return auth_status

    async def _on_transaction_stop(
        self,
        cp_id: str,
        transaction_id: int,
        id_tag: str,
        meter_stop: int,
        timestamp: str,
        reason: str,
    ) -> None:
        asyncio.create_task(
            self._message_handler._push_to_main_api(
                cp_id,
                "transaction_stop",
                {
                    "transaction_id": transaction_id,
                    "id_tag": id_tag,
                    "meter_stop": meter_stop,
                    "timestamp": timestamp,
                    "reason": reason,
                },
            )
        )
