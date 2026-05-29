"""Context-aware support summary ("This isn't right") orchestration.

Lives behind the support endpoints in :mod:`src.api.main` to keep that
module lean. Responsibilities:

* :func:`build_summary_text` — render the exact engineering summary sentence.
* :func:`decode_screenshot` — validate + decode the base64 UI screenshot.
* :func:`render_logs_excerpt` / :func:`extract_last_error_line` — turn ring-buffer
  records into a redacted, size-bounded excerpt and a single error line.
* :func:`persist_support_summary` — INSERT the bundle (TimescaleDB ``ts`` pool).
* :func:`fetch_support_summary` / :func:`delete_support_summary` — admin reads/erasure.
* :func:`erase_support_summaries_for_user` — right-to-erasure by reporter (DSAR hook).
* :func:`purge_expired_support_summaries` / :func:`run_support_summary_purge_loop` —
  retention enforcement.
* :func:`deliver_support_summary` — email the bundle to engineering (post-commit,
  best-effort), reusing the notifications email client.

GDPR: only the reporter's ``user_id`` (UUID) is stored — never email. The
log excerpt, error line and user note are passed through
:func:`src.observability.log_buffer.redact_log_line` before persistence.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Iterable, Optional

from ..notifications.email_client import EmailAttachment, EmailMessage
from .log_buffer import BufferedRecord, redact_log_line

logger = logging.getLogger(__name__)

# MVP allowlist: only images are accepted as screenshots.
ALLOWED_SCREENSHOT_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_CONTENT_TYPE_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}

# Rendered when the ring buffer had no ERROR-level record in the window.
NO_ERROR_PLACEHOLDER = "none"
# Cap on the single error line embedded in the summary sentence.
MAX_ERROR_LINE_CHARS = 500


class ScreenshotRejected(Exception):
    """The uploaded screenshot failed validation.

    Carries an HTTP status (400 bad type/decode, 413 too large) so the
    endpoint can return a matching error without leaking internals.
    """

    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


@dataclass(frozen=True)
class SupportSummaryBundle:
    """Everything persisted for one support report."""

    user_id: str
    organization_id: str
    page: str
    summary_text: str
    health_snapshot: dict[str, Any]
    depot_id: Optional[str] = None
    user_note: Optional[str] = None
    screenshot: Optional[bytes] = None
    screenshot_content_type: Optional[str] = None
    screenshot_size_bytes: Optional[int] = None
    screenshot_sha256: Optional[str] = None
    logs_excerpt: str = ""
    extra_metadata: dict[str, Any] = field(default_factory=dict)


# ── Pure helpers (no I/O) ─────────────────────────────────────────────────────


def build_summary_text(
    *,
    user_id: str,
    page: str,
    tiger_cloud_status: str,
    websocket_status: str,
    last_error_line: Optional[str],
) -> str:
    """Render the exact engineering summary sentence.

    Format (PRD wording):
        ``User {id} reported an error while viewing {page}. System status at
        time of report: Tiger Cloud {status}, WebSocket {status}. Last error
        log: {error}.``

    A missing/blank ``last_error_line`` renders ``Last error log: none.`` so
    there is never a dangling colon. The error line is truncated to
    :data:`MAX_ERROR_LINE_CHARS`.
    """
    error_part = (last_error_line or "").strip()
    if not error_part:
        error_part = NO_ERROR_PLACEHOLDER
    elif len(error_part) > MAX_ERROR_LINE_CHARS:
        error_part = error_part[:MAX_ERROR_LINE_CHARS] + "…"
    return (
        f"User {user_id} reported an error while viewing {page}. "
        f"System status at time of report: Tiger Cloud {tiger_cloud_status}, "
        f"WebSocket {websocket_status}. "
        f"Last error log: {error_part}."
    )


def decode_screenshot(
    screenshot_base64: Optional[str],
    declared_content_type: Optional[str],
    *,
    max_bytes: int,
) -> Optional[tuple[bytes, str]]:
    """Validate + decode a base64 (or ``data:`` URL) screenshot.

    Returns ``(raw_bytes, content_type)`` or ``None`` when no screenshot was
    supplied. Raises :class:`ScreenshotRejected` on a bad content type,
    malformed base64, an empty image, or one over ``max_bytes``.
    """
    if not screenshot_base64:
        return None

    payload = screenshot_base64.strip()
    content_type = declared_content_type

    if payload.startswith("data:"):
        header, _, data_part = payload.partition(",")
        if not data_part or ";base64" not in header.lower():
            raise ScreenshotRejected(400, "malformed data URL screenshot")
        ct_from_url = header[len("data:") :].split(";")[0].strip()
        if ct_from_url:
            content_type = ct_from_url
        payload = data_part

    if not content_type:
        raise ScreenshotRejected(400, "screenshot content type required")
    content_type = content_type.lower()
    if content_type not in ALLOWED_SCREENSHOT_CONTENT_TYPES:
        raise ScreenshotRejected(400, "unsupported screenshot content type")

    # Strip incidental whitespace/newlines so strict validation passes.
    payload = "".join(payload.split())
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ScreenshotRejected(400, "invalid base64 screenshot") from exc

    if not data:
        raise ScreenshotRejected(400, "empty screenshot")
    if len(data) > max_bytes:
        raise ScreenshotRejected(413, "screenshot exceeds max size")
    return data, content_type


def _format_record(record: BufferedRecord) -> str:
    iso = datetime.fromtimestamp(record.ts, tz=timezone.utc).isoformat()
    return f"{iso} {record.levelname} {record.logger_name}: {record.message}"


def render_logs_excerpt(records: Iterable[BufferedRecord], *, max_bytes: int) -> str:
    """Render records to a redacted, byte-bounded excerpt (oldest first).

    Each line is redacted via :func:`redact_log_line`. When the joined text
    exceeds ``max_bytes`` the most recent bytes are kept (the tail is the most
    relevant context for a just-reported problem).
    """
    lines = [redact_log_line(_format_record(r)) for r in records]
    text = "\n".join(lines)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    tail = encoded[-max_bytes:].decode("utf-8", errors="replace")
    return "…(truncated)…\n" + tail


def extract_last_error_line(record: Optional[BufferedRecord]) -> Optional[str]:
    """Redact the most-recent-error message, or ``None``."""
    if record is None:
        return None
    redacted = redact_log_line(record.message)
    return redacted or None


# ── Persistence (TimescaleDB ``ts`` pool) ─────────────────────────────────────


def _maybe_json(value: Any) -> Any:
    """asyncpg returns jsonb as text without a codec — parse to dict."""
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _rowcount(status: Any) -> int:
    """Parse the row count from an asyncpg command-status string."""
    try:
        return int(str(status).split()[-1])
    except (ValueError, IndexError):
        return 0


async def persist_support_summary(
    ts_pool: Any, bundle: SupportSummaryBundle, *, retention_days: int
) -> str:
    """INSERT the bundle and return the new row id (string).

    ``expires_at`` is set to ``now() + retention_days``; ``delivery_status``
    defaults to ``'pending'`` until :func:`deliver_support_summary` runs.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(days=max(1, int(retention_days)))
    async with ts_pool.acquire() as conn:
        new_id = await conn.fetchval(
            """
            INSERT INTO support_summaries (
                user_id, organization_id, depot_id, page, user_note,
                screenshot, screenshot_content_type, screenshot_size_bytes,
                screenshot_sha256, logs_excerpt, health_snapshot, summary_text,
                expires_at
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4, $5,
                $6, $7, $8,
                $9, $10, $11::jsonb, $12,
                $13
            )
            RETURNING id::text
            """,
            bundle.user_id,
            bundle.organization_id,
            bundle.depot_id,
            bundle.page,
            bundle.user_note,
            bundle.screenshot,
            bundle.screenshot_content_type,
            bundle.screenshot_size_bytes,
            bundle.screenshot_sha256,
            bundle.logs_excerpt,
            json.dumps(bundle.health_snapshot or {}, default=str),
            bundle.summary_text,
            expires_at,
        )
    return str(new_id)


