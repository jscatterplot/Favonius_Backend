"""Pure helpers shared by the Kempower adapter and the onboarding CLI."""

from __future__ import annotations

import hashlib
from datetime import datetime

KEMPOWER_EXTERNAL_ID_PREFIX = "kempower:"
"""Prefix applied to ``vehicles.external_id`` for Kempower-imported vehicles.

``vehicles.external_id`` carries a global ``UNIQUE WHERE external_id IS NOT NULL``
index (Supabase mig-006). Two Kempower customers can legitimately use the
same numeric ``vehicleId`` in their respective tenants, so we namespace the
Favonius-side identifier to avoid cross-tenant collisions while preserving
the original id verbatim after the prefix for traceability.
"""


def derive_vehicle_type(*, make: str | None, model: str | None) -> str:
    """Return the Favonius ``vehicle_type`` for a Kempower vehicle.

    Kempower doesn't expose a normalized vehicle category, so we infer
    one from the free-text ``make``/``model``. The result is one of
    ``"bus"`` (default — most depot fleets), ``"truck"``, ``"van"``, or
    ``"car"``. The operator can re-classify after import via
    ``PATCH /admin/depots/{id}/vehicles/{vid}``.
    """
    haystack = " ".join(filter(None, [(make or "").lower(), (model or "").lower()]))
    if not haystack:
        return "bus"
    if any(token in haystack for token in ("truck", "lorry", "actros", "tgs", "tgx")):
        return "truck"
    if any(token in haystack for token in ("van", "sprinter", "transit", "ducato")):
        return "van"
    if any(token in haystack for token in ("car", "model 3", "model y", "tesla")):
        return "car"
    return "bus"


def compute_import_row_hash(
    *,
    depot_id: str,
    start_time_utc: datetime,
    id_tag: str,
) -> str:
    """SHA-256 of ``(depot_id, start_time_utc.isoformat(), id_tag)``.

    Matches ``src/api/main.py::_compute_import_row_hash`` byte-for-byte so
    Kempower-imported rows land on the same
    ``charging_sessions_import_dedup_idx`` (migration 030) as XLSX-imported
    rows. Re-running the CLI with identical inputs produces identical hashes
    → ``ON CONFLICT DO NOTHING`` collapses the second run to zero inserts.
    """
    canonical = "|".join([depot_id, start_time_utc.isoformat(), id_tag])
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
