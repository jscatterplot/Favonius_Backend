"""Optimization metadata endpoints.

Read-only access to solver state and last-run history for a depot.
Mounted unconditionally in ``src/api/main.py``; any authenticated user with
depot access can call these (same gate as ``GET /depots/{id}/state``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..db import queries as db_queries
from ..db.exceptions import DatabaseError, StaticDbUnavailableError
from ..security.auth import verify_depot_access
from ..security.tenant_mirror import ensure_tenant_mirrored
from ..security.validators import validate_uuid

logger = logging.getLogger(__name__)

router = APIRouter(tags=["optimization"])


# ── Pool helpers (lazy-import avoids circular dependency on main.py) ─────────


def _get_db_pools() -> Any:
    from src.api import main as _main  # noqa: PLC0415

    if _main.db_pools is None:
        raise DatabaseError("Database not available")
    return _main.db_pools


# ── Response models ───────────────────────────────────────────────────────────


class SolverLastRunItem(BaseModel):
    """Metadata for a single optimization run."""

    run_id: str
    status: str
    solver_used: str
    solve_time_s: Optional[float] = None
    objective_value: Optional[float] = None
    peak_demand_kw: Optional[float] = None
    trigger_reason: Optional[str] = None
    horizon_start: Optional[str] = None
    horizon_end: Optional[str] = None
    timestamp: str


class SolverMetadataResponse(BaseModel):
    """Solver metadata for the optimization page."""

    depot_id: str
    solver_name: Optional[str] = None
    last_run: Optional[SolverLastRunItem] = None
    is_never_run: bool


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.get(
    "/depots/{depot_id}/optimization/solver",
    response_model=SolverMetadataResponse,
    summary="Get solver metadata for a depot",
    description="""
    Return the most recent optimization run and solver identity for the depot.

    **Authentication:** Requires JWT token in Authorization header.
    Any authenticated user with depot access (customer_operator or higher) may call this.

    **Response:**
    - ``solver_name``: ``'gurobi'`` or ``'highs'`` (from the last run); ``null`` if the depot
      has never been optimized.
    - ``last_run``: Full metadata for the most recent run; ``null`` if never run.
    - ``is_never_run``: ``true`` when no optimization has ever been executed for this depot.

    **Error codes:**
    - 400: Invalid depot_id
    - 403: No depot access
    - 503: Database temporarily unavailable (``STATIC_DB_UNAVAILABLE`` or ``DATABASE_ERROR``)
    """,
    responses={
        400: {"description": "Invalid depot_id"},
        403: {"description": "No depot access"},
        503: {"description": "Database temporarily unavailable"},
    },
)
async def get_solver_metadata(
    depot_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
) -> SolverMetadataResponse:
    """GET /depots/{depot_id}/optimization/solver — last run + solver identity."""
    validate_uuid(depot_id, field_name="depot_id")
    pools = _get_db_pools()
    await verify_depot_access(depot_id, user, pools.static)

    depot_uuid = UUID(depot_id)
    async with pools.ts.acquire() as conn:
        row = await db_queries.get_latest_optimization_run(conn, depot_uuid)

    if row is None:
        return SolverMetadataResponse(
            depot_id=depot_id,
            solver_name=None,
            last_run=None,
            is_never_run=True,
        )

    ts = row["run_time"]
    ts_str = ts.isoformat() if isinstance(ts, datetime) else str(ts)

    def _iso(val: Any) -> Optional[str]:
        if val is None:
            return None
        return val.isoformat() if isinstance(val, datetime) else str(val)

    last_run = SolverLastRunItem(
        run_id=str(row["run_id"]),
        status=row["status"] or "unknown",
        solver_used=row["solver_used"] or "gurobi",
        solve_time_s=row["solve_time_s"],
        objective_value=row["objective_value"],
        peak_demand_kw=row["peak_demand_kw"],
        trigger_reason=row["trigger_reason"],
        horizon_start=_iso(row["horizon_start"]),
        horizon_end=_iso(row["horizon_end"]),
        timestamp=ts_str,
    )

    return SolverMetadataResponse(
        depot_id=depot_id,
        solver_name=row["solver_used"] or "gurobi",
        last_run=last_run,
        is_never_run=False,
    )
