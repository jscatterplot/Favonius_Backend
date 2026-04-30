"""Unit tests for src.notifications.email_client."""

from __future__ import annotations

import pytest

from src.notifications.email_client import (
    DeliveryResult,
    EmailMessage,
    FakeEmailClient,
)


def _make_message(**overrides) -> EmailMessage:
    defaults = dict(
        to="ops@example.com",
        subject="[Favonius] critical: Charger faulted",
        html="<p>fault</p>",
        text="fault",
        from_address="alerts@favonius.energy",
    )
    defaults.update(overrides)
    return EmailMessage(**defaults)


class TestDeliveryResult:
    def test_ok_property_for_sent(self):
        assert DeliveryResult(status="sent", provider_message_id="x").ok is True

    def test_ok_property_for_failed(self):
        assert DeliveryResult(status="failed", provider_message_id=None).ok is False


class TestFakeEmailClientDefaults:
    @pytest.mark.asyncio
    async def test_send_records_message(self):
        client = FakeEmailClient()
        result = await client.send(_make_message())
        assert result.ok
        assert len(client.sent) == 1
        assert client.sent[0].to == "ops@example.com"

    @pytest.mark.asyncio
    async def test_each_call_returns_unique_provider_id(self):
        client = FakeEmailClient()
        a = await client.send(_make_message(to="a@x.com"))
        b = await client.send(_make_message(to="b@x.com"))
        assert a.provider_message_id != b.provider_message_id
        assert len(client.sent) == 2

    @pytest.mark.asyncio
    async def test_script_overrides_default(self):
        def script(_msg, count):
            return DeliveryResult(
                status="failed" if count == 2 else "sent",
                provider_message_id=None if count == 2 else f"ok-{count}",
                detail={"call": count},
            )

        client = FakeEmailClient(script=script)
        first = await client.send(_make_message(to="a@x.com"))
        second = await client.send(_make_message(to="b@x.com"))

        assert first.ok and first.provider_message_id == "ok-1"
        assert not second.ok and second.provider_message_id is None
        assert len(client.sent) == 2


class TestEmailMessageEquality:
    def test_messages_with_same_fields_are_equal(self):
        a = _make_message()
        b = _make_message()
        assert a == b

    def test_messages_are_immutable(self):
        msg = _make_message()
        with pytest.raises((AttributeError, TypeError)):
            msg.to = "other@x.com"  # type: ignore[misc]
