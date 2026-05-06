from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.websocket_handler.rfid_authorization as rfid_module
from src.websocket_handler.ocpp_handler import EnhancedOCPPChargePoint
from src.websocket_handler.rfid_authorization import (
    IN_MEMORY_STATE_RETENTION_S,
    INVALID_ATTEMPT_THRESHOLD,
    RFIDAuthDecision,
    RFIDAuthorizationService,
    RFIDAuthStatus,
    map_auth_status_to_ocpp201,
    user_message_for,
)
from src.websocket_handler.transaction_manager import IdToken, IdTokenType, TransactionManager


@pytest.mark.asyncio
async def test_rfid_authorization_accepts_known_tag() -> None:
    timescale = MagicMock()
    timescale.lookup_id_tag = AsyncMock(
        return_value={
            "vehicle_id": "veh-1",
            "driver_id": None,
            "card_id": "card-1",
            "depot_id": "dep-1",
            "source": "rfid_card",
        }
    )
    service = RFIDAuthorizationService(timescale, MagicMock())

    decision = await service.authorize("CP-1", "TAG-OK", "Authorize")

    assert decision.status == RFIDAuthStatus.ACCEPTED
    assert decision.source == "Authorize"
    assert decision.card_id == "card-1"
    timescale.lookup_id_tag.assert_awaited_once_with("TAG-OK", station_id="CP-1")


@pytest.mark.asyncio
async def test_first_accepted_authorize_skips_clear_invalid_rfid_attempts() -> None:
    """No DB round-trip for recovery when the tag has never been denied this process."""
    timescale = MagicMock()
    timescale.lookup_id_tag = AsyncMock(
        return_value={
            "vehicle_id": "veh-1",
            "card_id": "card-1",
            "source": "rfid_card",
        }
    )
    timescale.clear_invalid_rfid_attempts = AsyncMock()
    service = RFIDAuthorizationService(timescale, MagicMock())

    await service.authorize("CP-1", "TAG-NEW", "Authorize")

    timescale.clear_invalid_rfid_attempts.assert_not_called()


@pytest.mark.asyncio
async def test_rfid_authorization_throttles_repeated_invalid_tags() -> None:
    timescale = MagicMock()
    timescale.lookup_id_tag = AsyncMock(return_value=None)
    service = RFIDAuthorizationService(timescale, MagicMock())

    for _ in range(8):
        decision = await service.authorize("CP-1", "TAG-BAD", "Authorize")
        assert decision.status == RFIDAuthStatus.INVALID

    throttled = await service.authorize("CP-1", "TAG-BAD", "Authorize")
    assert throttled.status == RFIDAuthStatus.BLOCKED
    assert throttled.reason == "too_many_invalid_attempts"


@pytest.mark.asyncio
async def test_ocpp201_authorize_fails_closed_for_unknown_rfid() -> None:
    cp = EnhancedOCPPChargePoint.__new__(EnhancedOCPPChargePoint)
    cp.id = "CP-1"
    cp.rfid_authorization = MagicMock()
    cp.rfid_authorization.authorize = AsyncMock(
        return_value=RFIDAuthDecision(
            status=RFIDAuthStatus.INVALID,
            source="Authorize",
            reason="unknown_id_tag",
        )
    )

    response = await EnhancedOCPPChargePoint.on_authorize(cp, {"idToken": "UNKNOWN"})

    assert response.id_token_info["status"] == "Invalid"


@pytest.mark.asyncio
async def test_ocpp201_authorize_accepts_known_rfid() -> None:
    cp = EnhancedOCPPChargePoint.__new__(EnhancedOCPPChargePoint)
    cp.id = "CP-1"
    cp.rfid_authorization = MagicMock()
    cp.rfid_authorization.authorize = AsyncMock(
        return_value=RFIDAuthDecision(
            status=RFIDAuthStatus.ACCEPTED,
            source="rfid_card",
            reason="identity_matched",
            vehicle_id="veh-7",
            card_id="card-7",
        )
    )

    response = await EnhancedOCPPChargePoint.on_authorize(cp, {"idToken": "KNOWN"})

    assert response.id_token_info["status"] == "Accepted"


@pytest.mark.asyncio
async def test_request_start_transaction_rejects_invalid_profile_purpose() -> None:
    timescale = MagicMock()
    timescale.lookup_id_tag = AsyncMock(return_value={"source": "rfid_card"})
    manager = TransactionManager(timescale)

    result = await manager.request_start_transaction(
        "CP-1",
        1,
        None,
        IdToken(id_token="KNOWN", type=IdTokenType.ISO14443),
        {"chargingProfilePurpose": "TxDefaultProfile"},
    )

    assert result["status"] == "Rejected"
    assert result["statusInfo"]["reasonCode"] == "InvalidChargingProfile"


