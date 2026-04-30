"""Unit tests for src.notifications.webhook."""

from __future__ import annotations

import hmac
import time
from base64 import b64encode
from base64 import b64decode
from hashlib import sha256

import pytest

from src.notifications.webhook import parse_event, verify_signature


def _secret(signing_key: bytes = b"test-signing-key") -> str:
    return f"whsec_{b64encode(signing_key).decode()}"


def _sign(body: bytes, secret: str, msg_id: str, ts: int) -> str:
    secret_b64 = secret[6:] if secret.startswith("whsec_") else secret
    payload = f"{msg_id}.{ts}.".encode() + body
    digest = b64encode(hmac.new(b64decode(secret_b64), payload, sha256).digest()).decode()
    return f"v1,{digest}"


class TestVerifySignature:
    def test_valid_signature_passes(self):
        body = b'{"type":"email.delivered","data":{"email_id":"x"}}'
        secret = _secret()
        msg_id = "msg_123"
        ts = int(time.time())
        header = _sign(body, secret, msg_id, ts)

        assert verify_signature(
            secret=secret,
            body=body,
            signature_header=header,
            message_id=msg_id,
            timestamp_header=str(ts),
            now=ts,
        ) is True

    def test_tampered_body_fails(self):
        body = b'{"type":"email.delivered","data":{"email_id":"x"}}'
        secret = _secret()
        msg_id = "msg_123"
        ts = int(time.time())
        header = _sign(body, secret, msg_id, ts)

        tampered = body.replace(b"delivered", b"bounced!")
        assert verify_signature(
            secret=secret,
            body=tampered,
            signature_header=header,
            message_id=msg_id,
            timestamp_header=str(ts),
            now=ts,
        ) is False

    def test_wrong_secret_fails(self):
        body = b"hello"
        msg_id = "msg_123"
        ts = int(time.time())
        header = _sign(body, _secret(b"actual"), msg_id, ts)
        assert verify_signature(
            secret=_secret(b"other"),
            body=body,
            signature_header=header,
            message_id=msg_id,
            timestamp_header=str(ts),
            now=ts,
        ) is False

    def test_old_timestamp_rejected(self):
        body = b"hello"
        secret = _secret()
        msg_id = "msg_123"
        old_ts = 1000
        header = _sign(body, secret, msg_id, old_ts)
        assert verify_signature(
            secret=secret,
            body=body,
            signature_header=header,
            message_id=msg_id,
            timestamp_header=str(old_ts),
            tolerance_s=300,
            now=old_ts + 600,
        ) is False

    def test_future_timestamp_rejected(self):
        body = b"hello"
        secret = _secret()
        msg_id = "msg_123"
        ts = 2000
        header = _sign(body, secret, msg_id, ts)
        # `now` 600s in the past → also outside tolerance
        assert verify_signature(
            secret=secret,
            body=body,
            signature_header=header,
            message_id=msg_id,
            timestamp_header=str(ts),
            tolerance_s=300,
            now=ts - 600,
        ) is False

    def test_missing_header_returns_false(self):
        assert verify_signature(
            secret=_secret(),
            body=b"y",
            signature_header=None,
            message_id="m",
            timestamp_header="1",
        ) is False
        assert verify_signature(
            secret=_secret(),
            body=b"y",
            signature_header="",
            message_id="m",
            timestamp_header="1",
        ) is False

    def test_malformed_header_returns_false(self):
        assert verify_signature(
            secret=_secret(),
            body=b"y",
            signature_header="garbage",
            message_id="m",
            timestamp_header="1",
        ) is False
        assert verify_signature(
            secret=_secret(),
            body=b"y",
            signature_header="t=foo,v1=bar",
            message_id="m",
            timestamp_header="1",
        ) is False

    def test_empty_secret_returns_false(self):
        body = b"hello"
        msg_id = "msg_123"
        ts = int(time.time())
        header = _sign(body, _secret(), msg_id, ts)
        assert verify_signature(
            secret="",
            body=body,
            signature_header=header,
            message_id=msg_id,
            timestamp_header=str(ts),
        ) is False

    def test_missing_message_id_returns_false(self):
        body = b"hello"
        secret = _secret()
        ts = int(time.time())
        header = _sign(body, secret, "msg_123", ts)
        assert verify_signature(
            secret=secret,
            body=body,
            signature_header=header,
            message_id=None,
            timestamp_header=str(ts),
            now=ts,
        ) is False


class TestParseEvent:
    def test_delivered_event(self):
        ev = parse_event(
            {
                "type": "email.delivered",
                "data": {"email_id": "msg_abc", "to": ["ops@x.com"]},
            }
        )
        assert ev is not None
        assert ev.provider_message_id == "msg_abc"
        assert ev.status == "delivered"
        assert ev.detail["type"] == "email.delivered"

    @pytest.mark.parametrize(
        "event_type, expected_status",
        [
            ("email.sent", "sent"),
            ("email.delivered", "delivered"),
            ("email.bounced", "bounced"),
            ("email.complained", "complained"),
            ("email.failed", "failed"),
        ],
    )
    def test_known_event_types(self, event_type, expected_status):
        ev = parse_event({"type": event_type, "data": {"email_id": "x"}})
        assert ev is not None
        assert ev.status == expected_status

    def test_unknown_event_returns_none(self):
        ev = parse_event({"type": "email.opened", "data": {"email_id": "x"}})
        assert ev is None

    def test_missing_email_id_returns_none(self):
        ev = parse_event({"type": "email.delivered", "data": {}})
        assert ev is None

    def test_falls_back_to_id_field(self):
        # Resend has gone back-and-forth on email_id vs id; accept both.
        ev = parse_event({"type": "email.delivered", "data": {"id": "x"}})
        assert ev is not None
        assert ev.provider_message_id == "x"

    def test_no_data_field_returns_none(self):
        ev = parse_event({"type": "email.delivered"})
        assert ev is None

    def test_no_type_field_returns_none(self):
        ev = parse_event({"data": {"email_id": "x"}})
        assert ev is None
