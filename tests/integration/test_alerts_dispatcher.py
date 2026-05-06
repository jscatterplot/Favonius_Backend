"""Integration tests for AlertDispatcher.

Exercises the full claim → recipients → render → send → record cycle
against a real Postgres + the connector_status trigger from migration 022.
The email side is faked via FakeEmailClient.
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from src.notifications import alerts as alerts_repo
from src.notifications import recipients as recipients_repo
from src.notifications.dispatcher import AlertDispatcher
from src.notifications.email_client import DeliveryResult, FakeEmailClient
from src.notifications.severity import Severity


pytestmark = [pytest.mark.integration, pytest.mark.database]


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
async def org_depot_charger(db_pool):
    """Yield (org_id, depot_id, ocpp_id) as plain UUIDs.

    After migration 029 the static shadow tables are gone. Tenant context is
    carried directly in connector_status.organization_id / depot_id, so no
    shadow rows are needed.
    """
    org_id = uuid4()
    depot_id = uuid4()
    ocpp_id = f"disp_{uuid4().hex[:8]}"
    yield org_id, depot_id, ocpp_id
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM notification_alerts WHERE organization_id = $1", org_id)
        await conn.execute("DELETE FROM notification_recipients WHERE organization_id = $1", org_id)
        await conn.execute("DELETE FROM connector_status WHERE station_id = $1", ocpp_id)


@pytest_asyncio.fixture
async def dispatcher(db_pool):
    fake = FakeEmailClient()
    disp = AlertDispatcher(
        pool=db_pool,
        email_client=fake,
        default_from="alerts@favonius.energy",
        poll_interval_s=60.0,  # tests drive ticks manually
        resend_interval_s=3600,
        batch_size=50,
    )
    yield disp, fake
    await disp.stop()


class TestSingleAlertFlow:
    @pytest.mark.asyncio
    async def test_alert_with_one_recipient_dispatches_one_email(
        self, db_pool, dispatcher, org_depot_charger
    ):
        org_id, _, ocpp_id = org_depot_charger
        disp, fake = dispatcher

        async with db_pool.acquire() as conn:
            await recipients_repo.create(
                conn, organization_id=org_id, email="ops@example.com",
                min_severity=Severity.WARNING,
            )
            await conn.execute(
                "INSERT INTO connector_status "
                "(station_id, connector_id, status, error_code, organization_id) "
                "VALUES ($1, 1, 'Faulted', 'PowerMeterFailure', $2)",
                ocpp_id, org_id,
            )

        processed = await disp._tick()
        assert processed == 1
        assert len(fake.sent) == 1
        msg = fake.sent[0]
        assert msg.to == "ops@example.com"
        assert "CRITICAL" in msg.subject
        assert ocpp_id in msg.html

        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT a.last_notified_count, a.last_notified_at,
                       d.status as delivery_status, d.provider_message_id
                  FROM notification_alerts a
                  JOIN notification_deliveries d ON d.alert_id = a.id
                 WHERE a.dedup_key = $1
                """,
                f"charger_fault:{ocpp_id}:1",
            )
            assert row["last_notified_count"] == 1
            assert row["last_notified_at"] is not None
            assert row["delivery_status"] == "sent"
            assert row["provider_message_id"].startswith("fake-")


class TestRecipientFanout:
    @pytest.mark.asyncio
    async def test_multiple_recipients_each_get_one_email(
        self, db_pool, dispatcher, org_depot_charger
    ):
        org_id, _, ocpp_id = org_depot_charger
        disp, fake = dispatcher

        async with db_pool.acquire() as conn:
            for email in ("a@x.com", "b@x.com", "c@x.com"):
                await recipients_repo.create(conn, organization_id=org_id, email=email)
            await conn.execute(
                "INSERT INTO connector_status (station_id, connector_id, status, organization_id) "
                "VALUES ($1, 1, 'Faulted', $2)",
                ocpp_id, org_id,
            )

        await disp._tick()

        recipients_addressed = sorted(m.to for m in fake.sent)
        assert recipients_addressed == ["a@x.com", "b@x.com", "c@x.com"]

        async with db_pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM notification_deliveries d "
                "JOIN notification_alerts a ON a.id = d.alert_id "
                "WHERE a.organization_id = $1",
                org_id,
            )
            assert count == 3


