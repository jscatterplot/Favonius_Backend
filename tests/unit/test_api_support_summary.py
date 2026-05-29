"""Endpoint tests for the context-aware support summary feature.

Covers POST /support/summary (happy/sad/oversized/unauth/cross-tenant/503),
the buffer-integration path, and the admin GET / DELETE endpoints.
"""

import base64
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.api.main import app
from src.observability.log_buffer import RingBufferLogHandler
from src.security.tenant_mirror import ensure_tenant_mirrored

AUTH = {"Authorization": "Bearer test-token"}
NEW_ID = "11111111-1111-1111-1111-111111111111"
IMG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"img" * 8).decode()


def _operator(org_id: str | None = None) -> dict:
    return {
        "sub": str(uuid4()),
        "app_metadata": {
            "favonius_role": "customer_operator",
            "organization_id": org_id or str(uuid4()),
        },
    }


def _admin() -> dict:
    return {"sub": str(uuid4()), "app_metadata": {"favonius_role": "favonius_admin"}}


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _no_probe(monkeypatch):
    """Default to no WebSocket probe so health is deterministic ('unknown')."""
    monkeypatch.delenv("WEBSOCKET_HEALTH_PROBE_URL", raising=False)
    monkeypatch.setenv("SUPPORT_ENG_RECIPIENT", "")


def _capture_task(captured: list):
    def _cap(coro):
        captured.append(coro)
        coro.close()  # don't actually run delivery; avoid 'never awaited'

    return _cap


# ── POST /support/summary ─────────────────────────────────────────────────────