@pytest.mark.asyncio
async def test_request_start_transaction_rejects_concurrent_evse_session() -> None:
    timescale = MagicMock()
    timescale.lookup_id_tag = AsyncMock(return_value={"source": "rfid_card"})
    manager = TransactionManager(timescale)
    manager.active_transactions["CP-1:1"] = MagicMock()

    result = await manager.request_start_transaction(
        "CP-1",
        1,
        None,
        IdToken(id_token="KNOWN", type=IdTokenType.ISO14443),
        {"chargingProfilePurpose": "TxProfile"},
    )

    assert result["status"] == "Rejected"
    assert result["statusInfo"]["reasonCode"] == "ConcurrentTx"


# ---------------------------------------------------------------------------
# Tests added during code-review cleanup of the RFID auth feature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_start_transaction_serialises_concurrent_starts() -> None:
    """Two concurrent starts for the same EVSE: exactly one Accepted, one ConcurrentTx.

    Without the per-EVSE lock, both calls pass the slot-empty check before
    either populates ``active_transactions`` (the gap is the auth await),
    and the second silently overwrites the first.
    """
    timescale = MagicMock()
    timescale.lookup_id_tag = AsyncMock(return_value={"vehicle_id": "veh-1", "source": "rfid_card"})
    manager = TransactionManager(timescale)
    # _store_transaction and _check_evse_availability hit downstream methods
    # we don't care about for race semantics; stub them.
    manager._store_transaction = AsyncMock()
    manager._check_evse_availability = AsyncMock(return_value=True)

    async def start():
        return await manager.request_start_transaction(
            "CP-1",
            1,
            None,
            IdToken(id_token="KNOWN", type=IdTokenType.ISO14443),
        )

    results = await asyncio.gather(start(), start())
    statuses = sorted(r["status"] for r in results)
    assert statuses == ["Accepted", "Rejected"]
    rejected = next(r for r in results if r["status"] == "Rejected")
    assert rejected["statusInfo"]["reasonCode"] == "ConcurrentTx"


@pytest.mark.asyncio
async def test_authorize_warns_once_for_missing_client_method(caplog) -> None:
    """A typo or refactor that drops a TimescaleClient method must leave a log trail."""
    import logging

    timescale = MagicMock(spec=[])  # no methods at all
    timescale.lookup_id_tag = AsyncMock(return_value=None)
    logger = logging.getLogger("rfid_test")
    service = RFIDAuthorizationService(timescale, logger)

    with caplog.at_level(logging.WARNING, logger="rfid_test"):
        await service.authorize("CP-1", "TAG-X", "Authorize")
        await service.authorize("CP-1", "TAG-Y", "Authorize")

    missing_warnings = [
        r for r in caplog.records if "rfid_authorization_method_unavailable" in r.getMessage()
    ]
    method_names = {
        r.getMessage().split("method=", 1)[1].split(" ", 1)[0] for r in missing_warnings
    }
    # Each missing method warns exactly once even though we call authorize twice.
    assert "count_recent_invalid_rfid_attempts" in method_names
    assert "record_invalid_rfid_attempt" in method_names
    method_to_count = {n: sum(1 for m in method_names if m == n) for n in method_names}
    for name, count in method_to_count.items():
        assert count == 1, f"{name} warned {count} times, expected 1"


@pytest.mark.asyncio
async def test_recovery_event_is_idempotent_until_next_invalid() -> None:
    """clear_invalid_rfid_attempts is called once per recovery, not once per success."""
    timescale = MagicMock()
    # First call returns None (unknown), then the same tag becomes known.
    timescale.lookup_id_tag = AsyncMock(
        side_effect=[
            None,
            {"source": "rfid_card"},
            {"source": "rfid_card"},
            None,
            {"source": "rfid_card"},
        ]
    )
    timescale.record_invalid_rfid_attempt = AsyncMock()
    timescale.clear_invalid_rfid_attempts = AsyncMock()
    timescale.count_recent_invalid_rfid_attempts = AsyncMock(return_value=0)
    service = RFIDAuthorizationService(timescale, MagicMock())

    await service.authorize("CP-1", "TAG", "Authorize")  # invalid
    await service.authorize("CP-1", "TAG", "Authorize")  # accepted → 1 recovery
    await service.authorize("CP-1", "TAG", "Authorize")  # accepted → no extra recovery
    await service.authorize("CP-1", "TAG", "Authorize")  # invalid (resets marker)
    await service.authorize("CP-1", "TAG", "Authorize")  # accepted → 2nd recovery

    assert timescale.clear_invalid_rfid_attempts.await_count == 2


@pytest.mark.asyncio
async def test_throttle_threshold_uses_module_constant() -> None:
    """Sanity check that the threshold matches the documented module constant."""
    timescale = MagicMock()
    timescale.lookup_id_tag = AsyncMock(return_value=None)
    timescale.record_invalid_rfid_attempt = AsyncMock()
    timescale.count_recent_invalid_rfid_attempts = AsyncMock(return_value=0)
    timescale.clear_invalid_rfid_attempts = AsyncMock()
    service = RFIDAuthorizationService(timescale, MagicMock())

    for _ in range(INVALID_ATTEMPT_THRESHOLD):
        decision = await service.authorize("CP-X", "TAG-BAD", "Authorize")
        assert decision.status == RFIDAuthStatus.INVALID

    blocked = await service.authorize("CP-X", "TAG-BAD", "Authorize")
    assert blocked.status == RFIDAuthStatus.BLOCKED


