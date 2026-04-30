"""AT-17: Alerts pipeline end-to-end acceptance.

Full PRD-style flow exercising every layer:
  1. Charger reports Faulted via connector_status
     → trigger fn_alerts_on_connector_status creates a notification_alerts row
     → pg_notify fires on 'notification_alerts_new'
  2. AlertDispatcher tick claims the alert
     → recipients filtered by severity/alert_type
     → email rendered + sent via FakeEmailClient
     → notification_deliveries row inserted (status='sent')
  3. Resend webhook delivers an email.delivered event
     → notification_deliveries.status updates to 'delivered'
  4. Operator POSTs /depots/{id}/alerts/{id}/acknowledge
     → notification_alerts.status='acknowledged'
  5. Charger recovers (connector_status='Available')
     → trigger resolves the alert (status='resolved')
  6. GET /depots/{id}/alerts no longer lists the alert in active set

The whole flow runs against a real Postgres + a FakeEmailClient. It is
the canonical reference for "the alerts pipeline works" that future
refactors must keep green.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import sys
import time
from base64 import b64decode, b64encode
from hashlib import sha256
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest
import pytest_asyncio


# Pyomo stub — same as integration conftest.
if "pyomo" not in sys.modules:
    _pyomo_mock = MagicMock()
    sys.modules["pyomo"] = _pyomo_mock
    sys.modules["pyomo.environ"] = _pyomo_mock
    sys.modules["pyomo.core"] = _pyomo_mock
    sys.modules["pyomo.opt"] = _pyomo_mock


from src.api.main import app
from src.notifications import alerts as alerts_repo
from src.notifications import recipients as recipients_repo
from src.notifications.dispatcher import AlertDispatcher
from src.notifications.email_client import FakeEmailClient
from src.security.tenant_mirror import ensure_tenant_mirrored


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.acceptance,
    pytest.mark.database,
    pytest.mark.asyncio,
]

AUTH_HDR = {"Authorization": "Bearer test-token"}


def _admin_user() -> dict:
    return {
        "sub": str(uuid4()),
        "app_metadata": {"favonius_role": "favonius_admin"},
    }


def _sign(body: bytes, secret: str, ts: int) -> str:
    msg_id = "msg_e2e"
    secret_b64 = secret[6:] if secret.startswith("whsec_") else secret
    digest = b64encode(hmac.new(b64decode(secret_b64), f"{msg_id}.{ts}.".encode() + body, sha256).digest()).decode()
    return f"v1,{digest}"


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
async def world(db_pool):
    """Provision org + depot + charger + recipient. Yield IDs and cleanup."""
    org_id = uuid4()
    depot_id = uuid4()
    ocpp_id = f"e2e_{uuid4().hex[:8]}"

    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO organizations (organization_id, name) VALUES ($1, $2)",
            org_id, "AT-17 Org",
        )
        await conn.execute(
            """
            INSERT INTO depots (depot_id, name, latitude, longitude, max_grid_kw, organization_id)
            VALUES ($1, $2, 37.0, -122.0, 500.0, $3)
            """,
            depot_id, "AT-17 Depot", org_id,
        )
        await conn.execute(
            "INSERT INTO chargers (depot_id, ocpp_id, rated_kw) VALUES ($1, $2, $3)",
            depot_id, ocpp_id, 50.0,
        )
        recipient = await recipients_repo.create(
            conn, organization_id=org_id, email="ops@example.com"
        )

    yield {
        "org_id": org_id,
        "depot_id": depot_id,
        "ocpp_id": ocpp_id,
        "recipient_id": recipient.id,
    }

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM notification_alerts WHERE organization_id = $1", org_id)
        await conn.execute("DELETE FROM notification_recipients WHERE organization_id = $1", org_id)
        await conn.execute("DELETE FROM connector_status WHERE station_id = $1", ocpp_id)
        await conn.execute("DELETE FROM chargers WHERE ocpp_id = $1", ocpp_id)
        await conn.execute("DELETE FROM depots WHERE depot_id = $1", depot_id)
        await conn.execute("DELETE FROM organizations WHERE organization_id = $1", org_id)


@pytest_asyncio.fixture
async def api_client(db_pool, monkeypatch):
    pools = MagicMock()
    pools.static = db_pool
    pools.ts = db_pool
    monkeypatch.setattr("src.api.main.db_pools", pools)
    monkeypatch.setattr(
        "src.security.geo_block.check_ip_blocked",
        lambda *a, **k: MagicMock(blocked=False),
    )
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: _admin_user()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def dispatcher(db_pool):
    fake = FakeEmailClient()
    disp = AlertDispatcher(
        pool=db_pool,
        email_client=fake,
        default_from="alerts@favonius.energy",
        poll_interval_s=300.0,  # tests drive ticks manually
        resend_interval_s=3600,
        batch_size=50,
    )
    yield disp, fake
    await disp.stop()


class TestAlertsPipelineE2E:
    async def test_full_lifecycle_fault_to_email_to_ack_to_resolve(
        self, db_pool, api_client, dispatcher, world, monkeypatch
    ):
        org_id = world["org_id"]
        depot_id = world["depot_id"]
        ocpp_id = world["ocpp_id"]
        recipient_id = world["recipient_id"]
        disp, fake = dispatcher

        # ── Step 1: charger fault → trigger creates alert ──────────────────
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO connector_status (station_id, connector_id, status, error_code)
                VALUES ($1, 1, 'Faulted', 'PowerMeterFailure')
                """,
                ocpp_id,
            )
            alert_row = await conn.fetchrow(
                "SELECT id, severity, status FROM notification_alerts WHERE dedup_key = $1",
                f"charger_fault:{ocpp_id}:1",
            )
        assert alert_row is not None, "trigger must create notification_alerts row"
        alert_id = alert_row["id"]
        assert alert_row["severity"] == "critical"
        assert alert_row["status"] == "active"

        # ── Step 2: dispatcher tick → email + delivery row ─────────────────
        processed = await disp._tick()
        assert processed == 1
        assert len(fake.sent) == 1
        assert fake.sent[0].to == "ops@example.com"
        assert "PowerMeterFailure" in fake.sent[0].html

        async with db_pool.acquire() as conn:
            delivery = await conn.fetchrow(
                """
                SELECT status, provider_message_id
                  FROM notification_deliveries
                 WHERE alert_id = $1 AND recipient_id = $2
                """,
                alert_id, recipient_id,
            )
        assert delivery is not None
        assert delivery["status"] == "sent"
        provider_msg_id = delivery["provider_message_id"]
        assert provider_msg_id and provider_msg_id.startswith("fake-")

        # ── Step 3: Resend webhook → delivery flips to 'delivered' ─────────
        secret = "whsec_e2e"
        monkeypatch.setenv("RESEND_WEBHOOK_SECRET", secret)
        body = json.dumps(
            {
                "type": "email.delivered",
                "data": {"email_id": provider_msg_id, "to": ["ops@example.com"]},
            }
        ).encode()
        ts = int(time.time())
        webhook_resp = await api_client.post(
            "/webhooks/resend",
            content=body,
            headers={
                "X-Resend-Signature": _sign(body, secret, ts),
                "svix-id": "msg_e2e",
                "svix-timestamp": str(ts),
            },
        )
        assert webhook_resp.status_code == 200
        assert webhook_resp.json()["status"] == "ok"

        async with db_pool.acquire() as conn:
            updated = await conn.fetchrow(
                "SELECT status FROM notification_deliveries WHERE provider_message_id = $1",
                provider_msg_id,
            )
        assert updated["status"] == "delivered"

        # ── Step 4: operator acknowledges via API ──────────────────────────
        ack_resp = await api_client.post(
            f"/depots/{depot_id}/alerts/{alert_id}/acknowledge",
            headers=AUTH_HDR,
        )
        assert ack_resp.status_code == 200
        assert ack_resp.json()["status"] == "acknowledged"

        # ── Step 5: charger recovers → trigger resolves ────────────────────
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO connector_status (station_id, connector_id, status) "
                "VALUES ($1, 1, 'Available')",
                ocpp_id,
            )
            resolved = await conn.fetchrow(
                "SELECT status, resolved_at FROM notification_alerts WHERE id = $1",
                alert_id,
            )
        assert resolved["status"] == "resolved"
        assert resolved["resolved_at"] is not None

        # ── Step 6: GET /depots/{id}/alerts no longer surfaces it ──────────
        list_resp = await api_client.get(f"/depots/{depot_id}/alerts", headers=AUTH_HDR)
        assert list_resp.status_code == 200
        active_alert_ids = {a["id"] for a in list_resp.json()["notification_alerts"]}
        assert str(alert_id) not in active_alert_ids

        # ── Sanity: still exactly one email sent across the whole flow ─────
        assert len(fake.sent) == 1, "no extra email after ack or resolve"

    async def test_recurring_fault_dedupes_and_does_not_email_again(
        self, db_pool, dispatcher, world
    ):
        """A repeating Faulted row must UPSERT the same alert and the
        dispatcher must respect the resend window."""
        ocpp_id = world["ocpp_id"]
        disp, fake = dispatcher

        async with db_pool.acquire() as conn:
            for _ in range(3):
                await conn.execute(
                    "INSERT INTO connector_status (station_id, connector_id, status, error_code) "
                    "VALUES ($1, 1, 'Faulted', 'OverCurrentFailure')",
                    ocpp_id,
                )

        await disp._tick()
        await disp._tick()  # second tick within resend window — must be no-op

        assert len(fake.sent) == 1, "exactly one email despite 3 fault inserts and 2 ticks"

        async with db_pool.acquire() as conn:
            row_count = await conn.fetchval(
                "SELECT COUNT(*) FROM notification_alerts WHERE dedup_key = $1",
                f"charger_fault:{ocpp_id}:1",
            )
        assert row_count == 1, "all 3 Faulted inserts collapse into one alert"

    async def test_critical_only_recipient_skipped_for_warning_alert(
        self, db_pool, dispatcher, world
    ):
        """Severity gating: an Unavailable status produces a warning alert,
        which a critical-only recipient must not receive."""
        org_id = world["org_id"]
        ocpp_id = world["ocpp_id"]
        disp, fake = dispatcher

        async with db_pool.acquire() as conn:
            from src.notifications.severity import Severity

            await recipients_repo.create(
                conn, organization_id=org_id, email="critical-only@x.com",
                min_severity=Severity.CRITICAL,
            )
            await conn.execute(
                "INSERT INTO connector_status (station_id, connector_id, status) "
                "VALUES ($1, 1, 'Unavailable')",
                ocpp_id,
            )

        await disp._tick()

        # Existing default recipient (warning min) gets the email; critical-only does not.
        addressed = {m.to for m in fake.sent}
        assert "ops@example.com" in addressed
        assert "critical-only@x.com" not in addressed
