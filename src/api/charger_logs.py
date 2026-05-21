"""Charger-side log import orchestration.

Lives behind the upload endpoint and the trigger endpoint to keep
:mod:`src.api.main` lean. Three responsibilities:

* :func:`receive_upload` — stream the body into ``charger_log_imports``,
  enforce size cap, recompute the token hash, flip status to
  ``'received'``.
* :func:`parse_and_persist_entries` — pick the vendor parser via
  :mod:`src.adapters.chargers`, walk the entries, bulk-insert into
  ``charger_session_log_entries``, flip status to ``'parsed'``.
* :func:`reconcile_and_finalize` — run
  :func:`src.core.reconciliation.session_log_reconciliation.reconcile_session_log`
  and flip status to ``'reconciled'``.

The upload endpoint does (1) synchronously (so the charger gets a fast
ack) and schedules (2)+(3) as a post-commit ``asyncio.create_task``,
matching the pattern :class:`TimescaleClient.close_open_session` uses
for the session-cost calculator.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import asyncpg

from ..adapters.chargers import ChargerLogParseError, get_parser_for_vendor
from ..adapters.chargers.upload_token import (
    UploadTokenError,
    get_max_upload_bytes,
    verify_token,
)
from ..core.reconciliation import (
    ReconciliationResult,
    reconcile_session_log,
    write_session_log_reconciliation,
)
from ..monitoring.metrics import (
    CHARGER_LOG_IMPORTS,
    CHARGER_LOG_PARSE_FAILURES,
    CHARGER_LOG_RECONCILIATIONS,
)

logger = logging.getLogger(__name__)


class UploadRejected(Exception):
    """Upload could not be associated with a valid import row.

    Carries an HTTP status code so the endpoint can raise a matching
    HTTPException without leaking the underlying reason in the body.
    """

    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


@dataclass(frozen=True)
class UploadReceiveResult:
    """Successful upload metadata returned to the endpoint caller."""

    import_id: UUID
    session_id: Optional[UUID]
    station_id: str
    vendor: Optional[str]
    file_size_bytes: int
    content_sha256: str


async def receive_upload(
    ts_pool: asyncpg.Pool,
    *,
    token: str,
    body: bytes,
    file_name: Optional[str] = None,
) -> UploadReceiveResult:
    """Validate the token, persist the body, flip the row to ``'received'``.

    The body has already been read into memory by the FastAPI handler
    (subject to the cap from ``get_max_upload_bytes``). Storing as
    BYTEA on the same TimescaleDB pool keeps the blob colocated with
    ``charging_sessions`` per the plan in
    ``/root/.claude/plans/i-want-to-add-tranquil-torvalds.md``.

    Raises:
        UploadRejected: token invalid / expired (401), import not found
            (404), token hash mismatches the recorded hash (401), body
            too large (413), or import already terminal (409).
    """
    max_bytes = get_max_upload_bytes()
    if len(body) > max_bytes:
        raise UploadRejected(413, "upload exceeds max size")
    if len(body) == 0:
        raise UploadRejected(400, "empty body")

    try:
        decoded = verify_token(token)
    except UploadTokenError as exc:
        raise UploadRejected(401, f"invalid token: {exc}") from exc

    expected_hash = decoded.sha256_hex
    content_sha256 = hashlib.sha256(body).hexdigest()

    async with ts_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT id, session_id, station_id, vendor, status, upload_token_hash
                  FROM charger_log_imports
                 WHERE id = $1
                 FOR UPDATE
                """,
                decoded.import_id,
            )
            if row is None:
                raise UploadRejected(404, "import not found")
            if row["upload_token_hash"] not in (None, expected_hash):
                raise UploadRejected(401, "token mismatch")
            if row["status"] in ("received", "parsed", "reconciled"):
                # Re-upload of an already-received import. The token
                # hash matched so we trust the caller, but we won't
                # silently overwrite — return 409 so the operator
                # explicitly re-requests via a fresh GetDiagnostics.
                raise UploadRejected(409, "import already received")
            if row["status"] in ("failed", "expired"):
                raise UploadRejected(409, f"import is {row['status']}")

            await conn.execute(
                """
                UPDATE charger_log_imports
                   SET raw_payload     = $2,
                       file_size_bytes = $3,
                       content_sha256  = $4,
                       file_name       = COALESCE($5, file_name),
                       received_at     = NOW(),
                       status          = 'received'
                 WHERE id = $1
                """,
                decoded.import_id,
                body,
                len(body),
                content_sha256,
                file_name,
            )

    CHARGER_LOG_IMPORTS.labels(vendor=row["vendor"] or "unknown", status="received").inc()
    return UploadReceiveResult(
        import_id=decoded.import_id,
        session_id=row["session_id"],
        station_id=row["station_id"],
        vendor=row["vendor"],
        file_size_bytes=len(body),
        content_sha256=content_sha256,
    )


