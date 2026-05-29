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

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.api.agent import budget, document_render, document_store
from src.api.agent.audit import (
    agent_runs_close,
    agent_runs_open,
    agent_runs_step,
    classify_failure,
    sql_audit_target_type,
    write_agent_query_audit,
)
from src.api.agent.auth_context import ResolvedEntity, ResolvedTimeWindow, build_auth_context
from src.api.agent.document_prompts import (
    build_document_fill_system_prompt,
    format_document_fill_user_message,
    initial_draft,
)
from src.api.agent.document_tools import (
    DOCUMENT_FILL_TOOL_NAMES,
    build_document_fill_tool_registry,
)
from src.api.agent.feature_flag import is_agent_doc_fill_enabled
from src.api.agent.intents.consumption_by_user import (
    compile_consumption_by_user,
    summarize_consumption_rows,
)
from src.api.agent.intents.readiness import (
    DEPARTURES_SQL,
    LATEST_PLAN_SQL,
    PlanContext,
    ReadinessVerdict,
    SocReading,
    render_readiness_answer,
    resolve_readiness_window,
    summarize_readiness,
)
from src.api.agent.intents.savings import (
    render_savings_answer,
    resolve_savings_window,
)
from src.api.agent.llm_router import (
    configured_default_model,
    pick_model,
    resolve_org_two_model_enabled,
)
from src.api.agent.plan import QueryPlan
from src.api.agent.planner import classify as planner_classify
from src.api.agent.planner import fetch_org_sql_enabled, is_sql_mode_enabled
from src.api.agent.prompts import build_sql_agent_system_prompt, format_sql_agent_user_message
from src.api.agent.resolve import (
    load_depot_stations,
    load_depot_timezones,
    resolve_entities,
    resolve_time_window,
)
from src.api.agent.sql_tools import (
    EMIT_FINAL_ANSWER_TOOL,
    SQL_AGENT_TOOL_NAMES,
    build_sql_agent_tool_registry,
)
from src.api.agent.stream import SSEEventStream
from src.api.agent.view_context import AgentViewContext, build_page_context_payload
from src.api.agent_workflows.runtime import ToolNotAllowedError, run_qa_turn
from src.api.agent_workflows.tools import ToolNotRegisteredError
from src.api.savings import SavingsSummary, compute_savings_for_window
from src.db.queries import fetch_freshest_vehicle_socs
from src.monitoring.metrics import (
    AGENT_DOC_FILL_TURNS,
    AGENT_DOC_RENDERS,
    AGENT_RESOLVER_MISSES,
    AGENT_SQL_BUDGET_REFUSED,
    AGENT_SQL_TOOL_TURNS,
)
from src.security.data_freshness import MAX_TELEMETRY_AGE

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

    async def extract_plan(self, message: str, *, two_model_enabled: bool = False) -> QueryPlan:
        """Parse a user message into a strict :class:`QueryPlan`."""

    async def format_answer(
        self,
        plan: QueryPlan,
        resolved: list[dict[str, Any]],
        window: dict[str, Any],
        rows: list[dict[str, Any]],
        *,
        result_summary: Optional[dict[str, Any]] = None,
        two_model_enabled: bool = False,
    ) -> str:
        """Format a SQL result set into a natural-language reply."""


class RealLLMClient:
    """Default :class:`LLMClient` that calls the Anthropic SDK module."""

    async def extract_plan(self, message: str, *, two_model_enabled: bool = False) -> QueryPlan:
        from src.api.agent import llm  # local import to keep test envs llm-free

        return await llm.extract_plan(message, two_model_enabled=two_model_enabled)

    async def format_answer(
        self,
        plan: QueryPlan,
        resolved: list[dict[str, Any]],
        window: dict[str, Any],
        rows: list[dict[str, Any]],
        *,
        result_summary: Optional[dict[str, Any]] = None,
        two_model_enabled: bool = False,
    ) -> str:
        from src.api.agent import llm

        return await llm.format_answer(
            plan,
            resolved,
            window,
            rows,
            result_summary=result_summary,
            two_model_enabled=two_model_enabled,
        )


# ── Reply shape ────────────────────────────────────────────────────────────


class _CandidateOption(BaseModel):
    """One option in a disambiguation reply."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    display: str
    primary_id: Optional[str] = None


class AgentReply(BaseModel):
    """End-of-turn payload returned by both the JSON and SSE endpoints.

    Possible ``status`` values:

    - ``success``         — the answer is in ``text``. For a document-fill
                            finalize, ``download`` points to the finished file.
    - ``disambiguation``  — multiple matches; ``candidates`` lists them.
    - ``not_found``       — at least one subject did not resolve;
                            ``not_found`` carries the unresolved phrases.
    - ``error``           — the orchestrator failed; ``text`` is a generic
                            user-facing message (the cause is logged
                            server-side and never leaked here).
    - ``refused``         — a pre-LLM refusal; ``reason`` is a stable
                            machine-readable code (e.g.
                            ``monthly_budget_exceeded``) and ``text`` is the
                            user-facing message. No Anthropic tokens consumed.
    - ``needs_input``     — document-fill only: the agent asked the user to
                            clarify. ``questions`` carries the prompts,
                            ``session_id`` resumes the conversation, and
                            ``download`` (when present) is a preview. This is a
                            presentation status; the per-turn ``agent_runs`` row
                            still closes ``success`` (the turn succeeded), with
                            the awaiting-input lifecycle on the session row.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    status: str = Field(
        ..., description="success | disambiguation | not_found | error | refused | needs_input"
    )
    text: str
    intent: Optional[str] = None
    candidates: list[_CandidateOption] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)
    reason: Optional[str] = Field(
        default=None,
        description="Machine-readable code for status='refused' (e.g. monthly_budget_exceeded).",
    )
    # ── Document-fill fields (None/empty for every other path) ──────────────
    session_id: Optional[UUID] = Field(
        default=None, description="Document-fill session to resume (status needs_input/success)."
    )
    questions: list[dict[str, Any]] = Field(
        default_factory=list, description="Clarifying questions for status='needs_input'."
    )
    download: Optional[dict[str, Any]] = Field(
        default=None,
        description="Rendered document pointer {output_id, url, kind, fidelity, output_kind}.",
    )

    @classmethod
    def success(cls, *, run_id: UUID, intent: str, text: str) -> "AgentReply":
        return cls(run_id=run_id, status="success", text=text, intent=intent)

    @classmethod
    def document_ready(
        cls,
        *,
        run_id: UUID,
        session_id: UUID,
        text: str,
        download: Optional[dict[str, Any]],
    ) -> "AgentReply":
        """A finalized document-fill turn — the finished file is in ``download``."""
        return cls(
            run_id=run_id,
            status="success",
            text=text,
            intent="document_fill",
            session_id=session_id,
            download=download,
        )

    @classmethod
    def needs_input(
        cls,
        *,
        run_id: UUID,
        session_id: UUID,
        text: str,
        questions: list[dict[str, Any]],
        download: Optional[dict[str, Any]] = None,
    ) -> "AgentReply":
        """The fill agent paused to ask the user; conversation resumes via ``session_id``."""
        return cls(
            run_id=run_id,
            status="needs_input",
            text=text,
            intent="document_fill",
            session_id=session_id,
            questions=questions,
            download=download,
        )

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
    def refused(
        cls,
        *,
        run_id: UUID,
        reason: str,
        text: str,
        intent: Optional[str] = None,
    ) -> "AgentReply":
        """A pre-LLM refusal (e.g. the per-org monthly token budget, S4).

        ``reason`` is the stable machine-readable code the frontend keys off
        (``monthly_budget_exceeded``); ``text`` is the human-facing message.
        """
        return cls(run_id=run_id, status="refused", text=text, intent=intent, reason=reason)

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


