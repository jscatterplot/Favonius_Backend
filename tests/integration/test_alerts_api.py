"""Integration tests for the alerts pipeline API endpoints.

Uses httpx.AsyncClient instead of FastAPI's sync TestClient so the test,
the fixture setup, and the API handlers all share one event loop and the
asyncpg pool isn't fought over by two loops.
"""

from __future__ import annotations

import json
import os
import sys
import time
from base64 import b64decode, b64encode
from hashlib import sha256
import hmac
from unittest.mock import MagicMock
from uuid import uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio


# Pyomo stub: src.api.main pulls in src.core.optimizer which imports pyomo.
if "pyomo" not in sys.modules:
    _pyomo_mock = MagicMock()
    sys.modules["pyomo"] = _pyomo_mock
    sys.modules["pyomo.environ"] = _pyomo_mock
    sys.modules["pyomo.core"] = _pyomo_mock
    sys.modules["pyomo.opt"] = _pyomo_mock


from src.api.main import app
from src.notifications import alerts as alerts_repo
from src.notifications import recipients as recipients_repo
from src.security.tenant_mirror import ensure_tenant_mirrored


pytestmark = [pytest.mark.integration, pytest.mark.database, pytest.mark.asyncio]

AUTH_HDR = {"Authorization": "Bearer test-token"}


def _user(role: str, organization_id: str | None = None) -> dict:
    meta: dict = {"favonius_role": role}
    if role != "favonius_admin":
        meta["organization_id"] = organization_id
    return {"sub": str(uuid4()), "app_metadata": meta}


def _override_user(user: dict) -> None:
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: user


@pytest_asyncio.fixture
async def db_pool():
    db_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://favonius_test:test_password@localhost:5433/favonius_test",
    )
    try:
        pool = await asyncpg.create_pool(db_url, min_size=2, max_size=8, command_timeout=10)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"test database unavailable: {exc}")
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def org_depot(db_pool):
    org_id = uuid4()
    depot_id = uuid4()
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organizations (organization_id, name) VALUES ($1, $2)",
            org_id, "API Test Org",
        )
        await conn.execute(
            """
            INSERT INTO depots (depot_id, name, latitude, longitude, max_grid_kw, organization_id)
            VALUES ($1, $2, 37.0, -122.0, 500.0, $3)
            """,
            depot_id, "API Test Depot", org_id,
        )
    yield org_id, depot_id
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM notification_alerts WHERE organization_id = $1", org_id)
        await conn.execute("DELETE FROM notification_recipients WHERE organization_id = $1", org_id)
        await conn.execute("DELETE FROM depots WHERE depot_id = $1", depot_id)
        await conn.execute("DELETE FROM organizations WHERE organization_id = $1", org_id)


