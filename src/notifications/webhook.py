"""Resend webhook verification + event parsing.

Signature scheme (compatible with Svix-style signing as used by Resend):
  Header: X-Resend-Signature: t=<unix_timestamp>,v1=<hex_hmac_sha256>
  Compute: HMAC-SHA256(secret, f"{timestamp}.{body}")

Reject when:
  - signature header missing or malformed
  - timestamp is older than `tolerance_s` (default 300s) — replay protection
  - HMAC mismatch (constant-time compare)

Accepted Resend event types we care about:
  email.delivered, email.bounced, email.complained, email.failed
Other types are returned as `None` so the endpoint can ignore them.
"""

from __future__ import annotations

import hmac
import logging
import time
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

    parts = _parse_header(signature_header)
    if "t" not in parts or "v1" not in parts:
        logger.debug("webhook: header missing t or v1")
        return False

    try:
        ts = int(parts["t"])
    except ValueError:
        logger.debug("webhook: bad timestamp %r", parts["t"])
        return False

    current = now if now is not None else time.time()
    if abs(current - ts) > tolerance_s:
        logger.debug("webhook: timestamp out of tolerance (delta=%ss)", current - ts)
        return False

    payload = f"{ts}.".encode() + body
    expected = hmac.new(secret.encode(), payload, sha256).hexdigest()
    return hmac.compare_digest(expected, parts["v1"])


def _parse_header(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in header.split(","):
        token = token.strip()
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        out[key.strip()] = value.strip()
    return out


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
