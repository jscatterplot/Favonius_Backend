"""Endpoint-level tests for handoff send/receive HMAC + SSRF guard.

Reference: H4 / src/api/main.py::send_handoff, receive_handoff
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import status as http_status
from fastapi.testclient import TestClient

from src.api import main
from src.api.main import app
from src.security.handoff_validator import compute_handoff_signature
from src.security.tenant_mirror import ensure_tenant_mirrored


SIGNING_KEY = "unit-test-handoff-signing-key"
DEPOT_ID = str(uuid4())
DEST_DEPOT_ID = str(uuid4())
VEHICLE_ID = str(uuid4())


@pytest.fixture(autouse=True)
def _admin_token():
    """Stamp every request with a favonius_admin token + reset nonce store.

    Restore any prior override on teardown so other test modules' autouse
    fixtures aren't unexpectedly cleared.
    """
    user = {"sub": "test-admin", "app_metadata": {"favonius_role": "favonius_admin"}}
    prev = app.dependency_overrides.get(ensure_tenant_mirrored)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: user
    main.reset_handoff_nonces_for_tests()
    yield
    if prev is None:
        app.dependency_overrides.pop(ensure_tenant_mirrored, None)
    else:
        app.dependency_overrides[ensure_tenant_mirrored] = prev
    main.reset_handoff_nonces_for_tests()


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    pool.ts = pool
    pool.static = pool
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "external_id": "bus_1",
            "battery_kwh": 150.0,
            "max_charge_kw": 80.0,
        }
    )
    return pool


def _send_payload() -> dict:
    return {
        "dest_depot_id": DEST_DEPOT_ID,
        "expected_soc": 0.5,
        "arrival_time": (datetime.utcnow() + timedelta(hours=2)).isoformat(),
        "battery_kwh": 150.0,
        "max_charge_kw": 80.0,
    }


def _receive_body(now: datetime | None = None) -> dict:
    when = now or datetime.now(timezone.utc)
    return {
        "message_id": str(uuid4()),
        "origin_depot_id": str(uuid4()),
        "vehicle_id": str(uuid4()),
        "external_id": "bus_42",
        "expected_soc": 0.42,
        "arrival_time": (when + timedelta(hours=1)).isoformat(),
        "battery_kwh": 200.0,
        "max_charge_kw": 100.0,
        "nonce": str(uuid4()),
        "timestamp": when.isoformat(),
    }


# ---------------------------------------------------------------------------
# send_handoff
# ---------------------------------------------------------------------------


class TestSendHandoff:
    def test_missing_endpoint_config_returns_503(self, client, mock_pool, monkeypatch):
        monkeypatch.delenv("DEFAULT_DEPOT_ENDPOINT", raising=False)
        monkeypatch.delenv(f"DEPOT_{DEST_DEPOT_ID}_ENDPOINT", raising=False)
        monkeypatch.setenv("HANDOFF_SIGNING_KEY", SIGNING_KEY)

        with patch("src.api.main.db_pools", mock_pool):
            response = client.post(
                f"/depots/{DEPOT_ID}/vehicles/{VEHICLE_ID}/handoff",
                json=_send_payload(),
            )
        assert response.status_code == 503
        assert "DEPOT_ENDPOINT_NOT_CONFIGURED" in response.json()["detail"]

    def test_missing_signing_key_in_prod_returns_503(self, client, mock_pool, monkeypatch):
        monkeypatch.setattr(main, "_environment", "production")
        monkeypatch.setenv("DEFAULT_DEPOT_ENDPOINT", "https://depot.example.com")
        monkeypatch.delenv("HANDOFF_SIGNING_KEY", raising=False)

        with patch("src.api.main.db_pools", mock_pool), patch(
            "src.api.main.validate_handoff_destination"
        ):
            response = client.post(
                f"/depots/{DEPOT_ID}/vehicles/{VEHICLE_ID}/handoff",
                json=_send_payload(),
            )

        assert response.status_code == 503
        assert "HANDOFF_SIGNING_KEY_NOT_CONFIGURED" in response.json()["detail"]

    def test_private_target_returns_400(self, client, mock_pool, monkeypatch):
        monkeypatch.setattr(main, "_environment", "production")
        monkeypatch.setenv("DEFAULT_DEPOT_ENDPOINT", "https://depot.example.com")
        monkeypatch.setenv("HANDOFF_SIGNING_KEY", SIGNING_KEY)

        with patch("src.api.main.db_pools", mock_pool), patch(
            "src.security.handoff_validator._resolve_addresses"
        ) as resolve:
            import ipaddress

            resolve.return_value = [ipaddress.ip_address("10.0.0.1")]
            response = client.post(
                f"/depots/{DEPOT_ID}/vehicles/{VEHICLE_ID}/handoff",
                json=_send_payload(),
            )
        assert response.status_code == 400
        assert "disallowed address" in response.json()["detail"]

    def test_signs_outgoing_request_with_header(self, client, mock_pool, monkeypatch):
        """The send-side puts the signature in X-Handoff-Signature, not the body."""
        monkeypatch.setattr(main, "_environment", "production")
        monkeypatch.setenv("DEFAULT_DEPOT_ENDPOINT", "https://depot.example.com")
        monkeypatch.setenv("HANDOFF_SIGNING_KEY", SIGNING_KEY)

        captured: dict = {}

        class FakeResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"acknowledged_at": datetime.now(timezone.utc).isoformat()}

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, content=None, headers=None, **kwargs):
                captured["url"] = url
                captured["body"] = content
                captured["headers"] = headers or {}
                return FakeResponse()

        with patch("src.api.main.db_pools", mock_pool), patch(
            "src.security.handoff_validator._resolve_addresses"
        ) as resolve, patch("src.api.main.httpx.AsyncClient", FakeAsyncClient):
            import ipaddress

            resolve.return_value = [ipaddress.ip_address("8.8.8.8")]
            response = client.post(
                f"/depots/{DEPOT_ID}/vehicles/{VEHICLE_ID}/handoff",
                json=_send_payload(),
            )

        assert response.status_code == 200, response.json()
        assert "X-Handoff-Signature" in captured["headers"]
        sig = captured["headers"]["X-Handoff-Signature"]
        assert len(sig) == 64
        # The body must NOT contain a signature field (that's the whole H4
        # point: out of the JSON, into the header).
        body = json.loads(captured["body"])
        assert "signature" not in body
        # The header must verify against the body bytes the client sent.
        expected = compute_handoff_signature(SIGNING_KEY.encode(), captured["body"])
        assert sig == expected


# ---------------------------------------------------------------------------
# receive_handoff
# ---------------------------------------------------------------------------


class TestReceiveHandoff:
    def _post(self, client, body: dict, signature: str | None, mock_pool):
        body_bytes = json.dumps(body, sort_keys=True).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if signature is not None:
            headers["X-Handoff-Signature"] = signature
        with patch("src.api.main.db_pools", mock_pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch("src.api.main.controller_manager", None):
            mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
                return_value=None
            )
            return client.post(
                f"/depots/{DEPOT_ID}/handoff/receive",
                content=body_bytes,
                headers=headers,
            )

    def test_missing_signature_in_prod_returns_401(self, client, mock_pool, monkeypatch):
        monkeypatch.setattr(main, "_environment", "production")
        monkeypatch.setenv("HANDOFF_SIGNING_KEY", SIGNING_KEY)

        body = _receive_body()
        response = self._post(client, body, signature=None, mock_pool=mock_pool)
        assert response.status_code == 401
        assert "HANDOFF_SIGNATURE_REQUIRED" in response.json()["detail"]

    def test_invalid_signature_in_prod_returns_401(self, client, mock_pool, monkeypatch):
        monkeypatch.setattr(main, "_environment", "production")
        monkeypatch.setenv("HANDOFF_SIGNING_KEY", SIGNING_KEY)

        body = _receive_body()
        response = self._post(
            client, body, signature="0" * 64, mock_pool=mock_pool
        )
        assert response.status_code == 401
        assert "HANDOFF_SIGNATURE_INVALID" in response.json()["detail"]

    def test_valid_signature_in_prod_returns_200(self, client, mock_pool, monkeypatch):
        monkeypatch.setattr(main, "_environment", "production")
        monkeypatch.setenv("HANDOFF_SIGNING_KEY", SIGNING_KEY)

        body = _receive_body()
        body_bytes = json.dumps(body, sort_keys=True).encode("utf-8")
        sig = compute_handoff_signature(SIGNING_KEY.encode(), body_bytes)

        with patch("src.api.main.db_pools", mock_pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch("src.api.main.controller_manager", None):
            mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
                return_value=None
            )
            response = client.post(
                f"/depots/{DEPOT_ID}/handoff/receive",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Handoff-Signature": sig,
                },
            )

        assert response.status_code == 200, response.json()
        assert response.json()["status"] == "acknowledged"

    def test_missing_key_in_prod_returns_503(self, client, mock_pool, monkeypatch):
        monkeypatch.setattr(main, "_environment", "production")
        monkeypatch.delenv("HANDOFF_SIGNING_KEY", raising=False)

        body = _receive_body()
        response = self._post(client, body, signature="ignored", mock_pool=mock_pool)
        assert response.status_code == 503
        assert "HANDOFF_SIGNING_KEY_NOT_CONFIGURED" in response.json()["detail"]

    def test_dev_without_signature_logs_warning_and_proceeds(
        self, client, mock_pool, monkeypatch, caplog
    ):
        monkeypatch.setattr(main, "_environment", "development")
        monkeypatch.delenv("HANDOFF_SIGNING_KEY", raising=False)

        body = _receive_body()
        with caplog.at_level("WARNING", logger="src.api.main"):
            response = self._post(client, body, signature=None, mock_pool=mock_pool)
        assert response.status_code == 200, response.json()
        assert any(
            "without HMAC signature" in record.message for record in caplog.records
        )

    def test_replay_of_same_signature_rejected(self, client, mock_pool, monkeypatch):
        """Even with a valid signature, the same nonce must only fly once."""
        monkeypatch.setattr(main, "_environment", "production")
        monkeypatch.setenv("HANDOFF_SIGNING_KEY", SIGNING_KEY)

        body = _receive_body()
        body_bytes = json.dumps(body, sort_keys=True).encode("utf-8")
        sig = compute_handoff_signature(SIGNING_KEY.encode(), body_bytes)

        with patch("src.api.main.db_pools", mock_pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch("src.api.main.controller_manager", None):
            mock_pool.acquire.return_value.__aenter__.return_value.fetchrow = AsyncMock(
                return_value=None
            )
            r1 = client.post(
                f"/depots/{DEPOT_ID}/handoff/receive",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Handoff-Signature": sig,
                },
            )
            r2 = client.post(
                f"/depots/{DEPOT_ID}/handoff/receive",
                content=body_bytes,
                headers={
                    "Content-Type": "application/json",
                    "X-Handoff-Signature": sig,
                },
            )
        assert r1.status_code == 200
        assert r2.status_code == 401


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestHandoffRoundTrip:
    def test_send_payload_verifies_at_receive(self, monkeypatch):
        """What send_handoff signs must verify at the receive side."""
        monkeypatch.setenv("HANDOFF_SIGNING_KEY", SIGNING_KEY)
        # Reproduce the exact serialisation send_handoff uses.
        payload = {
            "message_id": str(uuid4()),
            "origin_depot_id": str(uuid4()),
            "vehicle_id": str(uuid4()),
            "external_id": "bus_99",
            "expected_soc": 0.7,
            "arrival_time": datetime.now(timezone.utc).isoformat(),
            "battery_kwh": 250.0,
            "max_charge_kw": 100.0,
            "nonce": str(uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        body_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
        sig = compute_handoff_signature(SIGNING_KEY.encode(), body_bytes)

        main.reset_handoff_nonces_for_tests()
        assert main._verify_handoff_payload(body_bytes, sig, SIGNING_KEY) is True
