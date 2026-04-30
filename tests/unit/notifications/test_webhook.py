"""Unit tests for src.notifications.webhook."""

from __future__ import annotations

import hmac
import time
from hashlib import sha256

import pytest

from src.notifications.webhook import parse_event, verify_signature


def _sign(body: bytes, secret: str, ts: int) -> str:
    payload = f"{ts}.".encode() + body
    digest = hmac.new(secret.encode(), payload, sha256).hexdigest()
    return f"t={ts},v1={digest}"


class TestVerifySignature:
    def test_valid_signature_passes(self):
        body = b'{"type":"email.delivered","data":{"email_id":"x"}}'
        secret = "whsec_test"
        ts = int(time.time())
        header = _sign(body, secret, ts)

        assert verify_signature(
            secret=secret, body=body, signature_header=header, now=ts
        ) is True

    def test_tampered_body_fails(self):
        body = b'{"type":"email.delivered","data":{"email_id":"x"}}'
        secret = "whsec_test"
        ts = int(time.time())
        header = _sign(body, secret, ts)

        tampered = body.replace(b"delivered", b"bounced!")
        assert verify_signature(
            secret=secret, body=tampered, signature_header=header, now=ts
        ) is False

    def test_wrong_secret_fails(self):
        body = b"hello"
        ts = int(time.time())
        header = _sign(body, "actual_secret", ts)
        assert verify_signature(
            secret="other_secret", body=body, signature_header=header, now=ts
        ) is False

    def test_old_timestamp_rejected(self):
        body = b"hello"
        secret = "whsec"
        old_ts = 1000
        header = _sign(body, secret, old_ts)
        assert verify_signature(
            secret=secret,
            body=body,
            signature_header=header,
            tolerance_s=300,
            now=old_ts + 600,
        ) is False

    def test_future_timestamp_rejected(self):
        body = b"hello"
        secret = "whsec"
        ts = 2000
        header = _sign(body, secret, ts)
        # `now` 600s in the past → also outside tolerance
        assert verify_signature(
            secret=secret,
            body=body,
            signature_header=header,
            tolerance_s=300,
            now=ts - 600,
        ) is False

    def test_missing_header_returns_false(self):
        assert verify_signature(secret="x", body=b"y", signature_header=None) is False
        assert verify_signature(secret="x", body=b"y", signature_header="") is False

    def test_malformed_header_returns_false(self):
        assert verify_signature(secret="x", body=b"y", signature_header="garbage") is False
        assert verify_signature(
            secret="x", body=b"y", signature_header="t=foo,v1=bar"
        ) is False

    def test_empty_secret_returns_false(self):
        body = b"hello"
        ts = int(time.time())
        header = _sign(body, "", ts)
        assert verify_signature(secret="", body=body, signature_header=header) is False


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