# ── Step labels (user-facing SSE progress text) ────────────────────────────
#
# The SSE ``step`` event's ``summary`` is shown verbatim in the chat UI, so it
# must read as plain progress text — never a raw phase or tool name. The
# technical detail (full plan, resolved entities, per-tool ok/error) still
# lands in ``agent_runs.steps_json`` via ``agent_runs_step`` for debugging;
# this map only governs what the operator sees stream by.
#
# Keyed by BOTH consumption fast-path phase names and SQL-mode tool names
# (they never collide). A new SQL tool with no entry here surfaces the generic
# fallback — ``tests/unit/agent/test_step_labels.py`` guards against drift.
_STEP_LABELS: dict[str, str] = {
    # Consumption fast-path phases.
    "planner_decision": "Understanding your question",
    "extract_plan": "Working out what you're asking",
    "resolve_entities": "Finding who and what you mentioned",
    "compile": "Preparing the query",
    "execute": "Fetching the data",
    # Readiness + savings deterministic fast-path intents.
    "readiness": "Checking departure readiness",
    "savings": "Calculating your savings",
    # SQL-mode tools. The SSE event name stays "tool_call"; the tool name is
    # the label key.
    "list_tables": "Reviewing the available data",
    "describe_table": "Checking the data structure",
    "sample_values": "Looking at sample values",
    "run_select_ts": "Querying charging & telemetry data",
    "run_select_static": "Querying depot & vehicle records",
    "current_time": "Checking the current time",
    "lookup_entity": "Finding the matching record",
    "get_page_context": "Checking what you're looking at",
    "get_template_text": "Reading your document",
    "emit_final_answer": "Composing your answer",
}

_DEFAULT_STEP_LABEL = "Working on your request"


def _friendly_step_label(name: str) -> str:
    """Return user-facing progress text for an internal step/tool name."""
    return _STEP_LABELS.get(name, _DEFAULT_STEP_LABEL)


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


# ── Depot-wide consumption (no named subject) ──────────────────────────────


async def _depot_wide_consumption_rows(
    static_pool: Any,
    ts_pool: Any,
    auth: Any,
    plan: QueryPlan,
    *,
    depot_ids: Optional[list[UUID]] = None,
) -> tuple[list[dict[str, Any]], ResolvedTimeWindow, int]:
    """Aggregate consumption across the caller's chargers.

    The tenant boundary is the set of depots and their charger
    ``station_id``\\ s (resolved server-side via :func:`load_depot_stations`)
    — never an LLM-supplied value. By default this spans every depot the
    caller can see (a no-subject "depot-wide" total); pass ``depot_ids`` to
    scope to a named subset (a resolved ``depot`` subject — "consumption at
    depot X"). The subset is intersected with ``auth.visible_depot_ids`` so
    a depot id can never widen the caller's scope.

    Sessions are summed one query per depot **timezone group**, a bounded
    loop that collapses to a single iteration for the common single-timezone
    org and keeps each ``DATE_TRUNC`` bucket anchored to the right local day.

    Returns the concatenated rows, a representative resolved window (the
    first timezone group's, used only for the formatter's period context),
    and the number of timezone groups for the audit trail.
    """
    visible = set(auth.visible_depot_ids)
    if depot_ids is None:
        scope_depot_ids = list(auth.visible_depot_ids)
    else:
        scope_depot_ids = [d for d in depot_ids if d in visible]

    depot_tzs = await load_depot_timezones(static_pool, scope_depot_ids)
    stations = await load_depot_stations(static_pool, scope_depot_ids)

    # Charger station_ids per depot — the live-OCPP linkage. Imported rows
    # carry a synthetic station_id and are matched by site_id instead, so
    # the loop below is driven by depots (not stations): a depot with
    # imported history but zero registered chargers is still queried.
    depot_station_ids: dict[UUID, list[str]] = {}
    for station in stations:
        depot_station_ids.setdefault(station["depot_id"], []).append(station["ocpp_id"])

    # Group depots by timezone. Depots with no timezone in ``sites`` fall
    # back to UTC.
    tz_depots: dict[str, set[UUID]] = {}
    for depot_id in scope_depot_ids:
        tz = depot_tzs.get(depot_id) or "UTC"
        tz_depots.setdefault(tz, set()).add(depot_id)

    rows_list: list[dict[str, Any]] = []
    window: Optional[ResolvedTimeWindow] = None
    # Iterate timezones in a stable order so the representative window below
    # (and the per-group params) don't depend on unordered DB rows.
    for tz in sorted(tz_depots):
        group_depot_ids = sorted(tz_depots[tz], key=str)
        ocpp_ids = sorted(ocpp for d in group_depot_ids for ocpp in depot_station_ids.get(d, []))
        depot_id = group_depot_ids[0]
        group_window = resolve_time_window(plan.time_window, [depot_id], {depot_id: tz})
        if window is None:
            window = group_window
        sql, params = compile_consumption_by_user(
            plan, [], group_window, station_ids=ocpp_ids, depot_ids=group_depot_ids
        )
        rows = await ts_pool.fetch(sql, *params)
        rows_list.extend(dict(r) for r in rows)

    if window is None:
        # Only reached when there are no in-scope depots at all (org has
        # none, or a resolved depot fell outside the visible set). Resolve a
        # best-effort window so the formatter can still report "no sessions"
        # against the period the user actually asked about.
        fallback_depot = UUID("00000000-0000-0000-0000-000000000000")
        window = resolve_time_window(plan.time_window, [fallback_depot], {fallback_depot: "UTC"})

    return rows_list, window, len(tz_depots)


# ── Deterministic-intent shared helpers ────────────────────────────────────


async def _resolve_scoped_depots(message: str, auth: Any, static_pool: Any) -> list[UUID]:
    """Scope a deterministic-intent turn to a depot named in the message.

    The planner routes readiness/savings before entity resolution, so a
    "…at Vilnius" / "Is Vilnius ready?" question would otherwise span every
    visible depot. LLM-free: when the caller sees more than one depot and one
    or more of their names appears as a whole word/phrase (case-insensitive)
    in the message, scope to those; otherwise return all visible depots.
    """
    visible = list(auth.visible_depot_ids)
    if len(visible) <= 1:
        return visible
    rows = await static_pool.fetch("SELECT id, name FROM sites WHERE id = ANY($1::uuid[])", visible)
    text = (message or "").lower()
    matched = [
        UUID(str(row["id"]))
        for row in rows
        if row["name"] and re.search(r"\b" + re.escape(row["name"].lower()) + r"\b", text)
    ]
    return matched or visible


# ── Readiness fast-path intent ─────────────────────────────────────────────


async def _fetch_readiness_socs(
    ts_pool: Any,
    vehicle_ids: list[UUID],
    recency_floor: datetime,
) -> dict[str, SocReading]:
    """Freshest SoC per vehicle, keyed for the readiness verdict.

    Thin adapter over the shared
    :func:`src.db.queries.fetch_freshest_vehicle_socs` (the same merge the
    optimizer's ``StateAssembler`` uses), mapping its rows to
    ``{vehicle_id: SocReading}``.
    """
    rows = await fetch_freshest_vehicle_socs(ts_pool, vehicle_ids, recency_floor=recency_floor)
    socs: dict[str, SocReading] = {}
    for row in rows:
        if row["soc"] is None:
            continue
        socs[str(row["vehicle_id"])] = SocReading(soc=float(row["soc"]), time=row["time"])
    return socs


