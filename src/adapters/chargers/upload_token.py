"""HMAC signed-URL tokens for the charger log upload endpoint.

The legacy WS handler — which actually holds the charger's WebSocket —
sends an OCPP ``GetDiagnostics`` containing a ``location`` URL the
charger will POST its diagnostics archive to. Chargers don't carry
Supabase JWTs, so the URL itself has to be the credential.

Design:
    * The URL embeds a short-lived HMAC token in the ``token`` query
      param: ``token=<import_id>.<expiry_unix>.<hmac_hex>``.
    * The upload endpoint parses the token, recomputes the HMAC, checks
      the expiry, and looks up the matching ``charger_log_imports`` row
      by ``id``. The ``upload_token_hash`` column on that row stores
      ``sha256(token)``, defeating replay across different imports.
    * Token TTL defaults to ``CHARGER_LOG_UPLOAD_TOKEN_TTL_S`` (1 h),
      enough headroom for slow diagnostics dumps but short enough that
      a leaked URL is useless within a workday.

Out-of-band rotation: rotating ``CHARGER_LOG_UPLOAD_SIGNING_KEY``
invalidates every outstanding URL. That's intentional — there is no
"previous key" rolling window. Operators issue at most a handful of
diagnostics pulls per day, so an invalidated URL just means a re-fetch.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode
from uuid import UUID

DEFAULT_TOKEN_TTL_S = 3600
DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB


class UploadTokenError(Exception):
    """Raised for any token shape, expiry, or signature failure.

    The upload endpoint maps this to HTTP 401 — never 4xx-detail-leak
    the specific reason, to keep timing-side channels narrow.
    """


@dataclass(frozen=True)
class UploadToken:
    """Decoded token components."""

    import_id: UUID
    expiry_unix: int
    raw: str

    @property
    def sha256_hex(self) -> str:
        """Hash to compare against ``charger_log_imports.upload_token_hash``."""
        return hashlib.sha256(self.raw.encode("utf-8")).hexdigest()


def get_signing_key() -> bytes:
    """Return the HMAC key as bytes.

    Raises:
        RuntimeError: When ``CHARGER_LOG_UPLOAD_SIGNING_KEY`` is unset.
            The upload endpoint translates this to a 503; the dispatch
            path refuses to enqueue, so chargers never see a malformed
            URL.
    """
    key = os.environ.get("CHARGER_LOG_UPLOAD_SIGNING_KEY")
    if not key:
        raise RuntimeError(
            "CHARGER_LOG_UPLOAD_SIGNING_KEY is unset; charger log uploads "
            "are disabled. See CLAUDE.md for the env-var reference."
        )
    return key.encode("utf-8")


def get_token_ttl_seconds() -> int:
    raw = os.environ.get("CHARGER_LOG_UPLOAD_TOKEN_TTL_S")
    if not raw:
        return DEFAULT_TOKEN_TTL_S
    try:
        ttl = int(raw)
    except ValueError:
        return DEFAULT_TOKEN_TTL_S
    return max(60, ttl)  # never accept sub-minute TTLs


def get_max_upload_bytes() -> int:
    raw = os.environ.get("CHARGER_LOG_UPLOAD_MAX_BYTES")
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        return max(1024, int(raw))
    except ValueError:
        return DEFAULT_MAX_BYTES


def get_upload_base_url() -> Optional[str]:
    """Public URL the charger reaches. ``None`` disables dispatch.

    Without this, dispatching ``get_diagnostics`` would generate a URL
    the charger can't dial, so the dispatch helper refuses up-front.
    """
    raw = os.environ.get("CHARGER_LOG_UPLOAD_BASE_URL")
    if not raw:
        return None
    return raw.rstrip("/")


def mint_token(import_id: UUID, *, now: Optional[float] = None, ttl_s: Optional[int] = None) -> str:
    """Return the token string to embed in the upload URL."""
    ts = int(now if now is not None else time.time())
    ttl = ttl_s if ttl_s is not None else get_token_ttl_seconds()
    expiry = ts + ttl
    body = f"{import_id}.{expiry}"
    sig = hmac.new(get_signing_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_token(token: str, *, now: Optional[float] = None) -> UploadToken:
    """Verify shape, signature, and expiry. Raises UploadTokenError on any failure."""
    if not token:
        raise UploadTokenError("missing token")
    parts = token.split(".")
    if len(parts) != 3:
        raise UploadTokenError("malformed token")
    raw_id, raw_exp, raw_sig = parts
    try:
        import_id = UUID(raw_id)
        expiry = int(raw_exp)
    except (ValueError, TypeError) as exc:
        raise UploadTokenError("malformed token") from exc

    body = f"{raw_id}.{raw_exp}"
    expected = hmac.new(get_signing_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, raw_sig):
        raise UploadTokenError("bad signature")

    ts = int(now if now is not None else time.time())
    if ts >= expiry:
        raise UploadTokenError("expired")

    return UploadToken(import_id=import_id, expiry_unix=expiry, raw=token)


def build_upload_url(import_id: UUID, *, base_url: Optional[str] = None) -> str:
    """Build the ``location=`` value passed to ``GetDiagnostics``.

    Returns ``""`` only when the base URL is unset — callers check
    this before enqueuing the OCPP command.
    """
    base = base_url if base_url is not None else get_upload_base_url()
    if not base:
        raise RuntimeError(
            "CHARGER_LOG_UPLOAD_BASE_URL is unset; cannot build a usable "
            "GetDiagnostics location URL."
        )
    token = mint_token(import_id)
    query = urlencode({"token": token, "import_id": str(import_id)})
    return f"{base}?{query}"


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TOKEN_TTL_S",
    "UploadToken",
    "UploadTokenError",
    "build_upload_url",
    "get_max_upload_bytes",
    "get_signing_key",
    "get_token_ttl_seconds",
    "get_upload_base_url",
    "mint_token",
    "verify_token",
]
