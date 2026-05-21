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
import re
from typing import Any, Optional, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.api.agent.audit import (
    agent_runs_close,
    agent_runs_open,
    agent_runs_step,
    sql_audit_target_type,
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
from src.api.agent_workflows.runtime import ToolNotAllowedError, run_qa_turn
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
        return cls(
            run_id=run_id,
            status="not_found",
            text=text,
            intent=intent,
            not_found=labels,
        )

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


async def _emit_answer_safe(
    sse: Optional[SSEEventStream], reply: "AgentReply", run_id: UUID
) -> None:
    """Emit the final SSE ``answer`` event without letting failures propagate.

    The ``agent_runs`` row is already closed by the caller at this point,
    so an SSE write error must NOT trigger the outer-exception path that
    would overwrite the terminal status with ``"error"`` — the run actually
    succeeded, only the post-close side-effect failed. Pre-close SSE
    ``step`` events are not wrapped because their failure SHOULD abort the
    turn (no audit row has been closed yet).
    """
    if sse is None:
        return
    try:
        await sse.emit("answer", reply.model_dump(mode="json"))
    except Exception:  # noqa: BLE001 — best-effort post-close
        logger.exception(
            "Failed to emit SSE answer for run %s; agent_runs row "
            "already closed with status %r — leaving it as-is.",
            run_id,
            reply.status,
        )


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
        decision = planner_classify(message, organization_id=auth.organization_id)
        await agent_runs_step(
            ts_pool,
            run_id,
            "planner_decision",
            {"route": decision.route, "reason": decision.reason},
        )
        await _emit_step("planner_decision", f"{decision.route} ({decision.reason})")

        if decision.route == "refuse":
            reply_text = "Please enter a question about depot charging or analytics."
            reply = AgentReply(
                run_id=run_id,
                status="not_found",
                text=reply_text,
                intent="refuse",
            )
            await agent_runs_close(ts_pool, run_id, "not_found", reply)
            await _emit_answer_safe(sse, reply, run_id)
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
            reply = AgentReply.disambiguation(
                run_id=run_id, intent=plan.intent, ambiguous=ambiguous
            )
            await agent_runs_close(ts_pool, run_id, "disambiguation", reply)
            await _emit_answer_safe(sse, reply, run_id)
            return reply

        # 3b. Not-found short-circuit.
        missing = [e for e in resolved if e.primary_id is None]
        if missing:
            for _entity in missing:
                AGENT_RESOLVER_MISSES.labels(kind="not_found").inc()
            reply = AgentReply.not_found_reply(
                run_id=run_id, intent=plan.intent, missing=missing
            )
            await agent_runs_close(ts_pool, run_id, "not_found", reply)
            await _emit_answer_safe(sse, reply, run_id)
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
        await _emit_answer_safe(sse, reply, run_id)
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

_AGENT_VIEWS_FN_RE = re.compile(r"agent_views\.(\w+)", re.IGNORECASE)


def _sql_functions_accessed(tool_calls: Any) -> list[str]:
    """Collect agent_views function names from SQL-mode run_select tool calls."""
    seen: set[str] = set()
    ordered: list[str] = []
    for tc in tool_calls:
        if tc.name not in ("run_select_ts", "run_select_static"):
            continue
        names: list[str] = []
        if tc.ok and isinstance(tc.result, dict):
            raw = tc.result.get("functions_used")
            if isinstance(raw, list):
                names = [str(n) for n in raw]
        if not names and isinstance(tc.arguments, dict):
            sql = tc.arguments.get("sql") or ""
            names = _AGENT_VIEWS_FN_RE.findall(str(sql))
        for fn in names:
            key = fn.lower()
            if key not in seen:
                seen.add(key)
                ordered.append(key)
    return ordered


def _extract_iterations(exc: BaseException) -> int:
    """Pull the ``iterations`` count off an exception raised by run_qa_turn.

    Both ``ToolNotAllowedError`` (via ctor) and the generic
    ``Exception`` path (via the ``runtime.py`` outer handler that
    attaches ``exc.iterations``) carry the integer. Defensive cast +
    None guard keep this safe for any future exception subclass that
    forgets to set the attribute.
    """
    return int(getattr(exc, "iterations", 0) or 0)


async def _mirror_sql_general_audit_from_calls(
    *,
    ts_pool: Any,
    auth: Any,
    run_id: UUID,
    partial_calls: list,
    abort_reason: str,
) -> None:
    """Mirror admin audit when SQL tools ran before an abort (P2 observability)."""
    sql_attempts = 0
    server_row_total = 0
    for tc in partial_calls:
        if tc.name not in ("run_select_ts", "run_select_static"):
            continue
        sql_attempts += 1
        if not tc.ok:
            continue
        result = tc.result if isinstance(tc.result, dict) else {}
        try:
            server_row_total += int(result.get("row_count", 0) or 0)
        except (TypeError, ValueError):
            pass
    if sql_attempts <= 0:
        return
    await agent_runs_step(
        ts_pool,
        run_id,
        "sql_loop_aborted",
        {
            "tool_call_count": len(partial_calls),
            "sql_attempts": sql_attempts,
            "server_row_total": server_row_total,
            "abort_reason": abort_reason,
        },
    )
    partial_functions_accessed = _sql_functions_accessed(partial_calls)
    await write_agent_query_audit(
        ts_pool,
        auth,
        run_id,
        "sql_general",
        server_row_total,
        target_type=sql_audit_target_type(partial_functions_accessed),
        functions_accessed=partial_functions_accessed or None,
    )


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
    config = agent_llm.CONFIG

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

    # Bugbot L-sev: previous shape was ``sql_tool_turns = 0`` +
    # try/except/else/finally with the variable reassigned in three
    # branches and a single ``finally: AGENT_SQL_TOOL_TURNS.observe(…)``.
    # The triple-assignment was correct today but fragile: any future
    # refactor that changed an except handler to fall through (instead
    # of return/raise) would silently observe a stale value from the
    # other branch. We now observe the metric LOCALLY in each branch,
    # with the value derived from the in-scope source (qa.iterations or
    # exc.iterations) right next to where the path is decided. One
    # observation per path; no shared mutable state.
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
    except (ToolNotRegisteredError, ToolNotAllowedError) as exc:
        # Both exception types carry ``iterations``: ToolNotAllowedError
        # via its ctor; ToolNotRegisteredError via the attribute
        # attached at the re-raise site in run_qa_turn. Without
        # observing here, every policy-violation turn would flat-line
        # the histogram at 0, biasing the distribution toward zero on
        # the runs we most need to monitor.
        AGENT_SQL_TOOL_TURNS.observe(_extract_iterations(exc))
        logger.error("SQL agent tool error: %s", exc)
        # Codex P2: if a disallowed tool aborted the loop AFTER one or
        # more SQL tools had already executed, we still owe those rows
        # an entry in the admin audit feed — otherwise the audit gap
        # lands exactly on policy-violation turns where the trail
        # matters most. ToolNotAllowedError carries the partial trace
        # as `exc.tool_calls`; ToolNotRegisteredError doesn't (it raises
        # before any tool dispatch in this turn) — falls through with
        # an empty list.
        partial_calls = list(getattr(exc, "tool_calls", []) or [])
        # Bugbot M-sev: wrap the audit mirror in try/except. If the DB
        # write raises (append-only trigger rejection, asyncpg conn
        # error, etc.), the unhandled exception would mask the original
        # ToolNotAllowedError / ToolNotRegisteredError context AND skip
        # the agent_runs_close + SSE emit + return below, leaving the
        # run row in 'running' state and the client with a raw 502
        # instead of the graceful error reply. Log and continue — the
        # outer-handler in run_turn is the audit safety net.
        try:
            await _mirror_sql_general_audit_from_calls(
                ts_pool=ts_pool,
                auth=auth,
                run_id=run_id,
                partial_calls=partial_calls,
                abort_reason=type(exc).__name__,
            )
        except Exception:  # noqa: BLE001 - best-effort error-path audit
            logger.exception(
                "Failed to mirror partial SQL audit for aborted run %s "
                "(original abort: %s); continuing with graceful error close.",
                run_id, type(exc).__name__,
            )
        reply = AgentReply.error(run_id=run_id)
        try:
            await agent_runs_close(ts_pool, run_id, "error", reply)
        except Exception:  # pragma: no cover - audit close is best-effort
            logger.exception("Failed to close agent_run %s in error state", run_id)
        await _emit_answer_safe(sse, reply, run_id)
        return reply
    except Exception as exc:
        AGENT_SQL_TOOL_TURNS.observe(_extract_iterations(exc))
        partial_calls = list(getattr(exc, "tool_calls", []) or [])
        # Wrap in try/except (Bugbot M-sev): if the audit write fails
        # here it would mask the original exception, swallowing the
        # actual cause of the SQL-mode failure that the outer
        # ``run_turn`` exception handler needs to log / convert to a
        # 502. Log the audit-side failure and re-raise the ORIGINAL
        # exception below.
        try:
            await _mirror_sql_general_audit_from_calls(
                ts_pool=ts_pool,
                auth=auth,
                run_id=run_id,
                partial_calls=partial_calls,
                abort_reason=type(exc).__name__,
            )
        except Exception:  # noqa: BLE001 - best-effort, never mask `exc`
            logger.exception(
                "Failed to mirror partial SQL audit on unhandled SQL-mode "
                "exception for run %s (original exception will still raise).",
                run_id,
            )
        raise
    else:
        AGENT_SQL_TOOL_TURNS.observe(qa.iterations)

    # Server-computed audit numbers: rely on the tool-call trace, not the
    # LLM-supplied `row_evidence` (the model can hallucinate that value).
    # Count BOTH successful and failed SQL attempts so the admin audit row
    # is written on problematic turns too — closing the observability gap
    # called out in review (P2 codex thread).
    sql_attempts = 0
    sql_executions = 0
    server_row_total = 0
    for tc in qa.tool_calls:
        if tc.name not in ("run_select_ts", "run_select_static"):
            continue
        sql_attempts += 1
        if not tc.ok:
            continue
        sql_executions += 1
        result = tc.result if isinstance(tc.result, dict) else {}
        try:
            server_row_total += int(result.get("row_count", 0) or 0)
        except (TypeError, ValueError):
            pass

    await agent_runs_step(
        ts_pool,
        run_id,
        "sql_loop_complete",
        {
            "iterations": qa.iterations,
            "tool_call_count": len(qa.tool_calls),
            "sql_attempts": sql_attempts,
            "sql_executions": sql_executions,
            "server_row_total": server_row_total,
            "model_row_evidence": qa.row_evidence,
            "status": qa.status,
        },
    )

    # Mirror to admin audit feed for EVERY turn that ATTEMPTED at least one
    # SQL tool — observability gaps on failure-only runs were called out in
    # review (P2). The row records server-counted successful rows; failed
    # attempts still leave a trail via agent_runs.steps_json + the run_id
    # back-reference in the audit metadata.
    if sql_attempts > 0:
        functions_accessed = _sql_functions_accessed(qa.tool_calls)
        await write_agent_query_audit(
            ts_pool,
            auth,
            run_id,
            "sql_general",
            server_row_total,
            target_type=sql_audit_target_type(functions_accessed),
            functions_accessed=functions_accessed or None,
        )

    if qa.status == "success" and qa.text:
        reply = AgentReply.success(run_id=run_id, intent="sql_general", text=qa.text)
        await agent_runs_close(ts_pool, run_id, "success", reply)
        await _emit_answer_safe(sse, reply, run_id)
        return reply

    text = qa.text.strip() if qa.text else ""
    if not text:
        text = (
            "I wasn't able to compose a complete answer to that question. "
            "Try rephrasing, or break it into smaller questions."
        )
    # ``terminator_failed`` (runtime.py: emit_final_answer dispatch raised)
    # is routed to ``error`` — it's a genuine system fault, not a
    # not-found. ``no_terminator`` / ``max_iterations`` are the "model
    # didn't give us an answer in time" buckets that map to not_found.
    reply = AgentReply(
        run_id=run_id,
        status="not_found"
        if qa.status in ("no_terminator", "max_iterations", "success")
        else "error",
        text=text,
        intent="sql_general",
    )
    await agent_runs_close(ts_pool, run_id, reply.status, reply)
    await _emit_answer_safe(sse, reply, run_id)
    return reply
