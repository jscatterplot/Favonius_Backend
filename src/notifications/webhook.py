"""Resend webhook verification + event parsing.

Signature scheme (Svix-style signing as used by Resend):
  Headers:
    svix-id: <message_id>
    svix-timestamp: <unix_timestamp>
    svix-signature: v1,<base64_hmac_sha256>
  Compute: HMAC-SHA256(decoded_secret, f"{id}.{timestamp}.{body}")

Reject when:
  - signature header missing or malformed
  - timestamp is older than `tolerance_s` (default 300s) — replay protection
  - HMAC mismatch (constant-time compare)

Accepted Resend event types we care about:
  email.delivered, email.bounced, email.complained, email.failed
Other types are returned as `None` so the endpoint can ignore them.
"""

from __future__ import annotations

import binascii
import hmac
import logging
import time
from base64 import b64decode, b64encode
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Optional

logger = logging.getLogger(__name__)


_DEFAULT_TOLERANCE_S = 300

_EVENT_STATUS_MAP = {
    "email.sent": "sent",
    "email.delivered": "delivered",
    "email.bounced": "bounced",
    "email.complained": "complained",
    "email.failed": "failed",
}


@dataclass(frozen=True)
class WebhookEvent:
    provider_message_id: str
    status: str
    detail: dict[str, Any]


def verify_signature(
    *,
    secret: str,
    body: bytes,
    signature_header: Optional[str],
    message_id: Optional[str],
    timestamp_header: Optional[str],
    tolerance_s: int = _DEFAULT_TOLERANCE_S,
    now: Optional[float] = None,
) -> bool:
    """Constant-time HMAC verification with replay protection.

    Returns True iff the signature is valid AND the timestamp is within
    `tolerance_s` seconds of now. False otherwise (logs reason at DEBUG).
    """
    if not secret:
        logger.debug("webhook: no secret configured; rejecting")
        return False
    if not signature_header:
        logger.debug("webhook: missing signature header")
        return False

    if not message_id:
        logger.debug("webhook: missing svix-id header")
        return False

    try:
        ts = int(timestamp_header or "")
    except ValueError:
        logger.debug("webhook: bad timestamp %r", timestamp_header)
        return False

    current = now if now is not None else time.time()
    if abs(current - ts) > tolerance_s:
        logger.debug("webhook: timestamp out of tolerance (delta=%ss)", current - ts)
        return False

    signing_secret = _decode_secret(secret)
    if signing_secret is None:
        logger.debug("webhook: invalid signing secret format")
        return False

    signature = _extract_v1_signature(signature_header)
    if not signature:
        logger.debug("webhook: header missing v1 signature")
        return False

    payload = f"{message_id}.{ts}.".encode() + body
    expected = b64encode(hmac.new(signing_secret, payload, sha256).digest()).decode()
    return hmac.compare_digest(expected, signature)


def _decode_secret(secret: str) -> Optional[bytes]:
    value = secret[6:] if secret.startswith("whsec_") else secret
    try:
        return b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return None


def _extract_v1_signature(signature_header: str) -> Optional[str]:
    for token in signature_header.split():
        version, sep, signature = token.partition(",")
        if sep and version == "v1" and signature:
            return signature
    return None


def parse_event(body: dict[str, Any]) -> Optional[WebhookEvent]:
    """Extract (provider_message_id, status, detail) from a Resend event.

    Returns None for event types we don't handle (e.g. email.opened).
    """
    event_type = body.get("type")
    if not event_type or event_type not in _EVENT_STATUS_MAP:
        return None

    data = body.get("data") or {}
    msg_id = data.get("email_id") or data.get("id")
    if not msg_id:
        return None

    return WebhookEvent(
        provider_message_id=str(msg_id),
        status=_EVENT_STATUS_MAP[event_type],
        detail={
            "type": event_type,
            "to": data.get("to"),
            "subject": data.get("subject"),
            "reason": data.get("reason"),
        },
    )


__all__ = ["WebhookEvent", "verify_signature", "parse_event"]
