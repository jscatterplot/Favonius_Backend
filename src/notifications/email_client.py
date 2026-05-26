"""Email delivery interface and shared types.

EmailDeliveryClient is a Protocol so the dispatcher only depends on the
shape `await client.send(msg) -> DeliveryResult`. ResendEmailClient and
FakeEmailClient both satisfy it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol


@dataclass(frozen=True)
class EmailAttachment:
    """A binary attachment. ``content`` is the raw (un-encoded) bytes; the
    provider client is responsible for any transport encoding (e.g. base64)."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass(frozen=True)
class EmailMessage:
    """Renderable email payload. The dispatcher fills `to` per recipient
    before passing the message to the client; html/text/subject are produced
    by the template renderer once per alert."""

    to: str
    subject: str
    html: str
    text: str
    from_address: str
    headers: dict[str, str] = field(default_factory=dict)
    attachments: list[EmailAttachment] = field(default_factory=list)


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of a single send call.

    `provider_message_id` is None on `failed` results — the dispatcher still
    records the failure in notification_deliveries so the operator can see
    delivery is broken. `detail` is provider-specific (HTTP status, error
    body) and is stored as JSONB on the delivery row.
    """

    status: str  # 'sent' | 'failed'
    provider_message_id: Optional[str]
    detail: Optional[dict[str, Any]] = None

    @property
    def ok(self) -> bool:
        return self.status == "sent"


class EmailDeliveryClient(Protocol):
    """Minimal interface the dispatcher consumes."""

    async def send(self, message: EmailMessage) -> DeliveryResult:  # pragma: no cover
        ...


class FakeEmailClient:
    """In-memory test double. Records every call and returns canned results.

    Default behavior is to accept everything; tests that need to simulate
    failures can pass a `script` callable that returns a DeliveryResult per
    invocation.
    """

    def __init__(
        self,
        *,
        script: Optional[Any] = None,
    ) -> None:
        self.sent: list[EmailMessage] = []
        self.script = script
        self._counter = 0

    async def send(self, message: EmailMessage) -> DeliveryResult:
        self.sent.append(message)
        self._counter += 1
        if self.script is not None:
            return self.script(message, self._counter)
        return DeliveryResult(
            status="sent",
            provider_message_id=f"fake-{self._counter:06d}",
            detail=None,
        )


__all__ = [
    "EmailMessage",
    "EmailAttachment",
    "DeliveryResult",
    "EmailDeliveryClient",
    "FakeEmailClient",
]