@pytest_asyncio.fixture
async def client(db_pool, monkeypatch):
    """AsyncClient with db_pools wired to the test pool."""
    pools = MagicMock()
    pools.static = db_pool
    pools.ts = db_pool
    monkeypatch.setattr("src.api.main.db_pools", pools)
    monkeypatch.setattr(
        "src.security.geo_block.check_ip_blocked",
        lambda *a, **k: MagicMock(blocked=False),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


# ===========================================================================
# Acknowledge
# ===========================================================================


class TestAcknowledgeEndpoint:
    async def test_active_alert_transitions(self, client, db_pool, org_depot):
        org_id, depot_id = org_depot
        _override_user(_user("favonius_admin"))

        async with db_pool.acquire() as conn:
            alert_id = await conn.fetchval(
                """
                INSERT INTO notification_alerts (
                    organization_id, depot_id, alert_type, severity, title, dedup_key
                ) VALUES ($1, $2, 'charger_fault', 'critical', 'test', 'k1')
                RETURNING id
                """,
                org_id, depot_id,
            )

        resp = await client.post(
            f"/depots/{depot_id}/alerts/{alert_id}/acknowledge",
            headers=AUTH_HDR,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == str(alert_id)
        assert body["status"] == "acknowledged"

    async def test_unknown_alert_returns_404(self, client, org_depot):
        _, depot_id = org_depot
        _override_user(_user("favonius_admin"))

        resp = await client.post(
            f"/depots/{depot_id}/alerts/{uuid4()}/acknowledge",
            headers=AUTH_HDR,
        )
        assert resp.status_code == 404

    async def test_already_acknowledged_returns_409(self, client, db_pool, org_depot):
        org_id, depot_id = org_depot
        _override_user(_user("favonius_admin"))

        async with db_pool.acquire() as conn:
            alert_id = await conn.fetchval(
                """
                INSERT INTO notification_alerts (
                    organization_id, depot_id, alert_type, severity, title,
                    dedup_key, status
                ) VALUES ($1, $2, 'charger_fault', 'warning', 't', 'k2', 'acknowledged')
                RETURNING id
                """,
                org_id, depot_id,
            )

        resp = await client.post(
            f"/depots/{depot_id}/alerts/{alert_id}/acknowledge",
            headers=AUTH_HDR,
        )
        assert resp.status_code == 409


# ===========================================================================
# Recipients CRUD
# ===========================================================================


class TestRecipientsCRUDEndpoints:
    async def test_create_then_list(self, client, org_depot):
        org_id, _ = org_depot
        _override_user(_user("favonius_admin"))

        resp = await client.post(
            f"/admin/organizations/{org_id}/notification_recipients",
            headers=AUTH_HDR,
            json={
                "email": "ops@example.com",
                "display_name": "Ops Team",
                "alert_types": ["charger_fault"],
                "min_severity": "warning",
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["email"] == "ops@example.com"
        assert body["min_severity"] == "warning"

        list_resp = await client.get(
            f"/admin/organizations/{org_id}/notification_recipients",
            headers=AUTH_HDR,
        )
        assert list_resp.status_code == 200
        recipients = list_resp.json()["recipients"]
        assert any(r["email"] == "ops@example.com" for r in recipients)

    async def test_create_invalid_severity_returns_400(self, client, org_depot):
        org_id, _ = org_depot
        _override_user(_user("favonius_admin"))

        resp = await client.post(
            f"/admin/organizations/{org_id}/notification_recipients",
            headers=AUTH_HDR,
            json={"email": "x@x.com", "min_severity": "fatal"},
        )
        assert resp.status_code == 400

    async def test_create_duplicate_returns_409(self, client, org_depot):
        org_id, _ = org_depot
        _override_user(_user("favonius_admin"))

        for expected in (201, 409):
            resp = await client.post(
                f"/admin/organizations/{org_id}/notification_recipients",
                headers=AUTH_HDR,
                json={"email": "dup@x.com"},
            )
            assert resp.status_code == expected, resp.text

    async def test_patch_updates_fields(self, client, org_depot):
        org_id, _ = org_depot
        _override_user(_user("favonius_admin"))

        created = (await client.post(
            f"/admin/organizations/{org_id}/notification_recipients",
            headers=AUTH_HDR,
            json={"email": "p@x.com", "min_severity": "info"},
        )).json()

        patch_resp = await client.patch(
            f"/admin/organizations/{org_id}/notification_recipients/{created['id']}",
            headers=AUTH_HDR,
            json={"min_severity": "critical", "active": False},
        )
        assert patch_resp.status_code == 200, patch_resp.text
        body = patch_resp.json()
        assert body["min_severity"] == "critical"
        assert body["active"] is False

    async def test_delete_returns_204_then_404(self, client, org_depot):
        org_id, _ = org_depot
        _override_user(_user("favonius_admin"))

        created = (await client.post(
            f"/admin/organizations/{org_id}/notification_recipients",
            headers=AUTH_HDR,
            json={"email": "d@x.com"},
        )).json()
        rid = created["id"]

        first = await client.delete(
            f"/admin/organizations/{org_id}/notification_recipients/{rid}",
            headers=AUTH_HDR,
        )
        assert first.status_code == 204

        second = await client.delete(
            f"/admin/organizations/{org_id}/notification_recipients/{rid}",
            headers=AUTH_HDR,
        )
        assert second.status_code == 404

    async def test_customer_admin_other_org_returns_403(self, client, org_depot):
        org_id, _ = org_depot
        _override_user(_user("customer_admin", organization_id=str(uuid4())))
        resp = await client.get(
            f"/admin/organizations/{org_id}/notification_recipients",
            headers=AUTH_HDR,
        )
        assert resp.status_code == 403

    async def test_unprivileged_role_returns_403(self, client, org_depot):
        org_id, _ = org_depot
        _override_user(_user("customer_operator", organization_id=str(org_id)))
        resp = await client.get(
            f"/admin/organizations/{org_id}/notification_recipients",
            headers=AUTH_HDR,
        )
        assert resp.status_code == 403


# ===========================================================================
# Resend webhook
# ===========================================================================


def _sign_webhook(body: bytes, secret: str, msg_id: str, ts: int) -> str:
    signing_secret = b64decode(secret[6:] if secret.startswith("whsec_") else secret)
    payload = f"{msg_id}.{ts}.".encode() + body
    digest = b64encode(hmac.new(signing_secret, payload, sha256).digest()).decode()
    return f"v1,{digest}"


class TestResendWebhook:
    async def test_invalid_signature_returns_401(self, client, monkeypatch):
        monkeypatch.setenv("RESEND_WEBHOOK_SECRET", "whsec_dGVzdA==")
        resp = await client.post(
            "/webhooks/resend",
            content=b'{"type":"email.delivered","data":{"email_id":"x"}}',
            headers={"X-Resend-Signature": "t=1,v1=garbage"},
        )
        assert resp.status_code == 401

    async def test_missing_secret_returns_401(self, client, monkeypatch):
        monkeypatch.delenv("RESEND_WEBHOOK_SECRET", raising=False)
        body = b'{"type":"email.delivered","data":{"email_id":"x"}}'
        ts = int(time.time())
        resp = await client.post(
            "/webhooks/resend",
            content=body,
            headers={
                "X-Resend-Signature": _sign_webhook(body, "whsec_dGVzdA==", "msg_1", ts),
                "svix-id": "msg_1",
                "svix-timestamp": str(ts),
            },
        )
        assert resp.status_code == 401

    async def test_valid_signature_updates_delivery_status(
        self, client, db_pool, org_depot, monkeypatch
    ):
        org_id, depot_id = org_depot
        secret = "whsec_dGVzdA=="
        monkeypatch.setenv("RESEND_WEBHOOK_SECRET", secret)

        async with db_pool.acquire() as conn:
            alert_id = await conn.fetchval(
                """
                INSERT INTO notification_alerts (
                    organization_id, depot_id, alert_type, severity, title, dedup_key
                ) VALUES ($1, $2, 'charger_fault', 'critical', 't', 'wbk1')
                RETURNING id
                """,
                org_id, depot_id,
            )
            rec = await recipients_repo.create(
                conn, organization_id=org_id, email="hook@x.com"
            )
            await alerts_repo.record_delivery(
                conn,
                alert_id=alert_id,
                recipient_id=rec.id,
                notified_count=1,
                provider_message_id="msg_webhook_target",
            )

        body = json.dumps(
            {
                "type": "email.bounced",
                "data": {"email_id": "msg_webhook_target", "to": ["hook@x.com"]},
            }
        ).encode()
        ts = int(time.time())
        msg_id = "msg_valid"
        resp = await client.post(
            "/webhooks/resend",
            content=body,
            headers={
                "X-Resend-Signature": _sign_webhook(body, secret, msg_id, ts),
                "svix-id": msg_id,
                "svix-timestamp": str(ts),
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT status, status_detail FROM notification_deliveries "
                "WHERE provider_message_id = $1",
                "msg_webhook_target",
            )
        assert row["status"] == "bounced"
        assert "email.bounced" in str(row["status_detail"])

    async def test_unknown_event_type_acked_silently(self, client, monkeypatch):
        secret = "whsec_dGVzdA=="
        monkeypatch.setenv("RESEND_WEBHOOK_SECRET", secret)
        body = b'{"type":"email.opened","data":{"email_id":"x"}}'
        ts = int(time.time())
        msg_id = "msg_opened"
        resp = await client.post(
            "/webhooks/resend",
            content=body,
            headers={
                "X-Resend-Signature": _sign_webhook(body, secret, msg_id, ts),
                "svix-id": msg_id,
                "svix-timestamp": str(ts),
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored"


# ===========================================================================
# /depots/{id}/alerts UNION
# ===========================================================================


class TestAlertsEndpointUnion:
    async def test_response_includes_notification_alerts(
        self, client, db_pool, org_depot
    ):
        org_id, depot_id = org_depot
        _override_user(_user("favonius_admin"))

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO notification_alerts (
                    organization_id, depot_id, alert_type, severity, title, dedup_key
                ) VALUES ($1, $2, 'charger_fault', 'critical', 'CP1 fault', 'k-union')
                """,
                org_id, depot_id,
            )

        resp = await client.get(f"/depots/{depot_id}/alerts", headers=AUTH_HDR)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "notification_alerts" in body
        types = [a["alert_type"] for a in body["notification_alerts"]]
        assert "charger_fault" in types
        assert any(a["title"] == "CP1 fault" for a in body["notification_alerts"])

    async def test_resolved_alerts_omitted(self, client, db_pool, org_depot):
        org_id, depot_id = org_depot
        _override_user(_user("favonius_admin"))

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO notification_alerts (
                    organization_id, depot_id, alert_type, severity, title,
                    dedup_key, status, resolved_at
                ) VALUES ($1, $2, 'charger_fault', 'critical', 'resolved-already',
                          'k-resolved', 'resolved', NOW())
                """,
                org_id, depot_id,
            )

        resp = await client.get(f"/depots/{depot_id}/alerts", headers=AUTH_HDR)
        assert resp.status_code == 200
        titles = [a["title"] for a in resp.json()["notification_alerts"]]
        assert "resolved-already" not in titles
