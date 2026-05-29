"""Persistence for traffic-fine documents and their triage results.

All rows live in the TimescaleDB operational database (migration 047) next to
``decisions`` and ``notification_alerts``; depot/org ids are bare UUIDs with no
FK to the Supabase static tables (the two-database invariant). Depot timezone is
read from the Supabase ``sites`` table via the static pool.

Functions take either a pool or a connection as ``conn``/``ts_pool`` (asyncpg's
``fetch``/``execute`` are present on both), so the triage orchestrator can run
the alert insert and the result update inside one transaction.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from src.core.traffic_fines.models import EarlyPaymentEvaluation

# Columns returned to the API (everything except the raw BYTEA payload).
_PUBLIC_COLS = (
    "id, depot_id, organization_id, uploaded_by, status, content_type, file_name, "
    "byte_size, is_traffic_fine, issuing_authority, issuing_country, fine_reference, "
    "currency, full_amount, early_payment_amount, discount_amount, iban, "
    "early_payment_deadline, early_payment_deadline_raw, evaluation_kind, "
    "hours_until_deadline, within_alert_window, decision_id, alert_id, extraction, "
    "error_detail, created_at, updated_at"
)


def _money(value: Optional[float]) -> Optional[Decimal]:
    """Coerce a float amount to Decimal for a NUMERIC column (asyncpg-safe)."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def coerce_jsonb(value: Any) -> dict[str, Any]:
    """Return a dict from an asyncpg JSONB column (str or already-decoded)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


async def insert_received_fine(
    ts_pool: Any,
    *,
    depot_id: UUID,
    organization_id: UUID,
    uploaded_by: Optional[UUID],
    content_type: Optional[str],
    file_name: Optional[str],
    raw_payload: bytes,
) -> UUID:
    """Insert a freshly uploaded fine (status='received'); return its id."""
    row = await ts_pool.fetchrow(
        """
        INSERT INTO traffic_fines (
            depot_id, organization_id, uploaded_by, status,
            content_type, file_name, byte_size, raw_payload
        ) VALUES ($1, $2, $3, 'received', $4, $5, $6, $7)
        RETURNING id
        """,
        depot_id,
        organization_id,
        uploaded_by,
        content_type,
        file_name,
        len(raw_payload),
        raw_payload,
    )
    return row["id"]


async def get_fine(ts_pool: Any, fine_id: UUID) -> Optional[dict[str, Any]]:
    """Load a fine row (including raw_payload) for triage. None if absent."""
    row = await ts_pool.fetchrow("SELECT * FROM traffic_fines WHERE id = $1", fine_id)
    return dict(row) if row else None


async def get_fine_public(
    ts_pool: Any, depot_id: UUID, fine_id: UUID
) -> Optional[dict[str, Any]]:
    """Load one fine for the API (no raw payload), scoped to a depot."""
    row = await ts_pool.fetchrow(
        f"SELECT {_PUBLIC_COLS} FROM traffic_fines WHERE id = $1 AND depot_id = $2",
        fine_id,
        depot_id,
    )
    return dict(row) if row else None


async def list_fines(
    ts_pool: Any, depot_id: UUID, *, limit: int = 100
) -> list[dict[str, Any]]:
    """List a depot's fines, most recent first (no raw payload)."""
    rows = await ts_pool.fetch(
        f"""
        SELECT {_PUBLIC_COLS}
          FROM traffic_fines
         WHERE depot_id = $1
         ORDER BY created_at DESC
         LIMIT $2
        """,
        depot_id,
        limit,
    )
    return [dict(r) for r in rows]


async def mark_status(
    ts_pool: Any, fine_id: UUID, status: str, *, error_detail: Optional[str] = None
) -> None:
    """Set an intermediate/terminal status (parsing, unsupported_media, …)."""
    await ts_pool.execute(
        """
        UPDATE traffic_fines
           SET status = $2, error_detail = $3, updated_at = NOW()
         WHERE id = $1
        """,
        fine_id,
        status,
        error_detail,
    )


