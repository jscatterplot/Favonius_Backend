"""FastAPI router for the depot chat agent.

Three endpoints (architecture doc §6.1), all mounted under ``/agent`` in
``src/api/main.py`` behind the ``AGENT_SEARCH_ENABLED`` feature flag:

- ``POST /agent/turn``         — synchronous; returns an :class:`AgentReply`.
- ``POST /agent/turn/stream``  — SSE; yields ``step`` events then ``answer``.
- ``GET  /agent/runs/{id}``    — fetch a stored ``agent_runs`` row, gated
                                 on ownership (or favonius_admin).

All three depend on :func:`src.security.auth.verify_token` for JWT auth. The
LLM-powered turn endpoints additionally depend on
:func:`verify_token_and_check_agent_limit`, which applies the same 10 req/min
cadence as ``POST /optimize`` via a dedicated in-memory bucket
(``check_agent_limit``). The read-only trace endpoint uses JWT auth only so
lightweight history reads do not consume the expensive-turn budget.

Geo-block + tenant-mirror inheritance is automatic: the router is mounted
on the same FastAPI app, and the global ``GeoBlockMiddleware`` (registered
last in ``main.py`` so it runs first) and the existing JWT pipeline cover
every request that lands here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from src.api.agent.controller import AgentReply, LLMClient, RealLLMClient, run_turn
from src.api.agent.stream import SSE_HEADERS, SSEEventStream
from src.api.agent.view_context import AgentViewContext
from src.monitoring.metrics import AGENT_TURN_DURATION, AGENT_TURNS
from src.security.auth import get_user_id, get_user_role, verify_token
from src.security.rate_limiter import get_rate_limiter

logger = logging.getLogger(__name__)


# ── Dependency providers ──────────────────────────────────────────────────
#
# These small wrappers exist so tests can override them via
# ``app.dependency_overrides`` without monkey-patching module globals. They
# all read from the lifespan-initialised state in ``src.api.main`` so the
# production path picks up the same pools the rest of the app uses.


def get_static_pool() -> Any:
    """Return the static (Supabase) asyncpg pool from the live app state."""
    from src.api import main as api_main  # local import: avoid circular import

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


def get_llm_client() -> LLMClient:
    """Return the default :class:`LLMClient`. Tests override this."""
    return RealLLMClient()


async def verify_token_and_check_agent_limit(user: dict = Depends(verify_token)) -> dict:
    """JWT auth then agent rate limit (10/min); returns the verified payload."""
    client_id = f"user:{get_user_id(user)}"
    limiter = get_rate_limiter()
    result = limiter.check_agent_limit(client_id)
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded: 10 requests per minute on /agent/*",
            headers={
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": str(result.remaining),
                "X-RateLimit-Reset": str(int(result.reset_at)),
            },
        )
    return user


# ── Request / response models ─────────────────────────────────────────────


class AgentTurnRequest(BaseModel):
    """One-shot turn request body.

    ``context`` is optional UI state describing what the user currently has
    open (page, selected depot, filters, focused item). It is NOT injected
    into the prompt; the SQL-mode agent pulls it on demand via the
    ``get_page_context`` tool only when a question is ambiguous. See
    ``src/api/agent/view_context.py``.

    ``session_id`` is an optional document-fill session (from
    ``POST /agent/documents``). When present and ``AGENT_DOC_FILL_ENABLED`` is
    on, the turn routes to the collaborative document-fill loop instead of the
    planner.
    """

    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1, max_length=2000)
    context: Optional[AgentViewContext] = None
    session_id: Optional[UUID] = None


class AgentRunRow(BaseModel):
    """``agent_runs`` row shape returned by ``GET /agent/runs/{id}``."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    user_id: UUID
    organization_id: Optional[UUID] = None
    depot_id: Optional[UUID] = None
    user_message: str
    final_intent: Optional[str] = None
    steps_json: list[dict[str, Any]] = Field(default_factory=list)
    status: str
    duration_ms: Optional[int] = None
    created_at: str


# ── Router ────────────────────────────────────────────────────────────────


router = APIRouter(prefix="/agent", tags=["agent"])


@router.post(
    "/turn",
    response_model=AgentReply,
    summary="Run one chat turn synchronously",
)
async def post_turn(
    body: AgentTurnRequest,
    user: dict = Depends(verify_token_and_check_agent_limit),
    static_pool: Any = Depends(get_static_pool),
    ts_pool: Any = Depends(get_ts_pool),
    llm_client: LLMClient = Depends(get_llm_client),
) -> AgentReply:
    """Run one turn end-to-end and return the final reply."""
    start = time.monotonic()
    try:
        reply = await run_turn(
            message=body.message,
            token_payload=user,
            static_pool=static_pool,
            ts_pool=ts_pool,
            llm_client=llm_client,
            context=body.context,
            session_id=body.session_id,
        )
        duration = time.monotonic() - start
        intent = reply.intent or "unknown"
        AGENT_TURNS.labels(status=reply.status, intent=intent).inc()
        AGENT_TURN_DURATION.labels(intent=intent).observe(duration)
        return reply
    except HTTPException:
        # Auth-context errors (403 on missing org, etc.) bubble up as-is.
        AGENT_TURNS.labels(status="error", intent="unknown").inc()
        raise
    except Exception:
        logger.exception("Agent turn failed (synchronous endpoint)")
        AGENT_TURNS.labels(status="error", intent="unknown").inc()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Agent turn failed. Please try again.",
        )


