"""Integration tests for src.notifications.alerts and recipients.

Exercises the repository functions against a real Postgres so the SQL,
ON CONFLICT semantics, and array predicates are validated end to end.
Skipped when TEST_DATABASE_URL is unreachable.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from src.notifications import alerts as alerts_repo
from src.notifications import recipients as recipients_repo
from src.notifications.severity import Severity


pytestmark = [pytest.mark.integration, pytest.mark.database]


@pytest_asyncio.fixture
async def db_pool():
    db_url = os.getenv(
        "TEST_DATABASE_URL",
        "postgresql://favonius_test:test_password@localhost:5433/favonius_test",
    )
    try:
        pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4, command_timeout=10)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(f"test database unavailable: {exc}")
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def org_and_depot(db_pool):
    """Yield (org_id, depot_id) as plain UUIDs.

    After migration 029 the static shadow tables are gone; organization_id and
    depot_id are unvalidated UUID references in notification_alerts.
    """
    org_id = uuid4()
    depot_id = uuid4()
    yield org_id, depot_id
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM notification_alerts WHERE organization_id = $1", org_id)
        await conn.execute("DELETE FROM notification_recipients WHERE organization_id = $1", org_id)


async def _insert_alert(
    conn,
    *,
    organization_id: UUID,
    depot_id: UUID,
    dedup_key: str,
    severity: str = "warning",
    alert_type: str = "charger_fault",
    status: str = "active",
    last_notified_at: datetime | None = None,
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO notification_alerts (
            organization_id, depot_id, alert_type, severity, title, dedup_key,
            status, last_notified_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        organization_id,
        depot_id,
        alert_type,
        severity,
        f"test alert for {dedup_key}",
        dedup_key,
        status,
        last_notified_at,
    )


# ===========================================================================
# alerts repository
# ===========================================================================


class TestClaimPendingAlerts:
    @pytest.mark.asyncio
    async def test_returns_unnotified_active_alerts_first(self, db_pool, org_and_depot):
        org_id, depot_id = org_and_depot
        async with db_pool.acquire() as conn:
            await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="a", severity="warning")
            await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="b", severity="critical")

            claimed = await alerts_repo.claim_pending_alerts(conn, resend_interval_s=3600, limit=10)
            keys = [a.dedup_key for a in claimed if a.organization_id == org_id]

            # severity_level DESC: critical (3) before warning (2)
            assert keys == ["b", "a"]
            assert all(a.last_notified_at is None for a in claimed if a.organization_id == org_id)

    @pytest.mark.asyncio
    async def test_skips_recently_notified(self, db_pool, org_and_depot):
        org_id, depot_id = org_and_depot
        recent = datetime.now(timezone.utc) - timedelta(seconds=60)
        async with db_pool.acquire() as conn:
            await _insert_alert(
                conn, organization_id=org_id, depot_id=depot_id, dedup_key="recent",
                last_notified_at=recent,
            )
            await _insert_alert(
                conn, organization_id=org_id, depot_id=depot_id, dedup_key="fresh",
            )
            claimed = await alerts_repo.claim_pending_alerts(conn, resend_interval_s=3600, limit=10)
            keys = [a.dedup_key for a in claimed if a.organization_id == org_id]
            assert "fresh" in keys
            assert "recent" not in keys, "alert notified within resend window should be skipped"

    @pytest.mark.asyncio
    async def test_returns_after_resend_interval_elapses(self, db_pool, org_and_depot):
        org_id, depot_id = org_and_depot
        old = datetime.now(timezone.utc) - timedelta(hours=2)
        async with db_pool.acquire() as conn:
            await _insert_alert(
                conn, organization_id=org_id, depot_id=depot_id, dedup_key="old",
                last_notified_at=old,
            )
            claimed = await alerts_repo.claim_pending_alerts(conn, resend_interval_s=3600, limit=10)
            keys = [a.dedup_key for a in claimed if a.organization_id == org_id]
            assert "old" in keys

    @pytest.mark.asyncio
    async def test_skips_resolved_and_acknowledged(self, db_pool, org_and_depot):
        org_id, depot_id = org_and_depot
        async with db_pool.acquire() as conn:
            await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="acked", status="acknowledged")
            await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="resolved", status="resolved")
            await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="active", status="active")

            claimed = await alerts_repo.claim_pending_alerts(conn, resend_interval_s=3600, limit=10)
            keys = [a.dedup_key for a in claimed if a.organization_id == org_id]
            assert keys == ["active"]


class TestMarkNotified:
    @pytest.mark.asyncio
    async def test_bumps_count_and_timestamp(self, db_pool, org_and_depot):
        org_id, depot_id = org_and_depot
        async with db_pool.acquire() as conn:
            alert_id = await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="x")

            new_count_1 = await alerts_repo.mark_notified(conn, alert_id)
            assert new_count_1 == 1

            new_count_2 = await alerts_repo.mark_notified(conn, alert_id)
            assert new_count_2 == 2

            row = await conn.fetchrow(
                "SELECT last_notified_at, last_notified_count FROM notification_alerts WHERE id=$1",
                alert_id,
            )
            assert row["last_notified_count"] == 2
            assert row["last_notified_at"] is not None

    @pytest.mark.asyncio
    async def test_missing_alert_raises(self, db_pool):
        async with db_pool.acquire() as conn:
            with pytest.raises(ValueError, match="not found"):
                await alerts_repo.mark_notified(conn, uuid4())


class TestListForDepot:
    @pytest.mark.asyncio
    async def test_orders_by_severity_then_recency(self, db_pool, org_and_depot):
        org_id, depot_id = org_and_depot
        async with db_pool.acquire() as conn:
            await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="a", severity="info")
            await asyncio.sleep(0.01)
            await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="b", severity="critical")
            await asyncio.sleep(0.01)
            await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="c", severity="warning")

            rows = await alerts_repo.list_for_depot(conn, depot_id)
            keys = [a.dedup_key for a in rows]
            assert keys == ["b", "c", "a"]

    @pytest.mark.asyncio
    async def test_default_excludes_resolved(self, db_pool, org_and_depot):
        org_id, depot_id = org_and_depot
        async with db_pool.acquire() as conn:
            await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="resolved", status="resolved")
            await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="active", status="active")
            rows = await alerts_repo.list_for_depot(conn, depot_id)
            assert [a.dedup_key for a in rows] == ["active"]

    @pytest.mark.asyncio
    async def test_explicit_statuses_filter(self, db_pool, org_and_depot):
        org_id, depot_id = org_and_depot
        async with db_pool.acquire() as conn:
            await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="r", status="resolved")
            await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="a", status="active")
            rows = await alerts_repo.list_for_depot(conn, depot_id, statuses=("resolved",))
            assert [a.dedup_key for a in rows] == ["r"]


class TestAcknowledge:
    @pytest.mark.asyncio
    async def test_active_alert_transitions_to_acknowledged(self, db_pool, org_and_depot):
        org_id, depot_id = org_and_depot
        user_id = uuid4()
        async with db_pool.acquire() as conn:
            alert_id = await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="ack-me")

            updated = await alerts_repo.acknowledge(conn, alert_id, user_id=user_id)
            assert updated is not None
            assert updated.status == "acknowledged"

            row = await conn.fetchrow(
                "SELECT acknowledged_at, acknowledged_by FROM notification_alerts WHERE id=$1",
                alert_id,
            )
            assert row["acknowledged_at"] is not None
            assert row["acknowledged_by"] == user_id

    @pytest.mark.asyncio
    async def test_resolved_alert_returns_none(self, db_pool, org_and_depot):
        org_id, depot_id = org_and_depot
        async with db_pool.acquire() as conn:
            alert_id = await _insert_alert(
                conn, organization_id=org_id, depot_id=depot_id, dedup_key="r2", status="resolved"
            )
            result = await alerts_repo.acknowledge(conn, alert_id, user_id=uuid4())
            assert result is None

    @pytest.mark.asyncio
    async def test_already_acknowledged_returns_none(self, db_pool, org_and_depot):
        org_id, depot_id = org_and_depot
        async with db_pool.acquire() as conn:
            alert_id = await _insert_alert(
                conn, organization_id=org_id, depot_id=depot_id, dedup_key="a2", status="acknowledged"
            )
            result = await alerts_repo.acknowledge(conn, alert_id, user_id=uuid4())
            assert result is None

    @pytest.mark.asyncio
    async def test_missing_alert_returns_none(self, db_pool):
        async with db_pool.acquire() as conn:
            result = await alerts_repo.acknowledge(conn, uuid4(), user_id=uuid4())
            assert result is None


class TestDeliveriesLedger:
    @pytest_asyncio.fixture
    async def alert_and_recipient(self, db_pool, org_and_depot):
        org_id, depot_id = org_and_depot
        async with db_pool.acquire() as conn:
            alert_id = await _insert_alert(conn, organization_id=org_id, depot_id=depot_id, dedup_key="d")
            recipient = await recipients_repo.create(
                conn, organization_id=org_id, email="ops@example.com"
            )
        yield alert_id, recipient.id

    @pytest.mark.asyncio
    async def test_record_delivery_inserts(self, db_pool, alert_and_recipient):
        alert_id, recipient_id = alert_and_recipient
        async with db_pool.acquire() as conn:
            inserted = await alerts_repo.record_delivery(
                conn,
                alert_id=alert_id,
                recipient_id=recipient_id,
                notified_count=1,
                provider_message_id="msg-abc",
            )
            assert inserted is True

    @pytest.mark.asyncio
    async def test_record_delivery_dedup_on_same_count(self, db_pool, alert_and_recipient):
        alert_id, recipient_id = alert_and_recipient
        async with db_pool.acquire() as conn:
            first = await alerts_repo.record_delivery(
                conn, alert_id=alert_id, recipient_id=recipient_id, notified_count=1,
                provider_message_id="msg-1",
            )
            second = await alerts_repo.record_delivery(
                conn, alert_id=alert_id, recipient_id=recipient_id, notified_count=1,
                provider_message_id="msg-2",
            )
            assert first is True
            assert second is False, "ON CONFLICT DO NOTHING should report no insert"

    @pytest.mark.asyncio
    async def test_different_notified_counts_create_separate_rows(
        self, db_pool, alert_and_recipient
    ):
        alert_id, recipient_id = alert_and_recipient
        async with db_pool.acquire() as conn:
            await alerts_repo.record_delivery(
                conn, alert_id=alert_id, recipient_id=recipient_id, notified_count=1,
                provider_message_id="m1",
            )
            await alerts_repo.record_delivery(
                conn, alert_id=alert_id, recipient_id=recipient_id, notified_count=2,
                provider_message_id="m2",
            )
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM notification_deliveries WHERE alert_id=$1",
                alert_id,
            )
            assert count == 2

    @pytest.mark.asyncio
    async def test_update_delivery_status_by_provider_id(self, db_pool, alert_and_recipient):
        alert_id, recipient_id = alert_and_recipient
        async with db_pool.acquire() as conn:
            await alerts_repo.record_delivery(
                conn, alert_id=alert_id, recipient_id=recipient_id, notified_count=1,
                provider_message_id="webhook-target",
            )
            updated = await alerts_repo.update_delivery_status(
                conn,
                provider_message_id="webhook-target",
                status="bounced",
                status_detail={"reason": "user not found"},
            )
            assert updated is True

            row = await conn.fetchrow(
                "SELECT status, status_detail, provider_updated_at FROM notification_deliveries WHERE provider_message_id=$1",
                "webhook-target",
            )
            assert row["status"] == "bounced"
            assert row["provider_updated_at"] is not None

    @pytest.mark.asyncio
    async def test_update_delivery_status_unknown_id_returns_false(self, db_pool):
        async with db_pool.acquire() as conn:
            result = await alerts_repo.update_delivery_status(
                conn, provider_message_id="does-not-exist", status="bounced"
            )
            assert result is False


# ===========================================================================
# recipients repository
# ===========================================================================


class TestRecipientsListForAlert:
    @pytest.mark.asyncio
    async def test_severity_threshold_gating(self, db_pool, org_and_depot):
        org_id, _ = org_and_depot
        async with db_pool.acquire() as conn:
            await recipients_repo.create(
                conn, organization_id=org_id, email="info@x.com", min_severity=Severity.INFO
            )
            await recipients_repo.create(
                conn, organization_id=org_id, email="warn@x.com", min_severity=Severity.WARNING
            )
            await recipients_repo.create(
                conn, organization_id=org_id, email="crit@x.com", min_severity=Severity.CRITICAL
            )

            critical_recips = await recipients_repo.list_for_alert(
                conn, organization_id=org_id, alert_type="charger_fault", severity=Severity.CRITICAL
            )
            assert {r.email for r in critical_recips} == {"info@x.com", "warn@x.com", "crit@x.com"}

            warning_recips = await recipients_repo.list_for_alert(
                conn, organization_id=org_id, alert_type="charger_fault", severity=Severity.WARNING
            )
            assert {r.email for r in warning_recips} == {"info@x.com", "warn@x.com"}

            info_recips = await recipients_repo.list_for_alert(
                conn, organization_id=org_id, alert_type="charger_fault", severity=Severity.INFO
            )
            assert {r.email for r in info_recips} == {"info@x.com"}

    @pytest.mark.asyncio
    async def test_alert_type_filter_with_wildcard(self, db_pool, org_and_depot):
        org_id, _ = org_and_depot
        async with db_pool.acquire() as conn:
            await recipients_repo.create(
                conn, organization_id=org_id, email="all@x.com", alert_types=("*",)
            )
            await recipients_repo.create(
                conn, organization_id=org_id, email="faults@x.com", alert_types=("charger_fault",)
            )
            await recipients_repo.create(
                conn, organization_id=org_id, email="opt@x.com", alert_types=("optimization_failed",)
            )

            for_faults = await recipients_repo.list_for_alert(
                conn, organization_id=org_id, alert_type="charger_fault", severity=Severity.WARNING
            )
            assert {r.email for r in for_faults} == {"all@x.com", "faults@x.com"}

            for_opt = await recipients_repo.list_for_alert(
                conn, organization_id=org_id, alert_type="optimization_failed", severity=Severity.WARNING
            )
            assert {r.email for r in for_opt} == {"all@x.com", "opt@x.com"}

    @pytest.mark.asyncio
    async def test_excludes_inactive_recipients(self, db_pool, org_and_depot):
        org_id, _ = org_and_depot
        async with db_pool.acquire() as conn:
            r = await recipients_repo.create(
                conn, organization_id=org_id, email="off@x.com"
            )
            await recipients_repo.update(
                conn, r.id, organization_id=org_id, active=False
            )
            results = await recipients_repo.list_for_alert(
                conn, organization_id=org_id, alert_type="charger_fault", severity=Severity.CRITICAL
            )
            assert all(r.email != "off@x.com" for r in results)

    @pytest.mark.asyncio
    async def test_other_org_recipients_not_returned(self, db_pool, org_and_depot):
        org_id, _ = org_and_depot
        # After migration 029 organizations shadow is dropped; other_org_id is an
        # unvalidated UUID reference (no FK on notification_recipients.organization_id).
        other_org_id = uuid4()
        async with db_pool.acquire() as conn:
            try:
                await recipients_repo.create(
                    conn, organization_id=other_org_id, email="other@x.com"
                )
                await recipients_repo.create(
                    conn, organization_id=org_id, email="mine@x.com"
                )
                results = await recipients_repo.list_for_alert(
                    conn, organization_id=org_id, alert_type="charger_fault", severity=Severity.WARNING
                )
                assert {r.email for r in results} == {"mine@x.com"}
            finally:
                await conn.execute(
                    "DELETE FROM notification_recipients WHERE organization_id = $1",
                    other_org_id,
                )


class TestRecipientsCRUD:
    @pytest.mark.asyncio
    async def test_create_then_get_by_id(self, db_pool, org_and_depot):
        org_id, _ = org_and_depot
        async with db_pool.acquire() as conn:
            created = await recipients_repo.create(
                conn,
                organization_id=org_id,
                email="op@x.com",
                display_name="Ops Lead",
                alert_types=("charger_fault", "optimization_failed"),
                min_severity=Severity.CRITICAL,
            )
            assert created.email == "op@x.com"
            assert created.min_severity is Severity.CRITICAL
            assert sorted(created.alert_types) == ["charger_fault", "optimization_failed"]
            assert created.active is True

            fetched = await recipients_repo.get_by_id(conn, created.id, organization_id=org_id)
            assert fetched is not None
            assert fetched.id == created.id

    @pytest.mark.asyncio
    async def test_get_by_id_scoped_to_org_returns_none_for_mismatch(
        self, db_pool, org_and_depot
    ):
        org_id, _ = org_and_depot
        async with db_pool.acquire() as conn:
            created = await recipients_repo.create(
                conn, organization_id=org_id, email="x@x.com"
            )
            mismatch = await recipients_repo.get_by_id(
                conn, created.id, organization_id=uuid4()
            )
            assert mismatch is None

    @pytest.mark.asyncio
    async def test_create_duplicate_email_per_org_violates_unique(self, db_pool, org_and_depot):
        org_id, _ = org_and_depot
        async with db_pool.acquire() as conn:
            await recipients_repo.create(conn, organization_id=org_id, email="dup@x.com")
            with pytest.raises(asyncpg.UniqueViolationError):
                await recipients_repo.create(conn, organization_id=org_id, email="dup@x.com")

    @pytest.mark.asyncio
    async def test_update_partial_fields(self, db_pool, org_and_depot):
        org_id, _ = org_and_depot
        async with db_pool.acquire() as conn:
            created = await recipients_repo.create(
                conn,
                organization_id=org_id,
                email="upd@x.com",
                display_name="Original",
                min_severity=Severity.INFO,
            )

            updated = await recipients_repo.update(
                conn,
                created.id,
                organization_id=org_id,
                min_severity=Severity.CRITICAL,
            )
            assert updated is not None
            assert updated.display_name == "Original"
            assert updated.min_severity is Severity.CRITICAL
            assert updated.active is True

    @pytest.mark.asyncio
    async def test_update_cross_org_returns_none(self, db_pool, org_and_depot):
        org_id, _ = org_and_depot
        async with db_pool.acquire() as conn:
            created = await recipients_repo.create(conn, organization_id=org_id, email="z@x.com")
            result = await recipients_repo.update(
                conn, created.id, organization_id=uuid4(), active=False
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_delete_returns_true_then_false(self, db_pool, org_and_depot):
        org_id, _ = org_and_depot
        async with db_pool.acquire() as conn:
            created = await recipients_repo.create(conn, organization_id=org_id, email="del@x.com")
            assert await recipients_repo.delete(conn, created.id, organization_id=org_id) is True
            assert await recipients_repo.delete(conn, created.id, organization_id=org_id) is False

    @pytest.mark.asyncio
    async def test_list_for_org_active_only_by_default(self, db_pool, org_and_depot):
        org_id, _ = org_and_depot
        async with db_pool.acquire() as conn:
            r1 = await recipients_repo.create(conn, organization_id=org_id, email="a@x.com")
            r2 = await recipients_repo.create(conn, organization_id=org_id, email="b@x.com")
            await recipients_repo.update(conn, r2.id, organization_id=org_id, active=False)

            active_only = await recipients_repo.list_for_org(conn, org_id)
            assert {r.email for r in active_only} == {"a@x.com"}

            with_inactive = await recipients_repo.list_for_org(
                conn, org_id, include_inactive=True
            )
            assert {r.email for r in with_inactive} == {"a@x.com", "b@x.com"}
