"""Persistence helpers for ``optimization_input_snapshots`` (migrations
019, 020).

The snapshot row is written *before* the solver runs. ``run_id`` is updated
afterwards to link the snapshot to its ``optimization_runs`` row. Snapshots
without a linked run still survive — useful for diagnosing solver crashes.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from uuid import UUID

from ..core.state.readiness import snapshot_to_payload

if TYPE_CHECKING:
    from ..core.models import OptimizationInputSnapshot
    from .pools import DatabasePools

logger = logging.getLogger(__name__)


_INSERT_SNAPSHOT_SQL = """
INSERT INTO optimization_input_snapshots (
    snapshot_id,
    depot_id,
    organization_id,
    captured_at,
    horizon_start,
    horizon_end,
    readiness_status,
    building_load_source,
    missing_inputs,
    assumptions,
    payload,
    payload_schema,
    weather_forecast_id
    recent_telemetry,
    code_version,
    solver_version,
    surrogate_model_version
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8,
    $9::jsonb, $10::jsonb, $11::jsonb, $12,
    $13::jsonb, $14, $15, $16
)
"""

_LINK_RUN_SQL = """
UPDATE optimization_input_snapshots
SET run_id = $1
WHERE snapshot_id = $2
"""


async def persist_snapshot(pools: "DatabasePools", snapshot: "OptimizationInputSnapshot") -> UUID:
    """Insert a snapshot row and return its snapshot_id.

    The payload is JSON-encoded once and passed through asyncpg's JSONB
    casting so the DB stores normalised JSON regardless of how the
    upstream dataclass was populated.
    """
    payload = snapshot_to_payload(snapshot)
    payload_json = json.dumps(payload, default=str)
    missing_json = json.dumps(snapshot.readiness.missing_inputs)
    assumptions_json = json.dumps(snapshot.readiness.assumptions, default=str)
    telemetry_json = json.dumps(snapshot.recent_telemetry, default=str)

    async with pools.ts.acquire() as conn:
        await conn.execute(
            _INSERT_SNAPSHOT_SQL,
            snapshot.snapshot_id,
            snapshot.depot_id,
            snapshot.organization_id,
            snapshot.captured_at,
            snapshot.horizon_start,
            snapshot.horizon_end,
            snapshot.readiness.status,
            snapshot.readiness.building_load_source,
            missing_json,
            assumptions_json,
            payload_json,
            snapshot.payload_schema,
            snapshot.weather_forecast_id,
            telemetry_json,
            snapshot.code_version,
            snapshot.solver_version,
            snapshot.surrogate_model_version,
        )
    logger.info(
        "Persisted optimization input snapshot %s for depot %s (status=%s, "
        "building_load_source=%s, code_version=%s)",
        snapshot.snapshot_id,
        snapshot.depot_id,
        snapshot.readiness.status,
        snapshot.readiness.building_load_source,
        snapshot.code_version,
    )
    return snapshot.snapshot_id


async def link_snapshot_to_run(pools: "DatabasePools", snapshot_id: UUID, run_id: UUID) -> None:
    """Backfill ``run_id`` on an existing snapshot row."""
    async with pools.ts.acquire() as conn:
        await conn.execute(_LINK_RUN_SQL, run_id, snapshot_id)