class TestSeverityGating:
    @pytest.mark.asyncio
    async def test_warning_alert_skips_critical_only_recipients(
        self, db_pool, dispatcher, org_depot_charger
    ):
        org_id, _, ocpp_id = org_depot_charger
        disp, fake = dispatcher

        async with db_pool.acquire() as conn:
            await recipients_repo.create(
                conn, organization_id=org_id, email="warn@x.com",
                min_severity=Severity.WARNING,
            )
            await recipients_repo.create(
                conn, organization_id=org_id, email="crit@x.com",
                min_severity=Severity.CRITICAL,
            )
            # Unavailable → warning per the trigger
            await conn.execute(
                "INSERT INTO connector_status (station_id, connector_id, status, organization_id) "
                "VALUES ($1, 1, 'Unavailable', $2)",
                ocpp_id, org_id,
            )

        await disp._tick()
        assert {m.to for m in fake.sent} == {"warn@x.com"}


class TestAlertTypeFilter:
    @pytest.mark.asyncio
    async def test_specific_alert_type_only_matches(
        self, db_pool, dispatcher, org_depot_charger
    ):
        org_id, _, ocpp_id = org_depot_charger
        disp, fake = dispatcher

        async with db_pool.acquire() as conn:
            # Wildcard recipient
            await recipients_repo.create(
                conn, organization_id=org_id, email="all@x.com",
                alert_types=("*",),
            )
            # Type-specific recipient
            await recipients_repo.create(
                conn, organization_id=org_id, email="faults@x.com",
                alert_types=("charger_fault",),
            )
            # Other-type-specific recipient — should NOT receive
            await recipients_repo.create(
                conn, organization_id=org_id, email="opt@x.com",
                alert_types=("optimization_failed",),
            )
            await conn.execute(
                "INSERT INTO connector_status (station_id, connector_id, status, organization_id) "
                "VALUES ($1, 1, 'Faulted', $2)",
                ocpp_id, org_id,
            )

        await disp._tick()
        assert {m.to for m in fake.sent} == {"all@x.com", "faults@x.com"}


class TestResendInterval:
    @pytest.mark.asyncio
    async def test_within_window_does_not_re_send(
        self, db_pool, dispatcher, org_depot_charger
    ):
        org_id, _, ocpp_id = org_depot_charger
        disp, fake = dispatcher

        async with db_pool.acquire() as conn:
            await recipients_repo.create(conn, organization_id=org_id, email="o@x.com")
            await conn.execute(
                "INSERT INTO connector_status (station_id, connector_id, status, organization_id) "
                "VALUES ($1, 1, 'Faulted', $2)",
                ocpp_id, org_id,
            )

        first = await disp._tick()
        second = await disp._tick()

        assert first == 1
        assert second == 0
        assert len(fake.sent) == 1, "second tick must not re-notify within resend window"


class TestNoRecipients:
    @pytest.mark.asyncio
    async def test_alert_with_no_recipients_marks_notified(
        self, db_pool, dispatcher, org_depot_charger
    ):
        org_id, _, ocpp_id = org_depot_charger
        disp, fake = dispatcher

        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO connector_status (station_id, connector_id, status, organization_id) "
                "VALUES ($1, 1, 'Faulted', $2)",
                ocpp_id, org_id,
            )

        await disp._tick()

        assert fake.sent == []
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT last_notified_count, last_notified_at FROM notification_alerts "
                "WHERE dedup_key = $1",
                f"charger_fault:{ocpp_id}:1",
            )
            assert row["last_notified_count"] == 1
            assert row["last_notified_at"] is not None


