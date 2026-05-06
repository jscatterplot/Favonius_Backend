from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.websocket_handler.ocpp_handler import EnhancedOCPPChargePoint
from src.websocket_handler.rfid_authorization import (
    RFIDAuthDecision,
    RFIDAuthStatus,
    RFIDAuthorizationService,
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


# ----- Operator override tests (manual_authorize feature) -----


@pytest.mark.asyncio
async def test_authorize_accepts_operator_override_when_id_tag_unknown() -> None:
    """Synthetic OP- tag with a matching unconsumed override → Accepted."""
    timescale = MagicMock()
    timescale.lookup_id_tag = AsyncMock(return_value=None)
    timescale.consume_operator_override = AsyncMock(
        return_value={
            "id": "00000000-0000-0000-0000-000000000001",
            "station_id": "CP-1",
            "connector_id": 1,
            "organization_id": "11111111-1111-1111-1111-111111111111",
            "depot_id": "22222222-2222-2222-2222-222222222222",
            "created_by": "33333333-3333-3333-3333-333333333333",
            "reason": None,
            "expires_at": None,
            "consumed_at": "2026-05-05T00:00:00+00:00",
        }
    )
    service = RFIDAuthorizationService(timescale, MagicMock())

    decision = await service.authorize("CP-1", "OP-deadbeef", "Authorize")

    assert decision.status == RFIDAuthStatus.ACCEPTED
    assert decision.reason == "operator_override"
    assert decision.depot_id == "22222222-2222-2222-2222-222222222222"
    timescale.consume_operator_override.assert_awaited_once_with("CP-1", "OP-deadbeef")


@pytest.mark.asyncio
async def test_authorize_rejects_when_override_already_consumed() -> None:
    """consume_operator_override returns None → fall through to invalid path."""
    timescale = MagicMock()
    timescale.lookup_id_tag = AsyncMock(return_value=None)
    timescale.consume_operator_override = AsyncMock(return_value=None)
    timescale.record_invalid_rfid_attempt = AsyncMock()
    timescale.count_recent_invalid_rfid_attempts = AsyncMock(return_value=0)
    service = RFIDAuthorizationService(timescale, MagicMock())

    decision = await service.authorize("CP-1", "OP-stale", "Authorize")

    assert decision.status == RFIDAuthStatus.INVALID
    assert decision.reason == "unknown_id_tag"
    timescale.record_invalid_rfid_attempt.assert_awaited_once()


@pytest.mark.asyncio
async def test_authorize_does_not_consume_override_when_id_tag_known() -> None:
    """Real RFID card lookup hits → fast-path success, no override fallback."""
    timescale = MagicMock()
    timescale.lookup_id_tag = AsyncMock(
        return_value={
            "vehicle_id": "veh-1",
            "card_id": "card-1",
            "source": "rfid_card",
        }
    )
    timescale.consume_operator_override = AsyncMock()
    timescale.clear_invalid_rfid_attempts = AsyncMock()
    timescale.count_recent_invalid_rfid_attempts = AsyncMock(return_value=0)
    service = RFIDAuthorizationService(timescale, MagicMock())

    decision = await service.authorize("CP-1", "RFID-123", "Authorize")

    assert decision.status == RFIDAuthStatus.ACCEPTED
    assert decision.reason == "identity_matched"
    timescale.consume_operator_override.assert_not_called()
