"""Persistence for the agent document-fill path (migration 047).

Three TimescaleDB tables (``db_pools.ts``): ``agent_document_templates``
(uploaded source blob), ``agent_document_fill_sessions`` (multi-turn
collaboration state), ``agent_document_outputs`` (rendered previews + final).

Access is **owner-scoped**: a session/template/output is visible only to the
``user_id`` that created it, or to a ``favonius_admin`` — mirroring the
ownership gate on ``GET /agent/runs/{id}``. Cross-user / cross-tenant reads
return ``None`` so the endpoint can answer 404 without leaking existence.

JSONB columns are written with explicit ``::jsonb`` casts over
``json.dumps`` and decoded defensively on read (asyncpg may hand JSONB back
as text when no connection codec is registered) — the same pattern the rest
of the agent uses (``audit.py``, ``router.py``).
"""

from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID


def _jsonb(value: Any) -> str:
    return json.dumps(value if value is not None else None, default=str)


def _decode_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return default


# ── Templates ────────────────────────────────────────────────────────────────


async def store_template(
    ts_pool: Any,
    *,
    organization_id: Optional[UUID],
    depot_id: Optional[UUID],
    uploaded_by: UUID,
    kind: str,
    pdf_form_type: Optional[str],
    file_name: Optional[str],
    file_size_bytes: int,
    content_sha256: str,
    raw_payload: bytes,
    detected_fields: list[dict[str, Any]],
    idempotency_key: Optional[str] = None,
) -> UUID:
    """Insert one ``agent_document_templates`` row; return its id.

    When ``idempotency_key`` is supplied and already present, returns the
    existing row's id instead of inserting (double-submit safe).
    """
    async with ts_pool.acquire() as conn:
        if idempotency_key:
            existing = await conn.fetchval(
                "SELECT id FROM agent_document_templates WHERE idempotency_key = $1",
                idempotency_key,
            )
            if existing is not None:
                return UUID(str(existing))
        row = await conn.fetchval(
            """
            INSERT INTO agent_document_templates (
                organization_id, depot_id, uploaded_by, kind, pdf_form_type,
                file_name, file_size_bytes, content_sha256, raw_payload,
                detected_fields, idempotency_key
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4, $5,
                $6, $7, $8, $9,
                $10::jsonb, $11
            )
            RETURNING id
            """,
            str(organization_id) if organization_id else None,
            str(depot_id) if depot_id else None,
            str(uploaded_by),
            kind,
            pdf_form_type,
            file_name,
            file_size_bytes,
            content_sha256,
            raw_payload,
            _jsonb(detected_fields),
            idempotency_key,
        )
    return UUID(str(row))


async def load_template(ts_pool: Any, template_id: UUID) -> Optional[dict[str, Any]]:
    """Load a template row (no scope check — callers scope via the session)."""
    async with ts_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, organization_id, depot_id, uploaded_by, kind, pdf_form_type,
                   file_name, file_size_bytes, content_sha256, raw_payload,
                   detected_fields, status, created_at
              FROM agent_document_templates
             WHERE id = $1::uuid
            """,
            str(template_id),
        )
    if row is None:
        return None
    out = dict(row)
    out["detected_fields"] = _decode_json(out.get("detected_fields"), [])
    return out


# ── Sessions ─────────────────────────────────────────────────────────────────


async def open_session(
    ts_pool: Any,
    *,
    template_id: UUID,
    organization_id: Optional[UUID],
    depot_id: Optional[UUID],
    user_id: UUID,
) -> UUID:
    """Open a fill session for a template; return its id (status='gathering')."""
    async with ts_pool.acquire() as conn:
        row = await conn.fetchval(
            """
            INSERT INTO agent_document_fill_sessions (
                template_id, organization_id, depot_id, user_id
            )
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid)
            RETURNING id
            """,
            str(template_id),
            str(organization_id) if organization_id else None,
            str(depot_id) if depot_id else None,
            str(user_id),
        )
    return UUID(str(row))


async def load_session_for_user(
    ts_pool: Any,
    session_id: UUID,
    user_id: UUID,
    *,
    is_admin: bool,
) -> Optional[dict[str, Any]]:
    """Load a session, scoped to its owner (or any session for an admin).

    Returns ``None`` when the session doesn't exist OR the caller is neither
    the owner nor a ``favonius_admin`` — the endpoint answers 404 either way.
    """
    async with ts_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, template_id, organization_id, depot_id, user_id, status,
                   draft, message_log, latest_output_id, created_at, updated_at
              FROM agent_document_fill_sessions
             WHERE id = $1::uuid
            """,
            str(session_id),
        )
    if row is None:
        return None
    if not is_admin and UUID(str(row["user_id"])) != user_id:
        return None
    out = dict(row)
    out["draft"] = _decode_json(out.get("draft"), {})
    out["message_log"] = _decode_json(out.get("message_log"), [])
    return out


