"""Resend HTTP API client implementing EmailDeliveryClient.

Resend API: POST https://api.resend.com/emails
Auth: Bearer <api_key>
Body: {from, to, subject, html, text, headers}
Success: 200/202 with {"id": "<uuid>"} → DeliveryResult(sent, id)
Client error (4xx): no retry; DeliveryResult(failed, None, detail={...})
Server error (5xx) / network: retry with exponential backoff up to max_retries

The retry policy is intentionally local — Resend has rate limits but
typical 5xx is transient. We don't want a global circuit breaker yet.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Optional

import httpx

from .email_client import DeliveryResult, EmailMessage

logger = logging.getLogger(__name__)


_RESEND_API_URL = "https://api.resend.com/emails"


class ResendEmailClient:
    """HTTP client for Resend's transactional email API."""

    def __init__(
        self,
        *,
        api_key: str,
        default_from: str,
        http_client: Optional[httpx.AsyncClient] = None,
        max_retries: int = 3,
        initial_backoff_s: float = 0.5,
        timeout_s: float = 10.0,
    ) -> None:
        if not api_key:
            raise ValueError("Resend API key is required")
        if not default_from:
            raise ValueError("default_from address is required")
        self._api_key = api_key
        self._default_from = default_from
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(timeout=timeout_s)
        self._max_retries = max_retries
        self._initial_backoff_s = initial_backoff_s

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send(self, message: EmailMessage) -> DeliveryResult:
        payload: dict[str, Any] = {
            "from": message.from_address or self._default_from,
            "to": [message.to],
            "subject": message.subject,
            "html": message.html,
            "text": message.text,
        }
        if message.headers:
            payload["headers"] = dict(message.headers)
        if message.attachments:
            payload["attachments"] = [
                {
                    "filename": att.filename,
                    "content": base64.b64encode(att.content).decode("ascii"),
                    "content_type": att.content_type,
                }
                for att in message.attachments
            ]

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        last_detail: dict[str, Any] = {}
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(_RESEND_API_URL, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                last_detail = {"error": "transport", "message": str(exc), "attempt": attempt + 1}
                if attempt < self._max_retries:
                    await self._backoff(attempt)
                    continue
                logger.warning("resend.send transport error after retries: %s", exc)
                return DeliveryResult(status="failed", provider_message_id=None, detail=last_detail)

            if 200 <= response.status_code < 300:
                body = _safe_json(response)
                msg_id = body.get("id") if isinstance(body, dict) else None
                if not msg_id:
                    return DeliveryResult(
                        status="failed",
                        provider_message_id=None,
                        detail={
                            "error": "no_id",
                            "body": body,
                            "status_code": response.status_code,
                        },
                    )
                return DeliveryResult(
                    status="sent",
                    provider_message_id=str(msg_id),
                    detail={"status_code": response.status_code},
                )

            if 400 <= response.status_code < 500:
                # Don't retry client errors; they'll keep failing.
                return DeliveryResult(
                    status="failed",
                    provider_message_id=None,
                    detail={
                        "error": "client_error",
                        "status_code": response.status_code,
                        "body": _safe_json(response),
                    },
                )

            # 5xx or other: retryable
            last_detail = {
                "error": "server_error",
                "status_code": response.status_code,
                "body": _safe_json(response),
                "attempt": attempt + 1,
            }
            if attempt < self._max_retries:
                await self._backoff(attempt)
                continue

        logger.warning("resend.send 5xx after retries: %s", last_detail)
        return DeliveryResult(status="failed", provider_message_id=None, detail=last_detail)

    async def _backoff(self, attempt: int) -> None:
        delay = self._initial_backoff_s * (2**attempt)
        await asyncio.sleep(delay)


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except (ValueError, httpx.DecodingError):
        return {"text": response.text[:500]}


__all__ = ["ResendEmailClient"]
