"""Raise early-payment alerts for traffic fines.

Shared by the upload-time triage (``src/api/agent_workflows/traffic_fine.py``)
and the deadline re-check sweep (``sweeper.py``) so the alert type, dedup key,
and detail payload are defined once. Lives in ``core`` (not ``api``) to keep the
sweep — which is core — free of an api-layer import; it depends only on the
notifications pipeline, which is sibling infrastructure.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from src.core.traffic_fines.models import EarlyPaymentEvaluation, TrafficFineExtraction
from src.notifications.alerts import upsert_alert
from src.notifications.severity import Severity

TRAFFIC_FINE_ALERT_TYPE = "traffic_fine_early_payment"


def dedup_key(depot_id: UUID, fine_id_display: str) -> str:
    """One active alert per (depot, fine) — stable across triage and sweeps."""
    return f"{TRAFFIC_FINE_ALERT_TYPE}:{depot_id}:{fine_id_display}"


def alert_detail(
    *,
    fine_id: UUID,
    evaluation: EarlyPaymentEvaluation,
    extraction: TrafficFineExtraction,
    decision_id: Optional[UUID] = None,
) -> dict[str, Any]:
    """JSON-serialisable alert detail (plain primitives — upsert_alert json.dumps)."""
    ev = evaluation
    return {
        "traffic_fine_id": str(fine_id),
        "decision_id": str(decision_id) if decision_id else None,
        "fine_reference": extraction.fine_reference,
        "issuing_authority": extraction.issuing_authority,
        "issuing_country": extraction.issuing_country,
        "currency": extraction.currency,
        "full_amount": extraction.full_amount,
        "early_payment_amount": extraction.early_payment_amount,
        "discount_amount": ev.discount_amount,
        "iban": extraction.iban,
        "early_payment_deadline": ev.deadline_utc.isoformat() if ev.deadline_utc else None,
        "hours_remaining": (
            round(ev.hours_remaining, 1) if ev.hours_remaining is not None else None
        ),
        "message": ev.message,
    }


async def raise_fine_alert(
    conn: Any,
    *,
    organization_id: UUID,
    depot_id: UUID,
    fine_id: UUID,
    evaluation: EarlyPaymentEvaluation,
    extraction: TrafficFineExtraction,
    decision_id: Optional[UUID] = None,
) -> UUID:
    """Upsert the early-payment alert; return the alert id.

    ``evaluation.message`` carries the exact spec wording and becomes the alert
    title (and email subject). Caller should only invoke when
    ``evaluation.within_window`` is True.
    """
    alert = await upsert_alert(
        conn,
        organization_id=organization_id,
        depot_id=depot_id,
        alert_type=TRAFFIC_FINE_ALERT_TYPE,
        severity=Severity.WARNING,
        title=evaluation.message or "Early payment discount closing soon",
        detail=alert_detail(
            fine_id=fine_id,
            evaluation=evaluation,
            extraction=extraction,
            decision_id=decision_id,
        ),
        dedup_key=dedup_key(depot_id, evaluation.fine_id_display),
    )
    return alert.id
