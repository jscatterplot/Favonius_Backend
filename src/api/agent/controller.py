"""End-to-end orchestrator for one chat turn.

The flow mirrors the architecture doc §3.7:

1. ``build_auth_context``                 — JWT → :class:`AuthContext`.
2. ``agent_runs_open``                    — open the run row (status=running).
3. ``llm_client.extract_plan``            — natural language → :class:`QueryPlan`.
4. ``resolve_entities``                   — names → UUIDs (auth-scoped).
5. Disambiguation / not-found short-circuits — close run + return early.
6. ``load_depot_timezones`` + ``resolve_time_window``.
7. ``compile_consumption_by_user``        — deterministic SQL builder.
8. ``ts_pool.fetch``                      — execute against TimescaleDB.
9. ``write_agent_query_audit``            — mirror to the existing admin
   audit feed (``action='agent.query'``).
10. ``llm_client.format_answer``          — rows → natural language.
11. ``agent_runs_close(status='success', reply)``.

When SSE is requested the orchestrator emits one ``step`` event per
phase plus a final ``answer`` event. The same writes still happen on the
``agent_runs`` row so the audit trail is unchanged whether the client
asked for streaming or not.

Errors are caught at the top level: the run is closed with
``status='error'`` and the exception is re-raised so the route handler
can translate it to a generic ``502`` response. Internal error text is
never leaked back to the client per the security review.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.api.agent.audit import (
    agent_runs_close,
    agent_runs_open,
    agent_runs_step,
    write_agent_query_audit,
)
from src.api.agent.auth_context import (
    ResolvedEntity,
    ResolvedTimeWindow,
    build_auth_context,
)
from src.api.agent.intents.consumption_by_user import compile_consumption_by_user
from src.api.agent.plan import QueryPlan
from src.api.agent.planner import classify as planner_classify
from src.api.agent.prompts import (
    build_sql_agent_system_prompt,
    format_sql_agent_user_message,
)
from src.api.agent.resolve import (
    load_depot_timezones,
    resolve_entities,
    resolve_time_window,
)
from src.api.agent.sql_tools import (
    SQL_AGENT_TOOL_NAMES,
    build_sql_agent_tool_registry,
)
from src.api.agent.stream import SSEEventStream
from src.api.agent_workflows.runtime import run_qa_turn
from src.api.agent_workflows.tools import ToolNotRegisteredError
from src.monitoring.metrics import (
    AGENT_RESOLVER_MISSES,
    AGENT_SQL_TOOL_TURNS,
)

logger = logging.getLogger(__name__)


# ── LLM client protocol ────────────────────────────────────────────────────
#
# The orchestrator depends only on this Protocol so tests can inject a fake
# without touching the Anthropic SDK. The default implementation in
# :class:`RealLLMClient` delegates to the module-level helpers in
# ``src.api.agent.llm`` — which already encapsulate prompt caching, retries,
# and rotation-aware key plumbing.


class LLMClient(Protocol):
    """Two-method interface the agent orchestrator needs."""

    async def extract_plan(self, message: str) -> QueryPlan:
        """Parse a user message into a strict :class:`QueryPlan`."""

    async def format_answer(
        self,
        plan: QueryPlan,
        resolved: list[dict[str, Any]],
        window: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> str:
        """Format a SQL result set into a natural-language reply."""


class RealLLMClient:
    """Default :class:`LLMClient` that calls the Anthropic SDK module."""

    async def extract_plan(self, message: str) -> QueryPlan:
        from src.api.agent import llm  # local import to keep test envs llm-free

        return await llm.extract_plan(message)

    async def format_answer(
        self,
        plan: QueryPlan,
        resolved: list[dict[str, Any]],
        window: dict[str, Any],
        rows: list[dict[str, Any]],
    ) -> str:
        from src.api.agent import llm

        return await llm.format_answer(plan, resolved, window, rows)


# ── Reply shape ────────────────────────────────────────────────────────────


class _CandidateOption(BaseModel):
    """One option in a disambiguation reply."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    display: str
    primary_id: Optional[str] = None


