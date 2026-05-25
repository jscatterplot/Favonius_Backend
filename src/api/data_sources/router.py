"""FastAPI router for the Data Sources page.

Mounted under ``/admin/data-sources`` behind ``DATA_SOURCES_ENABLED`` in
``src/api/main.py``. All endpoints require a ``customer_admin`` JWT and enforce
depot/org scope. Request bodies accept camelCase or snake_case; responses are
snake_case. Credentials are encrypted at rest and never returned.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from src.core.data_sources import errors as ds_errors
from src.core.data_sources import registry
from src.core.data_sources import repository as repo
from src.core.data_sources.ingestion import run_ingestion_job
from src.security.admin_audit import AdminAuditRow, write_admin_audit_row
from src.security.auth import get_user_id, verify_depot_access
from src.security.credential_cipher import encrypt_credentials
from src.security.tenant_mirror import ensure_tenant_mirrored

from .feature_flag import is_data_sources_ready
from .schemas import CreateConnectionRequest, UpdateConnectionRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/data-sources", tags=["data-sources"])

_PATCHABLE_STATUSES = {"active", "paused"}
_MIN_SYNC_INTERVAL = 15


# ── Pool providers (overridable in tests) ─────────────────────────────────


def get_static_pool() -> Any:
    """Return the Supabase static pool from live app state."""
    from src.api import main as api_main

    if api_main.db_pools is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return api_main.db_pools.static


def get_ts_pool() -> Any:
    """Return the TimescaleDB pool from live app state."""
    from src.api import main as api_main

    if api_main.db_pools is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return api_main.db_pools.ts


# ── Helpers ────────────────────────────────────────────────────────────────


def _require_admin_org(user: dict) -> str:
    from src.api.main import _require_customer_admin_with_org  # lazy: avoid cycle

    return _require_customer_admin_with_org(user)


def _spawn(coro: Any) -> None:
    from src.api import main as api_main  # lazy: avoid cycle

    api_main._create_background_task(coro)


def _require_ready() -> None:
    if not is_data_sources_ready():
        raise HTTPException(
            status_code=503,
            detail="Data Sources is not configured (missing encryption key)",
        )


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed: dict[str, Any] = json.loads(value)
        return parsed
    return dict(value)


def _connection_wire(rec: asyncpg.Record) -> dict[str, Any]:
    d = dict(rec)
    d["config"] = _as_dict(d.get("config"))
    return d


def _job_wire(rec: asyncpg.Record) -> dict[str, Any]:
    d = dict(rec)
    d["progress"] = _as_dict(d.get("progress"))
    return d


def _parse_before(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid 'before' timestamp: {value!r}"
        ) from exc


async def _audit(
    ts_pool: Any, *, action: str, user: dict, org_id: str, depot_id: str, target_id: str
) -> None:
    try:
        await write_admin_audit_row(
            ts_pool,
            AdminAuditRow(
                action=action,
                actor_user_id=get_user_id(user),
                actor_role="customer_admin",
                organization_id=org_id,
                depot_id=depot_id,
                target_type="data_source_connection",
                target_id=target_id,
            ),
        )
    except Exception:  # noqa: BLE001 — audit is best-effort.
        logger.exception("data-source audit write failed for action=%s", action)


# ── Endpoints ────────────────────────────────────────────────────────────


@router.get("/providers")
async def list_providers(user: dict = Depends(ensure_tenant_mirrored)) -> dict[str, Any]:
    """Return the provider catalogue that drives the dynamic connect-form."""
    _require_admin_org(user)
    return {"providers": [entry.model_dump() for entry in registry.iter_catalogue()]}


@router.post("/connections")
async def create_connection(
    body: CreateConnectionRequest,
    user: dict = Depends(ensure_tenant_mirrored),
    static_pool: Any = Depends(get_static_pool),
    ts_pool: Any = Depends(get_ts_pool),
) -> JSONResponse:
    """Create a connection, then auto-kick the first ingestion."""
    org_id = _require_admin_org(user)
    _require_ready()
    await verify_depot_access(body.depot_id, user, static_pool)

    try:
        provider = registry.get_provider(body.provider_key)
    except ds_errors.ProviderNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    interval = (
        body.sync_interval_minutes or provider.catalogue_entry().default_sync_interval_minutes
    )
    if interval < _MIN_SYNC_INTERVAL:
        raise HTTPException(
            status_code=422,
            detail=f"sync_interval_minutes must be >= {_MIN_SYNC_INTERVAL}",
        )

    try:
        await provider.validate_credentials(body.credentials, body.config)
    except ds_errors.CredentialValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"error_code": "CREDENTIAL_VALIDATION_FAILED", "detail": str(exc)},
        )

    token, version = encrypt_credentials(
        {
            "provider_key": body.provider_key,
            "secrets": body.credentials,
            "schema_version": 1,
        }
    )
    next_sync_at = datetime.now(timezone.utc) + timedelta(minutes=interval)

    try:
        conn_rec = await repo.insert_connection(
            static_pool,
            organization_id=org_id,
            site_id=body.depot_id,
            provider_key=body.provider_key,
            display_name=body.display_name,
            config=body.config,
            encrypted_credentials=token,
            encryption_version=version,
            sync_interval_minutes=interval,
            scheduled_sync_enabled=body.scheduled_sync_enabled,
            next_sync_at=next_sync_at,
            created_by=get_user_id(user),
        )
    except asyncpg.UniqueViolationError:
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "CONNECTION_ALREADY_EXISTS",
                "detail": "A connection for this depot and provider already exists",
            },
        )

    connection = _connection_wire(conn_rec)
    await _audit(
        ts_pool,
        action="data_source.connection.created",
        user=user,
        org_id=org_id,
        depot_id=body.depot_id,
        target_id=connection["id"],
    )

    # Auto-kick the first ingestion so the depot starts filling immediately.
    job_wire: Optional[dict[str, Any]] = None
    try:
        job_rec = await repo.enqueue_job(
            static_pool,
            connection_id=connection["id"],
            organization_id=org_id,
            site_id=body.depot_id,
            provider_key=body.provider_key,
            trigger="manual",
            triggered_by=get_user_id(user),
        )
        job_wire = _job_wire(job_rec)
        _spawn(run_ingestion_job(static_pool, ts_pool, job_id=job_wire["id"]))
    except asyncpg.UniqueViolationError:
        logger.info("Connection %s already has an active job", connection["id"])

    payload: dict[str, Any] = {"connection": connection}
    if job_wire is not None:
        payload["job"] = job_wire
        payload["status_url"] = f"/admin/data-sources/jobs/{job_wire['id']}"
    return JSONResponse(status_code=201, content=jsonable_encoder(payload))


@router.get("/connections")
async def list_connections(
    depot_id: Optional[str] = Query(default=None),
    user: dict = Depends(ensure_tenant_mirrored),
    static_pool: Any = Depends(get_static_pool),
) -> dict[str, Any]:
    """List the caller's connections, optionally filtered to one depot."""
    org_id = _require_admin_org(user)
    if depot_id is not None:
        await verify_depot_access(depot_id, user, static_pool)
    rows = await repo.list_connections(static_pool, organization_id=org_id, site_id=depot_id)
    return {"connections": [_connection_wire(r) for r in rows]}


