"""Durable ingestion-job driver.

``run_ingestion_job`` is invoked from three places — the manual /sync endpoint,
the scheduler, and the startup recovery sweep — always against an already-
inserted job row. It claims the row, decrypts the connection's credentials,
runs the provider, and writes the terminal state. It never raises: a background
task that died silently is the failure mode we explicitly guard against with the
job row + heartbeat + recovery sweep.

Security: decrypted credentials live only in the in-memory IngestionContext and
are never logged, never written to the job row, and never returned by the API.
Freshly-minted OCPP charger passwords (first import) are likewise discarded —
the operator sets them via the existing rotate-credentials endpoint; the job
just flags ``credentials_pending_rotation``.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from uuid import uuid4

import asyncpg

from src.security.admin_audit import AdminAuditRow, write_admin_audit_row
from src.security.credential_cipher import CredentialCipherError, decrypt_credentials

from . import repository as repo
from .base import IngestionContext
from .errors import ProviderNotFound
from .registry import get_provider

logger = logging.getLogger(__name__)

_ERROR_DETAIL_MAX = 2000


class _ProgressHandle:
    """Concrete :class:`~.base.JobProgressHandle` bound to a job row."""

    def __init__(self, static_pool: asyncpg.Pool, job_id: str) -> None:
        self._pool = static_pool
        self._job_id = job_id

    async def update(self, *, stage: Optional[str] = None, **counters: int) -> None:
        """Merge stage + cumulative counters into the job's progress JSONB."""
        fields: dict[str, Any] = dict(counters)
        if stage is not None:
            fields["stage"] = stage
        await repo.merge_job_progress(self._pool, self._job_id, fields)


async def run_ingestion_job(
    static_pool: asyncpg.Pool,
    ts_pool: asyncpg.Pool,
    *,
    job_id: str,
    allow_stale_running_claim: bool = False,
) -> None:
    """Execute one ingestion job to a terminal state. Never raises."""
    try:
        await _run(
            static_pool,
            ts_pool,
            job_id=job_id,
            allow_stale_running_claim=allow_stale_running_claim,
        )
    except Exception:  # noqa: BLE001 — background task: log, never propagate.
        logger.exception("Data-source ingestion job %s crashed", job_id)
        try:
            await repo.finalize_job(
                static_pool,
                job_id,
                status="failed",
                progress={"stage": "error"},
                error_detail="internal error (see server logs)",
                import_batch_id=None,
                allow_pending=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to mark job %s failed after crash", job_id)


async def _run(
    static_pool: asyncpg.Pool,
    ts_pool: asyncpg.Pool,
    *,
    job_id: str,
    allow_stale_running_claim: bool,
) -> None:
    claimed = await repo.claim_job(
        static_pool,
        job_id,
        allow_running_reclaim=allow_stale_running_claim,
    )
    if claimed is None:
        logger.info("Job %s already terminal or gone; skipping", job_id)
        return

    connection_id = claimed["connection_id"]
    secret_row = await repo.get_connection_secret(static_pool, connection_id)
    if secret_row is None:
        await repo.finalize_job(
            static_pool,
            job_id,
            status="failed",
            progress={"stage": "error"},
            error_detail="connection no longer exists",
            import_batch_id=None,
        )
        return

    # Decrypt credentials (in-memory only).
    try:
        payload = decrypt_credentials(
            secret_row["encrypted_credentials"], secret_row["encryption_version"]
        )
    except CredentialCipherError as exc:
        logger.warning("Job %s: credential decryption failed: %s", job_id, exc)
        await _finalize(
            static_pool,
            ts_pool,
            job=claimed,
            status="failed",
            progress={"stage": "error"},
            error_detail="credential decryption failed (key rotated?)",
            import_batch_id=None,
        )
        return

    try:
        provider = get_provider(secret_row["provider_key"])
    except ProviderNotFound as exc:
        await _finalize(
            static_pool,
            ts_pool,
            job=claimed,
            status="failed",
            progress={"stage": "error"},
            error_detail=str(exc),
            import_batch_id=None,
        )
        return

    config = _as_dict(secret_row["config"])
    batch_id = uuid4()
    ctx = IngestionContext(
        connection_id=connection_id,
        job_id=job_id,
        depot_id=secret_row["site_id"],
        organization_id=secret_row["organization_id"],
        credentials=payload.get("secrets", {}),
        config=config,
        static_pool=static_pool,
        ts_pool=ts_pool,
        progress=_ProgressHandle(static_pool, job_id),
        batch_id=batch_id,
    )

    try:
        result = await provider.run_ingestion(ctx)
    except Exception as exc:  # noqa: BLE001 — provider should be self-contained.
        logger.exception("Job %s: provider raised", job_id)
        await _finalize(
            static_pool,
            ts_pool,
            job=claimed,
            status="failed",
            progress={"stage": "error"},
            error_detail=_truncate(str(exc)),
            import_batch_id=str(batch_id),
        )
        return

    final_progress = {
        "stage": "done",
        "chargers_created": result.chargers_created,
        "vehicles_created": result.vehicles_created,
        "sessions_inserted": result.sessions_inserted,
        "access_rows_upserted": result.access_rows_upserted,
        "skipped_count": len(result.skipped),
        "credentials_pending_rotation": result.chargers_created > 0,
    }
    await _finalize(
        static_pool,
        ts_pool,
        job=claimed,
        status=result.status,
        progress=final_progress,
        error_detail=_truncate(result.error_detail) if result.error_detail else None,
        import_batch_id=str(batch_id),
    )


async def _finalize(
    static_pool: asyncpg.Pool,
    ts_pool: asyncpg.Pool,
    *,
    job: asyncpg.Record,
    status: str,
    progress: dict[str, Any],
    error_detail: Optional[str],
    import_batch_id: Optional[str],
) -> None:
    """Write the job's terminal state, update the connection, and audit."""
    await repo.finalize_job(
        static_pool,
        job["id"],
        status=status,
        progress=progress,
        error_detail=error_detail,
        import_batch_id=import_batch_id,
    )
    await repo.touch_connection_after_run(static_pool, job["connection_id"], last_status=status)
    # Best-effort completion audit (counts only — never secrets).
    audit_meta = {k: v for k, v in progress.items() if k != "stage"}
    audit_meta["trigger"] = job["trigger"]
    try:
        await write_admin_audit_row(
            ts_pool,
            AdminAuditRow(
                action="data_source.sync.completed",
                actor_user_id=job["triggered_by"],
                actor_role="system",
                organization_id=job["organization_id"],
                depot_id=job["site_id"],
                target_type="data_source_connection",
                target_id=job["connection_id"],
                metadata={"status": status, "provider": job["provider_key"], **audit_meta},
            ),
        )
    except Exception:  # noqa: BLE001 — audit is best-effort.
        logger.exception("Failed to write completion audit for job %s", job["id"])


def _as_dict(value: Any) -> dict[str, Any]:
    """Normalise an asyncpg JSONB value (str or dict) into a dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json

        parsed: dict[str, Any] = json.loads(value)
        return parsed
    return dict(value)


def _truncate(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return text if len(text) <= _ERROR_DETAIL_MAX else text[:_ERROR_DETAIL_MAX] + "…"