def test_submit_happy_path(client, mock_db_pool):
    pool, conn = mock_db_pool
    conn.fetchval = AsyncMock(return_value=NEW_ID)
    user = _operator()
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: user
    captured: list = []
    with (
        patch("src.api.main.db_pools", pool),
        patch("src.api.main.ocpp_server", None),
        patch("src.api.main.get_log_buffer", return_value=None),
        patch("src.api.main._create_background_task", _capture_task(captured)),
    ):
        resp = client.post("/support/summary", headers=AUTH, json={"page": "/depots/x/state"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == NEW_ID
    assert body["status"] == "received"
    assert body["delivery_status"] == "pending"
    # The engineering summary_text / logs are NEVER returned to the operator
    # (the process-wide buffer can contain other tenants' log lines).
    assert "summary_text" not in body
    assert "logs_excerpt" not in body
    assert body["health_snapshot"]["tiger_cloud"] == "healthy"
    assert body["health_snapshot"]["websocket"] == "unknown"
    assert len(captured) == 1  # delivery scheduled post-commit


def test_submit_with_screenshot(client, mock_db_pool):
    pool, conn = mock_db_pool
    conn.fetchval = AsyncMock(return_value=NEW_ID)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _operator()
    captured: list = []
    with (
        patch("src.api.main.db_pools", pool),
        patch("src.api.main.ocpp_server", None),
        patch("src.api.main.get_log_buffer", return_value=None),
        patch("src.api.main._create_background_task", _capture_task(captured)),
    ):
        resp = client.post(
            "/support/summary",
            headers=AUTH,
            json={
                "page": "/depots/x/state",
                "screenshot_base64": IMG_B64,
                "screenshot_content_type": "image/png",
            },
        )
    assert resp.status_code == 201, resp.text
    assert conn.fetchval.await_count >= 1  # INSERT happened


def test_submit_reflects_recent_error_in_stored_summary(client, mock_db_pool):
    """The engineering summary captures the recent error (redacted), but the
    operator response never carries it (cross-tenant leak fix)."""
    pool, conn = mock_db_pool
    conn.fetchval = AsyncMock(return_value=NEW_ID)
    buf = RingBufferLogHandler(max_records=50, max_age_seconds=0)
    buf.emit(
        logging.LogRecord(
            name="svc",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="DB exploded for user@example.com",
            args=(),
            exc_info=None,
        )
    )
    captured_bundles: list = []

    async def _fake_persist(_pool, bundle, *, retention_days):
        captured_bundles.append(bundle)
        return NEW_ID

    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _operator()
    with (
        patch("src.api.main.db_pools", pool),
        patch("src.api.main.ocpp_server", None),
        patch("src.api.main.get_log_buffer", return_value=buf),
        patch("src.api.main.persist_support_summary", _fake_persist),
        patch("src.api.main._create_background_task", _capture_task([])),
    ):
        resp = client.post("/support/summary", headers=AUTH, json={"page": "/p"})
    assert resp.status_code == 201, resp.text
    # Operator response must NOT carry the (possibly cross-tenant) error line.
    assert "summary_text" not in resp.json()
    # The stored/engineering bundle DOES reflect the error, redacted.
    assert len(captured_bundles) == 1
    stored = captured_bundles[0]
    assert "Last error log: DB exploded" in stored.summary_text
    assert "user@example.com" not in stored.summary_text  # redacted
    assert "user@example.com" not in stored.logs_excerpt


def test_submit_redacts_page_into_stored_summary(client, mock_db_pool):
    """A token in the page query string is redacted before persist/email."""
    pool, conn = mock_db_pool
    conn.fetchval = AsyncMock(return_value=NEW_ID)
    captured_bundles: list = []

    async def _fake_persist(_pool, bundle, *, retention_days):
        captured_bundles.append(bundle)
        return NEW_ID

    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _operator()
    with (
        patch("src.api.main.db_pools", pool),
        patch("src.api.main.ocpp_server", None),
        patch("src.api.main.get_log_buffer", return_value=None),
        patch("src.api.main.persist_support_summary", _fake_persist),
        patch("src.api.main._create_background_task", _capture_task([])),
    ):
        resp = client.post(
            "/support/summary",
            headers=AUTH,
            json={"page": "/x?access_token=eyJabc.eyJdef.sigsigsigsig"},
        )
    assert resp.status_code == 201, resp.text
    assert len(captured_bundles) == 1
    assert "eyJabc.eyJdef.sigsigsigsig" not in captured_bundles[0].page


def test_submit_rejects_bad_content_type(client, mock_db_pool):
    pool, conn = mock_db_pool
    conn.fetchval = AsyncMock(return_value=NEW_ID)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _operator()
    with (
        patch("src.api.main.db_pools", pool),
        patch("src.api.main.ocpp_server", None),
        patch("src.api.main.get_log_buffer", return_value=None),
    ):
        resp = client.post(
            "/support/summary",
            headers=AUTH,
            json={
                "page": "/p",
                "screenshot_base64": IMG_B64,
                "screenshot_content_type": "application/pdf",
            },
        )
    assert resp.status_code == 400


def test_submit_rejects_oversize_screenshot(client, mock_db_pool, monkeypatch):
    monkeypatch.setenv("SUPPORT_SCREENSHOT_MAX_BYTES", "100")
    pool, conn = mock_db_pool
    conn.fetchval = AsyncMock(return_value=NEW_ID)
    big = base64.b64encode(b"x" * 400).decode()
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _operator()
    with (
        patch("src.api.main.db_pools", pool),
        patch("src.api.main.ocpp_server", None),
        patch("src.api.main.get_log_buffer", return_value=None),
    ):
        resp = client.post(
            "/support/summary",
            headers=AUTH,
            json={"page": "/p", "screenshot_base64": big, "screenshot_content_type": "image/png"},
        )
    assert resp.status_code == 413


def test_submit_unauthenticated_returns_401(client):
    app.dependency_overrides[ensure_tenant_mirrored] = _raise_401
    resp = client.post("/support/summary", headers=AUTH, json={"page": "/p"})
    assert resp.status_code == 401


def test_submit_missing_org_returns_403(client, mock_db_pool):
    pool, _ = mock_db_pool
    # operator with no organization_id claim
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: {
        "sub": str(uuid4()),
        "app_metadata": {"favonius_role": "customer_operator"},
    }
    with patch("src.api.main.db_pools", pool):
        resp = client.post("/support/summary", headers=AUTH, json={"page": "/p"})
    assert resp.status_code == 403


def test_submit_cross_tenant_depot_returns_403(client, mock_db_pool):
    pool, conn = mock_db_pool
    conn.fetchval = AsyncMock(return_value=NEW_ID)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _operator()
    denied = AsyncMock(side_effect=HTTPException(status_code=403, detail="denied"))
    with patch("src.api.main.db_pools", pool), patch("src.api.main.verify_depot_access", denied):
        resp = client.post(
            "/support/summary",
            headers=AUTH,
            json={"page": "/p", "depot_id": str(uuid4())},
        )
    assert resp.status_code == 403


def test_submit_db_unavailable_returns_503(client):
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _operator()
    with patch("src.api.main.db_pools", None):
        resp = client.post("/support/summary", headers=AUTH, json={"page": "/p"})
    assert resp.status_code == 503


# ── GET / DELETE (engineering only) ───────────────────────────────────────────


def _stored_row():
    return {
        "id": NEW_ID,
        "user_id": str(uuid4()),
        "organization_id": str(uuid4()),
        "depot_id": None,
        "page": "/depots/x/state",
        "user_note": None,
        "screenshot_content_type": "image/png",
        "screenshot_size_bytes": 42,
        "screenshot_sha256": "abc",
        "logs_excerpt": "redacted logs",
        "health_snapshot": {"tiger_cloud": "healthy", "websocket": "unknown"},
        "summary_text": "User u reported an error.",
        "delivery_status": "sent",
        "delivery_detail": None,
        "created_at": datetime(2026, 5, 29, tzinfo=timezone.utc),
        "expires_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
    }


def test_get_summary_as_admin(client, mock_db_pool):
    pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(return_value=_stored_row())
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _admin()
    with patch("src.api.main.db_pools", pool):
        resp = client.get(f"/support/summary/{NEW_ID}", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == NEW_ID
    assert body["has_screenshot"] is True
    assert "screenshot" not in body  # raw bytes never inlined


def test_get_summary_as_operator_forbidden(client, mock_db_pool):
    pool, _ = mock_db_pool
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _operator()
    with patch("src.api.main.db_pools", pool):
        resp = client.get(f"/support/summary/{NEW_ID}", headers=AUTH)
    assert resp.status_code == 403


def test_get_summary_missing_returns_404(client, mock_db_pool):
    pool, conn = mock_db_pool
    conn.fetchrow = AsyncMock(return_value=None)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _admin()
    with patch("src.api.main.db_pools", pool):
        resp = client.get(f"/support/summary/{NEW_ID}", headers=AUTH)
    assert resp.status_code == 404


def test_delete_summary_as_admin(client, mock_db_pool):
    pool, conn = mock_db_pool
    conn.fetchval = AsyncMock(return_value=NEW_ID)  # delete RETURNING id
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _admin()
    with patch("src.api.main.db_pools", pool):
        resp = client.delete(f"/support/summary/{NEW_ID}", headers=AUTH)
    assert resp.status_code == 204


def test_delete_summary_missing_returns_404(client, mock_db_pool):
    pool, conn = mock_db_pool
    conn.fetchval = AsyncMock(return_value=None)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _admin()
    with patch("src.api.main.db_pools", pool):
        resp = client.delete(f"/support/summary/{NEW_ID}", headers=AUTH)
    assert resp.status_code == 404


def test_delete_summary_as_operator_forbidden(client, mock_db_pool):
    pool, _ = mock_db_pool
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _operator()
    with patch("src.api.main.db_pools", pool):
        resp = client.delete(f"/support/summary/{NEW_ID}", headers=AUTH)
    assert resp.status_code == 403


def _raise_401():
    raise HTTPException(status_code=401, detail="Invalid token")
