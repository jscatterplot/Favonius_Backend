"""Per-turn audit writers for the depot chat agent.

The agent leaves two trails:

- ``agent_runs`` is a domain-specific run record (mirrors
  ``optimization_runs``): one row per turn with a JSONB step log,
  outcome status, and duration. Indexed for "show this user's last 50
  chats" UX. See ``migrations/025_agent_runs.sql``.
- ``audit_log`` is the existing admin audit feed. Every executed query
  also writes one ``action='agent.query'`` row here so agent reads are
  visible alongside the cross-org reads and credential rotations
  admins already monitor. See ``src/security/admin_audit.py``.

The writers in this module are intentionally tiny — every helper
turns into a single SQL statement so the per-turn cost is bounded and
each side effect is independently mockable in tests.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

from src.api.agent.auth_context import AuthContext
from src.api.agent.llm import LLMExtractionError
from src.api.agent.sql_executor import SqlExecutorError, SqlExecutorTimeoutError
from src.api.agent_workflows.runtime import ToolNotAllowedError
from src.api.agent_workflows.tools import ToolNotRegisteredError
from src.security.admin_audit import AdminAuditRow, write_admin_audit_row


async def agent_runs_open(ts_pool: Any, auth: AuthContext, message: str) -> UUID:
    """Open a new ``agent_runs`` row and return its ``run_id``.

    The placeholder ``status='running'`` matches the migration's CHECK
    constraint and signals that the row is mid-turn. The final status
    (``success`` / ``disambiguation`` / ``not_found`` / ``error``) is
    written by :func:`agent_runs_close`.

    Args:
        ts_pool: asyncpg pool for the time-series database where
            ``agent_runs`` lives.
        auth: The caller's :class:`AuthContext`. ``organization_id``
            is stored as-is (``NULL`` for favonius_admin).
        message: The raw user message that opened the turn.

    Returns:
        The newly inserted ``run_id``.
    """
    async with ts_pool.acquire() as conn:
        run_id = await conn.fetchval(
            """
            INSERT INTO agent_runs (
                user_id,
                organization_id,
                depot_id,
                user_message,
                status
            )
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4, 'running')
            RETURNING run_id
            """,
            str(auth.user_id),
            str(auth.organization_id) if auth.organization_id else None,
            None,  # depot_id may span multiple; populated by the close step if known
            message,
        )
    return UUID(str(run_id))


async def agent_runs_step(
    ts_pool: Any,
    run_id: UUID,
    name: str,
    payload: Any,
) -> None:
    """Append one step to a run's ``steps_json`` array.

    Each step is stored as ``{"name": ..., "payload": ...}``. The
    JSONB ``||`` operator concatenates arrays, so wrapping the entry
    in a one-element list extends the existing trace rather than
    replacing it.

    Args:
        ts_pool: asyncpg pool for the time-series database.
        run_id: The ``run_id`` returned by :func:`agent_runs_open`.
        name: Step name (e.g. ``extract_plan``, ``resolve_entities``,
            ``compile``, ``execute``).
        payload: Arbitrary JSON-serializable payload. Datetimes and
            UUIDs are coerced via ``json.dumps(default=str)``.
    """
    step_blob = json.dumps([{"name": name, "payload": payload}], default=str)
    async with ts_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_runs
            SET steps_json = steps_json || $1::jsonb
            WHERE run_id = $2::uuid
            """,
            step_blob,
            str(run_id),
        )