@pytest.mark.asyncio
async def test_stale_invalid_keys_are_pruned_from_in_memory_state(monkeypatch) -> None:
    timescale = MagicMock()
    timescale.lookup_id_tag = AsyncMock(return_value=None)
    timescale.record_invalid_rfid_attempt = AsyncMock()
    timescale.count_recent_invalid_rfid_attempts = AsyncMock(return_value=0)
    service = RFIDAuthorizationService(timescale, MagicMock())
    key = ("CP-LEAK", "TAG-FAKE")
    for state in (
        rfid_module._invalid_attempts,
        rfid_module._throttled_until,
        rfid_module._pending_recovery_audit,
        rfid_module._recovery_logged,
    ):
        state.clear()

    now = 1_000_000.0
    monkeypatch.setattr(rfid_module.time, "time", lambda: now)
    await service.authorize(*key, "Authorize")
    assert key in rfid_module._invalid_attempts
    assert key in rfid_module._pending_recovery_audit

    now += IN_MEMORY_STATE_RETENTION_S + 1
    await service.authorize("CP-OTHER", "TAG-OTHER", "Authorize")
    assert key not in rfid_module._invalid_attempts
    assert key not in rfid_module._throttled_until
    assert key not in rfid_module._pending_recovery_audit
    assert key not in rfid_module._recovery_logged


def test_map_auth_concurrent_tx_matches_ocpp201_concurrent_tx() -> None:
    assert map_auth_status_to_ocpp201(RFIDAuthStatus.CONCURRENT_TX) == "ConcurrentTx"


def test_user_message_for_distinguishes_failure_modes() -> None:
    """Driver-facing messages must differentiate Expired/Blocked/Invalid/etc."""

    def _decision(status: RFIDAuthStatus) -> RFIDAuthDecision:
        return RFIDAuthDecision(status=status, source="Authorize", reason="x")

    accepted = user_message_for(_decision(RFIDAuthStatus.ACCEPTED))
    expired = user_message_for(_decision(RFIDAuthStatus.EXPIRED))
    blocked = user_message_for(_decision(RFIDAuthStatus.BLOCKED))
    concurrent = user_message_for(_decision(RFIDAuthStatus.CONCURRENT_TX))
    invalid = user_message_for(_decision(RFIDAuthStatus.INVALID))

    assert len({accepted, expired, blocked, concurrent, invalid}) == 5


@pytest.mark.asyncio
async def test_validate_started_transaction_token_imports_resolve() -> None:
    """Regression: ``RFIDAuthStatus`` must be importable in ocpp_handler.

    This test would have caught the bug where _validate_started_transaction_token
    raised NameError because RFIDAuthStatus wasn't imported in the module.
    """
    cp = EnhancedOCPPChargePoint.__new__(EnhancedOCPPChargePoint)
    cp.id = "CP-1"
    cp.logger = MagicMock()
    cp.rfid_authorization = MagicMock()
    cp.rfid_authorization.authorize = AsyncMock(
        return_value=RFIDAuthDecision(
            status=RFIDAuthStatus.INVALID, source="TransactionEventStarted", reason="x"
        )
    )
    cp.timescale_client = MagicMock()
    cp.timescale_client.lookup_id_tag = AsyncMock(return_value=None)

    # If RFIDAuthStatus.ACCEPTED is unresolved at module level this raises NameError.
    await EnhancedOCPPChargePoint._validate_started_transaction_token(cp, "TOK")
    cp.logger.warning.assert_called_once()


@pytest.mark.asyncio
async def test_validate_started_transaction_token_skips_authorize_for_known_token() -> None:
    cp = EnhancedOCPPChargePoint.__new__(EnhancedOCPPChargePoint)
    cp.id = "CP-1"
    cp.logger = MagicMock()
    cp.rfid_authorization = MagicMock()
    cp.rfid_authorization.authorize = AsyncMock()
    cp.timescale_client = MagicMock()
    cp.timescale_client.lookup_id_tag = AsyncMock(return_value={"card_id": "card-1"})

    await EnhancedOCPPChargePoint._validate_started_transaction_token(cp, "KNOWN")

    cp.rfid_authorization.authorize.assert_not_called()
    cp.logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_validate_started_transaction_token_lookup_error_is_warning_only() -> None:
    cp = EnhancedOCPPChargePoint.__new__(EnhancedOCPPChargePoint)
    cp.id = "CP-1"
    cp.logger = MagicMock()
    cp.rfid_authorization = MagicMock()
    cp.rfid_authorization.authorize = AsyncMock()
    cp.timescale_client = MagicMock()
    cp.timescale_client.lookup_id_tag = AsyncMock(side_effect=RuntimeError("db down"))

    await EnhancedOCPPChargePoint._validate_started_transaction_token(cp, "TOK")

    cp.rfid_authorization.authorize.assert_not_called()
    cp.logger.warning.assert_called_once()
