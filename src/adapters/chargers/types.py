"""Shared types for the per-vendor charger log parsers.

Kept separate from the registry so the reconciler can import the entry
type without pulling in vendor packages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


class ChargerLogParseError(Exception):
    """Raised by a vendor parser when the blob cannot be interpreted.

    The reconciler catches this and lands the import with
    ``status='failed'`` and ``session_log_reconciliations.source='parse_failed'``.
    """


@dataclass(frozen=True)
class ChargerLogEntry:
    """One normalized row produced by a vendor parser.

    Field shape mirrors the ``telemetry`` table so the reconciler's JOIN
    against telemetry is symmetric. Vendor-specific fields the parser
    could not classify land in ``raw_fields`` so a future schema bump
    isn't needed to surface them.

    Attributes:
        time: UTC timestamp of the sample.
        transaction_id: OCPP transaction id this entry belongs to (when
            the charger associates it). Optional because some vendors
            emit per-connector logs without explicit tx ids.
        connector_id: 1-based connector id, if known.
        soc: State of charge as a fraction in [0, 1], or None.
        charging_kw: Instantaneous power, kW. Sign convention matches
            telemetry (positive = charging).
        energy_kwh: Cumulative energy delivered this session, kWh.
        raw_fields: Anything else the parser observed.
    """

    time: datetime
    transaction_id: Optional[int] = None
    connector_id: Optional[int] = None
    soc: Optional[float] = None
    charging_kw: Optional[float] = None
    energy_kwh: Optional[float] = None
    raw_fields: dict[str, Any] = field(default_factory=dict)
