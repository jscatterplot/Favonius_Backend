"""Traffic-fine upload + read endpoints (admin, depot-scoped).

An operator uploads a fine document (PDF or image); it is stored and a
post-commit background task runs the ``traffic_fine_triage`` workflow
(multimodal extraction → deterministic deadline evaluation → alert to the
Logistics Manager). Mounted behind ``TRAFFIC_FINE_AGENT_ENABLED``.

Raw-body upload (no python-multipart dependency), mirroring the charger-log
upload: validate cheaply, stream the body under a hard byte cap, store, then
schedule triage. Reads are depot-scoped; uploads require customer_admin or
favonius_admin.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from src.api.agent_workflows.traffic_fine import triage_traffic_fine
from src.core.traffic_fines import config, media, repository
from src.security.admin_audit import AdminAuditRow, write_admin_audit_row
from src.security.auth import get_user_id, get_user_role, verify_depot_access
from src.security.tenant_mirror import ensure_tenant_mirrored
from src.security.validators import validate_uuid

logger = logging.getLogger(__name__)

router = APIRouter(tags=["traffic-fines"])

_ALLOWED_UPLOAD_ROLES = {"customer_admin", "favonius_admin"}

# Keep strong refs to in-flight triage tasks so they aren't garbage-collected
# before they finish (asyncio holds only weak references).
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _get_db_pools() -> Any:
    """Return the app's DatabasePools (lazy import avoids a main.py cycle)."""
    from src.api import main as _main  # noqa: PLC0415

    if _main.db_pools is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error_code": "DATABASE_UNAVAILABLE", "message": "Database not available"},
        )
    return _main.db_pools


def _spawn(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def _read_capped_body(request: Request, max_bytes: int) -> bytes:
    """Stream the request body with a hard ceiling (defends chunked uploads)."""
    body = bytearray()
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            if len(body) + len(chunk) > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "error_code": "TRAFFIC_FINE_UPLOAD_TOO_LARGE",
                        "message": "upload exceeds max size",
                    },
                )
            body.extend(chunk)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — treat a mid-stream drop as a client error
        raise HTTPException(
            status_code=400,
            detail={"error_code": "TRAFFIC_FINE_UPLOAD_FAILED", "message": f"stream error: {exc}"},
        ) from exc
    return bytes(body)


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    """Public JSON view of a fine row (FastAPI encodes UUID/datetime/Decimal)."""
    out = dict(row)
    out["extraction"] = repository.coerce_jsonb(out.get("extraction"))
    return out


@router.post(
    "/admin/depots/{depot_id}/traffic-fines",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a traffic-fine document for triage",
)
async def upload_traffic_fine(
    depot_id: str,
    request: Request,
    file_name: Optional[str] = Query(None, description="Optional original filename"),
    user: dict = Depends(ensure_tenant_mirrored),
) -> dict[str, Any]:
    """Store an uploaded fine and schedule the triage workflow.

    Accepts a raw PDF/image body (Content-Type informational; the type is
    confirmed by magic bytes). Returns 202 with a ``status_url`` to poll.
    """
    validate_uuid(depot_id, field_name="depot_id")
    pools = _get_db_pools()
    await verify_depot_access(depot_id, user, pools.static)

    role = get_user_role(user)
    if role not in _ALLOWED_UPLOAD_ROLES:
        raise HTTPException(
            status_code=403,
            detail={
                "error_code": "FORBIDDEN",
                "message": "customer_admin or favonius_admin required to upload fines",
            },
        )

    depot_uuid = UUID(depot_id)
    org_id = await pools.static.fetchval(
        "SELECT organization_id FROM sites WHERE id = $1", depot_uuid
    )
    if org_id is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "DEPOT_NOT_FOUND", "message": "depot not found"},
        )

    body = await _read_capped_body(request, config.upload_max_bytes())
    if not body:
        raise HTTPException(
            status_code=400,
            detail={"error_code": "TRAFFIC_FINE_EMPTY", "message": "empty upload"},
        )

    media_type = media.detect_media_type(body)
    if not media.is_supported(media_type):
        raise HTTPException(
            status_code=415,
            detail={
                "error_code": "TRAFFIC_FINE_UNSUPPORTED_MEDIA",
                "message": "upload must be a PDF or PNG/JPEG/GIF/WEBP image",
            },
        )

    uploaded_by = UUID(get_user_id(user))
    content_type = request.headers.get("content-type") or media_type

    fine_id = await repository.insert_received_fine(
        pools.ts,
        depot_id=depot_uuid,
        organization_id=org_id,
        uploaded_by=uploaded_by,
        content_type=content_type,
        file_name=file_name,
        raw_payload=body,
    )

    await write_admin_audit_row(
        pools.ts,
        AdminAuditRow(
            action="traffic_fine.uploaded",
            actor_user_id=str(uploaded_by),
            actor_role=role,
            organization_id=str(org_id),
            depot_id=depot_id,
            target_type="traffic_fine",
            target_id=str(fine_id),
            metadata={"content_type": content_type, "byte_size": len(body)},
        ),
    )

    # Post-commit: run extraction + evaluation + alert off the request path.
    _spawn(triage_traffic_fine(fine_id, ts_pool=pools.ts, static_pool=pools.static))

    return {
        "id": str(fine_id),
        "status": "received",
        "status_url": f"/admin/depots/{depot_id}/traffic-fines/{fine_id}",
    }


@router.get(
    "/admin/depots/{depot_id}/traffic-fines",
    summary="List uploaded traffic fines for a depot",
)
async def list_traffic_fines(
    depot_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
) -> dict[str, Any]:
    """List a depot's fines, most recent first (no raw document bytes)."""
    validate_uuid(depot_id, field_name="depot_id")
    pools = _get_db_pools()
    await verify_depot_access(depot_id, user, pools.static)
    rows = await repository.list_fines(pools.ts, UUID(depot_id), limit=100)
    return {"fines": [_serialize(r) for r in rows]}


@router.get(
    "/admin/depots/{depot_id}/traffic-fines/{fine_id}",
    summary="Get one traffic fine and its triage result",
)
async def get_traffic_fine(
    depot_id: str,
    fine_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
) -> dict[str, Any]:
    """Return one fine's parsed fields, evaluation, and alert/decision links."""
    validate_uuid(depot_id, field_name="depot_id")
    validate_uuid(fine_id, field_name="fine_id")
    pools = _get_db_pools()
    await verify_depot_access(depot_id, user, pools.static)
    row = await repository.get_fine_public(pools.ts, UUID(depot_id), UUID(fine_id))
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": "TRAFFIC_FINE_NOT_FOUND", "message": "fine not found"},
        )
    return _serialize(row)