async def _finish_intent_success(
    ts_pool: Any,
    sse: Optional[SSEEventStream],
    run_id: UUID,
    *,
    auth: Any,
    intent: str,
    text: str,
    row_count: int,
    target_type: str,
) -> AgentReply:
    """Shared success bookend for the deterministic fast-path intents.

    Mirrors a query into the admin audit feed, builds the success reply,
    closes the ``agent_runs`` row, and emits the final SSE answer. Each
    deterministic intent handler ends with this so the
    audit/close/emit plumbing lives in one place (the consumption and
    sql_general paths have their own richer flows and don't use it).
    """
    await write_agent_query_audit(ts_pool, auth, run_id, intent, row_count, target_type=target_type)
    reply = AgentReply.success(run_id=run_id, intent=intent, text=text)
    await agent_runs_close(ts_pool, run_id, "success", reply)
    await _emit_answer_safe(sse, reply, run_id)
    return reply


async def _run_readiness_turn(
    *,
    run_id: UUID,
    auth: Any,
    static_pool: Any,
    ts_pool: Any,
    sse: Optional[SSEEventStream],
    emit_step: Any,
    message: str = "",
    now: Optional[datetime] = None,
) -> AgentReply:
    """Answer "are we ready to depart?" deterministically (no LLM call).

    Three set-based queries — upcoming departures (static), freshest SoC per
    departing vehicle (TS), latest plan per depot (TS) — reduced to a depot
    verdict by :func:`summarize_readiness`, which aligns each vehicle's plan
    SoC to its departure instant. Scope honours a named depot in the message
    (``_resolve_scoped_depots``) and the look-ahead honours a day phrase
    (``resolve_readiness_window``: "tomorrow" / "today" / default next-24h);
    both fall back to all visible depots / next-24h when absent.
    """
    as_of = now if now is not None else datetime.now(timezone.utc)
    await emit_step("readiness")

    depot_ids = await _resolve_scoped_depots(message, auth, static_pool)
    if not depot_ids:
        reply = AgentReply(
            run_id=run_id,
            status="not_found",
            text="I couldn't find any depots in your account to check departure readiness for.",
            intent="readiness",
        )
        await agent_runs_close(ts_pool, run_id, "not_found", reply)
        await _emit_answer_safe(sse, reply, run_id)
        return reply

    # A day phrase ("tomorrow"/"today") is anchored in the depot's timezone only
    # when exactly one depot is in scope; otherwise the window stays the
    # tz-agnostic next-24h.
    tz_name = None
    if len(depot_ids) == 1:
        tz_name = (await load_depot_timezones(static_pool, depot_ids)).get(depot_ids[0])
    window_start, window_end, window_label = resolve_readiness_window(message, as_of, tz_name)

    rows = await static_pool.fetch(DEPARTURES_SQL, depot_ids, window_start, window_end)
    departures = [dict(r) for r in rows]
    await agent_runs_step(
        ts_pool,
        run_id,
        "readiness_departures",
        {"count": len(departures), "depots": len(depot_ids), "window": window_label},
    )

    if not departures:
        verdict = ReadinessVerdict(
            window_label=window_label, total=0, ready=0, at_risk=0, unknown=0, vehicles=[]
        )
    else:
        vehicle_ids = sorted({UUID(d["vehicle_id"]) for d in departures})
        depot_uuids = sorted({UUID(d["depot_id"]) for d in departures})
        # Scan-floor for the SoC merge: 24h before the earliest thing we care
        # about (now, or the window start if it is in the past).
        soc_floor = min(as_of, window_start) - timedelta(hours=24)
        socs, plan_rows = await asyncio.gather(
            _fetch_readiness_socs(ts_pool, vehicle_ids, soc_floor),
            ts_pool.fetch(LATEST_PLAN_SQL, depot_uuids),
        )
        plans_by_depot = {
            str(r["depot_id"]): PlanContext(
                schedule_json=r["schedule_json"],
                horizon_start=r["horizon_start"],
                horizon_end=r["horizon_end"],
            )
            for r in plan_rows
        }
        verdict = summarize_readiness(
            departures,
            socs,
            plans_by_depot,
            now=as_of,
            max_age=MAX_TELEMETRY_AGE,
            window_label=window_label,
        )

    await agent_runs_step(
        ts_pool,
        run_id,
        "readiness_verdict",
        {
            "total": verdict.total,
            "ready": verdict.ready,
            "at_risk": verdict.at_risk,
            "unknown": verdict.unknown,
            "overall": verdict.overall,
        },
    )
    text = render_readiness_answer(verdict)
    return await _finish_intent_success(
        ts_pool,
        sse,
        run_id,
        auth=auth,
        intent="readiness",
        text=text,
        row_count=verdict.total,
        target_type="schedules",
    )


# ── Savings fast-path intent ───────────────────────────────────────────────


async def _run_savings_turn(
    *,
    run_id: UUID,
    message: str,
    auth: Any,
    static_pool: Any,
    ts_pool: Any,
    sse: Optional[SSEEventStream],
    emit_step: Any,
    now: Optional[datetime] = None,
) -> AgentReply:
    """Answer "how much did we save (overnight / this month / …)?" — no LLM.

    Scope honours a depot named in the message (``_resolve_scoped_depots``),
    else all visible depots. Resolves the window from the message + each
    depot's timezone (:func:`resolve_savings_window`), reuses
    :func:`src.api.savings.compute_savings_for_window` per depot, and
    aggregates the euro figures. Each depot computes its own window in its own
    timezone, so a multi-timezone org's "overnight" is correct per site.
    """
    as_of = now if now is not None else datetime.now(timezone.utc)
    await emit_step("savings")

    depot_ids = await _resolve_scoped_depots(message, auth, static_pool)
    if not depot_ids:
        reply = AgentReply(
            run_id=run_id,
            status="not_found",
            text="I couldn't find any depots in your account to calculate savings for.",
            intent="savings",
        )
        await agent_runs_close(ts_pool, run_id, "not_found", reply)
        await _emit_answer_safe(sse, reply, run_id)
        return reply

    depot_tzs = await load_depot_timezones(static_pool, depot_ids)
    # Window is resolved per depot (each in its own tz); the label + kind are
    # constant across depots (they depend only on the message), so we read them
    # off any resolved window rather than re-classifying the message.
    windows = {d: resolve_savings_window(message, as_of, depot_tzs.get(d)) for d in depot_ids}
    first_window = next(iter(windows.values()))
    label = first_window.label
    kind = first_window.kind

    # Compute every depot concurrently — the calls are independent (3 DB hits
    # each) and `load_depot_timezones` already ran, so there's no shared state.
    results = await asyncio.gather(
        *(
            compute_savings_for_window(
                static_pool,
                ts_pool,
                str(d),
                period_start=w.period_start,
                period_end=w.period_end,
                now=as_of,
            )
            for d, w in windows.items()
        ),
        return_exceptions=True,
    )
    summaries = [r for r in results if isinstance(r, SavingsSummary)]
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures and not summaries:
        # Every depot computation failed — surface the failure (run_turn closes
        # the run as 'error') rather than returning a misleading "no charging".
        raise failures[0]
    if failures:
        logger.warning(
            "savings: %d/%d depot computations failed; reporting partial aggregate",
            len(failures),
            len(results),
        )

    # Aggregate ONLY depots with a priceable baseline. A depot with no
    # bidding zone / no prices (baseline_known=False) is excluded from BOTH
    # sides of the comparison so its unpriced spend can't dilute the saving;
    # this also means a genuine baseline that sums to 0 (negative prices
    # cancelling) is still reported as a known baseline, not "no price data".
    priced = [s for s in summaries if s.baseline_known]
    if priced:
        total_actual = sum(s.actual_eur for s in priced)
        total_baseline = sum(s.baseline_eur for s in priced)
        baseline_known = True
        depot_count = len(priced)
    else:
        # Nothing priceable — report total spend across the depots that
        # computed, with no baseline (render says "can't estimate").
        total_actual = sum(s.actual_eur for s in summaries)
        total_baseline = 0.0
        baseline_known = False
        depot_count = len(summaries)

    actual = round(total_actual, 2)
    baseline = round(total_baseline, 2)
    saved = round(baseline - actual, 2)
    saved_pct = round((saved / abs(baseline)) * 100.0, 1) if baseline != 0 else 0.0

    await agent_runs_step(
        ts_pool,
        run_id,
        "savings_window",
        {
            "kind": kind,
            "label": label,
            "depots": depot_count,
            "failed_depots": len(failures),
            "baseline_known": baseline_known,
            "actual_eur": actual,
            "baseline_eur": baseline,
        },
    )
    text = render_savings_answer(
        actual_eur=actual,
        baseline_eur=baseline,
        saved_eur=saved,
        saved_pct=saved_pct,
        label=label,
        depot_count=depot_count,
        baseline_known=baseline_known,
    )
    return await _finish_intent_success(
        ts_pool,
        sse,
        run_id,
        auth=auth,
        intent="savings",
        text=text,
        row_count=depot_count,
        target_type="charging_sessions",
    )


