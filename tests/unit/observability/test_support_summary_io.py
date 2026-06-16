"""Unit tests for support-summary persistence, purge, erasure and delivery.

These use mock asyncpg pools/connections — no real DB. They assert the
SQL-shaping helpers return the right values and that delivery wires the
email client and updates delivery state.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.notifications.email_client import DeliveryResult, FakeEmailClient
from src.observability.support_summary import (
    SupportSummaryBundle,
    delete_support_summary,
    deliver_support_summary,
    erase_support_summaries_for_user,
    persist_support_summary,
    purge_expired_support_summaries,
)

pytestmark = pytest.mark.asyncio

NEW_ID = "11111111-1111-1111-1111-111111111111"


def _pool_with_conn():
    pool = MagicMock()
    conn = AsyncMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    pool.ts = pool
    pool.static = pool
    return pool, conn


async def test_persist_returns_new_id():
    pool, conn = _pool_with_conn()
    conn.fetchval = AsyncMock(return_value=NEW_ID)
    bundle = SupportSummaryBundle(
        user_id="u", organization_id="o", page="/p", summary_text="S", health_snapshot={}
    )
    new_id = await persist_support_summary(pool, bundle, retention_days=90)
    assert new_id == NEW_ID
    assert conn.fetchval.await_count == 1


async def test_delete_returns_true_when_removed():
    pool, conn = _pool_with_conn()
    conn.fetchval = AsyncMock(return_value=NEW_ID)
    assert await delete_support_summary(pool, NEW_ID) is True


async def test_delete_returns_false_when_missing():
    pool, conn = _pool_with_conn()
    conn.fetchval = AsyncMock(return_value=None)
    assert await delete_support_summary(pool, NEW_ID) is False


async def test_erase_for_user_returns_count():
    pool, conn = _pool_with_conn()
    conn.execute = AsyncMock(return_value="DELETE 3")
    assert await erase_support_summaries_for_user(pool, "user-uuid") == 3


async def test_purge_returns_count():
    pool, conn = _pool_with_conn()
    conn.execute = AsyncMock(return_value="DELETE 4")
    assert await purge_expired_support_summaries(pool) == 4


async def test_purge_handles_zero():
    pool, conn = _pool_with_conn()
    conn.execute = AsyncMock(return_value="DELETE 0")
    assert await purge_expired_support_summaries(pool) == 0


def _delivery_row(**overrides):
    row = {
        "id": NEW_ID,
        "user_id": "u",
        "organization_id": "o",
        "depot_id": None,
        "page": "/depots/x/state",
        "user_note": None,
        "screenshot_content_type": "image/png",
        "screenshot_size_bytes": 3,
        "screenshot_sha256": "abc",
        "logs_excerpt": "some recent logs",
        "health_snapshot": {"tiger_cloud": "healthy", "websocket": "unknown"},
        "summary_text": "User u reported an error.",
        "delivery_status": "pending",
        "delivery_detail": None,
        "created_at": None,
        "expires_at": None,
        "screenshot": b"abc",
    }
    row.update(overrides)
    return row


async def test_deliver_emails_with_attachments_and_marks_sent():
    pool, conn = _pool_with_conn()
    conn.fetchrow = AsyncMock(return_value=_delivery_row())
    conn.execute = AsyncMock(return_value="UPDATE 1")
    client = FakeEmailClient()
    await deliver_support_summary(
        pool, client, summary_id=NEW_ID, default_from="alerts@x.com", eng_recipient="eng@x.com"
    )
    assert len(client.sent) == 1
    msg = client.sent[0]
    assert msg.to == "eng@x.com"
    filenames = [a.filename for a in msg.attachments]
    assert any(f.endswith(".png") for f in filenames)
    assert any(f.endswith("-logs.txt") for f in filenames)
    # delivery_status updated (one UPDATE).
    assert conn.execute.await_count == 1


async def test_deliver_skips_without_recipient():
    pool, conn = _pool_with_conn()
    conn.fetchrow = AsyncMock(return_value=_delivery_row())
    conn.execute = AsyncMock(return_value="UPDATE 1")
    client = FakeEmailClient()
    await deliver_support_summary(
        pool, client, summary_id=NEW_ID, default_from="alerts@x.com", eng_recipient=None
    )
    assert len(client.sent) == 0
    # marked skipped via an UPDATE
    assert conn.execute.await_count == 1


async def test_deliver_marks_failed_on_send_error():
    pool, conn = _pool_with_conn()
    conn.fetchrow = AsyncMock(return_value=_delivery_row(screenshot=None))
    conn.execute = AsyncMock(return_value="UPDATE 1")

    def _boom(message, counter):
        raise RuntimeError("provider down")

    client = FakeEmailClient(script=_boom)
    # Must not raise — delivery is best-effort.
    await deliver_support_summary(
        pool, client, summary_id=NEW_ID, default_from="alerts@x.com", eng_recipient="eng@x.com"
    )
    assert conn.execute.await_count == 1


async def test_deliver_marks_failed_on_provider_failure():
    pool, conn = _pool_with_conn()
    conn.fetchrow = AsyncMock(return_value=_delivery_row(logs_excerpt=""))
    conn.execute = AsyncMock(return_value="UPDATE 1")

    def _fail(message, counter):
        return DeliveryResult(status="failed", provider_message_id=None, detail={"code": 500})

    client = FakeEmailClient(script=_fail)
    await deliver_support_summary(
        pool, client, summary_id=NEW_ID, default_from="alerts@x.com", eng_recipient="eng@x.com"
    )
    assert len(client.sent) == 1
    assert conn.execute.await_count == 1


async def test_deliver_noop_when_summary_missing():
    pool, conn = _pool_with_conn()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.execute = AsyncMock(return_value="UPDATE 0")
    client = FakeEmailClient()
    await deliver_support_summary(
        pool, client, summary_id=NEW_ID, default_from="alerts@x.com", eng_recipient="eng@x.com"
    )
    assert len(client.sent) == 0
    assert conn.execute.await_count == 0
