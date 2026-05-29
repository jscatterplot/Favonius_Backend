"""In-process ring buffer of recent log records, for support bundles.

This service has no log store — logs go to stdout/stderr. The
context-aware support summary ("This isn't right") needs *the last few
minutes of FastAPI logs* at the instant a user reports a problem, so we
attach a bounded, thread-safe ring buffer to the root logger at startup
and read back from it on demand.

The buffer is bounded by **both** record count and age so memory stays
small even under bursty-then-idle traffic. It mirrors the get/set
singleton discipline of :mod:`src.security.audit_log`, but is
read-oriented (no DB flush).

Known limitation (accepted, documented): the buffer is per-process and
lost on restart; under multiple uvicorn workers only the worker that
served the report has its logs. A durable application-log table is the
documented upgrade path if cross-worker / crash-surviving logs are later
required.
"""

from __future__ import annotations

import logging
import re
import threading
import time as _time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BufferedRecord:
    """A lightweight, immutable snapshot of a log record.

    Stored instead of the live :class:`logging.LogRecord` so the buffer
    does not pin large objects passed as log arguments — the message is
    rendered once, at capture time.
    """

    ts: float
    level: int
    levelname: str
    logger_name: str
    message: str


# Ordered redaction passes applied to log text *before it leaves the
# process* (when building/persisting a support bundle). Kept out of the
# hot ``emit`` path — redacting every log line would be wasteful while the
# data never leaves memory. Order matters: more specific patterns first.
#
# Best-effort denylist, deliberately biased toward OVER-redaction (losing a
# little log context beats leaking a secret in a GDPR-bounded bundle). It
# covers the log shapes this service actually produces: dict/JSON
# (``{"password": "x"}``), key=value / key: value, connection-string
# passwords, bearer/JWT, prefixed API keys, and long hex blobs.
_SECRET_KEYS = (
    r"authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|secret|token|password|passwd|pwd"
)
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # JSON Web Tokens (header.payload.signature, base64url).
    (
        re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"),
        "[REDACTED_JWT]",
    ),
    # Email addresses.
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[REDACTED_EMAIL]"),
    # Connection-string passwords: scheme://user:PASSWORD@host
    (re.compile(r"(://[^:/?#\s]+:)([^@\s]+)(@)"), r"\1[REDACTED]\3"),
    # key=value / key: value secrets. Handles quoted keys ({"password": ...})
    # and quoted values; an UNQUOTED value runs until the next "<ident>=/:"
    # field or end-of-line, so space-bearing values are fully covered (this
    # over-redacts the rest of the field rather than leaking the tail). Running
    # before the bare-bearer pass means "Authorization: Bearer x" collapses to
    # one "[REDACTED]" instead of double-redacting.
    (
        re.compile(
            r"""(?ix)
            (["']?\b(?:"""
            + _SECRET_KEYS
            + r""")\b["']?\s*[:=]\s*)
            ("[^"]*"|'[^']*'|(?:(?!\s+[\w.-]+\s*[:=])[^,;}\n\r])+)
            """
        ),
        r"\1[REDACTED]",
    ),
    # Bare "Bearer <token>" not already handled by the key=value pass above.
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    # Prefixed API keys (Stripe/OpenAI-style: sk-, sk_live_, pk_, rk_, ak_).
    (re.compile(r"\b(?:sk|pk|rk|ak)[-_][A-Za-z0-9_-]{8,}"), "[REDACTED_KEY]"),
    # Long hex blobs (>= 32 chars) — likely keys/hashes/credentials.
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "[REDACTED_HEX]"),
]


def redact_log_line(text: Optional[str]) -> str:
    """Scrub likely-PII / secrets from a single line of log text.

    Idempotent and defensive: returns ``""`` for falsy input and never
    raises. Applied by the support-summary consumer to both the log
    excerpt and the most-recent-error line, and to the user note.
    """
    if not text:
        return ""
    out = str(text)
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    return out


