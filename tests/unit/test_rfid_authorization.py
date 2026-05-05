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