@router.get("/connections/{connection_id}")
async def get_connection(
    connection_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
    static_pool: Any = Depends(get_static_pool),
) -> dict[str, Any]:
    """Fetch one connection (scoped to the caller's org)."""
    org_id = _require_admin_org(user)
    rec = await repo.get_connection(static_pool, connection_id, organization_id=org_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="connection not found")
    return _connection_wire(rec)


@router.patch("/connections/{connection_id}")
async def update_connection(
    connection_id: str,
    body: UpdateConnectionRequest,
    user: dict = Depends(ensure_tenant_mirrored),
    static_pool: Any = Depends(get_static_pool),
    ts_pool: Any = Depends(get_ts_pool),
) -> Any:
    """Patch a connection's settings or rotate its credentials."""
    org_id = _require_admin_org(user)
    rec = await repo.get_connection(static_pool, connection_id, organization_id=org_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="connection not found")

    updates: dict[str, Any] = {}
    if body.display_name is not None:
        updates["display_name"] = body.display_name
    if body.scheduled_sync_enabled is not None:
        updates["scheduled_sync_enabled"] = body.scheduled_sync_enabled
    if body.sync_interval_minutes is not None:
        if body.sync_interval_minutes < _MIN_SYNC_INTERVAL:
            raise HTTPException(
                status_code=422,
                detail=f"sync_interval_minutes must be >= {_MIN_SYNC_INTERVAL}",
            )
        updates["sync_interval_minutes"] = body.sync_interval_minutes
    if body.status is not None:
        if body.status not in _PATCHABLE_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"status must be one of {sorted(_PATCHABLE_STATUSES)}",
            )
        updates["status"] = body.status

    token: Optional[bytes] = None
    version: Optional[int] = None
    if body.credentials is not None:
        try:
            provider = registry.get_provider(rec["provider_key"])
            await provider.validate_credentials(body.credentials, _as_dict(rec["config"]))
        except ds_errors.ProviderNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ds_errors.CredentialValidationError as exc:
            return JSONResponse(
                status_code=422,
                content={
                    "error_code": "CREDENTIAL_VALIDATION_FAILED",
                    "detail": str(exc),
                },
            )
        token, version = encrypt_credentials(
            {
                "provider_key": rec["provider_key"],
                "secrets": body.credentials,
                "schema_version": 1,
            }
        )

    if not updates and token is None:
        return _connection_wire(rec)

    updated = await repo.update_connection(
        static_pool,
        connection_id,
        updates=updates,
        encrypted_credentials=token,
        encryption_version=version,
    )
    await _audit(
        ts_pool,
        action="data_source.connection.updated",
        user=user,
        org_id=org_id,
        depot_id=rec["site_id"],
        target_id=connection_id,
    )
    return _connection_wire(updated)