class TestSendFailure:
    @pytest.mark.asyncio
    async def test_failed_send_records_failed_delivery(
        self, db_pool, org_depot_charger
    ):
        org_id, _, ocpp_id = org_depot_charger

        def script(_msg, _count):
            return DeliveryResult(
                status="failed",
                provider_message_id=None,
                detail={"error": "client_error", "status_code": 422},
            )

        fake = FakeEmailClient(script=script)
        disp = AlertDispatcher(
            pool=db_pool,
            email_client=fake,
            default_from="alerts@favonius.energy",
            poll_interval_s=60.0,
            resend_interval_s=3600,
            batch_size=10,
        )
        try:
            async with db_pool.acquire() as conn:
                await recipients_repo.create(conn, organization_id=org_id, email="o@x.com")
                await conn.execute(
                    "INSERT INTO connector_status "
                    "(station_id, connector_id, status, organization_id) "
                    "VALUES ($1, 1, 'Faulted', $2)",
                    ocpp_id, org_id,
                )

            await disp._tick()

            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT d.status, d.status_detail, d.provider_message_id
                      FROM notification_deliveries d
                      JOIN notification_alerts a ON a.id = d.alert_id
                     WHERE a.dedup_key = $1
                    """,
                    f"charger_fault:{ocpp_id}:1",
                )
                assert row["status"] == "failed"
                assert row["provider_message_id"] is None
                assert "422" in str(row["status_detail"])
        finally:
            await disp.stop()


class TestNotifyWakeup:
    @pytest.mark.asyncio
    async def test_pg_notify_wakes_dispatcher(
        self, db_pool, org_depot_charger
    ):
        """End-to-end NOTIFY → dispatcher wakeup → email sent."""
        org_id, _, ocpp_id = org_depot_charger

        fake = FakeEmailClient()
        disp = AlertDispatcher(
            pool=db_pool,
            email_client=fake,
            default_from="alerts@favonius.energy",
            poll_interval_s=300.0,  # very long, so polling can't be the cause
            resend_interval_s=3600,
            batch_size=10,
        )
        try:
            async with db_pool.acquire() as conn:
                await recipients_repo.create(conn, organization_id=org_id, email="o@x.com")

            await disp.start()
            # Let the listener actually subscribe before we fire the trigger.
            await asyncio.sleep(0.1)

            async with db_pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO connector_status "
                    "(station_id, connector_id, status, organization_id) "
                    "VALUES ($1, 1, 'Faulted', $2)",
                    ocpp_id, org_id,
                )

            for _ in range(20):
                if fake.sent:
                    break
                await asyncio.sleep(0.1)

            assert fake.sent, "dispatcher did not wake within 2s"
            assert fake.sent[0].to == "o@x.com"
        finally:
            await disp.stop()


class TestCurrentlySendingDedup:
    @pytest.mark.asyncio
    async def test_in_flight_alert_is_skipped_by_concurrent_tick(
        self, db_pool, dispatcher, org_depot_charger
    ):
        """Two ticks racing must not double-process the same alert id."""
        org_id, _, ocpp_id = org_depot_charger
        disp, fake = dispatcher

        async with db_pool.acquire() as conn:
            await recipients_repo.create(conn, organization_id=org_id, email="o@x.com")
            await conn.execute(
                "INSERT INTO connector_status (station_id, connector_id, status, organization_id) "
                "VALUES ($1, 1, 'Faulted', $2)",
                ocpp_id, org_id,
            )

        # Run two ticks concurrently. The second one must observe an empty
        # filtered batch because the first added the alert id to
        # _currently_sending. Subtle: the claim query may still return the
        # alert in both ticks, but the filter dedup catches it.
        results = await asyncio.gather(disp._tick(), disp._tick())
        # Combined work should be 1 alert / 1 email.
        assert sum(results) >= 1
        assert len(fake.sent) == 1