class RingBufferLogHandler(logging.Handler):
    """Bounded, thread-safe in-memory ring buffer of recent log records.

    Capped by ``max_records`` (count) and ``max_age_seconds`` (age). A
    ``time_fn`` injection point keeps age eviction deterministic in tests.
    """

    def __init__(
        self,
        *,
        max_records: int,
        max_age_seconds: float,
        time_fn: Callable[[], float] = _time.time,
    ) -> None:
        super().__init__()
        self._max_records = max(1, int(max_records))
        self._max_age = float(max_age_seconds)
        self._time_fn = time_fn
        self._lock = threading.Lock()
        # maxlen gives O(1) count-eviction for free.
        self._records: deque[BufferedRecord] = deque(maxlen=self._max_records)

    def emit(self, record: logging.LogRecord) -> None:
        """Append a rendered snapshot of ``record``; never raise.

        A logging handler that raises corrupts the emitting call site, so
        any failure is routed to :meth:`logging.Handler.handleError`.
        """
        try:
            buffered = BufferedRecord(
                ts=self._time_fn(),
                level=record.levelno,
                levelname=record.levelname,
                logger_name=record.name,
                message=record.getMessage(),
            )
            with self._lock:
                self._records.append(buffered)
                self._prune_locked()
        except Exception:  # noqa: BLE001 - handlers must never raise
            self.handleError(record)

    def _prune_locked(self) -> None:
        """Drop records older than the age cap. Caller holds the lock."""
        if self._max_age <= 0:
            return
        cutoff = self._time_fn() - self._max_age
        records = self._records
        while records and records[0].ts < cutoff:
            records.popleft()

    def get_records(self, *, since_seconds: float) -> list[BufferedRecord]:
        """Return records captured within the last ``since_seconds``, oldest first."""
        cutoff = self._time_fn() - since_seconds
        with self._lock:
            return [r for r in self._records if r.ts >= cutoff]

    def most_recent_error(
        self, *, since_seconds: Optional[float] = None
    ) -> Optional[BufferedRecord]:
        """Return the newest ``ERROR``+ record within the window, or ``None``.

        Scans right-to-left (newest first). When ``since_seconds`` is given,
        records older than the window are not considered.
        """
        cutoff = (self._time_fn() - since_seconds) if since_seconds is not None else None
        with self._lock:
            for record in reversed(self._records):
                if cutoff is not None and record.ts < cutoff:
                    break
                if record.level >= logging.ERROR:
                    return record
        return None

    def clear(self) -> None:
        """Drop all buffered records (test/shutdown hook)."""
        with self._lock:
            self._records.clear()


# ── Module-level singleton ────────────────────────────────────────────────────

_log_buffer: Optional[RingBufferLogHandler] = None
_install_lock = threading.Lock()


def get_log_buffer() -> Optional[RingBufferLogHandler]:
    """Return the installed ring buffer, or ``None`` if not installed.

    Callers (e.g. the support endpoint) must tolerate ``None`` — the buffer
    is only present when :func:`install_log_buffer` ran at app startup, which
    is not the case in unit tests that don't exercise the lifespan.
    """
    return _log_buffer


def install_log_buffer(
    *,
    max_records: int,
    max_age_seconds: float,
    level: int = logging.INFO,
) -> RingBufferLogHandler:
    """Install the ring buffer on the root logger (idempotent).

    Attaching to the root logger captures every ``logging.getLogger(__name__)``
    logger in the process — including the request/response logs emitted by
    ``LoggingMiddleware`` — which is exactly the "last few minutes of FastAPI
    logs" the support summary needs. If already installed, returns the existing
    handler unchanged.

    Only the *handler's* level is set; the root logger's level is left alone so
    installing the buffer never overrides the operator's ``LOG_LEVEL`` or
    changes global stdout verbosity. The buffer captures whatever the
    configured logging already propagates.
    """
    global _log_buffer
    with _install_lock:
        if _log_buffer is not None:
            return _log_buffer
        handler = RingBufferLogHandler(max_records=max_records, max_age_seconds=max_age_seconds)
        handler.setLevel(level)
        logging.getLogger().addHandler(handler)
        _log_buffer = handler
        return handler


def uninstall_log_buffer() -> None:
    """Detach and clear the ring buffer (test/shutdown hook)."""
    global _log_buffer
    with _install_lock:
        if _log_buffer is None:
            return
        logging.getLogger().removeHandler(_log_buffer)
        _log_buffer.clear()
        _log_buffer = None


__all__ = [
    "BufferedRecord",
    "RingBufferLogHandler",
    "redact_log_line",
    "get_log_buffer",
    "install_log_buffer",
    "uninstall_log_buffer",
]