async def fetch_support_summary(
    ts_pool: Any, summary_id: str, *, include_screenshot: bool = False
) -> Optional[dict[str, Any]]:
    """Fetch one summary as a dict, or ``None`` when missing.

    ``health_snapshot`` / ``delivery_detail`` are parsed back to dicts. The
    raw ``screenshot`` bytes are only included when ``include_screenshot``.
    """
    columns = (
        "id::text AS id, user_id::text AS user_id, "
        "organization_id::text AS organization_id, depot_id::text AS depot_id, "
        "page, user_note, screenshot_content_type, screenshot_size_bytes, "
        "screenshot_sha256, logs_excerpt, health_snapshot, summary_text, "
        "delivery_status, delivery_detail, created_at, expires_at"
    )
    if include_screenshot:
        columns += ", screenshot"
    async with ts_pool.acquire() as conn:
        row = await conn.fetchrow(
            f"SELECT {columns} FROM support_summaries WHERE id = $1::uuid",
            summary_id,
        )
    if row is None:
        return None
    result = dict(row)
    result["health_snapshot"] = _maybe_json(result.get("health_snapshot")) or {}
    result["delivery_detail"] = _maybe_json(result.get("delivery_detail"))
    return result


async def delete_support_summary(ts_pool: Any, summary_id: str) -> bool:
    """Hard-delete one summary by id. Returns ``True`` if a row was removed."""
    async with ts_pool.acquire() as conn:
        deleted = await conn.fetchval(
            "DELETE FROM support_summaries WHERE id = $1::uuid RETURNING id::text",
            summary_id,
        )
    return deleted is not None


