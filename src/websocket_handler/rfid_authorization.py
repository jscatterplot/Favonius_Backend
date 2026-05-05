"""Shared RFID/idTag authorization for legacy websocket handler paths.

This module centralizes fleet-card validation so OCPP 1.6 and OCPP 2.0.1
handlers enforce the same fail-closed semantics.
"""

from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from .monitoring import RFID_AUTH_ATTEMPTS_TOTAL


class RFIDAuthStatus(str, Enum):
    """Internal status taxonomy for card-based authorization."""

    ACCEPTED = "accepted"
    INVALID = "invalid"
    EXPIRED = "expired"
    BLOCKED = "blocked"
    CONCURRENT_TX = "concurrent_tx"


def map_auth_status_to_ocpp201(status: RFIDAuthStatus) -> str:
    """Map internal RFID auth outcomes to OCPP 2.0.1 idTokenInfo.status."""
    if status == RFIDAuthStatus.ACCEPTED:
        return "Accepted"
    if status == RFIDAuthStatus.EXPIRED:
        return "Expired"
    if status in {RFIDAuthStatus.BLOCKED, RFIDAuthStatus.CONCURRENT_TX}:
        return "Blocked"
    return "Invalid"


@dataclass(frozen=True)
class RFIDAuthDecision:
    """Decision payload consumed by protocol-specific handlers."""

    status: RFIDAuthStatus
    source: str
    reason: str
    vehicle_id: Optional[str] = None
    driver_id: Optional[str] = None
    card_id: Optional[str] = None
    depot_id: Optional[str] = None


class RFIDAuthorizationService:
    """Resolve and validate an RFID/idTag against fleet records."""

    def __init__(self, timescale_client: Any, logger: Any) -> None:
        self._timescale = timescale_client
        self._logger = logger
        self._invalid_attempts: dict[tuple[str, str], list[float]] = {}
        self._throttled_until: dict[tuple[str, str], float] = {}

    async def _call_client_method(self, method_name: str, *args: Any) -> Any:
        method = getattr(self._timescale, method_name, None)
        if method is None:
            return None
        try:
            result = method(*args)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception as exc:
            self._logger.warning("rfid_authorization_%s_failed: %s", method_name, exc)
        return None

    async def _is_throttled(self, station_id: str, id_tag: str) -> bool:
        durable_count = await self._call_client_method(
            "count_recent_invalid_rfid_attempts",
            station_id,
            id_tag,
            60,
        )
        if isinstance(durable_count, int) and durable_count >= 8:
            return True

        key = (station_id, id_tag)
        expires_at = self._throttled_until.get(key)
        now = time.time()
        return bool(expires_at and expires_at > now)

    async def _record_invalid_attempt(self, station_id: str, id_tag: str) -> None:
        await self._call_client_method("record_invalid_rfid_attempt", station_id, id_tag)
        key = (station_id, id_tag)
        now = time.time()
        window_s = 60.0
        attempts = [ts for ts in self._invalid_attempts.get(key, []) if (now - ts) <= window_s]
        attempts.append(now)
        self._invalid_attempts[key] = attempts
        if len(attempts) >= 8:
            self._throttled_until[key] = now + 120.0

    async def authorize(self, station_id: str, id_tag: str, source: str) -> RFIDAuthDecision:
        """Authorize an idTag using canonical DB-backed fleet lookup."""
        if not id_tag or not id_tag.strip():
            RFID_AUTH_ATTEMPTS_TOTAL.labels(source=source, outcome=RFIDAuthStatus.INVALID.value).inc()
            return RFIDAuthDecision(
                status=RFIDAuthStatus.INVALID,
                source=source,
                reason="missing_id_tag",
            )

        if await self._is_throttled(station_id, id_tag):
            RFID_AUTH_ATTEMPTS_TOTAL.labels(source=source, outcome=RFIDAuthStatus.BLOCKED.value).inc()
            return RFIDAuthDecision(
                status=RFIDAuthStatus.BLOCKED,
                source=source,
                reason="too_many_invalid_attempts",
            )

        try:
            row = await self._timescale.lookup_id_tag(id_tag, station_id=station_id)
        except Exception as exc:
            RFID_AUTH_ATTEMPTS_TOTAL.labels(source=source, outcome=RFIDAuthStatus.INVALID.value).inc()
            self._logger.error(
                "rfid_authorize_error source=%s station_id=%s id_tag=%s error=%s",
                source,
                station_id,
                id_tag,
                exc,
            )
            return RFIDAuthDecision(
                status=RFIDAuthStatus.INVALID,
                source=source,
                reason="lookup_error",
            )

        if not row:
            await self._record_invalid_attempt(station_id, id_tag)
            RFID_AUTH_ATTEMPTS_TOTAL.labels(source=source, outcome=RFIDAuthStatus.INVALID.value).inc()
            self._logger.info(
                "rfid_authorize_denied source=%s station_id=%s id_tag=%s status=%s reason=%s",
                source,
                station_id,
                id_tag,
                RFIDAuthStatus.INVALID.value,
                "unknown_id_tag",
            )
            return RFIDAuthDecision(
                status=RFIDAuthStatus.INVALID,
                source=source,
                reason="unknown_id_tag",
            )

        decision = RFIDAuthDecision(
            status=RFIDAuthStatus.ACCEPTED,
            source=source,
            reason="identity_matched",
            vehicle_id=row.get("vehicle_id"),
            driver_id=row.get("driver_id"),
            card_id=row.get("card_id"),
            depot_id=row.get("depot_id"),
        )
        self._invalid_attempts.pop((station_id, id_tag), None)
        self._throttled_until.pop((station_id, id_tag), None)
        await self._call_client_method("clear_invalid_rfid_attempts", station_id, id_tag)
        RFID_AUTH_ATTEMPTS_TOTAL.labels(source=source, outcome=RFIDAuthStatus.ACCEPTED.value).inc()
        self._logger.info(
            "rfid_authorize_accepted source=%s station_id=%s id_tag=%s identity_source=%s vehicle_id=%s card_id=%s",
            source,
            station_id,
            id_tag,
            str(row.get("source") or "unknown"),
            decision.vehicle_id,
            decision.card_id,
        )
        return decision
