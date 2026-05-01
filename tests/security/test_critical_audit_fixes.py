"""Regression tests for the three critical findings from the 2026-05-01 audit.

C1 — OCPP WebSocket Basic Auth on ``/ocpp/{charge_point_id}``
C2 — HMAC signature verification on ``POST /depots/{id}/handoff/receive``
C3 — ``/internal/ocpp-event`` fails closed when ``INTERNAL_API_TOKEN`` is unset

Each test below either exercises the helper directly or asserts the wiring
is present in ``src/api/main.py`` source so a future refactor cannot silently
remove it. Tests do not require a running database.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import bcrypt
import pytest


# ---------------------------------------------------------------------------
# C1 — OCPP WebSocket Basic Auth
# ---------------------------------------------------------------------------


def _mock_static_pool(row: dict | None) -> MagicMock:
    """Return an asyncpg-style pool whose conn.fetchrow returns ``row``."""
    pool = MagicMock()
    conn = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=conn)
    ctx.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=ctx)
    conn.fetchrow = AsyncMock(return_value=row)
    conn.execute = AsyncMock(return_value=None)
    return pool


class TestOcppBasicAuthHelper:
    """Direct exercises of ``verify_ocpp_basic_auth``."""

    @pytest.fixture
    def cp_id(self) -> str:
        return "ORG-DEPOT-CHARGER-001"

    @pytest.fixture
    def password(self) -> str:
        return "s3cret-very-long-password-for-bcrypt"

    @pytest.fixture
    def password_hash(self, password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()

    @staticmethod
    def _basic(user: str, pwd: str) -> str:
        from base64 import b64encode

        return "Basic " + b64encode(f"{user}:{pwd}".encode()).decode()

    @pytest.mark.asyncio
    async def test_valid_credentials_accepted(self, cp_id, password, password_hash):
        from src.security.ocpp_auth import verify_ocpp_basic_auth

        pool = _mock_static_pool({"password_hash": password_hash})
        ok = await verify_ocpp_basic_auth(self._basic(cp_id, password), cp_id, pool)
        assert ok is True

    @pytest.mark.asyncio
    async def test_missing_header_rejected(self, cp_id):
        from src.security.ocpp_auth import verify_ocpp_basic_auth

        pool = _mock_static_pool({"password_hash": "irrelevant"})
        assert await verify_ocpp_basic_auth(None, cp_id, pool) is False
        assert await verify_ocpp_basic_auth("", cp_id, pool) is False
        assert await verify_ocpp_basic_auth("Bearer xxx", cp_id, pool) is False

    @pytest.mark.asyncio
    async def test_username_must_match_charge_point_id(
        self, cp_id, password, password_hash
    ):
        """A leaked credential cannot impersonate a different charger."""
        from src.security.ocpp_auth import verify_ocpp_basic_auth

        pool = _mock_static_pool({"password_hash": password_hash})
        # Right password for the wrong username — must fail.
        ok = await verify_ocpp_basic_auth(
            self._basic("DIFFERENT-CHARGER", password), cp_id, pool
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_wrong_password_rejected(self, cp_id, password_hash):
        from src.security.ocpp_auth import verify_ocpp_basic_auth

        pool = _mock_static_pool({"password_hash": password_hash})
        ok = await verify_ocpp_basic_auth(
            self._basic(cp_id, "wrong-password"), cp_id, pool
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_unknown_station_rejected(self, cp_id, password):
        """No row in station_credentials => reject (and bcrypt still runs)."""
        from src.security.ocpp_auth import verify_ocpp_basic_auth

        pool = _mock_static_pool(None)
        ok = await verify_ocpp_basic_auth(self._basic(cp_id, password), cp_id, pool)
        assert ok is False

    @pytest.mark.asyncio
    async def test_db_failure_rejects_safely(self, cp_id, password):
        from src.security.ocpp_auth import verify_ocpp_basic_auth

        pool = MagicMock()
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(side_effect=RuntimeError("DB down"))
        ctx.__aexit__ = AsyncMock(return_value=None)
        pool.acquire = MagicMock(return_value=ctx)
        ok = await verify_ocpp_basic_auth(self._basic(cp_id, password), cp_id, pool)
        assert ok is False

    @pytest.mark.asyncio
    async def test_malformed_basic_header_rejected(self, cp_id):
        from src.security.ocpp_auth import verify_ocpp_basic_auth

        pool = _mock_static_pool({"password_hash": "x"})
        for header in [
            "Basic",
            "Basic !not-base64",
            "Basic " + "AAAA",  # decodes but no colon
            "Basic " + "OnRlc3Q=",  # ":test"
        ]:
            assert await verify_ocpp_basic_auth(header, cp_id, pool) is False


class TestOcppRouteWiring:
    """Source-level assertions that the FastAPI route calls the verifier."""

    def test_fastapi_route_calls_verifier(self):
        from src.api import main

        source = inspect.getsource(main.ocpp_websocket)
        assert "verify_ocpp_basic_auth" in source, (
            "FastAPI /ocpp/{cp_id} route must verify Basic Auth before accept()"
        )
        # Ensure the awaited verifier call appears before the awaited accept().
        # (Bare `websocket.accept` strings can appear in the docstring; match
        # the actual call form.)
        idx_verify = source.index("await verify_ocpp_basic_auth")
        idx_accept = source.index("await websocket.accept")
        assert idx_verify < idx_accept

    def test_standalone_server_uses_process_request(self):
        from src.adapters.ocpp import server as ocpp_server_module

        source = inspect.getsource(ocpp_server_module.OCPPServer)
        assert "_process_request" in source
        assert "process_request=self._process_request" in source
        assert "verify_ocpp_basic_auth" in source


# ---------------------------------------------------------------------------
# C2 — Handoff HMAC signature verification
# ---------------------------------------------------------------------------


def _signed_handoff_body(signing_key: str, *, ts: float | None = None) -> dict:
    """Build a payload with valid signature, nonce, and timestamp."""
    payload = {
        "message_id": "11111111-1111-1111-1111-111111111111",
        "origin_depot_id": "22222222-2222-2222-2222-222222222222",
        "vehicle_id": "33333333-3333-3333-3333-333333333333",
        "external_id": "bus_42",
        "expected_soc": 0.42,
        "arrival_time": "2026-05-01T12:00:00+00:00",
        "battery_kwh": 250.0,
        "max_charge_kw": 150.0,
        "nonce": f"nonce-{time.time_ns()}",
        "timestamp": datetime.fromtimestamp(
            ts if ts is not None else time.time(), tz=timezone.utc
        ).isoformat(),
    }
    body_bytes = json.dumps(payload, sort_keys=True).encode()
    payload["signature"] = hmac.new(
        signing_key.encode(), body_bytes, hashlib.sha256
    ).hexdigest()
    return payload


class TestHandoffSignatureVerifier:
    """Direct exercises of ``_verify_handoff_payload``."""

    SIGNING_KEY = "test-signing-key-for-handoff-hmac"

    def setup_method(self):
        from src.api import main

        main.reset_handoff_nonces_for_tests()

    def test_valid_signature_accepted(self):
        from src.api.main import _verify_handoff_payload

        body = _signed_handoff_body(self.SIGNING_KEY)
        assert _verify_handoff_payload(body, self.SIGNING_KEY) is True

    def test_missing_signature_rejected(self):
        from src.api.main import _verify_handoff_payload

        body = _signed_handoff_body(self.SIGNING_KEY)
        body.pop("signature")
        assert _verify_handoff_payload(body, self.SIGNING_KEY) is False

    def test_tampered_payload_rejected(self):
        from src.api.main import _verify_handoff_payload

        body = _signed_handoff_body(self.SIGNING_KEY)
        # Forge a different origin_depot_id without resigning.
        body["origin_depot_id"] = "99999999-9999-9999-9999-999999999999"
        assert _verify_handoff_payload(body, self.SIGNING_KEY) is False

    def test_wrong_signing_key_rejected(self):
        from src.api.main import _verify_handoff_payload

        body = _signed_handoff_body(self.SIGNING_KEY)
        assert _verify_handoff_payload(body, "different-key") is False

    def test_expired_timestamp_rejected(self):
        from src.api.main import _verify_handoff_payload

        # Sign a body whose timestamp is 1 hour in the past.
        body = _signed_handoff_body(self.SIGNING_KEY, ts=time.time() - 3600)
        assert _verify_handoff_payload(body, self.SIGNING_KEY) is False

    def test_replay_rejected(self):
        from src.api.main import _verify_handoff_payload

        body = _signed_handoff_body(self.SIGNING_KEY)
        assert _verify_handoff_payload(body, self.SIGNING_KEY) is True
        # Identical body — same nonce — must be rejected the second time.
        assert _verify_handoff_payload(body, self.SIGNING_KEY) is False

    def test_round_trip_matches_send_handoff_canonicalization(self):
        """The verifier MUST accept what send_handoff produces."""
        from src.api.main import _verify_handoff_payload

        # Reproduce send_handoff's canonicalization exactly.
        payload = {
            "message_id": "msg-id",
            "origin_depot_id": "origin",
            "vehicle_id": "veh",
            "external_id": "bus_1",
            "expected_soc": 0.5,
            "arrival_time": "2026-05-01T10:00:00+00:00",
            "battery_kwh": 200.0,
            "max_charge_kw": 100.0,
            "nonce": "unique-nonce-abc",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        body_bytes = json.dumps(payload, sort_keys=True).encode()
        payload["signature"] = hmac.new(
            self.SIGNING_KEY.encode(), body_bytes, hashlib.sha256
        ).hexdigest()
        assert _verify_handoff_payload(payload, self.SIGNING_KEY) is True


class TestReceiveHandoffWiring:
    """Source-level assertions that ``receive_handoff`` enforces the signature."""

    def test_receive_handoff_calls_verifier(self):
        from src.api import main

        source = inspect.getsource(main.receive_handoff)
        assert "_verify_handoff_payload" in source
        assert "HANDOFF_SIGNING_KEY" in source
        # 401 on bad sig, 503 on missing key.
        assert "401" in source and "503" in source

    def test_send_handoff_requires_signing_key(self):
        from src.api import main

        source = inspect.getsource(main.send_handoff)
        # Refuses to send if the key isn't configured (fail-closed at source).
        assert "HANDOFF_SIGNING_KEY" in source
        assert 'detail="Inter-depot handoff is unavailable: signing key not configured"' in source


# ---------------------------------------------------------------------------
# C3 — /internal/ocpp-event fail-closed
# ---------------------------------------------------------------------------


class TestInternalOcppEventFailClosed:
    """Verify the endpoint refuses every request when no token is configured."""

    @pytest.mark.asyncio
    async def test_returns_503_when_token_unset(self):
        from fastapi import HTTPException

        from src.api import main

        payload = main._OcppEventPayload(
            charge_point_id="cp-1",
            event_type="meter_values",
            data={},
        )
        request = MagicMock()
        request.headers = {}

        with patch.object(main, "_INTERNAL_API_TOKEN", ""):
            with pytest.raises(HTTPException) as exc:
                await main.receive_ocpp_event(payload=payload, request=request)
        assert exc.value.status_code == 503

    @pytest.mark.asyncio
    async def test_returns_401_with_wrong_token(self):
        from fastapi import HTTPException

        from src.api import main

        payload = main._OcppEventPayload(
            charge_point_id="cp-1",
            event_type="meter_values",
            data={},
        )
        request = MagicMock()
        request.headers = {"X-Internal-Token": "attacker-guess"}

        with patch.object(main, "_INTERNAL_API_TOKEN", "real-secret-token-value"):
            with pytest.raises(HTTPException) as exc:
                await main.receive_ocpp_event(payload=payload, request=request)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_passes_token_check_with_correct_token(self):
        """With a valid token but no controller_manager configured we must
        still get past the auth gate and hit the unavailable branch."""
        from src.api import main

        payload = main._OcppEventPayload(
            charge_point_id="cp-1",
            event_type="meter_values",
            data={},
        )
        request = MagicMock()
        request.headers = {"X-Internal-Token": "correct-token"}

        with patch.object(main, "_INTERNAL_API_TOKEN", "correct-token"), patch.object(
            main, "controller_manager", None
        ), patch.object(main, "db_pools", None):
            result = await main.receive_ocpp_event(payload=payload, request=request)
        assert result == {"status": "unavailable"}

    def test_endpoint_uses_compare_digest(self):
        """The token comparison must remain timing-safe."""
        from src.api import main

        source = inspect.getsource(main.receive_ocpp_event)
        assert "compare_digest" in source

    def test_endpoint_does_not_treat_missing_token_as_open(self):
        """Regression guard: the dev/staging "noop when token missing" branch
        was the C3 vulnerability. Make sure it is not present."""
        from src.api import main

        source = inspect.getsource(main.receive_ocpp_event)
        # The old code path used `elif _environment == "production"` to gate
        # the open-in-non-prod fall-through. The fixed code refuses
        # unconditionally with 503.
        assert "Internal endpoint not configured" in source
        assert 'elif _environment == "production"' not in source