class AgentReply(BaseModel):
    """End-of-turn payload returned by both the JSON and SSE endpoints.

    Four possible ``status`` values:

    - ``success``         — the answer is in ``text``.
    - ``disambiguation``  — multiple matches; ``candidates`` lists them.
    - ``not_found``       — at least one subject did not resolve;
                            ``not_found`` carries the unresolved phrases.
    - ``error``           — the orchestrator failed; ``text`` is a generic
                            user-facing message (the cause is logged
                            server-side and never leaked here).
    """

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: str = Field(..., description="success | disambiguation | not_found | error")
    text: str
    intent: Optional[str] = None
    candidates: list[_CandidateOption] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)

    @classmethod
    def success(cls, *, run_id: UUID, intent: str, text: str) -> "AgentReply":
        return cls(run_id=run_id, status="success", text=text, intent=intent)

    @classmethod
    def disambiguation(
        cls, *, run_id: UUID, intent: str, ambiguous: list[ResolvedEntity]
    ) -> "AgentReply":
        candidates: list[_CandidateOption] = []
        for entity in ambiguous:
            for cand in entity.candidates or [entity]:
                candidates.append(
                    _CandidateOption(
                        kind=cand.kind,
                        display=cand.display,
                        primary_id=str(cand.primary_id) if cand.primary_id else None,
                    )
                )
        if candidates:
            options = "\n".join(f"- {c.display}" for c in candidates)
            text = f"I found multiple matches. Please clarify which one you mean:\n{options}"
        else:
            # Defensive: shouldn't happen because the caller only invokes
            # this branch when ``ambiguous`` has at least one entry, but
            # an empty candidates list would otherwise yield a stub reply.
            text = "I found multiple matches. Please clarify which one you mean."
        return cls(
            run_id=run_id,
            status="disambiguation",
            text=text,
            intent=intent,
            candidates=candidates,
        )

    @classmethod
    def not_found_reply(
        cls, *, run_id: UUID, intent: str, missing: list[ResolvedEntity]
    ) -> "AgentReply":
        labels = [m.display for m in missing]
        text = (
            "I couldn't find " + ", ".join(repr(label) for label in labels) + " "
            "in your depots. Double-check the spelling, or try the person's "
            "employee ID or a vehicle's license plate."
        )
        return cls(run_id=run_id, status="not_found", text=text, intent=intent, not_found=labels)

    @classmethod
    def error(cls, *, run_id: UUID, message: Optional[str] = None) -> "AgentReply":
        # ``message`` is accepted so the orchestrator can stash the raw
        # exception string into the run trace's close call, but the
        # user-facing ``text`` is always the canned safe string. Internal
        # detail is captured server-side via ``logger.exception`` instead.
        del message  # unused on purpose
        return cls(
            run_id=run_id,
            status="error",
            text="Something went wrong handling your request. Please try again.",
        )


# ── Step-summary helpers (shown in SSE) ────────────────────────────────────


def _summarize_plan(plan: QueryPlan) -> str:
    if plan.time_window.kind == "relative":
        when = plan.time_window.relative or "?"
    else:
        when = f"{plan.time_window.from_iso}..{plan.time_window.to_iso}"
    n_subjects = len(plan.subjects)
    return f"{plan.intent}, {n_subjects} subject(s), {when}"


def _summarize_resolved(resolved: list[ResolvedEntity]) -> str:
    if not resolved:
        return "no subjects to resolve"
    parts: list[str] = []
    for e in resolved:
        if e.candidates:
            parts.append(f"{e.kind}: ambiguous ({len(e.candidates)} candidates)")
        elif e.primary_id is None:
            parts.append(f"{e.kind}: not found ({e.display!r})")
        else:
            parts.append(f"{e.kind}: {e.display}")
    return "; ".join(parts)


def _describe_params(params: list[Any]) -> list[dict[str, Any]]:
    """Compact, leak-safe shape descriptor for the compile step's audit row."""
    out: list[dict[str, Any]] = []
    for p in params:
        if isinstance(p, list):
            out.append({"type": "list", "len": len(p)})
        elif isinstance(p, str):
            out.append({"type": "str"})
        else:
            out.append({"type": type(p).__name__})
    return out


# ── Orchestrator ───────────────────────────────────────────────────────────


