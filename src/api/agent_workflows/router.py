"""FastAPI router for depot-agent workflows (sprint 6 / today view).

Four endpoints, mounted at ``/agent-workflows`` behind the
``DEPOT_AGENT_ENABLED`` flag:

    POST   /agent-workflows/today/{depot_id}
    POST   /agent-workflows/today/{depot_id}/stream
    GET    /agent-workflows/decisions/{decision_id}
    GET    /agent-workflows/workflows/{name}/decisions

Auth — every endpoint requires a valid Supabase JWT (401 otherwise),
and depot-scoped endpoints additionally call
:func:`verify_depot_access` (403 if the caller's organization does
not own the depot, with ``favonius_admin`` bypassing the tenant
check). The audit-log list scopes results by ``visible_depot_ids``
computed from the token, so a customer admin cannot enumerate other
tenants' decisions.

Rate limit — ``POST`` endpoints share a 10 req/min/user bucket (the
``check_agent_workflow_limit`` bucket on :class:`RateLimiter`). The
``GET`` endpoints rely on the global 100 req/min API limit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from src.api.agent.auth_context import build_auth_context
from src.api.agent.stream import SSE_HEADERS, SSEEventStream
from src.core.workflows import WORKFLOW_NAME as READINESS_WORKFLOW_NAME
from src.core.workflows.orchestrator import (
    execute_readiness_workflow,
    fetch_decision,
    find_recent_decision,
    list_decisions,
    persist_decision,
)
from src.core.workflows.readiness import ReadinessDecision, ToolCall
from src.core.workflows.tools import DatabaseToolBundle, ToolBundle
from src.db.queries import get_depot_by_id
from src.monitoring.metrics import (
    AGENT_WORKFLOW_EXCEPTIONS_OUT,
    AGENT_WORKFLOW_REQUESTS,
    AGENT_WORKFLOW_RUN_DURATION,
    AGENT_WORKFLOW_RUNS,
)
from src.security.auth import (
    get_user_id,
    get_user_organization_id,
    get_user_role,
    is_platform_admin,
    verify_depot_access,
    verify_token,
)
from src.security.rate_limiter import get_rate_limiter

from .feature_flag import is_depot_agent_enabled

logger = logging.getLogger(__name__)

# Idempotency window for POST /today/{depot_id}: subsequent calls inside
# this window return the cached Decision row rather than re-running the
# workflow. Matches the sprint-6 acceptance criterion ("idempotent
# within a 60s window").
IDEMPOTENCY_WINDOW_SECONDS = 60


# ── Dependency providers ──────────────────────────────────────────────────


def get_static_pool() -> Any:
    """Return the static (Supabase) asyncpg pool from the live app state."""
    from src.api import main as api_main

    if api_main.db_pools is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not available",
        )
    return api_main.db_pools.static


def get_ts_pool() -> Any:
    """Return the time-series (TimescaleDB) asyncpg pool from the live app state."""
    from src.api import main as api_main

    if api_main.db_pools is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not available",
        )
    return api_main.db_pools.ts


def get_db_pools() -> Any:
    """Return the dual-pool container used by :class:`DatabaseToolBundle`."""
    from src.api import main as api_main

    if api_main.db_pools is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not available",
        )
    return api_main.db_pools


def get_tool_bundle() -> ToolBundle:
    """Return the live :class:`DatabaseToolBundle`. Tests override this."""
    return DatabaseToolBundle(pools=get_db_pools())


def require_feature_flag() -> None:
    """Reject requests when ``DEPOT_AGENT_ENABLED`` is off.

    Per the sprint-6 acceptance criterion the endpoint MUST refuse with
    404 when the flag is off — same shape as a missing route. The
    flag is also enforced at mount time, so reaching this dependency
    means a test or a runtime flip enabled the import but the env now
    says off.
    """
    if not is_depot_agent_enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


async def verify_token_and_check_workflow_limit(user: dict = Depends(verify_token)) -> dict:
    """JWT auth then workflow rate limit (10 req/min/user); returns the payload.

    The 429 carries ``X-RateLimit-*`` headers so SPAs can show the
    operator how long they need to wait before the next refresh.
    """
    client_id = f"user:{get_user_id(user)}"
    limiter = get_rate_limiter()
    result = limiter.check_agent_workflow_limit(client_id)
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded: 10 requests per minute on /agent-workflows/*",
            headers={
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": str(result.remaining),
                "X-RateLimit-Reset": str(int(result.reset_at)),
            },
        )
    return user


# ── Response models ───────────────────────────────────────────────────────


class CoverageBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    vehicles_checked: int
    chargers_checked: int
    routes_checked: int


class ExceptionItem(BaseModel):
    model_config = ConfigDict(extra="allow")
    vehicle_id: str
    issue: str
    evidence: dict[str, Any] = Field(default_factory=dict)
    proposed_action: Optional[dict[str, Any]] = None
    permission_required: str = "inform"


class TodayPayload(BaseModel):
    """The §6.1 "today view" payload returned by POST /today/{depot_id}."""

    model_config = ConfigDict(extra="forbid")
    decision_id: UUID
    depot_id: str
    window: str
    coverage: CoverageBlock
    status: str
    exceptions: list[ExceptionItem] = Field(default_factory=list)
    triggered_by: str
    cached: bool = False
    created_at: datetime


class DecisionEnvelope(BaseModel):
    """Full Decision row returned by GET /decisions/{decision_id}."""

    model_config = ConfigDict(extra="forbid")
    decision_id: UUID
    workflow_name: str
    workflow_version: str
    depot_id: UUID
    organization_id: Optional[UUID] = None
    triggered_by: str
    triggered_by_user_id: Optional[UUID] = None
    inputs_hash: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    output: dict[str, Any]
    permission_tier: str
    status: str
    duration_ms: Optional[int] = None
    created_at: datetime


class DecisionListItem(BaseModel):
    """Compact row shape for the paginated audit-log view."""

    model_config = ConfigDict(extra="forbid")
    decision_id: UUID
    depot_id: UUID
    triggered_by: str
    status: str
    workflow_version: str
    has_exceptions: bool
    exception_count: int
    duration_ms: Optional[int] = None
    created_at: datetime


class DecisionListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[DecisionListItem]
    next_before: Optional[datetime] = None


# ── Router ────────────────────────────────────────────────────────────────


router = APIRouter(
    prefix="/agent-workflows",
    tags=["agent-workflows"],
    dependencies=[Depends(require_feature_flag)],
)


@router.post(
    "/today/{depot_id}",
    response_model=TodayPayload,
    summary="Run a daily readiness check and return the §6.1 today-view payload",
)
async def post_today(
    depot_id: UUID,
    user: dict = Depends(verify_token_and_check_workflow_limit),
    static_pool: Any = Depends(get_static_pool),
    ts_pool: Any = Depends(get_ts_pool),
    tools: ToolBundle = Depends(get_tool_bundle),
) -> TodayPayload:
    """Trigger a fresh readiness run for ``depot_id``.

    Repeat calls inside :data:`IDEMPOTENCY_WINDOW_SECONDS` return the
    cached Decision row (``cached=True``) without re-running the
    workflow. The first call inside a fresh window runs the workflow,
    persists the row, and returns the new payload.
    """
    await verify_depot_access(str(depot_id), user, static_pool)
    return await _run_today_synchronous(
        depot_id=depot_id,
        user=user,
        static_pool=static_pool,
        ts_pool=ts_pool,
        tools=tools,
    )


@router.post(
    "/today/{depot_id}/stream",
    summary="Stream a readiness run as SSE step + result events",
)
async def post_today_stream(
    depot_id: UUID,
    user: dict = Depends(verify_token_and_check_workflow_limit),
    static_pool: Any = Depends(get_static_pool),
    ts_pool: Any = Depends(get_ts_pool),
    tools: ToolBundle = Depends(get_tool_bundle),
) -> StreamingResponse:
    """SSE variant of POST /today/{depot_id}.

    Emits one ``step`` event per tool call and a final ``result``
    event carrying the same :class:`TodayPayload` the JSON endpoint
    returns. On errors emits a single ``error`` event with a generic
    detail — internal exceptions are logged but never reach the
    client.
    """
    await verify_depot_access(str(depot_id), user, static_pool)

    stream = SSEEventStream()

    async def _run() -> None:
        try:
            payload = await _run_today_synchronous(
                depot_id=depot_id,
                user=user,
                static_pool=static_pool,
                ts_pool=ts_pool,
                tools=tools,
                emit_steps_to=stream,
            )
            await stream.emit("result", payload.model_dump(mode="json"))
        except HTTPException as exc:
            logger.warning("agent-workflows stream HTTPException status=%s", exc.status_code)
            await stream.emit("error", {"status": exc.status_code, "detail": exc.detail})
        except Exception:
            logger.exception("agent-workflows stream failed")
            await stream.emit(
                "error",
                {"status": 502, "detail": "Workflow run failed. Please try again."},
            )
        finally:
            stream.close()

    async def _iterator() -> AsyncIterator[bytes]:
        producer = asyncio.create_task(_run())
        async for chunk in stream:
            yield chunk
        await producer

    AGENT_WORKFLOW_REQUESTS.labels(endpoint="today_stream", status_code="200").inc()
    return StreamingResponse(_iterator(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get(
    "/decisions/{decision_id}",
    response_model=DecisionEnvelope,
    summary="Fetch a stored workflow decision (the 'why' view)",
)
async def get_decision(
    decision_id: UUID,
    user: dict = Depends(verify_token),
    ts_pool: Any = Depends(get_ts_pool),
) -> DecisionEnvelope:
    """Return the full :class:`DecisionEnvelope` for ``decision_id``.

    Ownership-gated: rows whose ``organization_id`` does not match the
    caller's are returned as 404 (not 403) so a customer admin cannot
    enumerate other tenants' decision ids. ``favonius_admin`` may
    fetch any row.
    """
    row = await fetch_decision(ts_pool, decision_id)
    if row is None:
        AGENT_WORKFLOW_REQUESTS.labels(endpoint="get_decision", status_code="404").inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Decision not found")

    if not is_platform_admin(user):
        caller_org = get_user_organization_id(user)
        row_org = row.get("organization_id")
        if row_org is not None and str(row_org) != str(caller_org):
            AGENT_WORKFLOW_REQUESTS.labels(endpoint="get_decision", status_code="404").inc()
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Decision not found")

    envelope = _row_to_envelope(row)
    AGENT_WORKFLOW_REQUESTS.labels(endpoint="get_decision", status_code="200").inc()
    return envelope


@router.get(
    "/workflows/{name}/decisions",
    response_model=DecisionListResponse,
    summary="List recent workflow decisions (audit log)",
)
async def list_workflow_decisions(
    name: str,
    depot_id: Optional[UUID] = Query(default=None),
    since: Optional[datetime] = Query(default=None),
    before: Optional[datetime] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(verify_token),
    static_pool: Any = Depends(get_static_pool),
    ts_pool: Any = Depends(get_ts_pool),
) -> DecisionListResponse:
    """Paginated audit-log view (PRD §7.3).

    Scoped by the caller's ``visible_depot_ids``. ``before`` and
    ``since`` are both exclusive bounds on ``created_at``; cursor
    backwards through history by passing the previous page's
    ``next_before`` as the next ``before``.
    """
    auth = await build_auth_context(user, static_pool)
    if depot_id is not None:
        if depot_id not in auth.visible_depot_ids and auth.role != "favonius_admin":
            AGENT_WORKFLOW_REQUESTS.labels(endpoint="list_decisions", status_code="404").inc()
            # 404 not 403 so the caller can't enumerate depot ids in other tenants.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
        scoped_depots = [depot_id]
    else:
        scoped_depots = list(auth.visible_depot_ids)

    rows = await list_decisions(
        ts_pool,
        workflow_name=name,
        depot_ids=scoped_depots,
        since=since,
        before=before,
        limit=limit,
    )
    items = [_row_to_list_item(r) for r in rows]
    next_before = rows[-1]["created_at"] if len(rows) == limit else None
    AGENT_WORKFLOW_REQUESTS.labels(endpoint="list_decisions", status_code="200").inc()
    return DecisionListResponse(items=items, next_before=next_before)


# ── Shared synchronous run path ───────────────────────────────────────────


async def _run_today_synchronous(
    *,
    depot_id: UUID,
    user: dict,
    static_pool: Any,
    ts_pool: Any,
    tools: ToolBundle,
    emit_steps_to: Optional[SSEEventStream] = None,
) -> TodayPayload:
    """Shared executor for both the JSON and SSE today endpoints.

    1. Check the 60-second idempotency cache; return cached row if hit.
    2. Resolve depot timezone for the §6.1 ``window`` string.
    3. Call :func:`execute_readiness_workflow`.
    4. Persist the new Decision row.
    5. Surface metrics and return the :class:`TodayPayload`.

    When ``emit_steps_to`` is set, each tool-call name is emitted as a
    ``step`` event before the final ``result`` payload — the SSE
    handler consumes it; the JSON handler always passes ``None``.
    """
    start_ts = time.monotonic()
    cached_row = await find_recent_decision(
        ts_pool,
        workflow_name=READINESS_WORKFLOW_NAME,
        depot_id=depot_id,
        within_seconds=IDEMPOTENCY_WINDOW_SECONDS,
    )
    if cached_row is not None:
        endpoint_label = "today_stream" if emit_steps_to is not None else "today"
        AGENT_WORKFLOW_REQUESTS.labels(endpoint=endpoint_label, status_code="200").inc()
        if emit_steps_to is not None:
            await emit_steps_to.emit("step", {"name": "cache_hit", "decision_id": str(cached_row["decision_id"])})
        return _row_to_today_payload(cached_row, cached=True)

    depot = await get_depot_by_id(static_pool, str(depot_id))
    if depot is None:
        AGENT_WORKFLOW_REQUESTS.labels(
            endpoint="today_stream" if emit_steps_to is not None else "today",
            status_code="404",
        ).inc()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Depot not found")

    tz_name = str(depot.get("timezone") or "UTC")
    organization_id_str = depot.get("organization_id")
    organization_id = UUID(str(organization_id_str)) if organization_id_str else None

    if emit_steps_to is not None:
        await emit_steps_to.emit("step", {"name": "start", "depot_id": str(depot_id)})

    decision = await execute_readiness_workflow(
        depot_id=depot_id,
        organization_id=organization_id,
        triggered_by="manual",
        triggered_by_user_id=UUID(get_user_id(user)),
        tools=tools,
        depot_timezone=tz_name,
    )

    if emit_steps_to is not None:
        for tc in decision.tool_calls:
            await emit_steps_to.emit(
                "step",
                {"name": tc.name, "result_summary": tc.result_summary, "duration_ms": tc.duration_ms},
            )

    try:
        await persist_decision(ts_pool, decision)
    except Exception:
        # Persisting is best-effort for the response path — log and
        # continue so the operator still sees the today view. The
        # next run will create a row.
        logger.exception("Failed to persist workflow_decisions row; returning response anyway")

    _emit_workflow_metrics(decision, time.monotonic() - start_ts)
    endpoint_label = "today_stream" if emit_steps_to is not None else "today"
    AGENT_WORKFLOW_REQUESTS.labels(endpoint=endpoint_label, status_code="200").inc()
    return _decision_to_today_payload(decision)


# ── Conversion helpers ────────────────────────────────────────────────────


def _decision_to_today_payload(decision: ReadinessDecision) -> TodayPayload:
    out = decision.output.to_payload()
    return TodayPayload(
        decision_id=decision.decision_id,
        depot_id=out["depot_id"],
        window=out["window"],
        coverage=CoverageBlock(**out["coverage"]),
        status=out["status"],
        exceptions=[ExceptionItem(**e) for e in out["exceptions"]],
        triggered_by=decision.triggered_by,
        cached=False,
        created_at=decision.created_at,
    )


def _row_to_today_payload(row: dict[str, Any], *, cached: bool) -> TodayPayload:
    output = _coerce_jsonb(row["output"])
    return TodayPayload(
        decision_id=UUID(str(row["decision_id"])),
        depot_id=output["depot_id"],
        window=output["window"],
        coverage=CoverageBlock(**output["coverage"]),
        status=output["status"],
        exceptions=[ExceptionItem(**e) for e in output.get("exceptions", [])],
        triggered_by=row["triggered_by"],
        cached=cached,
        created_at=row["created_at"],
    )


def _row_to_envelope(row: dict[str, Any]) -> DecisionEnvelope:
    output = _coerce_jsonb(row["output"])
    tool_calls = _coerce_jsonb(row["tool_calls"])
    if not isinstance(tool_calls, list):
        tool_calls = []
    return DecisionEnvelope(
        decision_id=UUID(str(row["decision_id"])),
        workflow_name=row["workflow_name"],
        workflow_version=row["workflow_version"],
        depot_id=UUID(str(row["depot_id"])),
        organization_id=UUID(str(row["organization_id"])) if row.get("organization_id") else None,
        triggered_by=row["triggered_by"],
        triggered_by_user_id=UUID(str(row["triggered_by_user_id"]))
        if row.get("triggered_by_user_id")
        else None,
        inputs_hash=row["inputs_hash"],
        tool_calls=tool_calls,
        output=output,
        permission_tier=row["permission_tier"],
        status=row["status"],
        duration_ms=row.get("duration_ms"),
        created_at=row["created_at"],
    )


def _row_to_list_item(row: dict[str, Any]) -> DecisionListItem:
    output = _coerce_jsonb(row["output"])
    exceptions = output.get("exceptions", []) if isinstance(output, dict) else []
    return DecisionListItem(
        decision_id=UUID(str(row["decision_id"])),
        depot_id=UUID(str(row["depot_id"])),
        triggered_by=row["triggered_by"],
        status=row["status"],
        workflow_version=row["workflow_version"],
        has_exceptions=bool(exceptions),
        exception_count=len(exceptions) if isinstance(exceptions, list) else 0,
        duration_ms=row.get("duration_ms"),
        created_at=row["created_at"],
    )


def _coerce_jsonb(value: Any) -> Any:
    """asyncpg returns JSONB as text when no codec is set; decode safely."""
    if isinstance(value, str):
        import json

        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _emit_workflow_metrics(decision: ReadinessDecision, duration_s: float) -> None:
    AGENT_WORKFLOW_RUNS.labels(
        workflow_name=decision.workflow_name,
        triggered_by=decision.triggered_by,
        status=decision.status,
    ).inc()
    AGENT_WORKFLOW_RUN_DURATION.labels(workflow_name=decision.workflow_name).observe(duration_s)
    for exc in decision.output.exceptions:
        action_type = exc.proposed_action.get("type") if exc.proposed_action else "none"
        AGENT_WORKFLOW_EXCEPTIONS_OUT.labels(
            workflow_name=decision.workflow_name,
            exception_type=str(action_type or "none"),
        ).inc()
