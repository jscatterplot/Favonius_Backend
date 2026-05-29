"""Pydantic + dataclass types for the traffic-fine triage agent.

``TrafficFineExtraction`` is the structured-output schema the LLM fills in from
an uploaded fine document via a single tool call. It mirrors the ``QueryPlan``
pattern in ``src/api/agent/plan.py``: ``extra="forbid"`` so the model cannot
smuggle unexpected keys past validation, and ``model_json_schema()`` doubles as
the Anthropic tool ``input_schema``. Every field is optional because a
real-world fine may omit any of them; the evaluator treats absence explicitly.

``EarlyPaymentEvaluation`` is the deterministic verdict the evaluator returns.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class TrafficFineExtraction(BaseModel):
    """Fields extracted from a single traffic-fine document."""

    model_config = ConfigDict(extra="forbid")

    is_traffic_fine: bool = Field(
        default=True,
        description="False if the document does not appear to be a traffic fine.",
    )
    issuing_authority: Optional[str] = Field(
        default=None,
        description="Name of the issuing authority, e.g. 'Bussgeldstelle Berlin'.",
    )
    issuing_country: Optional[str] = Field(
        default=None,
        description="Country that issued the fine, e.g. 'Germany' or 'Lithuania'.",
    )
    fine_reference: Optional[str] = Field(
        default=None,
        description="The fine's reference / case number (the '#ID' shown to operators).",
    )
    currency: Optional[str] = Field(
        default=None,
        description="ISO 4217 currency code of the amounts, e.g. 'EUR'.",
    )
    full_amount: Optional[float] = Field(
        default=None,
        description="The standard (non-discounted) fine amount.",
    )
    early_payment_amount: Optional[float] = Field(
        default=None,
        description="The reduced amount payable within the early-payment window.",
    )
    stated_discount_amount: Optional[float] = Field(
        default=None,
        description="An explicit early-payment discount, if the document gives one directly.",
    )
    early_payment_deadline: Optional[str] = Field(
        default=None,
        description="The early-payment discount deadline as an ISO 8601 date or datetime.",
    )
    iban: Optional[str] = Field(
        default=None,
        description="The IBAN to pay the fine into, if stated.",
    )


EvaluationKind = Literal[
    "within_window",
    "not_yet",
    "expired",
    "no_deadline",
    "not_a_fine",
]


@dataclass(frozen=True)
class EarlyPaymentEvaluation:
    """Deterministic verdict on a fine's early-payment window.

    ``within_window`` is True (and ``message`` populated) only when
    ``kind == 'within_window'``. ``hours_remaining`` and ``deadline_utc`` are
    populated whenever a deadline was parseable.
    """

    kind: EvaluationKind
    fine_id_display: str
    within_window: bool = False
    hours_remaining: Optional[float] = None
    deadline_utc: Optional[datetime] = None
    discount_amount: Optional[float] = None
    currency: Optional[str] = None
    currency_symbol: str = ""
    message: Optional[str] = None