async def run_turn(
    message: str,
    token_payload: dict,
    static_pool: Any,
    ts_pool: Any,
    llm_client: LLMClient,
    *,
    sse: Optional[SSEEventStream] = None,
) -> AgentReply:
    """End-to-end orchestration of one chat turn.

    Args:
        message: Raw user message.
        token_payload: Decoded JWT payload from ``verify_token``.
        static_pool: asyncpg pool for the Supabase static schema.
        ts_pool: asyncpg pool for the TimescaleDB time-series schema.
        llm_client: Object implementing :class:`LLMClient`.
        sse: Optional :class:`SSEEventStream`. When provided, the
            orchestrator emits ``step`` events for each phase and a
            final ``answer`` event with the reply payload.

    Returns:
        An :class:`AgentReply`. Raises only on unrecoverable failures
        — the caller is expected to translate to ``502`` and let the
        ``agent_runs.status='error'`` row carry the trail.
    """
    auth = await build_auth_context(token_payload, static_pool)
    run_id = await agent_runs_open(ts_pool, auth, message)

    async def _emit_step(name: str, summary: str) -> None:
        if sse is not None:
            await sse.emit("step", {"name": name, "summary": summary})

    try:
        # 0. Planner — pick consumption fast path, sql_general, or refuse.
        decision = planner_classify(
            message, organization_id=auth.organization_id
        )
        await agent_runs_step(
            ts_pool,
            run_id,
            "planner_decision",
            {"route": decision.route, "reason": decision.reason},
        )
        await _emit_step("planner_decision", f"{decision.route} ({decision.reason})")

        if decision.route == "refuse":
            reply = AgentReply(
                run_id=run_id,
                status="not_found",
                text=(
                    "I can only answer depot analytics questions, and right "
                    "now my analytical mode is disabled for your organisation. "
                    "Try a consumption question like 'how much did <driver> "
                    "charge last month?'."
                ),
                intent="refuse",
            )
            await agent_runs_close(ts_pool, run_id, "not_found", reply)
            if sse is not None:
                await sse.emit("answer", reply.model_dump(mode="json"))
            return reply

        if decision.route == "sql_general":
            return await _run_sql_general_turn(
                run_id=run_id,
                message=message,
                auth=auth,
                static_pool=static_pool,
                ts_pool=ts_pool,
                sse=sse,
                emit_step=_emit_step,
            )

        # 1. Extract the plan.
        plan = await llm_client.extract_plan(message)
        await agent_runs_step(ts_pool, run_id, "extract_plan", plan.model_dump())
        await _emit_step("extract_plan", _summarize_plan(plan))

        # 2. Resolve entity mentions to UUIDs.
        resolved = await resolve_entities(plan.subjects, auth, static_pool)
        await agent_runs_step(
            ts_pool,
            run_id,
            "resolve_entities",
            [e.model_dump() for e in resolved],
        )
        await _emit_step("resolve_entities", _summarize_resolved(resolved))

        # 3a. Disambiguation short-circuit.
        ambiguous = [e for e in resolved if e.candidates]
        if ambiguous:
            for _entity in ambiguous:
                AGENT_RESOLVER_MISSES.labels(kind="ambiguous").inc()
            reply = AgentReply.disambiguation(run_id=run_id, intent=plan.intent, ambiguous=ambiguous)
            await agent_runs_close(ts_pool, run_id, "disambiguation", reply)
            if sse is not None:
                await sse.emit("answer", reply.model_dump(mode="json"))
            return reply

        # 3b. Not-found short-circuit.
        missing = [e for e in resolved if e.primary_id is None]
        if missing:
            for _entity in missing:
                AGENT_RESOLVER_MISSES.labels(kind="not_found").inc()
            reply = AgentReply.not_found_reply(run_id=run_id, intent=plan.intent, missing=missing)
            await agent_runs_close(ts_pool, run_id, "not_found", reply)
            if sse is not None:
                await sse.emit("answer", reply.model_dump(mode="json"))
            return reply

        # 4. Resolve time window in the depot timezone.
        depot_tzs = await load_depot_timezones(static_pool, auth.visible_depot_ids)
        window: ResolvedTimeWindow = resolve_time_window(
            plan.time_window, auth.visible_depot_ids, depot_tzs
        )

        # 5. Compile + execute SQL.
        sql, params = compile_consumption_by_user(plan, resolved, window)
        await agent_runs_step(
            ts_pool,
            run_id,
            "compile",
            {"intent": plan.intent, "param_shapes": _describe_params(params)},
        )
        await _emit_step("compile", f"{plan.intent} SQL prepared")

        rows = await ts_pool.fetch(sql, *params)
        rows_list = [dict(r) for r in rows]
        await agent_runs_step(ts_pool, run_id, "execute", {"row_count": len(rows_list)})
        await _emit_step("execute", f"{len(rows_list)} rows returned")

        # 6. Mirror the executed query into the admin audit feed.
        await write_agent_query_audit(
            ts_pool,
            auth,
            run_id,
            plan.intent,
            len(rows_list),
        )

        # 7. Format + close.
        text = await llm_client.format_answer(
            plan,
            [e.model_dump(mode="json") for e in resolved],
            window.model_dump(mode="json"),
            rows_list,
        )
        reply = AgentReply.success(run_id=run_id, intent=plan.intent, text=text)
        await agent_runs_close(ts_pool, run_id, "success", reply)
        if sse is not None:
            await sse.emit("answer", reply.model_dump(mode="json"))
        return reply

    except Exception:
        # Stamp the row with status='error' on a best-effort basis so the
        # audit trail is preserved, then re-raise so the route handler can
        # convert this into a sanitized 502. The exception text is logged
        # server-side only — never returned in the HTTP body.
        logger.exception("Agent turn failed (run_id=%s)", run_id)
        try:
            error_reply = AgentReply.error(run_id=run_id)
            await agent_runs_close(ts_pool, run_id, "error", error_reply)
        except Exception:  # pragma: no cover - audit close is best-effort
            logger.exception("Failed to close agent_run %s in error state", run_id)
        raise


