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

# Throttle thresholds: how many invalid attempts in what window trigger a block,
# and how long the block lasts. Single source of truth — duplicated previously
# between `_is_throttled` and `_record_invalid_attempt`.
INVALID_ATTEMPT_THRESHOLD = 8
INVALID_ATTEMPT_WINDOW_S = 60
THROTTLE_DURATION_S = 120
IN_MEMORY_STATE_RETENTION_S = INVALID_ATTEMPT_WINDOW_S + THROTTLE_DURATION_S

# Process-wide throttle / recovery state so every ``RFIDAuthorizationService``
# shares the same in-memory fallback when durable DB helpers are unavailable.
_invalid_attempts: dict[tuple[str, str], list[float]] = {}
_throttled_until: dict[tuple[str, str], float] = {}
_recovery_logged: set[tuple[str, str]] = set()
_pending_recovery_audit: set[tuple[str, str]] = set()
_missing_methods_logged: set[str] = set()


def _prune_in_memory_state(now: float) -> None:
    """Drop stale in-memory throttle state to keep memory bounded."""
    window_s = float(INVALID_ATTEMPT_WINDOW_S)
    active_keys: set[tuple[str, str]] = set()

    for key, timestamps in list(_invalid_attempts.items()):
        fresh_attempts = [ts for ts in timestamps if (now - ts) <= window_s]
        if fresh_attempts:
            _invalid_attempts[key] = fresh_attempts
            active_keys.add(key)
        else:
            _invalid_attempts.pop(key, None)

    for key, expires_at in list(_throttled_until.items()):
        if expires_at > now:
            active_keys.add(key)
            continue
        _throttled_until.pop(key, None)

    for key in list(_pending_recovery_audit):
        if key in active_keys:
            continue
        _pending_recovery_audit.discard(key)
        _recovery_logged.discard(key)

    _recovery_logged.clear()


class RFIDAuthStatus(str, Enum):
    """Internal status taxonomy for card-based authorization."""

    ACCEPTED = "accepted"
    INVALID = "invalid"
    EXPIRED = "expired"
    BLOCKED = "blocked"
    CONCURRENT_TX = "concurrent_tx"


_OCPP201_OUTCOME_STR: dict[RFIDAuthStatus, str] = {
    RFIDAuthStatus.ACCEPTED: "Accepted",
    RFIDAuthStatus.EXPIRED: "Expired",
    RFIDAuthStatus.BLOCKED: "Blocked",
    RFIDAuthStatus.CONCURRENT_TX: "ConcurrentTx",
    RFIDAuthStatus.INVALID: "Invalid",
}


def map_auth_status_to_ocpp201(status: RFIDAuthStatus) -> str:
    """Map internal RFID auth outcomes to OCPP 2.0.1 idTokenInfo.status."""
    return _OCPP201_OUTCOME_STR[status]