@router.delete("/connections/{connection_id}")
async def delete_connection(
    connection_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
    static_pool: Any = Depends(get_static_pool),
    ts_pool: Any = Depends(get_ts_pool),
) -> Response:
    """Soft-delete a connection (status='disabled')."""
    org_id = _require_admin_org(user)
    rec = await repo.get_connection(static_pool, connection_id, organization_id=org_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="connection not found")
    await repo.disable_connection(static_pool, connection_id)
    await _audit(
        ts_pool,
        action="data_source.connection.deleted",
        user=user,
        org_id=org_id,
        depot_id=rec["site_id"],
        target_id=connection_id,
    )
    return Response(status_code=204)


@router.post("/connections/{connection_id}/sync")
async def trigger_sync(
    connection_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
    static_pool: Any = Depends(get_static_pool),
    ts_pool: Any = Depends(get_ts_pool),
) -> JSONResponse:
    """Enqueue + kick a manual sync. 409 if one is already in flight."""
    org_id = _require_admin_org(user)
    _require_ready()
    rec = await repo.get_connection(static_pool, connection_id, organization_id=org_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="connection not found")
    if rec["status"] == "disabled":
        raise HTTPException(status_code=409, detail="connection is disabled")

    try:
        job_rec = await repo.enqueue_job(
            static_pool,
            connection_id=connection_id,
            organization_id=org_id,
            site_id=rec["site_id"],
            provider_key=rec["provider_key"],
            trigger="manual",
            triggered_by=get_user_id(user),
        )
    except asyncpg.UniqueViolationError:
        return JSONResponse(
            status_code=409,
            content={
                "error_code": "CONNECTION_SYNC_IN_PROGRESS",
                "detail": "A sync is already running for this connection",
            },
        )

    job_wire = _job_wire(job_rec)
    _spawn(run_ingestion_job(static_pool, ts_pool, job_id=job_wire["id"]))
    await _audit(
        ts_pool,
        action="data_source.sync.triggered",
        user=user,
        org_id=org_id,
        depot_id=rec["site_id"],
        target_id=connection_id,
    )
    return JSONResponse(
        status_code=202,
        content=jsonable_encoder(
            {"job": job_wire, "status_url": f"/admin/data-sources/jobs/{job_wire['id']}"}
        ),
    )


@router.get("/connections/{connection_id}/jobs")
async def list_jobs(
    connection_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    before: Optional[str] = Query(default=None),
    user: dict = Depends(ensure_tenant_mirrored),
    static_pool: Any = Depends(get_static_pool),
) -> dict[str, Any]:
    """List a connection's sync history (newest first)."""
    org_id = _require_admin_org(user)
    rec = await repo.get_connection(static_pool, connection_id, organization_id=org_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="connection not found")
    rows = await repo.list_jobs(
        static_pool, connection_id, limit=limit, before=_parse_before(before)
    )
    return {"jobs": [_job_wire(r) for r in rows]}


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
    static_pool: Any = Depends(get_static_pool),
) -> dict[str, Any]:
    """Poll one ingestion job's status/progress (scoped to the caller's org)."""
    org_id = _require_admin_org(user)
    rec = await repo.get_job(static_pool, job_id, organization_id=org_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_wire(rec)