# ── SQL-mode (general analytics) sub-orchestrator ──────────────────────────


async def _run_sql_general_turn(
    *,
    run_id: UUID,
    message: str,
    auth: Any,
    static_pool: Any,
    ts_pool: Any,
    sse: Optional[SSEEventStream],
    emit_step: Any,
) -> AgentReply:
    """Drive the text-to-SQL agent loop via WorkflowAgent.run_qa_turn.

    The Anthropic client + model selection are reused from
    ``src.api.agent.llm`` so a single Anthropic singleton serves both
    paths. Per-tool-call step events flow into both ``agent_runs`` and
    the SSE stream via the ``on_step`` callback.
    """
    from src.api.agent import llm as agent_llm  # local: keeps test envs llm-free

    client = agent_llm._get_client()
    config = agent_llm.get_config()

    registry = build_sql_agent_tool_registry(static_pool, ts_pool, auth)

    async def _on_step(tool_call: Any) -> None:
        name = tool_call.name
        # Strip oversized payloads so steps_json stays readable.
        result = tool_call.result
        if isinstance(result, dict) and isinstance(result.get("rows"), list):
            rows = result["rows"]
            preview = {
                **{k: v for k, v in result.items() if k != "rows"},
                "row_preview": rows[:3],
                "row_total": result.get("row_count", len(rows)),
            }
        else:
            preview = result
        payload = {
            "tool": name,
            "ok": tool_call.ok,
            "input": tool_call.arguments,
            "result_preview": preview,
            "error": tool_call.error,
        }
        await agent_runs_step(ts_pool, run_id, "tool_call", payload)
        summary = f"{name}: {'ok' if tool_call.ok else 'error'}"
        await emit_step("tool_call", summary)

    try:
        qa = await run_qa_turn(
            anthropic_client=client,
            model=config.model,
            system_prompt=build_sql_agent_system_prompt(),
            user_message=format_sql_agent_user_message(message),
            tool_registry=registry,
            allowed_tools=SQL_AGENT_TOOL_NAMES,
            max_iterations=8,
            max_tokens=2048,
            temperature=0.0,
            on_step=_on_step,
        )
    except ToolNotRegisteredError as exc:
        logger.error("SQL agent ToolNotRegisteredError: %s", exc)
        reply = AgentReply.error(run_id=run_id)
        await agent_runs_close(ts_pool, run_id, "error", reply)
        if sse is not None:
            await sse.emit("answer", reply.model_dump(mode="json"))
        return reply

    AGENT_SQL_TOOL_TURNS.observe(qa.iterations)
    await agent_runs_step(
        ts_pool,
        run_id,
        "sql_loop_complete",
        {
            "iterations": qa.iterations,
            "tool_call_count": len(qa.tool_calls),
            "row_evidence": qa.row_evidence,
            "status": qa.status,
        },
    )

    if qa.status == "success" and qa.text:
        # Count SQL executions in the trace for audit metadata.
        sql_executions = sum(
            1 for tc in qa.tool_calls if tc.name in ("run_select_ts", "run_select_static")
        )
        await write_agent_query_audit(ts_pool, auth, run_id, "sql_general", qa.row_evidence)
        reply = AgentReply.success(run_id=run_id, intent="sql_general", text=qa.text)
        await agent_runs_close(ts_pool, run_id, "success", reply)
        if sse is not None:
            await sse.emit("answer", reply.model_dump(mode="json"))
        _ = sql_executions  # currently unused beyond the count; left for future audit metadata
        return reply

    # Non-success outcomes: terminator missing, max iterations hit, etc.
    text = qa.text.strip() if qa.text else ""
    if not text:
        text = (
            "I wasn't able to compose a complete answer to that question. "
            "Try rephrasing, or break it into smaller questions."
        )
    reply = AgentReply(
        run_id=run_id,
        status="not_found" if qa.status in ("no_terminator", "max_iterations") else "error",
        text=text,
        intent="sql_general",
    )
    await agent_runs_close(ts_pool, run_id, reply.status, reply)
    if sse is not None:
        await sse.emit("answer", reply.model_dump(mode="json"))
    return reply