async def erase_support_summaries_for_user(ts_pool: Any, user_id: str) -> int:
    """Right-to-erasure: hard-delete all summaries filed by ``user_id``.

    Returns the number of rows removed. Intended for wiring into a future
    data-subject-request flow.
    """
    async with ts_pool.acquire() as conn:
        status = await conn.execute(
            "DELETE FROM support_summaries WHERE user_id = $1::uuid",
            user_id,
        )
    return _rowcount(status)


async def purge_expired_support_summaries(ts_pool: Any) -> int:
    """Hard-delete summaries past their retention horizon. Returns the count."""
    async with ts_pool.acquire() as conn:
        status = await conn.execute("DELETE FROM support_summaries WHERE expires_at < NOW()")
    return _rowcount(status)


async def _update_delivery(
    ts_pool: Any, summary_id: str, status: str, detail: Optional[dict[str, Any]]
) -> None:
    async with ts_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE support_summaries
               SET delivery_status = $2,
                   delivery_detail = $3::jsonb
             WHERE id = $1::uuid
            """,
            summary_id,
            status,
            json.dumps(detail, default=str) if detail is not None else None,
        )


async def deliver_support_summary(
    ts_pool: Any,
    email_client: Any,
    *,
    summary_id: str,
    default_from: str,
    eng_recipient: Optional[str],
) -> None:
    """Email the stored summary to engineering; update ``delivery_status``.

    Post-commit, best-effort: the DB row is the source of truth, so a failed
    or unconfigured send never raises out (it only records ``failed`` /
    ``skipped``). Reuses :class:`EmailMessage` / :class:`EmailAttachment`.
    """
    row = await fetch_support_summary(ts_pool, summary_id, include_screenshot=True)
    if row is None:
        logger.warning("support delivery: summary %s vanished before send", summary_id)
        return

    if not eng_recipient:
        await _update_delivery(
            ts_pool, summary_id, "skipped", {"reason": "no SUPPORT_ENG_RECIPIENT configured"}
        )
        logger.warning(
            "support delivery: no engineering recipient configured; %s stored only",
            summary_id,
        )
        return
    if email_client is None:
        await _update_delivery(ts_pool, summary_id, "skipped", {"reason": "no email client"})
        return

    attachments: list[EmailAttachment] = []
    screenshot = row.get("screenshot")
    content_type = row.get("screenshot_content_type") or "application/octet-stream"
    if screenshot:
        ext = _CONTENT_TYPE_EXT.get(content_type, "bin")
        attachments.append(
            EmailAttachment(
                filename=f"support-{summary_id}.{ext}",
                content=bytes(screenshot),
                content_type=content_type,
            )
        )
    logs = row.get("logs_excerpt") or ""
    if logs:
        attachments.append(
            EmailAttachment(
                filename=f"support-{summary_id}-logs.txt",
                content=logs.encode("utf-8"),
                content_type="text/plain",
            )
        )

    health = row.get("health_snapshot") or {}
    summary_text = row.get("summary_text") or ""
    subject = (
        f"[Support] {row.get('page')} — "
        f"Tiger Cloud {health.get('tiger_cloud', '?')} / "
        f"WebSocket {health.get('websocket', '?')}"
    )
    text_body = summary_text
    if logs:
        text_body += "\n\n--- recent logs ---\n" + logs
    html_body = f"<p>{escape(summary_text)}</p>"
    if logs:
        html_body += f"<pre>{escape(logs)}</pre>"

    message = EmailMessage(
        to=eng_recipient,
        subject=subject,
        html=html_body,
        text=text_body,
        from_address=default_from,
        attachments=attachments,
    )

    try:
        result = await email_client.send(message)
        status = "sent" if getattr(result, "ok", False) else "failed"
        detail = getattr(result, "detail", None)
    except Exception as exc:  # noqa: BLE001 - delivery is best-effort
        logger.exception("support delivery: send failed for %s", summary_id)
        status = "failed"
        detail = {"error": str(exc)}
    await _update_delivery(ts_pool, summary_id, status, detail)


async def run_support_summary_purge_loop(ts_pool: Any, *, interval_seconds: float) -> None:
    """Background loop that hard-deletes expired summaries on a cadence.

    Never raises out (a failed cycle is logged and retried), mirroring the
    other background loops in this codebase.
    """
    while True:
        try:
            await asyncio.sleep(interval_seconds)
            deleted = await purge_expired_support_summaries(ts_pool)
            if deleted:
                logger.info("support summary purge: deleted %d expired rows", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("support summary purge loop cycle failed")


__all__ = [
    "ALLOWED_SCREENSHOT_CONTENT_TYPES",
    "NO_ERROR_PLACEHOLDER",
    "ScreenshotRejected",
    "SupportSummaryBundle",
    "build_summary_text",
    "decode_screenshot",
    "deliver_support_summary",
    "delete_support_summary",
    "erase_support_summaries_for_user",
    "extract_last_error_line",
    "fetch_support_summary",
    "persist_support_summary",
    "purge_expired_support_summaries",
    "render_logs_excerpt",
    "run_support_summary_purge_loop",
]
