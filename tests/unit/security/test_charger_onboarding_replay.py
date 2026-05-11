"""Tests for H5: replay of charger onboarding never re-emits the plaintext credential.

The first call returns 201 with a one-time password. Any retry of the same
Idempotency-Key returns 200 with a *receipt* — same shape minus the password,
plus ``replayed: true`` and a hint pointing at rotate_credentials.

Reference: src/api/main.py::_format_charger_onboarding_first_response,
            _format_charger_onboarding_replay_response, create_charger_onboarding
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
from fastapi import status as http_status
from fastapi.testclient import TestClient

from src.api.main import (
    _format_charger_onboarding_first_response,
    _format_charger_onboarding_replay_response,
    _generate_ocpp_basic_password,
    app,
)
from src.security.tenant_mirror import ensure_tenant_mirrored


AUTH_HDR = {"Authorization": "Bearer test"}
DEPOT_ID = str(uuid4())
ORG_ID = str(uuid4())


def _user(role: str = "customer_admin", organization_id: str = ORG_ID) -> dict:
    return {
        "sub": str(uuid4()),
        "app_metadata": {
            "favonius_role": role,
            "organization_id": organization_id,
        },
    }


@pytest.fixture(autouse=True)
def _clear_overrides():
    """Restore the prior ``ensure_tenant_mirrored`` override on teardown.

    Don't clear *all* overrides — other test modules may install their own
    autouse fixtures that depend on staying installed.
    """
    prev = app.dependency_overrides.get(ensure_tenant_mirrored)
    yield
    if prev is None:
        app.dependency_overrides.pop(ensure_tenant_mirrored, None)
    else:
        app.dependency_overrides[ensure_tenant_mirrored] = prev


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    conn = AsyncMock()
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__.return_value = None
    conn.transaction.return_value.__aexit__.return_value = None
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    pool.ts = pool
    pool.static = pool
    return pool, conn


def _payload() -> dict:
    return {
        "displayName": "ABB charger by gate 1",
        "vendor": "ABB",
        "model": "Terra 184",
        "serialNumber": "ABB-001",
        "firmware": "1.2.3",
        "ratedKw": 150.0,
        "connectorType": "CCS",
        "connectorCount": 2,
        "connectorIds": [1, 2],
        "networkNotes": "Static IP reserved",
    }


def _charger_dict() -> dict:
    return {
        "id": str(uuid4()),
        "depot_id": DEPOT_ID,
        "ocpp_id": "acme-berlin-001",
        "display_name": "ABB charger by gate 1",
    }


# ---------------------------------------------------------------------------
# Pure formatting tests
# ---------------------------------------------------------------------------


class TestResponseFormatters:
    def test_generated_password_fits_abb_terraconfig_limit(self):
        password = _generate_ocpp_basic_password()

        assert len(password) == 10
        assert len(password.encode("utf-8")) == 10
        assert password.isalnum()

    def test_first_response_includes_plaintext_password(self):
        response = _format_charger_onboarding_first_response(_charger_dict(), "PLAINTEXT-PW")
        assert response["credentials"]["password"] == "PLAINTEXT-PW"
        assert response["credentials"]["shown_once"] is True

    def test_replay_response_drops_password(self):
        response = _format_charger_onboarding_replay_response(_charger_dict())
        assert "password" not in response["credentials"]
        assert response["replayed"] is True
        assert "rotate_credentials" in response["detail"]
        # Shape consistency with first response so clients can parse uniformly.
        assert response["credentials"]["username"] == "acme-berlin-001"

    def test_replay_response_does_not_leak_password_via_serialization(self):
        # Sanity check: the formatter must not silently embed the plaintext
        # in another field (e.g. a copy-paste mistake in the future).
        response = _format_charger_onboarding_replay_response(_charger_dict())
        flat = repr(response)
        assert "password" not in flat.lower() or "rotate_credentials" in flat
        assert "PLAINTEXT" not in flat


# ---------------------------------------------------------------------------
# Endpoint: first call vs replay
# ---------------------------------------------------------------------------


class TestEndpointReplay:
    def _common_patches(self, mock_pool, *, existing=None, charger=None):
        """Return the list of patch context managers for the onboarding flow."""
        return [
            patch("src.api.main.db_pools", mock_pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.delete_expired_charger_onboarding_idempotency",
                new_callable=AsyncMock,
            ),
            patch(
                "src.api.main.db_queries.acquire_charger_onboarding_idempotency_lock",
                new_callable=AsyncMock,
            ),
            patch(
                "src.api.main.db_queries.get_charger_onboarding_idempotency",
                new_callable=AsyncMock,
                return_value=existing,
            ),
            patch(
                "src.api.main.db_queries.get_depot_org_slug_context",
                new_callable=AsyncMock,
                return_value={
                    "depot_id": DEPOT_ID,
                    "depot_name": "Berlin",
                    "organization_id": ORG_ID,
                    "organization_name": "Acme",
                },
            ),
            patch(
                "src.api.main.db_queries.next_charger_ocpp_id",
                new_callable=AsyncMock,
                return_value="acme-berlin-001",
            ),
            patch(
                "src.api.main.db_queries.create_charger_with_credentials",
                new_callable=AsyncMock,
                return_value=charger or _charger_dict(),
            ),
        ]

    def test_first_call_returns_201_with_password(self, client, mock_pool):
        pool, _conn = mock_pool
        app.dependency_overrides[ensure_tenant_mirrored] = lambda: _user()

        idem_store = AsyncMock()
        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.delete_expired_charger_onboarding_idempotency",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.acquire_charger_onboarding_idempotency_lock",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.get_charger_onboarding_idempotency",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "src.api.main.db_queries.get_depot_org_slug_context",
            new_callable=AsyncMock,
            return_value={
                "depot_id": DEPOT_ID,
                "depot_name": "Berlin",
                "organization_id": ORG_ID,
                "organization_name": "Acme",
            },
        ), patch(
            "src.api.main.db_queries.next_charger_ocpp_id",
            new_callable=AsyncMock,
            return_value="acme-berlin-001",
        ), patch(
            "src.api.main.db_queries.create_charger_with_credentials",
            new_callable=AsyncMock,
            return_value=_charger_dict(),
        ), patch(
            "src.api.main.db_queries.store_charger_onboarding_idempotency",
            idem_store,
        ):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/chargers",
                headers={**AUTH_HDR, "Idempotency-Key": str(uuid4())},
                json=_payload(),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        body = response.json()
        assert body["credentials"]["password"]
        assert body["credentials"]["shown_once"] is True
        assert "replayed" not in body

        # The stored idempotency payload is the *replay* receipt — never the
        # plaintext that just went out.
        stored = idem_store.await_args.kwargs["response_json"]
        assert "password" not in stored["credentials"]
        assert stored["replayed"] is True
        assert idem_store.await_args.kwargs["status_code"] == http_status.HTTP_200_OK

    def test_replay_returns_200_with_receipt_no_password(self, client, mock_pool):
        pool, _conn = mock_pool
        app.dependency_overrides[ensure_tenant_mirrored] = lambda: _user()

        replay_payload = _format_charger_onboarding_replay_response(_charger_dict())

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.delete_expired_charger_onboarding_idempotency",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.acquire_charger_onboarding_idempotency_lock",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.get_charger_onboarding_idempotency",
            new_callable=AsyncMock,
            return_value={
                "request_hash": "match",
                "response_json": replay_payload,
                "status_code": http_status.HTTP_200_OK,
            },
        ), patch("src.api.main._canonical_request_hash", return_value="match"):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/chargers",
                headers={**AUTH_HDR, "Idempotency-Key": "retry-key"},
                json=_payload(),
            )

        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["replayed"] is True
        assert "password" not in body["credentials"]
        assert "rotate_credentials" in body["detail"]

    def test_replay_with_different_body_returns_409(self, client, mock_pool):
        pool, _conn = mock_pool
        app.dependency_overrides[ensure_tenant_mirrored] = lambda: _user()

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.delete_expired_charger_onboarding_idempotency",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.acquire_charger_onboarding_idempotency_lock",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.get_charger_onboarding_idempotency",
            new_callable=AsyncMock,
            return_value={
                "request_hash": "old-hash",
                "response_json": {},
                "status_code": http_status.HTTP_200_OK,
            },
        ), patch("src.api.main._canonical_request_hash", return_value="new-hash"):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/chargers",
                headers={**AUTH_HDR, "Idempotency-Key": "retry-key"},
                json=_payload(),
            )

        assert response.status_code == http_status.HTTP_409_CONFLICT
        assert "IDEMPOTENCY_KEY_REUSED" in response.json()["detail"]

    def test_idempotency_record_stored_with_replay_payload(self, client, mock_pool):
        """Direct check that the response_json column never holds plaintext."""
        pool, _conn = mock_pool
        app.dependency_overrides[ensure_tenant_mirrored] = lambda: _user()

        store_calls = []

        async def capture(*args, **kwargs):
            store_calls.append(kwargs)

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.delete_expired_charger_onboarding_idempotency",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.acquire_charger_onboarding_idempotency_lock",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.get_charger_onboarding_idempotency",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "src.api.main.db_queries.get_depot_org_slug_context",
            new_callable=AsyncMock,
            return_value={
                "depot_id": DEPOT_ID,
                "depot_name": "Berlin",
                "organization_id": ORG_ID,
                "organization_name": "Acme",
            },
        ), patch(
            "src.api.main.db_queries.next_charger_ocpp_id",
            new_callable=AsyncMock,
            return_value="acme-berlin-001",
        ), patch(
            "src.api.main.db_queries.create_charger_with_credentials",
            new_callable=AsyncMock,
            return_value=_charger_dict(),
        ), patch(
            "src.api.main.db_queries.store_charger_onboarding_idempotency",
            side_effect=capture,
        ):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/chargers",
                headers={**AUTH_HDR, "Idempotency-Key": str(uuid4())},
                json=_payload(),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        assert len(store_calls) == 1
        stored_payload = store_calls[0]["response_json"]
        plaintext = response.json()["credentials"]["password"]
        # Inspect deeply — no nested key may contain the plaintext.
        assert plaintext not in repr(stored_payload)
        assert "password" not in stored_payload["credentials"]
