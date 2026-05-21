"""HTTP-level tests for the charger-log upload endpoint.

Exercises the path/middleware/auth wiring (size cap bypass for the
upload path, token-only auth without JWT, status codes). The
orchestration is mocked so this test stays a pure API test.
"""

from __future__ import annotations

import io
import tarfile
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.adapters.chargers.upload_token import mint_token


@pytest.fixture(autouse=True)
def _upload_env(monkeypatch):
    monkeypatch.setenv("CHARGER_LOG_UPLOAD_SIGNING_KEY", "endpoint-test-key-1234567890")
    monkeypatch.setenv("CHARGER_LOG_UPLOAD_BASE_URL", "https://example.test/internal/charger_logs/upload")


@pytest.fixture()
def client():
    from src.api.main import app

    return TestClient(app)


def _sample_blob() -> bytes:
    csv = (
        b"timestamp,transaction_id,connector_id,soc_percent,power_w,energy_wh\n"
        b"2024-05-15T10:00:00Z,42,1,15.0,7400,0\n"
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="session_42.csv")
        info.size = len(csv)
        tar.addfile(info, io.BytesIO(csv))
    return buf.getvalue()


class TestUploadEndpoint:
    def test_rejects_when_db_unavailable(self, client):
        token = mint_token(uuid4())
        # The endpoint guards on db_pools being set; the TestClient
        # initialises without running lifespan, so db_pools stays None.
        # Posting body without scheduling lifespan returns 503.
        response = client.post(
            f"/internal/charger_logs/upload?token={token}",
            content=_sample_blob(),
        )
        assert response.status_code == 503
        assert response.json()["detail"]["error_code"] == "DATABASE_UNAVAILABLE"

    def test_rejects_malformed_token(self, client):
        """A token that fails the HMAC check returns 401 — without
        leaking which sub-step failed."""
        # Force db_pools to a non-None value so the endpoint doesn't
        # short-circuit on the 503 path. Patch out receive_upload to
        # ensure the auth check happens before any DB I/O.
        from src.api import main as main_module

        sentinel_pool = object()
        with patch.object(main_module, "db_pools", _StubPools(sentinel_pool)):
            response = client.post(
                "/internal/charger_logs/upload?token=not-a-real-token",
                content=_sample_blob(),
            )
        assert response.status_code == 401

    def test_size_cap_bypasses_global_default(self, client, monkeypatch):
        """The upload path's cap (50 MiB default) must override the
        global 1 MiB default — confirmed by the middleware accepting
        a 2 MiB body that would normally be rejected as 413."""
        monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "1048576")  # 1 MiB
        monkeypatch.setenv("CHARGER_LOG_UPLOAD_MAX_BYTES", "5242880")  # 5 MiB

        from src.api import main as main_module

        sentinel_pool = _StubPools(object())
        token = mint_token(uuid4())
        body = b"x" * (2 * 1024 * 1024)  # 2 MiB

        # Patch receive_upload so we don't need real DB rows; just
        # confirm the middleware lets the body through.
        async def _fake_receive(*a, **k):
            from src.api.charger_logs import UploadRejected

            raise UploadRejected(404, "import not found")  # easy distinguishable

        with patch.object(main_module, "db_pools", sentinel_pool), patch(
            "src.api.main.receive_upload", side_effect=_fake_receive
        ):
            response = client.post(
                f"/internal/charger_logs/upload?token={token}",
                content=body,
                headers={"Content-Length": str(len(body))},
            )
        # The middleware passed the 2 MiB body; receive_upload took
        # over and returned 404 (the fake). If the middleware had
        # rejected, we'd see 413 instead.
        assert response.status_code == 404
        assert response.json()["detail"]["error_code"] == "CHARGER_LOG_UPLOAD_REJECTED"


class _StubPools:
    """Quick replacement for ``DatabasePools`` that's truthy in the
    ``if db_pools is None`` check but never touches a real DB."""

    def __init__(self, ts):
        self.ts = ts
        self.static = ts