async def update_session(
    ts_pool: Any,
    session_id: UUID,
    *,
    status: str,
    draft: dict[str, Any],
    message_log: list[dict[str, Any]],
    latest_output_id: Optional[UUID],
) -> None:
    """Persist the turn's outcome onto the session row."""
    async with ts_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_document_fill_sessions
               SET status           = $2,
                   draft            = $3::jsonb,
                   message_log      = $4::jsonb,
                   latest_output_id = $5::uuid,
                   updated_at       = NOW()
             WHERE id = $1::uuid
            """,
            str(session_id),
            status,
            _jsonb(draft),
            _jsonb(message_log),
            str(latest_output_id) if latest_output_id else None,
        )


# ── Outputs ──────────────────────────────────────────────────────────────────


async def store_output(
    ts_pool: Any,
    *,
    template_id: UUID,
    session_id: UUID,
    run_id: Optional[UUID],
    organization_id: Optional[UUID],
    depot_id: Optional[UUID],
    kind: str,
    output_kind: str,
    fidelity: str,
    file_name: Optional[str],
    file_size_bytes: int,
    content_sha256: str,
    raw_payload: bytes,
    field_values: dict[str, Any],
    replacements: list[dict[str, Any]],
) -> UUID:
    """Insert one rendered output (preview or final); return its id."""
    async with ts_pool.acquire() as conn:
        row = await conn.fetchval(
            """
            INSERT INTO agent_document_outputs (
                template_id, session_id, run_id, organization_id, depot_id,
                kind, output_kind, fidelity, file_name, file_size_bytes,
                content_sha256, raw_payload, field_values, replacements
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4::uuid, $5::uuid,
                $6, $7, $8, $9, $10,
                $11, $12, $13::jsonb, $14::jsonb
            )
            RETURNING id
            """,
            str(template_id),
            str(session_id),
            str(run_id) if run_id else None,
            str(organization_id) if organization_id else None,
            str(depot_id) if depot_id else None,
            kind,
            output_kind,
            fidelity,
            file_name,
            file_size_bytes,
            content_sha256,
            raw_payload,
            _jsonb(field_values),
            _jsonb(replacements),
        )
    return UUID(str(row))


async def load_output_for_user(
    ts_pool: Any,
    output_id: UUID,
    user_id: UUID,
    *,
    is_admin: bool,
) -> Optional[dict[str, Any]]:
    """Load a rendered output for download, scoped to the session owner.

    Joins to the owning session so the gate matches
    :func:`load_session_for_user`: owner-only, or any output for an admin.
    Returns ``None`` (→ 404) otherwise.
    """
    async with ts_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT o.id, o.kind, o.output_kind, o.fidelity, o.file_name,
                   o.raw_payload, o.created_at, s.user_id AS session_user_id
              FROM agent_document_outputs o
              LEFT JOIN agent_document_fill_sessions s ON s.id = o.session_id
             WHERE o.id = $1::uuid
            """,
            str(output_id),
        )
    if row is None:
        return None
    owner = row["session_user_id"]
    if not is_admin and (owner is None or UUID(str(owner)) != user_id):
        return None
    return dict(row)


__all__ = [
    "store_template",
    "load_template",
    "open_session",
    "load_session_for_user",
    "update_session",
    "store_output",
    "load_output_for_user",
]