async def parse_and_persist_entries(
    ts_pool: asyncpg.Pool,
    *,
    import_id: UUID,
) -> int:
    """Run the per-vendor parser and insert ``charger_session_log_entries``.

    Returns the number of entries inserted. Flips the import row to
    ``'parsed'`` (or ``'failed'`` on parser exception). Unsupported
    vendor → status stays ``'received'`` so a future parser can pick
    it up; we don't mark it failed because the raw blob is still
    investigable.
    """
    async with ts_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, station_id, charger_id, connector_id, session_id,
                   vendor, raw_payload, status
              FROM charger_log_imports
             WHERE id = $1
            """,
            import_id,
        )
    if row is None:
        return 0
    if row["status"] not in ("received",):
        # Already parsed/reconciled (idempotent re-invoke), or in a
        # terminal failed state — nothing to do.
        return 0
    if row["raw_payload"] is None:
        await _mark_import_failed(ts_pool, import_id, "raw_payload is NULL")
        CHARGER_LOG_IMPORTS.labels(
            vendor=row["vendor"] or "unknown", status="failed"
        ).inc()
        return 0

    parser = get_parser_for_vendor(row["vendor"])
    if parser is None:
        CHARGER_LOG_PARSE_FAILURES.labels(
            vendor=row["vendor"] or "unknown", reason="unsupported_vendor"
        ).inc()
        logger.info(
            "No parser registered for vendor=%s (import=%s); raw blob retained",
            row["vendor"],
            import_id,
        )
        return 0

    try:
        entries = list(parser(bytes(row["raw_payload"])))
    except ChargerLogParseError as exc:
        await _mark_import_failed(ts_pool, import_id, f"parse_failed: {exc}")
        CHARGER_LOG_IMPORTS.labels(
            vendor=row["vendor"] or "unknown", status="failed"
        ).inc()
        CHARGER_LOG_PARSE_FAILURES.labels(
            vendor=row["vendor"] or "unknown", reason="corrupt_archive"
        ).inc()
        return 0
    except Exception as exc:  # noqa: BLE001
        await _mark_import_failed(ts_pool, import_id, f"parser exception: {exc}")
        CHARGER_LOG_IMPORTS.labels(
            vendor=row["vendor"] or "unknown", status="failed"
        ).inc()
        CHARGER_LOG_PARSE_FAILURES.labels(
            vendor=row["vendor"] or "unknown", reason="parse_exception"
        ).inc()
        return 0

    if not entries:
        # Empty parse is not an error — the operator still has the
        # raw blob and can investigate. The reconciler will land
        # ``source='no_log_entries'``.
        async with ts_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE charger_log_imports
                   SET status     = 'parsed',
                       parsed_at  = NOW()
                 WHERE id = $1
                   AND status = 'received'
                """,
                import_id,
            )
        CHARGER_LOG_IMPORTS.labels(
            vendor=row["vendor"] or "unknown", status="parsed"
        ).inc()
        CHARGER_LOG_PARSE_FAILURES.labels(
            vendor=row["vendor"] or "unknown", reason="no_entries"
        ).inc()
        return 0

    rows = [
        (
            e.time,
            row["station_id"],
            row["charger_id"],
            e.connector_id if e.connector_id is not None else row["connector_id"],
            e.transaction_id,
            import_id,
            e.soc,
            e.charging_kw,
            e.energy_kwh,
            _json_or_null(e.raw_fields),
        )
        for e in entries
    ]

    async with ts_pool.acquire() as conn:
        async with conn.transaction():
            # copy_records_to_table would be faster on big logs but
            # ABB diagnostics rarely exceed a few hundred rows; INSERT
            # batches keep the SQL legible and reuse the same conn.
            await conn.executemany(
                """
                INSERT INTO charger_session_log_entries (
                    time, station_id, charger_id, connector_id, transaction_id,
                    log_import_id, soc, charging_kw, energy_kwh, raw_fields
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, $7, $8, $9, $10::jsonb
                )
                """,
                rows,
            )
            await conn.execute(
                """
                UPDATE charger_log_imports
                   SET status     = 'parsed',
                       parsed_at  = NOW()
                 WHERE id = $1
                   AND status = 'received'
                """,
                import_id,
            )

    CHARGER_LOG_IMPORTS.labels(vendor=row["vendor"] or "unknown", status="parsed").inc()
    logger.info(
        "Parsed %d entries for import=%s vendor=%s",
        len(entries),
        import_id,
        row["vendor"],
    )
    return len(entries)


