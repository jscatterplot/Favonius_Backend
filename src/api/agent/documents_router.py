"""HTTP endpoints for the agent document-fill path (migration 047).

Mounted under ``/agent`` in ``src/api/main.py`` behind
``AGENT_DOC_FILL_ENABLED`` (off by default). Three routes:

- ``POST /agent/documents``                       — upload a DOCX/PDF; stores
  the template AND opens a fill session; returns ``{session_id, document_id, …}``.
- ``GET  /agent/documents/{output_id}/download``   — download a rendered output.
- ``GET  /agent/documents/sessions/{session_id}``  — the session's working state.

The collaborative fill itself happens via ``POST /agent/turn`` with the
returned ``session_id`` (handled in ``controller._run_document_fill_turn``).

Auth: JWT on every route. The upload builds the full :class:`AuthContext`
(depot scope); download/session reads are owner-scoped (the session's
``user_id`` must match the caller, or the caller is ``favonius_admin``) —
cross-user / cross-tenant reads return 404 so we never leak existence.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)

from src.api.agent import document_render, document_store
from src.api.agent.auth_context import build_auth_context
from src.api.agent.router import get_static_pool, get_ts_pool
from src.security.auth import get_user_id, get_user_role, verify_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent-documents"])

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MIME = "application/pdf"
_DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MiB


def get_doc_upload_max_bytes() -> int:
    """Per-upload size cap (read at call time so tests can monkeypatch env)."""
    try:
        return max(1, int(os.environ.get("AGENT_DOC_UPLOAD_MAX_BYTES", _DEFAULT_MAX_UPLOAD_BYTES)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_UPLOAD_BYTES


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Stream the upload into memory, enforcing ``max_bytes`` (413 if over)."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Document exceeds the maximum upload size",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    depot_id: Optional[UUID] = Query(default=None),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    user: dict = Depends(verify_token),
    static_pool: Any = Depends(get_static_pool),
    ts_pool: Any = Depends(get_ts_pool),
) -> dict[str, Any]:
    """Upload a DOCX/PDF template (or finished old report) and open a session."""
    auth = await build_auth_context(user, static_pool)

    body = await _read_capped(file, get_doc_upload_max_bytes())
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file")

    kind = document_render.sniff_kind(body)
    if kind is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only DOCX and PDF documents are supported",
        )

    try:
        extracted = document_render.extract(body, kind)
    except document_render.DocumentRenderError as exc:
        logger.info("document upload extract failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Could not read the document (it may be corrupt or password-protected)",
        ) from exc

    # Optional depot scope hint — must be one the caller can see (404, no leak).
    if depot_id is not None and depot_id not in set(auth.visible_depot_ids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Depot not found")

    import hashlib

    detected_fields = [{"name": f.name, "source": f.source} for f in extracted.fields]
    template_id = await document_store.store_template(
        ts_pool,
        organization_id=auth.organization_id,
        depot_id=depot_id,
        uploaded_by=auth.user_id,
        kind=kind,
        pdf_form_type=extracted.pdf_form_type,
        file_name=file.filename,
        file_size_bytes=len(body),
        content_sha256=hashlib.sha256(body).hexdigest(),
        raw_payload=body,
        detected_fields=detected_fields,
        idempotency_key=idempotency_key,
    )
    session_id = None
    if idempotency_key:
        session_id = await document_store.find_session_for_template_user(
            ts_pool,
            template_id=template_id,
            user_id=auth.user_id,
        )
    if session_id is None:
        session_id = await document_store.open_session(
            ts_pool,
            template_id=template_id,
            organization_id=auth.organization_id,
            depot_id=depot_id,
            user_id=auth.user_id,
        )
    return {
        "session_id": str(session_id),
        "document_id": str(template_id),
        "kind": kind,
        "pdf_form_type": extracted.pdf_form_type,
        "detected_fields": detected_fields,
        "file_name": file.filename,
        "file_size_bytes": len(body),
    }


@router.get("/documents/{output_id}/download")
async def download_document(
    output_id: UUID,
    user: dict = Depends(verify_token),
    ts_pool: Any = Depends(get_ts_pool),
) -> Response:
    """Download a rendered output (owner-scoped; cross-user/tenant → 404)."""
    is_admin = get_user_role(user) == "favonius_admin"
    caller = UUID(get_user_id(user))
    row = await document_store.load_output_for_user(ts_pool, output_id, caller, is_admin=is_admin)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if row.get("raw_payload") is None:
        # Retention nulled the blob; metadata is kept.
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="This document is no longer available"
        )
    kind = row["kind"]
    media = _DOCX_MIME if kind == "docx" else _PDF_MIME
    filename = row.get("file_name") or f"document.{'docx' if kind == 'docx' else 'pdf'}"
    return Response(
        content=bytes(row["raw_payload"]),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/documents/sessions/{session_id}")
async def get_session(
    session_id: UUID,
    user: dict = Depends(verify_token),
    ts_pool: Any = Depends(get_ts_pool),
) -> dict[str, Any]:
    """Return a fill session's working state for the UI (owner-scoped)."""
    is_admin = get_user_role(user) == "favonius_admin"
    caller = UUID(get_user_id(user))
    session = await document_store.load_session_for_user(
        ts_pool, session_id, caller, is_admin=is_admin
    )
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")

    draft = session.get("draft") or {}
    latest_output = None
    if session.get("latest_output_id"):
        oid = session["latest_output_id"]
        latest_output = {
            "output_id": str(oid),
            "url": f"/agent/documents/{oid}/download",
        }
    created = session.get("created_at")
    updated = session.get("updated_at")
    return {
        "session_id": str(session["id"]),
        "template_id": str(session["template_id"]),
        "status": session["status"],
        "draft": draft,
        "open_questions": draft.get("open_questions", []) if isinstance(draft, dict) else [],
        "message_log": session.get("message_log") or [],
        "latest_output": latest_output,
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
    }