async def agent_runs_close(
    ts_pool: Any,
    run_id: UUID,
    status: str,
    reply: Any,
    *,
    failure_reason: Optional[str] = None,
) -> None:
    """Stamp final status, duration, intent, and failure reason onto a run.

    ``duration_ms`` is computed from the row's ``created_at`` so it
    reflects the wall-clock time of the turn end-to-end and is captured
    once (idempotent re-calls don't reset it). ``final_intent`` is
    pulled from ``reply.intent`` when present (dict or attribute) so
    callers can pass either an :class:`AgentReply` instance or a plain
    dict produced by the formatter.

    Args:
        ts_pool: asyncpg pool for the time-series database.
        run_id: The ``run_id`` returned by :func:`agent_runs_open`.
        status: One of ``success``, ``disambiguation``, ``not_found``,
            ``error``. Other values violate the migration's CHECK
            constraint.
        reply: The end-of-turn reply object (or dict). Inspected for
            an ``intent`` field; ignored otherwise.
        failure_reason: One of the seven S3.5 taxonomy values (migration
            045), or ``None`` for a success / graceful non-failure. Always
            obtained from :func:`classify_failure` — never a bare literal.
            COALESCE-preserved like ``duration_ms`` so a graceful re-close
            (``None``) cannot erase a previously recorded reason.
    """
    final_intent = _extract_intent(reply)
    async with ts_pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE agent_runs
            SET status         = $1,
                final_intent   = COALESCE($2, final_intent),
                failure_reason = COALESCE($3, failure_reason),
                duration_ms    = COALESCE(
                    duration_ms,
                    (EXTRACT(EPOCH FROM (NOW() - created_at)) * 1000)::int
                )
            WHERE run_id = $4::uuid
            """,
            status,
            final_intent,
            failure_reason,
            str(run_id),
        )


# ── Failure taxonomy (S3.5) ─────────────────────────────────────────────────
#
# classify_failure() is THE single source of truth for the seven
# ``failure_reason`` string literals. No other module may write these strings;
# every call site classifies via this function and passes the result to
# :func:`agent_runs_close`. The set is frozen to match the CHECK constraint in
# migrations/045_agent_failure_reason.sql — do not extend it without revisiting
# PLAN.md Open Question 6.
#
# These two sets are the only error_kinds the read-only executor emits
# (src/api/agent/sql_executor.py, surfaced by sql_tools._make_runner). Any
# OTHER error_kind a ``run_select_*`` call reports is, by construction, a
# validator rejection — the validator owns the long tail of rejection kinds
# (src/api/agent/sql_validator.py), so we infer it by exclusion rather than
# enumerating ~17 kinds that would drift.
_EXECUTOR_TIMEOUT_KIND = "timeout"
_EXECUTOR_TOOL_ERROR_KINDS = frozenset({"role_error", "plan_error", "exec_error"})

# SQL-executing tool names whose failure results carry an error_kind. Mirrors
# the names registered in src/api/agent/sql_tools.py (kept local to avoid a
# src→src import just for a 3-tuple; a shared constant is an easy followup).
_SQL_TOOL_NAMES = frozenset({"run_select_ts", "run_select_static", "sample_values"})

# Terminal statuses that are NOT failures — failure_reason stays NULL.
_GRACEFUL_STATUSES = frozenset({"success", "running", "disambiguation", "not_found"})

# S3.5 followups (failure modes that don't map cleanly to the seven categories
# are classified as 'other' or folded per the notes below; revisit per
# PLAN.md Open Question 6 after a month of data):
#  * ``no_terminator`` / ``max_iterations`` fold into ``llm_error`` (the model
#    exited the loop without delivering an answer). Arguably its own category
#    ("model_gave_up"); merged for now.
#  * ``empty_result`` is detected in SQL mode only (the spec's
#    ``emit_final_answer`` site). The consumption fast path's zero-row case is
#    not flagged yet.
#  * The "zero rows" rule sums ``row_count`` across ALL successful
#    ``run_select_*`` calls (see classify_failure docstring). A turn whose
#    resolve-query returns a row but whose main aggregation returns none is
#    therefore NOT flagged — conservative, no false positives. Revisit if
#    "last-query-empty" proves more useful.
#  * ``budget_exceeded`` has no concrete exception yet (S4). It is matched by
#    class name so it lights up the moment budget.py lands BudgetExceededError.
#  * DB / compile errors on the consumption fast path fall through to 'other'
#    (they are not executor-tool failures). Split out later only if noisy.


def classify_failure(exc_or_status: Any) -> Optional[str]:
    """Map a turn's failure signal to one of the seven ``failure_reason`` values.

    This is the ONLY place the failure_reason string literals appear. Returns
    ``None`` for a success or graceful non-failure (the column stays NULL).

    Accepts one of:
      * ``None`` → ``None``.
      * an ``Exception`` — classified by type.
      * a terminal QA-result-like object (duck-typed on ``.status``,
        ``.tool_calls``, ``.empty_result`` — i.e.
        :class:`~src.api.agent_workflows.runtime.QAResult`) — classified from
        the tool-call trace and terminal status.
      * a bare status / error_kind ``str`` — for callers holding only a token.

    Categories:
      * ``validator_rejected`` — a ``run_select_*`` call was rejected by the
        SQL validator (any executor error_kind that is not ``timeout`` /
        ``role_error`` / ``plan_error`` / ``exec_error``).
      * ``executor_timeout`` — :class:`SqlExecutorTimeoutError`, or error_kind
        ``timeout``: the read-only ``statement_timeout`` fired.
      * ``empty_result`` — the turn ANSWERED (``emit_final_answer`` succeeded)
        but every underlying query came back empty. **Zero rows** is defined
        precisely as: at least one ``run_select_ts`` / ``run_select_static``
        call executed successfully (``ok=True``) AND the SUM of the
        server-reported ``row_count`` across all such successful calls equals
        zero. A turn that ran no ``run_select_*`` at all (e.g. answered from
        ``current_time`` only) is NOT ``empty_result``. This signal is computed
        at the ``emit_final_answer`` site in ``run_qa_turn`` and surfaced as
        :attr:`QAResult.empty_result`; it takes precedence over a ``success``
        status (an answered-but-empty turn is flagged even though it succeeded).
      * ``budget_exceeded`` — a per-org token-budget refusal (S4). Matched by
        exception class name (``BudgetExceededError``) until S4 lands it.
      * ``tool_error`` — a tool failed or was misused: executor
        role/plan/exec errors, a disallowed / unregistered tool
        (:class:`ToolNotAllowedError` / :class:`ToolNotRegisteredError`), or
        the terminator dispatch raising (status ``terminator_failed``).
      * ``llm_error`` — the LLM layer failed: an Anthropic SDK error, or the
        model exiting the loop without an answer (``no_terminator`` /
        ``max_iterations``).
      * ``other`` — anything not attributable more precisely.
    """
    if exc_or_status is None:
        return None
    if isinstance(exc_or_status, BaseException):
        return _classify_exception(exc_or_status)
    if isinstance(exc_or_status, str):
        return _classify_status_token(exc_or_status)
    return _classify_qa_result(exc_or_status)


def _classify_exception(exc: BaseException) -> str:
    """Classify a raised exception. See :func:`classify_failure`."""
    # S4 hook: the budget exception isn't built yet (PLAN.md §S4). Match by
    # name so it classifies correctly the moment budget.py lands it, without
    # importing a class that doesn't exist.
    if type(exc).__name__ == "BudgetExceededError":
        return "budget_exceeded"
    # SqlExecutorTimeoutError subclasses SqlExecutorError — check it first.
    if isinstance(exc, SqlExecutorTimeoutError):
        return "executor_timeout"
    if isinstance(exc, (SqlExecutorError, ToolNotAllowedError, ToolNotRegisteredError)):
        return "tool_error"
    # The consumption path's extractor raises this when the model fails to emit
    # a valid QueryPlan (no tool call, or malformed twice) — an LLM-origin
    # failure, not an "other".
    if isinstance(exc, LLMExtractionError):
        return "llm_error"
    # Anthropic SDK errors (rate limit, 5xx, connection, malformed output).
    # Match by module so audit.py needn't import the SDK (the agent keeps it
    # an optional import for llm-free test envs).
    root_module = (type(exc).__module__ or "").split(".", 1)[0]
    if root_module == "anthropic":
        return "llm_error"
    return "other"


def _classify_status_token(token: str) -> Optional[str]:
    """Classify a bare status / error_kind token. See :func:`classify_failure`."""
    if token in _GRACEFUL_STATUSES:
        return None
    if token == "empty_result":
        return "empty_result"
    if token == "terminator_failed":
        return "tool_error"
    if token in ("no_terminator", "max_iterations"):
        return "llm_error"
    if token == _EXECUTOR_TIMEOUT_KIND:
        return "executor_timeout"
    if token in _EXECUTOR_TOOL_ERROR_KINDS:
        return "tool_error"
    # A bare, unrecognised token (including the generic ``error`` status)
    # carries no more-specific signal.
    return "other"


def _classify_qa_result(qa: Any) -> Optional[str]:
    """Classify a terminal QA-result-like object. See :func:`classify_failure`."""
    # empty_result takes precedence: it is recorded even when the turn
    # otherwise succeeded (status == "success").
    if getattr(qa, "empty_result", False):
        return "empty_result"
    status = getattr(qa, "status", None)
    if status == "success":
        return None
    # ``terminator_failed`` is itself a terminal tool-path failure (the model
    # asked to stop but its emit_final_answer was malformed). Per the taxonomy
    # contract it is tool_error, and it must NOT be shadowed by a stale earlier
    # SQL-tool error the model recovered from before stopping — so classify it
    # before consulting the per-call error_kind heuristic below.
    if status == "terminator_failed":
        return "tool_error"
    # Otherwise the dominant cause is the most recent failed SQL-tool error_kind
    # (e.g. a validator rejection the model never recovered from — the §S3.5
    # "deliberate validator rejection" done-when case), even when the terminal
    # status is the generic "model gave up" marker (max_iterations / no_terminator).
    kind = _last_failed_sql_tool_kind(getattr(qa, "tool_calls", None) or [])
    if kind is not None:
        if kind == _EXECUTOR_TIMEOUT_KIND:
            return "executor_timeout"
        if kind in _EXECUTOR_TOOL_ERROR_KINDS:
            return "tool_error"
        return "validator_rejected"
    # No tool-level error_kind — classify by terminal status.
    return _classify_status_token(status) if isinstance(status, str) else "other"


def _last_failed_sql_tool_kind(tool_calls: Any) -> Optional[str]:
    """Return the error_kind of the LAST failed SQL-executing tool call, if any.

    Covers run_select_ts / run_select_static AND sample_values — all three run
    LLM SQL through the validate→execute path and surface the same error_kind
    envelope, so a turn that dies after a failed sample_values is attributed to
    its tool/validator cause rather than falling through to the status token.
    """
    kind: Optional[str] = None
    for tc in tool_calls:
        if getattr(tc, "name", None) not in _SQL_TOOL_NAMES:
            continue
        if getattr(tc, "ok", True):
            continue
        result = getattr(tc, "result", None)
        if isinstance(result, dict) and result.get("error_kind"):
            kind = str(result["error_kind"])
    return kind


def sql_audit_target_type(functions_accessed: list[str]) -> str:
    """Map agent_views functions touched in a SQL-mode turn to ``target_type``.

    ``audit_log.target_type`` is VARCHAR(64); multi-table turns use a
    comma-separated label (truncated when needed) with the full list in
    metadata ``functions_accessed``.
    """
    if not functions_accessed:
        return "agent_views"
    if len(functions_accessed) == 1:
        return functions_accessed[0]
    joined = ",".join(functions_accessed)
    if len(joined) <= 64:
        return joined
    return joined[:61] + "..."


async def write_agent_query_audit(
    ts_pool: Any,
    auth: AuthContext,
    run_id: UUID,
    intent: str,
    row_count: int,
    depot_id: Optional[UUID] = None,
    *,
    target_type: str = "charging_sessions",
    functions_accessed: Optional[list[str]] = None,
) -> None:
    """Mirror an executed agent query into ``audit_log``.

    Thin wrapper around :func:`write_admin_audit_row` that fills in
    the agent-specific fields. The ``target_id`` is the agent's
    ``run_id`` (stringified) so a reviewer can pivot from the
    ``audit_log`` row back to the full step trace in ``agent_runs``.

    Args:
        ts_pool: asyncpg pool used by the underlying
            :func:`write_admin_audit_row` (passed through unchanged).
        auth: The caller's :class:`AuthContext`. ``user_id`` and
            ``role`` populate the actor fields; ``organization_id``
            populates the org column.
        run_id: The agent's per-turn run identifier. Stored as
            ``target_id`` so the audit row links back to ``agent_runs``.
        intent: The compiled intent name (e.g. ``consumption_by_user``).
        row_count: Number of rows the query returned.
        depot_id: Optional depot UUID when the query targets a single
            depot. ``None`` when the query may span multiple depots.
        target_type: Data surface for the admin audit feed (e.g.
            ``charging_sessions`` for the consumption fast path, or an
            ``agent_views`` function name for SQL mode).
        functions_accessed: Optional list of ``agent_views`` function
            names from SQL-mode tool calls; stored in metadata for
            multi-table turns.
    """
    metadata: dict[str, Any] = {"intent": intent, "row_count": row_count}
    if functions_accessed:
        metadata["functions_accessed"] = functions_accessed
    row = AdminAuditRow(
        action="agent.query",
        actor_user_id=str(auth.user_id),
        actor_role=auth.role,
        organization_id=str(auth.organization_id) if auth.organization_id else None,
        depot_id=str(depot_id) if depot_id else None,
        target_type=target_type,
        target_id=str(run_id),
        metadata=metadata,
    )
    await write_admin_audit_row(ts_pool, row)


def _extract_intent(reply: Any) -> Optional[str]:
    """Return ``reply.intent`` if present (dict or attribute access)."""
    if reply is None:
        return None
    if isinstance(reply, dict):
        value = reply.get("intent")
    else:
        value = getattr(reply, "intent", None)
    return str(value) if value is not None else None