def user_message_for(decision: "RFIDAuthDecision") -> str:
    """Driver-facing message string for OCPP ``personal_message.content``.

    Differentiates between failure modes so a fleet operator (or driver looking
    at the charger UI) can tell "card expired" from "you've been temporarily
    blocked for too many bad reads" from "we don't recognise this card".
    """
    if decision.status == RFIDAuthStatus.ACCEPTED:
        return "Authorized"
    if decision.status == RFIDAuthStatus.EXPIRED:
        return "RFID card expired"
    if decision.status == RFIDAuthStatus.BLOCKED:
        return "RFID temporarily blocked — too many failed attempts"
    if decision.status == RFIDAuthStatus.CONCURRENT_TX:
        return "Charger already in use"
    return "Unknown RFID card"


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

    async def _call_client_method(self, method_name: str, *args: Any) -> Any:
        method = getattr(self._timescale, method_name, None)
        if method is None:
            if method_name not in _missing_methods_logged:
                self._logger.warning(
                    "rfid_authorization_method_unavailable method=%s "
                    "(abuse controls degraded — TimescaleClient missing this method)",
                    method_name,
                )
                _missing_methods_logged.add(method_name)
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
        now = time.time()
        _prune_in_memory_state(now)
        durable_count = await self._call_client_method(
            "count_recent_invalid_rfid_attempts",
            station_id,
            id_tag,
            INVALID_ATTEMPT_WINDOW_S,
        )
        if isinstance(durable_count, int) and durable_count >= INVALID_ATTEMPT_THRESHOLD:
            return True

        key = (station_id, id_tag)
        expires_at = _throttled_until.get(key)
        return bool(expires_at and expires_at > now)

    async def _record_invalid_attempt(self, station_id: str, id_tag: str) -> None:
        await self._call_client_method("record_invalid_rfid_attempt", station_id, id_tag)
        key = (station_id, id_tag)
        now = time.time()
        _prune_in_memory_state(now)
        window_s = float(INVALID_ATTEMPT_WINDOW_S)
        attempts = [ts for ts in _invalid_attempts.get(key, []) if (now - ts) <= window_s]
        attempts.append(now)
        _invalid_attempts[key] = attempts
        if len(attempts) >= INVALID_ATTEMPT_THRESHOLD:
            _throttled_until[key] = now + THROTTLE_DURATION_S
        # A new invalid resets the recovery marker — the next success on
        # this (station, tag) pair will write exactly one recovered event.
        _recovery_logged.discard(key)
        _pending_recovery_audit.add(key)

    async def authorize(self, station_id: str, id_tag: str, source: str) -> RFIDAuthDecision:
        """Authorize an idTag using canonical DB-backed fleet lookup."""
        if not id_tag or not id_tag.strip():
            RFID_AUTH_ATTEMPTS_TOTAL.labels(
                source=source, outcome=RFIDAuthStatus.INVALID.value
            ).inc()
            return RFIDAuthDecision(
                status=RFIDAuthStatus.INVALID,
                source=source,
                reason="missing_id_tag",
            )

        if await self._is_throttled(station_id, id_tag):
            RFID_AUTH_ATTEMPTS_TOTAL.labels(
                source=source, outcome=RFIDAuthStatus.BLOCKED.value
            ).inc()
            return RFIDAuthDecision(
                status=RFIDAuthStatus.BLOCKED,
                source=source,
                reason="too_many_invalid_attempts",
            )

        lookup_failed = False
        try:
            row = await self._timescale.lookup_id_tag(id_tag, station_id=station_id)
        except Exception as exc:
            lookup_failed = True
            row = None
            self._logger.error(
                "rfid_authorize_error source=%s station_id=%s id_tag=%s error=%s",
                source,
                station_id,
                id_tag,
                exc,
            )

        if not row:
            # Check the operator override path before recording an invalid
            # attempt — a customer_admin who pressed "Manual Authorize" in
            # the platform UI minted a one-shot synthetic id_tag in
            # ``operator_authorization_overrides`` (migration 031). Atomic
            # consume: at most one charger Authorize/StartTransaction
            # claims the row.
            override = await self._call_client_method(
                "consume_operator_override", station_id, id_tag
            )
            if isinstance(override, dict):
                RFID_AUTH_ATTEMPTS_TOTAL.labels(
                    source=source, outcome=RFIDAuthStatus.ACCEPTED.value
                ).inc()
                self._logger.info(
                    "rfid_authorize_accepted_via_operator_override "
                    "source=%s station_id=%s id_tag=%s override_id=%s created_by=%s",
                    source,
                    station_id,
                    id_tag,
                    override.get("id"),
                    override.get("created_by"),
                )
                return RFIDAuthDecision(
                    status=RFIDAuthStatus.ACCEPTED,
                    source=source,
                    reason="operator_override",
                    depot_id=str(override["depot_id"]) if override.get("depot_id") else None,
                )
            # Lookup raised: don't penalise the tag with an invalid-attempt
            # row (which would eventually throttle a legitimate card during
            # a DB outage or schema drift) and surface the lookup_error
            # reason for observability.
            if lookup_failed:
                RFID_AUTH_ATTEMPTS_TOTAL.labels(
                    source=source, outcome=RFIDAuthStatus.INVALID.value
                ).inc()
                return RFIDAuthDecision(
                    status=RFIDAuthStatus.INVALID,
                    source=source,
                    reason="lookup_error",
                )
            await self._record_invalid_attempt(station_id, id_tag)
            RFID_AUTH_ATTEMPTS_TOTAL.labels(
                source=source, outcome=RFIDAuthStatus.INVALID.value
            ).inc()
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
        key = (station_id, id_tag)
        _invalid_attempts.pop(key, None)
        _throttled_until.pop(key, None)
        # Only emit a recovery event the first time we succeed after a streak of
        # invalid attempts. Subsequent successes are suppressed until another
        # invalid arrives (which clears the marker in `_record_invalid_attempt`).
        # Skip ``clear_invalid_rfid_attempts`` when this tag has never hit
        # `_record_invalid_attempt` in this process — that path persists an
        # invalid row and sets ``_pending_recovery_audit``.
        if key not in _recovery_logged:
            if key in _pending_recovery_audit:
                await self._call_client_method("clear_invalid_rfid_attempts", station_id, id_tag)
                _pending_recovery_audit.discard(key)
            _recovery_logged.add(key)
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