# Deterministic fast-path intents share one handler signature; the orchestrator
# dispatches by route through this registry so adding intent #3 is a handler +
# one entry here (no new branch). The consumption fast path and sql_general have
# their own richer flows and are dispatched explicitly in run_turn.
_DETERMINISTIC_INTENT_HANDLERS: dict[str, Callable[..., Awaitable[AgentReply]]] = {
    "savings": _run_savings_turn,
    "readiness": _run_readiness_turn,
}


# ── Orchestrator ───────────────────────────────────────────────────────────


async def run_turn(
    message: str,
    token_payload: dict,
    static_pool: Any,
    ts_pool: Any,
    llm_client: LLMClient,
    *,
    sse: Optional[SSEEventStream] = None,
    context: Optional[AgentViewContext] = None,
    session_id: Optional[UUID] = None,
    now: Optional[datetime] = None,
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
        context: Optional UI state (page, selected depot, filters, focused
            item). Only consulted on the SQL-mode path, where it is parked for
            the ``get_page_context`` tool. Ignored by the consumption fast path
            (a one-shot extraction with no tool loop).
        session_id: Optional document-fill session id. When present and the
            feature flag is on, the turn routes straight to the collaborative
            document-fill loop (``_run_document_fill_turn``), bypassing the
            planner — the document is the subject, not the message text.
        now: Optional clock override for deterministic fast-path intents
            (readiness, savings). Defaults to UTC "now" when omitted.

    Returns:
        An :class:`AgentReply`. Raises only on unrecoverable failures
        — the caller is expected to translate to ``502`` and let the
        ``agent_runs.status='error'`` row carry the trail.
    """
    auth = await build_auth_context(token_payload, static_pool)
    run_id = await agent_runs_open(ts_pool, auth, message)

    async def _emit_step(name: str, *, label_key: str | None = None) -> None:
        # ``name`` is the SSE event's step name (a phase, or "tool_call");
        # ``label_key`` overrides which entry drives the user-facing summary
        # so SQL-mode tool calls (all emitted under name="tool_call") still
        # get a per-tool label keyed by the actual tool name.
        if sse is not None:
            summary = _friendly_step_label(label_key or name)
            await sse.emit("step", {"name": name, "summary": summary})

    try:
        # 0a. Document-fill: a session_id (+ feature on) routes straight to the
        # collaborative fill loop, before any planner classification — here the
        # uploaded document is the subject, not the message text.
        if session_id is not None and is_agent_doc_fill_enabled():
            await agent_runs_step(
                ts_pool,
                run_id,
                "planner_decision",
                {"route": "document_fill", "reason": "document_session"},
            )
            await _emit_step("planner_decision")
            return await _run_document_fill_turn(
                run_id=run_id,
                message=message,
                auth=auth,
                static_pool=static_pool,
                ts_pool=ts_pool,
                session_id=session_id,
                sse=sse,
                emit_step=_emit_step,
            )

        # 0. Planner — pick consumption fast path, sql_general, or refuse.
        #
        # Two-phase approach: run a cheap text-only pre-check first.  Only
        # messages that fall through to the "fallback" branch (i.e. non-
        # consumption, non-empty) could ever route to sql_general, so we
        # defer the static-DB org lookup until we know it's actually needed.
        # fetch_org_sql_enabled returns True for None org_id (admin/system
        # callers), so no separate role check is required here.
        if is_sql_mode_enabled():
            _pre = planner_classify(message, sql_mode_allowed=False)
            if _pre.reason == "consumption_fallback_no_sql_mode":
                sql_mode_allowed = await fetch_org_sql_enabled(static_pool, auth.organization_id)
                decision = planner_classify(message, sql_mode_allowed=sql_mode_allowed)
            else:
                decision = _pre
        else:
            decision = planner_classify(message, sql_mode_allowed=False)
        await agent_runs_step(
            ts_pool,
            run_id,
            "planner_decision",
            {"route": decision.route, "reason": decision.reason},
        )
        await _emit_step("planner_decision")

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

        deterministic_handler = _DETERMINISTIC_INTENT_HANDLERS.get(decision.route)
        if deterministic_handler is not None:
            return await deterministic_handler(
                run_id=run_id,
                message=message,
                auth=auth,
                static_pool=static_pool,
                ts_pool=ts_pool,
                sse=sse,
                emit_step=_emit_step,
                now=now,
            )

        if decision.route == "sql_general":
            return await _run_sql_general_turn(
                run_id=run_id,
                message=message,
                auth=auth,
                static_pool=static_pool,
                ts_pool=ts_pool,
                sse=sse,
                emit_step=_emit_step,
                context=context,
            )

        # 0c. Resolve the per-org two-model split (PLAN.md §S5b, build-only
        # spike). Fail-safe to off. We record the routing so the choice is
        # auditable in agent_runs.steps_json (the spike's verification anchor);
        # llm.py applies the same pick_model() at its own two call sites.
        # configured_default_model() reads AGENT_LLM_MODEL llm-free, so a turn
        # with an injected fake/custom llm_client never imports the
        # Anthropic-backed llm module on this path.
        two_model_enabled = await resolve_org_two_model_enabled(static_pool, auth.organization_id)
        default_model = configured_default_model()
        explore_model = pick_model(
            "explore", two_model_enabled=two_model_enabled, default_model=default_model
        )
        format_model = pick_model(
            "format", two_model_enabled=two_model_enabled, default_model=default_model
        )
        await agent_runs_step(
            ts_pool,
            run_id,
            "model_route",
            {
                "two_model_enabled": two_model_enabled,
                "explore_model": explore_model,
                "format_model": format_model,
            },
        )

        # 1. Extract the plan.
        plan = await llm_client.extract_plan(message, two_model_enabled=two_model_enabled)
        await agent_runs_step(ts_pool, run_id, "extract_plan", plan.model_dump())
        await _emit_step("extract_plan")

        # 1a. Branch on subject presence. An empty ``subjects`` list has
        # two meanings, disambiguated by ``depot_wide`` (see the QueryPlan
        # docstring + the extraction prompt):
        #   - subjects present          → resolve + subject aggregation.
        #   - subjects empty, depot_wide → in-scope depot-wide total.
        #   - subjects empty, otherwise  → out-of-scope refusal (mirrors the
        #     planner ``refuse`` branch; the compiler raises on an empty
        #     subject set by contract, so we never let it fall through).
        if not plan.subjects and not plan.depot_wide:
            reply = AgentReply(
                run_id=run_id,
                status="not_found",
                text=(
                    "I can only answer questions about depot charging and "
                    "consumption for a specific driver, vehicle, depot, or card. "
                    "I couldn't find anything like that to look up in your "
                    "request — try naming a driver or vehicle, or rephrasing."
                ),
                intent=plan.intent,
            )
            await agent_runs_close(ts_pool, run_id, "not_found", reply)
            await _emit_answer_safe(sse, reply, run_id)
            return reply

        resolved: list[ResolvedEntity]
        window: ResolvedTimeWindow

        if plan.subjects:
            # 2. Resolve entity mentions to UUIDs.
            resolved = await resolve_entities(plan.subjects, auth, static_pool)
            await agent_runs_step(
                ts_pool,
                run_id,
                "resolve_entities",
                [e.model_dump() for e in resolved],
            )
            await _emit_step("resolve_entities")

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

            # 3c. Depot-only subjects → depot-scoped total. A depot names a
            # *scope*, not a row filter, and the consumption compiler only
            # filters by driver/card/vehicle (it raises on a depot subject).
            # So reuse the depot-wide aggregation (site_id / station_id)
            # restricted to the resolved depots. The resolver already scoped
            # them to the caller's visible depots; the helper intersects
            # again as defence-in-depth.
            depot_subjects = [e for e in resolved if e.kind == "depot"]
            other_subjects = [e for e in resolved if e.kind in ("driver", "rfid", "vehicle")]

            if depot_subjects and not other_subjects:
                scoped_depot_ids = [e.primary_id for e in depot_subjects if e.primary_id]
                await _emit_step("compile")
                rows_list, window, tz_groups = await _depot_wide_consumption_rows(
                    static_pool, ts_pool, auth, plan, depot_ids=scoped_depot_ids
                )
                await agent_runs_step(
                    ts_pool,
                    run_id,
                    "compile",
                    {
                        "intent": plan.intent,
                        "mode": "depot_scoped",
                        "depot_count": len(scoped_depot_ids),
                        "tz_groups": tz_groups,
                    },
                )
                await agent_runs_step(ts_pool, run_id, "execute", {"row_count": len(rows_list)})
                await _emit_step("execute")
            else:
                # 4. Resolve time window; a named depot scopes subject queries.
                visible = set(auth.visible_depot_ids)
                scoped_depot_ids = [
                    e.primary_id
                    for e in depot_subjects
                    if e.primary_id is not None and e.primary_id in visible
                ]
                if scoped_depot_ids:
                    depot_tzs = await load_depot_timezones(static_pool, scoped_depot_ids)
                    window = resolve_time_window(plan.time_window, scoped_depot_ids, depot_tzs)
                else:
                    depot_tzs = await load_depot_timezones(static_pool, auth.visible_depot_ids)
                    window = resolve_time_window(
                        plan.time_window, auth.visible_depot_ids, depot_tzs
                    )

                # 5. Compile + execute SQL (depot entities are scope, not filters).
                compile_resolved = [e for e in resolved if e.kind != "depot"]
                compile_kwargs: dict[str, Any] = {}
                if scoped_depot_ids:
                    stations = await load_depot_stations(static_pool, scoped_depot_ids)
                    compile_kwargs = {
                        "depot_ids": scoped_depot_ids,
                        "station_ids": sorted(s["ocpp_id"] for s in stations),
                    }
                sql, params = compile_consumption_by_user(
                    plan, compile_resolved, window, **compile_kwargs
                )
                await agent_runs_step(
                    ts_pool,
                    run_id,
                    "compile",
                    {
                        "intent": plan.intent,
                        "mode": "subject",
                        "param_shapes": _describe_params(params),
                    },
                )
                await _emit_step("compile")

                rows = await ts_pool.fetch(sql, *params)
                rows_list = [dict(r) for r in rows]
                await agent_runs_step(ts_pool, run_id, "execute", {"row_count": len(rows_list)})
                await _emit_step("execute")
        else:
            # Depot-wide total: no subjects to resolve. Scope to the visible
            # depots' chargers and sum, one query per timezone group.
            resolved = []
            await _emit_step("compile")
            rows_list, window, tz_groups = await _depot_wide_consumption_rows(
                static_pool, ts_pool, auth, plan
            )
            await agent_runs_step(
                ts_pool,
                run_id,
                "compile",
                {"intent": plan.intent, "mode": "depot_wide", "tz_groups": tz_groups},
            )
            await agent_runs_step(ts_pool, run_id, "execute", {"row_count": len(rows_list)})
            await _emit_step("execute")

        # 6. Mirror the executed query into the admin audit feed.
        await write_agent_query_audit(
            ts_pool,
            auth,
            run_id,
            plan.intent,
            len(rows_list),
        )

        # 7. Summarize (three-state honesty) + format + close.
        summary = summarize_consumption_rows(rows_list)
        text = await llm_client.format_answer(
            plan,
            [e.model_dump(mode="json") for e in resolved],
            window.model_dump(mode="json"),
            rows_list,
            result_summary=summary,
            two_model_enabled=two_model_enabled,
        )
        reply = AgentReply.success(run_id=run_id, intent=plan.intent, text=text)
        await agent_runs_close(ts_pool, run_id, "success", reply)
        await _emit_answer_safe(sse, reply, run_id)
        return reply

    except Exception as exc:
        # Stamp the row with status='error' on a best-effort basis so the
        # audit trail is preserved, then re-raise so the route handler can
        # convert this into a sanitized 502. The exception text is logged
        # server-side only — never returned in the HTTP body.
        logger.exception("Agent turn failed (run_id=%s)", run_id)
        try:
            error_reply = AgentReply.error(run_id=run_id)
            await agent_runs_close(
                ts_pool, run_id, "error", error_reply, failure_reason=classify_failure(exc)
            )
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
    context: Optional[AgentViewContext] = None,
) -> AgentReply:
    """Drive the text-to-SQL agent loop via WorkflowAgent.run_qa_turn.

    The Anthropic client + model selection are reused from
    ``src.api.agent.llm`` so a single Anthropic singleton serves both
    paths. Per-tool-call step events flow into both ``agent_runs`` and
    the SSE stream via the ``on_step`` callback.

    ``context`` (optional UI state) is sanitized against ``auth`` and parked
    in the tool registry so the agent can pull it via ``get_page_context``
    when a question is ambiguous. It is never injected into the prompt.
    """
    # S4 per-org monthly token budget — gate FIRST, before importing the llm
    # module or building any Anthropic client. A None reservation means the org
    # is over its monthly ceiling: refuse here, touching ZERO Anthropic code
    # (no llm import, no _get_client, no messages.create), and record a
    # first-class 'refused' / budget_exceeded outcome. system_prompt is reused
    # by run_qa_turn below when we proceed.
    system_prompt = build_sql_agent_system_prompt()
    est_tokens = budget.estimate_turn_tokens(system_prompt)
    reservation = await budget.check_and_reserve(auth.organization_id, est_tokens)
    if reservation is None:
        AGENT_SQL_BUDGET_REFUSED.labels(
            organization_id=str(auth.organization_id) if auth.organization_id else "none"
        ).inc()
        await agent_runs_step(ts_pool, run_id, "budget_refused", {"est_tokens": est_tokens})
        reply = AgentReply.refused(
            run_id=run_id,
            reason="monthly_budget_exceeded",
            text=(
                "This organisation has reached its monthly usage limit for the "
                "analytics assistant. The limit resets at the start of next month — "
                "contact your administrator if you need it raised."
            ),
            intent="sql_general",
        )
        await agent_runs_close(
            ts_pool, run_id, "refused", reply, failure_reason=classify_failure("refused")
        )
        await _emit_answer_safe(sse, reply, run_id)
        return reply

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
        await emit_step("tool_call", label_key=name)

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
        # Import + build the client + registry INSIDE the try so a failure here
        # (the llm module import eagerly loads the Anthropic SDK + validates
        # config; or _get_client/registry init) still reconciles the reservation
        # via the handlers below — otherwise counter.reserved would leak and
        # wrongly refuse future turns even though no tokens were spent. Importing
        # here (not at function top) also keeps the over-budget refusal above
        # free of any llm dependency.
        from src.api.agent import llm as agent_llm  # local: keeps test envs llm-free

        # Sanitize + park the UI context for the get_page_context tool. depot_id
        # is intersected with auth.visible_depot_ids inside the builder (it can
        # only narrow scope, never widen it); the payload is recorded as the
        # first SQL-mode step when the app actually sent something.
        page_context_payload = build_page_context_payload(context, auth)
        if context is not None:
            await agent_runs_step(ts_pool, run_id, "page_context", page_context_payload)

        client = agent_llm._get_client()
        config = agent_llm.CONFIG
        registry = build_sql_agent_tool_registry(
            static_pool, ts_pool, auth, page_context=page_context_payload
        )
        qa = await run_qa_turn(
            anthropic_client=client,
            model=config.model,
            system_prompt=system_prompt,
            user_message=format_sql_agent_user_message(message),
            tool_registry=registry,
            allowed_tools=SQL_AGENT_TOOL_NAMES,
            max_iterations=8,
            # 4096 (up from 2048) for adaptive-thinking headroom across the
            # tool-use loop; effort drives how deeply the model reasons on
            # complex queries (thinking-capable models only).
            max_tokens=4096,
            temperature=0.0,
            effort=config.effort,
            on_step=_on_step,
        )
    except asyncio.CancelledError as exc:
        # CancelledError is a BaseException, so it bypasses the `except`
        # handlers below: reconcile the reservation explicitly so a cancelled
        # turn (client disconnect / server shutdown / timeout) neither leaks
        # counter.reserved nor drops already-spent tokens. run_qa_turn attaches
        # the partial token totals to the exception (it catches BaseException),
        # so account real usage rather than zero — otherwise repeated
        # cancellations could incur model cost while bypassing the ceiling.
        budget.record_actual(
            reservation,
            getattr(exc, "input_tokens", 0),
            getattr(exc, "output_tokens", 0),
        )
        raise
    except (ToolNotRegisteredError, ToolNotAllowedError) as exc:
        # Both exception types carry ``iterations``: ToolNotAllowedError
        # via its ctor; ToolNotRegisteredError via the attribute
        # attached at the re-raise site in run_qa_turn. Without
        # observing here, every policy-violation turn would flat-line
        # the histogram at 0, biasing the distribution toward zero on
        # the runs we most need to monitor.
        AGENT_SQL_TOOL_TURNS.observe(_extract_iterations(exc))
        # Reconcile the budget reservation against tokens actually spent before
        # the abort (run_qa_turn attaches them to the exception); keeps a failed
        # turn from leaking its rough estimate into the org's running total.
        budget.record_actual(
            reservation,
            getattr(exc, "input_tokens", 0),
            getattr(exc, "output_tokens", 0),
        )
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
                run_id,
                type(exc).__name__,
            )
        reply = AgentReply.error(run_id=run_id)
        try:
            await agent_runs_close(
                ts_pool, run_id, "error", reply, failure_reason=classify_failure(exc)
            )
        except Exception:  # pragma: no cover - audit close is best-effort
            logger.exception("Failed to close agent_run %s in error state", run_id)
        await _emit_answer_safe(sse, reply, run_id)
        return reply
    except Exception as exc:
        AGENT_SQL_TOOL_TURNS.observe(_extract_iterations(exc))
        budget.record_actual(
            reservation,
            getattr(exc, "input_tokens", 0),
            getattr(exc, "output_tokens", 0),
        )
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
        # Reconcile the rough reservation against the real usage the loop spent
        # (summed across every round-trip; see QAResult.input/output_tokens).
        budget.record_actual(reservation, qa.input_tokens, qa.output_tokens)

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

    # Bugbot M-sev: wrap success-path audit writes in try/except (same
    # rationale as the error-path mirrors above). A transient DB failure
    # here must not skip agent_runs_close + SSE emit + return below.
    try:
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
    except Exception:  # noqa: BLE001 - best-effort success-path audit
        logger.exception(
            "Failed to record sql_loop_complete step for run %s; "
            "continuing with answer delivery.",
            run_id,
        )

    # Mirror to admin audit feed for EVERY turn that ATTEMPTED at least one
    # SQL tool — observability gaps on failure-only runs were called out in
    # review (P2). The row records server-counted successful rows; failed
    # attempts still leave a trail via agent_runs.steps_json + the run_id
    # back-reference in the audit metadata.
    if sql_attempts > 0:
        functions_accessed = _sql_functions_accessed(qa.tool_calls)
        try:
            await write_agent_query_audit(
                ts_pool,
                auth,
                run_id,
                "sql_general",
                server_row_total,
                target_type=sql_audit_target_type(functions_accessed),
                functions_accessed=functions_accessed or None,
            )
        except Exception:  # noqa: BLE001 - best-effort success-path audit
            logger.exception(
                "Failed to write SQL general audit for run %s; " "continuing with answer delivery.",
                run_id,
            )

    if qa.status == "success" and qa.text:
        reply = AgentReply.success(run_id=run_id, intent="sql_general", text=qa.text)
        # A successful turn normally classifies to None; the exception is an
        # answered-but-empty turn, which classify_failure flags as
        # 'empty_result' off QAResult.empty_result.
        await agent_runs_close(
            ts_pool, run_id, "success", reply, failure_reason=classify_failure(qa)
        )
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
        status=(
            "not_found" if qa.status in ("no_terminator", "max_iterations", "success") else "error"
        ),
        text=text,
        intent="sql_general",
    )
    # The user-facing reply.status collapses several qa.status values to
    # not_found; classify_failure reads the richer qa (status + tool-call
    # trace) so the recorded failure_reason keeps the true cause — e.g. a
    # validator rejection the model never recovered from.
    await agent_runs_close(
        ts_pool, run_id, reply.status, reply, failure_reason=classify_failure(qa)
    )
    await _emit_answer_safe(sse, reply, run_id)
    return reply


# ── Document-fill (collaborative DOCX/PDF) sub-orchestrator ────────────────

# Trim the per-session conversation log to the last N text turns when replaying
# it into the loop — bounds token growth across a long collaboration.
_DOC_FILL_MAX_LOG_TURNS = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _filled_filename(original: Optional[str], kind: str) -> str:
    """Derive a download filename for the rendered output."""
    ext = "docx" if kind == "docx" else "pdf"
    if original:
        base = original.rsplit(".", 1)[0] if "." in original else original
        return f"{base}_filled.{ext}"
    return f"filled.{ext}"


def _doc_fill_sql_audit_numbers(tool_calls: Any) -> tuple[int, int]:
    """(sql_attempts, server_row_total) over a doc-fill turn's run_select calls."""
    attempts = 0
    rows = 0
    for tc in tool_calls:
        if tc.name not in ("run_select_ts", "run_select_static"):
            continue
        attempts += 1
        if tc.ok and isinstance(tc.result, dict):
            try:
                rows += int(tc.result.get("row_count", 0) or 0)
            except (TypeError, ValueError):
                pass
    return attempts, rows


async def _run_document_fill_turn(
    *,
    run_id: UUID,
    message: str,
    auth: Any,
    static_pool: Any,
    ts_pool: Any,
    session_id: UUID,
    sse: Optional[SSEEventStream],
    emit_step: Any,
) -> AgentReply:
    """Drive one turn of the collaborative document-fill loop.

    Sibling of :func:`_run_sql_general_turn`: same Anthropic tool-use loop
    (``run_qa_turn``), same per-org token budget, same audit writers — but it
    (1) loads the uploaded document + the multi-turn session, (2) replays the
    compacted conversation history, (3) reads the document terminator back from
    ``qa.tool_calls`` for its structured ``field_values`` / ``replacements`` /
    ``mode`` / ``questions``, (4) enforces grounding by dropping unknown fields
    and unanchored replacements, (5) renders the filled document (preview on
    ``ask``, final on ``finalize``), and (6) persists the session. The per-turn
    ``agent_runs`` row always closes ``success``; the awaiting-input lifecycle
    lives on the session row (``reply.status='needs_input'`` is presentation).
    """
    is_admin = auth.role == "favonius_admin"

    async def _terminal(status: str, text: str, *, failure: Optional[str]) -> AgentReply:
        """Build, close (status), emit, count, and return a terminal reply."""
        reply = AgentReply(
            run_id=run_id,
            status=status,
            text=text,
            intent="document_fill",
            session_id=session_id,
        )
        await agent_runs_close(ts_pool, run_id, status, reply, failure_reason=failure)
        await _emit_answer_safe(sse, reply, run_id)
        AGENT_DOC_FILL_TURNS.labels(mode="none", status=reply.status).inc()
        return reply

    # 1. Load the session (owner-scoped) + its template blob.
    session = await document_store.load_session_for_user(
        ts_pool, session_id, auth.user_id, is_admin=is_admin
    )
    if session is None or session.get("status") == "abandoned":
        return await _terminal(
            "not_found",
            (
                "I couldn't find that document session — it may have expired or "
                "belong to someone else. Upload the document again to start over."
            ),
            failure=None,
        )

    template = await document_store.load_template(ts_pool, UUID(str(session["template_id"])))
    if template is None or template.get("raw_payload") is None:
        return await _terminal(
            "error",
            "The uploaded document is no longer available. Please upload it again.",
            failure="other",
        )

    # 2. Extract text + detected fields fresh from the stored blob (single
    # source of truth). Never raises into the loop — a parse failure ends the
    # turn gracefully.
    try:
        extracted = document_render.extract(bytes(template["raw_payload"]), template["kind"])
    except document_render.DocumentRenderError as exc:
        logger.warning("doc-fill extract failed (run=%s): %s", run_id, exc)
        return await _terminal(
            "error",
            "I couldn't read that document. It may be corrupt or password-protected.",
            failure="other",
        )

    template_text = extracted.text
    detected_fields = [{"name": f.name, "source": f.source} for f in extracted.fields]
    field_names = {f.name for f in extracted.fields}
    depot_id = UUID(str(session["depot_id"])) if session.get("depot_id") else None

    # 3. Per-org monthly token budget — gate before any Anthropic import.
    system_prompt = build_document_fill_system_prompt()
    est_tokens = budget.estimate_turn_tokens(system_prompt)
    reservation = await budget.check_and_reserve(auth.organization_id, est_tokens)
    if reservation is None:
        AGENT_SQL_BUDGET_REFUSED.labels(
            organization_id=str(auth.organization_id) if auth.organization_id else "none"
        ).inc()
        await agent_runs_step(ts_pool, run_id, "budget_refused", {"est_tokens": est_tokens})
        reply = AgentReply(
            run_id=run_id,
            status="refused",
            text=(
                "This organisation has reached its monthly usage limit for the "
                "assistant. The limit resets at the start of next month — contact "
                "your administrator if you need it raised."
            ),
            intent="document_fill",
            reason="monthly_budget_exceeded",
            session_id=session_id,
        )
        await agent_runs_close(
            ts_pool, run_id, "refused", reply, failure_reason=classify_failure("refused")
        )
        await _emit_answer_safe(sse, reply, run_id)
        AGENT_DOC_FILL_TURNS.labels(mode="none", status="refused").inc()
        return reply

    draft = session.get("draft") or initial_draft()
    message_log = list(session.get("message_log") or [])
    history = [
        {"role": m.get("role", "user"), "content": m.get("text", "")}
        for m in message_log
        if isinstance(m, dict) and m.get("text")
    ]

    async def _on_step(tool_call: Any) -> None:
        result = tool_call.result
        if isinstance(result, dict) and isinstance(result.get("rows"), list):
            rows = result["rows"]
            preview: Any = {
                **{k: v for k, v in result.items() if k != "rows"},
                "row_preview": rows[:3],
                "row_total": result.get("row_count", len(rows)),
            }
        elif tool_call.name == "get_template_text" and isinstance(result, dict):
            # Don't dump the full document text into every step trace.
            preview = {
                "kind": result.get("kind"),
                "pdf_form_type": result.get("pdf_form_type"),
                "field_count": len(result.get("fields") or []),
                "text_chars": len(result.get("text") or ""),
            }
        else:
            preview = result
        await agent_runs_step(
            ts_pool,
            run_id,
            "tool_call",
            {
                "tool": tool_call.name,
                "ok": tool_call.ok,
                "input": tool_call.arguments,
                "result_preview": preview,
                "error": tool_call.error,
            },
        )
        await emit_step("tool_call", label_key=tool_call.name)

    try:
        from src.api.agent import llm as agent_llm  # local: keep test envs llm-free

        client = agent_llm._get_client()
        config = agent_llm.CONFIG
        registry = build_document_fill_tool_registry(
            static_pool,
            ts_pool,
            auth,
            template_text=template_text,
            template_kind=template["kind"],
            template_pdf_form_type=template["pdf_form_type"],
            detected_fields=detected_fields,
        )
        qa = await run_qa_turn(
            anthropic_client=client,
            model=config.model,
            system_prompt=system_prompt,
            user_message=format_document_fill_user_message(message, draft),
            tool_registry=registry,
            allowed_tools=DOCUMENT_FILL_TOOL_NAMES,
            max_iterations=10,
            max_tokens=4096,
            temperature=0.0,
            effort=config.effort,
            on_step=_on_step,
            history=history,
        )
    except asyncio.CancelledError as exc:
        budget.record_actual(
            reservation, getattr(exc, "input_tokens", 0), getattr(exc, "output_tokens", 0)
        )
        raise
    except (ToolNotRegisteredError, ToolNotAllowedError) as exc:
        budget.record_actual(
            reservation, getattr(exc, "input_tokens", 0), getattr(exc, "output_tokens", 0)
        )
        logger.error("doc-fill tool error (run=%s): %s", run_id, exc)
        return await _terminal(
            "error",
            "Something went wrong filling your document. Please try again.",
            failure=classify_failure(exc),
        )
    except Exception as exc:
        budget.record_actual(
            reservation, getattr(exc, "input_tokens", 0), getattr(exc, "output_tokens", 0)
        )
        raise
    else:
        budget.record_actual(reservation, qa.input_tokens, qa.output_tokens)

    # Mirror data reads to the admin audit feed (parity with SQL mode).
    sql_attempts, server_row_total = _doc_fill_sql_audit_numbers(qa.tool_calls)
    if sql_attempts > 0:
        functions_accessed = _sql_functions_accessed(qa.tool_calls)
        try:
            await write_agent_query_audit(
                ts_pool,
                auth,
                run_id,
                "document_fill",
                server_row_total,
                depot_id=depot_id,
                target_type=sql_audit_target_type(functions_accessed),
                functions_accessed=functions_accessed or None,
            )
        except Exception:  # noqa: BLE001 - best-effort audit
            logger.exception("Failed to write doc-fill audit for run %s", run_id)

    # No usable terminator → the model didn't deliver a fill this turn.
    term = next(
        (tc for tc in reversed(qa.tool_calls) if tc.name == EMIT_FINAL_ANSWER_TOOL and tc.ok),
        None,
    )
    if term is None or qa.status != "success":
        text = (qa.text or "").strip() or (
            "I wasn't able to work on the document this time. "
            "Please try rephrasing what you'd like changed."
        )
        status = (
            "not_found" if qa.status in ("no_terminator", "max_iterations", "success") else "error"
        )
        return await _terminal(status, text, failure=classify_failure(qa))

    args = term.arguments if isinstance(term.arguments, dict) else {}
    mode = str(args.get("mode") or "finalize").strip().lower()
    if mode not in ("ask", "finalize"):
        mode = "finalize"
    raw_fields = args.get("field_values") if isinstance(args.get("field_values"), dict) else {}
    raw_reps = args.get("replacements") if isinstance(args.get("replacements"), list) else []
    questions = [q for q in (args.get("questions") or []) if isinstance(q, dict)]
    answer_text = str(args.get("text") or "")

    # Grounding enforcement: drop unknown field names and unanchored
    # replacements so no ungrounded edit is ever written to the document.
    field_values = {
        k: ("" if v is None else str(v)) for k, v in raw_fields.items() if k in field_names
    }
    dropped_fields = [k for k in raw_fields if k not in field_names]
    replacements: list[dict[str, Any]] = []
    dropped_anchors = 0
    for rep in raw_reps:
        if not isinstance(rep, dict):
            continue
        find = str(rep.get("find") or "")
        if find and find in template_text:
            replacements.append(
                {
                    "find": find,
                    "replace": str(rep.get("replace") or ""),
                    "label": str(rep.get("label") or ""),
                }
            )
        else:
            dropped_anchors += 1

    # Merge into the running draft (new values/anchors win).
    merged_fields = {**(draft.get("field_values") or {}), **field_values}
    rep_by_find: dict[str, dict[str, Any]] = {
        r["find"]: r
        for r in (draft.get("replacements") or [])
        if isinstance(r, dict) and r.get("find")
    }
    for r in replacements:
        rep_by_find[r["find"]] = r
    merged_reps = list(rep_by_find.values())
    new_draft = {
        "field_values": merged_fields,
        "replacements": merged_reps,
        "open_questions": questions if mode == "ask" else [],
        "notes": draft.get("notes", ""),
    }

    # Render: a preview each turn that has something to apply, always on finalize.
    download: Optional[dict[str, Any]] = None
    output_id: Optional[UUID] = None
    if merged_fields or merged_reps or mode == "finalize":
        try:
            rendered, fidelity = document_render.render(
                kind=template["kind"],
                pdf_form_type=template["pdf_form_type"],
                body=bytes(template["raw_payload"]),
                field_values=merged_fields,
                replacements=merged_reps,
                full_text=template_text,
            )
            out_name = _filled_filename(template.get("file_name"), template["kind"])
            output_id = await document_store.store_output(
                ts_pool,
                template_id=UUID(str(template["id"])),
                session_id=session_id,
                run_id=run_id,
                organization_id=auth.organization_id,
                depot_id=depot_id,
                kind=template["kind"],
                output_kind=("final" if mode == "finalize" else "preview"),
                fidelity=fidelity,
                file_name=out_name,
                file_size_bytes=len(rendered),
                content_sha256=hashlib.sha256(rendered).hexdigest(),
                raw_payload=rendered,
                field_values=merged_fields,
                replacements=merged_reps,
            )
            download = {
                "output_id": str(output_id),
                "url": f"/agent/documents/{output_id}/download",
                "kind": template["kind"],
                "fidelity": fidelity,
                "output_kind": "final" if mode == "finalize" else "preview",
            }
            AGENT_DOC_RENDERS.labels(kind=template["kind"], fidelity=fidelity, outcome="ok").inc()
        except document_render.DocumentRenderError as exc:
            logger.warning("doc-fill render failed (run=%s): %s", run_id, exc)
            AGENT_DOC_RENDERS.labels(kind=template["kind"], fidelity="none", outcome="error").inc()
            answer_text = (
                answer_text + "\n\n(Note: I couldn't render the document this time.)"
            ).strip()

    # Surface dropped (ungrounded) edits so the user can correct them.
    notes: list[str] = []
    if dropped_fields:
        notes.append(
            "Some requested fields aren't in the document and were skipped: "
            + ", ".join(sorted(dropped_fields)[:8])
            + "."
        )
    if dropped_anchors:
        notes.append(
            f"{dropped_anchors} proposed change(s) didn't match the document text "
            "and were skipped."
        )
    if notes:
        answer_text = (answer_text + "\n\n" + " ".join(notes)).strip()

    if mode == "ask":
        reply_text = answer_text or "I have a few questions before I finish."
    else:
        reply_text = answer_text or "I've updated the document — download it below."

    # Append this turn to the conversation log (trimmed).
    new_log = list(message_log)
    new_log.append({"role": "user", "text": message, "run_id": str(run_id), "ts": _now_iso()})
    new_log.append(
        {"role": "assistant", "text": reply_text, "run_id": str(run_id), "ts": _now_iso()}
    )
    new_log = new_log[-_DOC_FILL_MAX_LOG_TURNS:]

    latest_output = output_id or (
        UUID(str(session["latest_output_id"])) if session.get("latest_output_id") else None
    )
    session_status = "awaiting_input" if mode == "ask" else "finalized"
    await document_store.update_session(
        ts_pool,
        session_id,
        status=session_status,
        draft=new_draft,
        message_log=new_log,
        latest_output_id=latest_output,
    )

    if mode == "ask":
        reply = AgentReply.needs_input(
            run_id=run_id,
            session_id=session_id,
            text=reply_text,
            questions=questions,
            download=download,
        )
    else:
        reply = AgentReply.document_ready(
            run_id=run_id, session_id=session_id, text=reply_text, download=download
        )

    # The per-turn agent_runs row closes 'success' for both ask and finalize
    # (the turn succeeded); the awaiting-input lifecycle is on the session row.
    await agent_runs_close(ts_pool, run_id, "success", reply, failure_reason=classify_failure(qa))
    await _emit_answer_safe(sse, reply, run_id)
    AGENT_DOC_FILL_TURNS.labels(mode=mode, status=reply.status).inc()
    return reply