async def save_triage_result(
    conn: Any,
    fine_id: UUID,
    *,
    status: str,
    extraction: Optional[dict[str, Any]],
    evaluation: Optional[EarlyPaymentEvaluation],
    decision_id: Optional[UUID] = None,
    alert_id: Optional[UUID] = None,
    error_detail: Optional[str] = None,
) -> None:
    """Persist parsed fields + evaluation + links.

    Runs on the caller's ``conn`` so it can share the transaction that raises
    the alert (alert insert + status flip commit atomically).
    """
    ext = extraction or {}
    ev = evaluation
    await conn.execute(
        """
        UPDATE traffic_fines SET
            status = $2,
            is_traffic_fine = $3,
            issuing_authority = $4,
            issuing_country = $5,
            fine_reference = $6,
            currency = $7,
            full_amount = $8,
            early_payment_amount = $9,
            discount_amount = $10,
            iban = $11,
            early_payment_deadline = $12,
            early_payment_deadline_raw = $13,
            evaluation_kind = $14,
            hours_until_deadline = $15,
            within_alert_window = $16,
            decision_id = $17,
            alert_id = $18,
            extraction = $19::jsonb,
            error_detail = $20,
            updated_at = NOW()
         WHERE id = $1
        """,
        fine_id,
        status,
        ext.get("is_traffic_fine"),
        ext.get("issuing_authority"),
        ext.get("issuing_country"),
        ext.get("fine_reference"),
        ext.get("currency"),
        _money(ext.get("full_amount")),
        _money(ext.get("early_payment_amount")),
        _money(ev.discount_amount) if ev else None,
        ext.get("iban"),
        ev.deadline_utc if ev else None,
        ext.get("early_payment_deadline"),
        ev.kind if ev else None,
        ev.hours_remaining if ev else None,
        ev.within_window if ev else None,
        decision_id,
        alert_id,
        json.dumps(ext) if extraction is not None else None,
        error_detail,
    )


async def mark_alerted(
    conn: Any,
    fine_id: UUID,
    *,
    alert_id: UUID,
    evaluation: EarlyPaymentEvaluation,
) -> None:
    """Flip a fine to 'alerted' after a sweep raise, preserving parsed fields.

    Unlike :func:`save_triage_result`, this never overwrites the extraction or
    ``decision_id`` set at parse time — the sweep only adds the alert link.
    """
    await conn.execute(
        """
        UPDATE traffic_fines SET
            status = 'alerted',
            alert_id = $2,
            within_alert_window = TRUE,
            evaluation_kind = $3,
            hours_until_deadline = $4,
            updated_at = NOW()
         WHERE id = $1
        """,
        fine_id,
        alert_id,
        evaluation.kind,
        evaluation.hours_remaining,
    )


async def sweep_candidates(
    ts_pool: Any, *, window_hours: float, limit: int = 200
) -> list[dict[str, Any]]:
    """Parsed fines with a future deadline now inside the alert window.

    These are re-evaluated (deterministically, no LLM) by the sweep so a fine
    uploaded well ahead of its deadline still alerts when the window opens.
    """
    rows = await ts_pool.fetch(
        """
        SELECT id, depot_id, organization_id, fine_reference, extraction
          FROM traffic_fines
         WHERE status IN ('parsed', 'no_alert')
           AND early_payment_deadline IS NOT NULL
           AND early_payment_deadline > NOW()
           AND early_payment_deadline <= NOW() + ($1::double precision * INTERVAL '1 hour')
         ORDER BY early_payment_deadline ASC
         LIMIT $2
        """,
        float(window_hours),
        limit,
    )
    return [dict(r) for r in rows]


async def get_depot_timezone(static_pool: Any, depot_id: UUID) -> Optional[str]:
    """Return the depot's IANA timezone from Supabase ``sites`` (or None)."""
    return await static_pool.fetchval(
        "SELECT timezone FROM sites WHERE id = $1", depot_id
    )