async def reconcile_and_finalize(
    ts_pool: asyncpg.Pool,
    *,
    import_id: UUID,
) -> Optional[ReconciliationResult]:
    """Run the reconciler and flip the import row to ``'reconciled'``.

    Returns the :class:`ReconciliationResult`. When the import has no
    associated ``session_id`` (depot-wide pull, future), returns
    ``None`` and leaves status at ``'parsed'``.
    """
    async with ts_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, session_id, vendor, status
              FROM charger_log_imports
             WHERE id = $1
            """,
            import_id,
        )
    if row is None or row["status"] != "parsed":
        return None
    if row["session_id"] is None:
        return None

    result = await reconcile_session_log(
        ts_pool,
        session_id=row["session_id"],
        log_import_id=import_id,
    )
    await write_session_log_reconciliation(
        ts_pool,
        session_id=row["session_id"],
        log_import_id=import_id,
        result=result,
    )

    async with ts_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE charger_log_imports
               SET status         = 'reconciled',
                   reconciled_at  = NOW()
             WHERE id = $1
               AND status = 'parsed'
            """,
            import_id,
        )

    CHARGER_LOG_IMPORTS.labels(vendor=row["vendor"] or "unknown", status="reconciled").inc()
    CHARGER_LOG_RECONCILIATIONS.labels(source=result.source).inc()
    return result


async def run_post_upload_pipeline(
    ts_pool: asyncpg.Pool,
    *,
    import_id: UUID,
) -> None:
    """Parse + reconcile, used as a post-commit ``asyncio.create_task``.

    Wraps both stages in a single coroutine so the upload endpoint can
    schedule the heavy work without blocking the charger's ack. Errors
    are logged; the row is left in whatever non-terminal state the
    pipeline last achieved, and a follow-up call can resume from there
    (the per-stage status guards make it idempotent).
    """
    try:
        await parse_and_persist_entries(ts_pool, import_id=import_id)
        await reconcile_and_finalize(ts_pool, import_id=import_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Post-upload pipeline failed for import=%s: %s", import_id, exc
        )


def schedule_post_upload_pipeline(
    ts_pool: asyncpg.Pool,
    *,
    import_id: UUID,
    background_tasks: Optional[set] = None,
) -> asyncio.Task:
    """Schedule :func:`run_post_upload_pipeline` as a background task.

    Adds the task to ``background_tasks`` (the main.py-level set) so
    it isn't garbage-collected mid-execution, mirroring the pattern
    used by other fire-and-forget tasks in the codebase.
    """
    task = asyncio.create_task(run_post_upload_pipeline(ts_pool, import_id=import_id))
    if background_tasks is not None:
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
    return task


async def _mark_import_failed(
    ts_pool: asyncpg.Pool, import_id: UUID, reason: str
) -> None:
    async with ts_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE charger_log_imports
               SET status        = 'failed',
                   error_message = $2
             WHERE id = $1
               AND status NOT IN ('reconciled', 'expired')
            """,
            import_id,
            reason[:500],
        )


def _json_or_null(value: Optional[dict]) -> Optional[str]:
    import json

    if not value:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return None


__all__ = [
    "UploadRejected",
    "UploadReceiveResult",
    "parse_and_persist_entries",
    "receive_upload",
    "reconcile_and_finalize",
    "run_post_upload_pipeline",
    "schedule_post_upload_pipeline",
]