@router.post(
    "/turn/stream",
    summary="Run one chat turn and stream step events via SSE",
)
async def post_turn_stream(
    body: AgentTurnRequest,
    user: dict = Depends(verify_token_and_check_agent_limit),
    static_pool: Any = Depends(get_static_pool),
    ts_pool: Any = Depends(get_ts_pool),
    llm_client: LLMClient = Depends(get_llm_client),
) -> StreamingResponse:
    """Run one turn and stream step + answer events as SSE.

    The orchestrator runs in a background task that emits encoded SSE
    events into a shared queue. A failure inside the orchestrator emits
    a single ``error`` event with a generic message and closes the
    stream — the underlying exception is logged but never reaches the
    client. The synchronous JSON endpoint returns a 502 in the same
    case; the SSE endpoint returns a 200 with the error event because
    headers have already been sent by then.
    """

    stream = SSEEventStream()

    async def _run() -> None:
        start = time.monotonic()
        try:
            reply = await run_turn(
                message=body.message,
                token_payload=user,
                static_pool=static_pool,
                ts_pool=ts_pool,
                llm_client=llm_client,
                sse=stream,
                context=body.context,
                session_id=body.session_id,
            )
            duration = time.monotonic() - start
            intent = reply.intent or "unknown"
            AGENT_TURNS.labels(status=reply.status, intent=intent).inc()
            AGENT_TURN_DURATION.labels(intent=intent).observe(duration)
        except HTTPException as exc:
            # Auth/scope errors thrown inside build_auth_context — surface
            # the status code in the event payload for client-side display
            # without leaking the exception's detail beyond what HTTPException
            # already exposes via its status code.
            logger.warning("Agent SSE turn failed with HTTPException status=%s", exc.status_code)
            AGENT_TURNS.labels(status="error", intent="unknown").inc()
            await stream.emit("error", {"status": exc.status_code, "detail": exc.detail})
        except Exception:
            logger.exception("Agent SSE turn failed")
            AGENT_TURNS.labels(status="error", intent="unknown").inc()
            await stream.emit(
                "error",
                {"status": 502, "detail": "Agent turn failed. Please try again."},
            )
        finally:
            stream.close()

    async def _iterator() -> AsyncIterator[bytes]:
        # Strong ref for the generator's lifetime — asyncio docs warn that a
        # task with no other references may be GC'd before it completes.
        producer_task = asyncio.create_task(_run())
        async for chunk in stream:
            yield chunk
        await producer_task

    return StreamingResponse(
        _iterator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.get(
    "/runs/{run_id}",
    response_model=AgentRunRow,
    summary="Fetch a stored agent run trace",
)
async def get_run(
    run_id: UUID,
    user: dict = Depends(verify_token),
    ts_pool: Any = Depends(get_ts_pool),
) -> AgentRunRow:
    """Return the stored ``agent_runs`` row for ``run_id``.

    Access is gated on ownership: the row's ``user_id`` must equal the
    caller's ``sub`` claim, or the caller must be ``favonius_admin``. A
    mismatched tenant user gets a 404 (not 403) so we don't leak the
    existence of another tenant's run IDs.
    """
    caller_id = UUID(get_user_id(user))
    role = get_user_role(user)

    async with ts_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                run_id,
                user_id,
                organization_id,
                depot_id,
                user_message,
                final_intent,
                steps_json,
                status,
                duration_ms,
                created_at
            FROM agent_runs
            WHERE run_id = $1::uuid
            """,
            str(run_id),
        )

    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    row_user_id = UUID(str(row["user_id"]))
    if role != "favonius_admin" and row_user_id != caller_id:
        # Don't differentiate "exists but yours? no" from "doesn't exist"
        # in the response — both are 404 to avoid run-id enumeration.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    steps = row["steps_json"]
    if isinstance(steps, str):
        # asyncpg may return JSONB as raw text when a connection-level
        # codec is not registered; decode defensively.
        import json as _json

        steps = _json.loads(steps)

    return AgentRunRow(
        run_id=UUID(str(row["run_id"])),
        user_id=row_user_id,
        organization_id=UUID(str(row["organization_id"])) if row["organization_id"] else None,
        depot_id=UUID(str(row["depot_id"])) if row["depot_id"] else None,
        user_message=row["user_message"],
        final_intent=row["final_intent"],
        steps_json=list(steps or []),
        status=row["status"],
        duration_ms=row["duration_ms"],
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
    )
