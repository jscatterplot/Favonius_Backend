"""Tool registry for the depot chat agent's SQL mode.

Eight tools (list_tables, describe_table, sample_values, run_select_static,
run_select_ts, current_time, lookup_entity, emit_final_answer) closed over
the caller's :class:`~src.api.agent.auth_context.AuthContext`. The LLM
never sees credentials — only the tool surface.

Mirrors the closure pattern in
``src/api/agent_workflows/readiness_tools.py:build_readiness_tool_registry``.
Connection acquire happens per tool call (P2: never hold a pool slot
across the 1-2s LLM round-trip).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg

from src.api.agent.auth_context import AuthContext
from src.api.agent.catalogue import (
    describe_function,
    list_functions_summary,
)
from src.api.agent.resolve import resolve_entities
from src.api.agent.plan import EntityMention
from src.api.agent.sql_executor import (
    SqlExecutorError,
    SqlExecutorPlanError,
    SqlExecutorRoleError,
    SqlExecutorTimeoutError,
    run_select,
)
from src.api.agent.sql_validator import (
    AGENT_VIEWS_FUNCTIONS_STATIC,
    AGENT_VIEWS_FUNCTIONS_TS,
    validate_sql,
)
from src.api.agent_workflows.runtime import EMIT_FINAL_ANSWER_TOOL as _RUNTIME_TERMINATOR
from src.api.agent_workflows.tools import ToolRegistry
from src.monitoring.metrics import AGENT_SQL_VALIDATIONS

logger = logging.getLogger(__name__)


# Terminator tool name — runtime closes the loop on this call. Aliased to
# the runtime's canonical constant (imported at module top) so a future
# rename in one place doesn't silently desync the registration vs the
# loop's terminator detection.
EMIT_FINAL_ANSWER_TOOL = _RUNTIME_TERMINATOR


def build_sql_agent_tool_registry(
    static_pool: asyncpg.Pool,
    ts_pool: asyncpg.Pool,
    auth: AuthContext,
) -> ToolRegistry:
    """Build the eight-tool registry the SQL-mode agent uses for one turn.

    All callables capture ``auth`` and the two pools at build time so the
    LLM-facing surface stays plain ``await fn(**input)``.
    """
    registry = ToolRegistry()

    # ── list_tables ─────────────────────────────────────────────────────
    async def _list_tables(**_: Any) -> list[dict]:
        return list_functions_summary()

    registry.register(
        "list_tables",
        description=(
            "Return every agent_views function (purpose + pool) the LLM can SELECT from. "
            "The complete catalogue is also in the system prompt — use this tool only "
            "if you need to re-confirm a name."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
        fn=_list_tables,
    )

    # ── describe_table ──────────────────────────────────────────────────
    async def _describe_table(*, table: str, **_: Any) -> dict | str:
        desc = describe_function(table)
        if desc is None:
            return f"Unknown table-function: {table!r}. Call list_tables to see allowed names."
        return desc

    registry.register(
        "describe_table",
        description="Return columns + types + business notes for one agent_views function.",
        input_schema={
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "Unqualified name (e.g. 'sessions') or qualified ('agent_views.sessions').",
                }
            },
            "required": ["table"],
        },
        fn=_describe_table,
    )

    # ── sample_values ───────────────────────────────────────────────────
    # Identifier regex used to scrub LLM-supplied table/column names. Plain
    # SQL identifiers only — alphanumerics + underscore, must start with a
    # letter, capped at 63 chars (Postgres's default NAMEDATALEN limit minus
    # the NUL). The validator catches malicious *values* in WHERE clauses
    # via sqlglot; here we are interpolating into the SELECT-list and the
    # FROM clause as raw text, so we need a stricter belt before reaching
    # the validator.
    _IDENT_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,62}$")

    async def _sample_values(*, table: str, column: str, n: int = 5, **_: Any) -> dict:
        """Return up to n DISTINCT values from a column. Built on top of
        the same validate→execute path so the safety properties hold.

        Implementation: synthesise ``SELECT DISTINCT <col> FROM
        agent_views.<table>($1) WHERE … LIMIT n``. The validator rejects
        if <table> isn't allowed; the executor enforces scope. ``column``
        and ``table`` are pre-validated as plain identifiers BEFORE reaching
        the SQL builder so a malicious value cannot escape the projection
        and reach the validator with a payload that already changed shape.
        """
        if n < 1 or n > 10:
            return {"error": "n must be in [1, 10]"}
        table_in = (table or "").strip().lower()
        if table_in.startswith("agent_views."):
            table_in = table_in.split(".", 1)[1]
        if "(" in table_in:
            table_in = table_in.split("(", 1)[0]
        if not _IDENT_RE.fullmatch(table_in):
            return {"error": f"table must be a plain identifier; got {table!r}"}
        if not _IDENT_RE.fullmatch(column or ""):
            return {"error": f"column must be a plain identifier; got {column!r}"}

        # Decide which pool by allowlist membership.
        if table_in in AGENT_VIEWS_FUNCTIONS_TS:
            pool, allowed, role, label = (
                ts_pool, AGENT_VIEWS_FUNCTIONS_TS, "agent_reader_ts", "ts",
            )
        elif table_in in AGENT_VIEWS_FUNCTIONS_STATIC:
            pool, allowed, role, label = (
                static_pool, AGENT_VIEWS_FUNCTIONS_STATIC, "agent_reader_static", "static",
            )
        else:
            return {"error": f"Unknown table {table!r}"}

        # Hypertable functions need a time predicate; default to last 30d.
        time_clause = ""
        from src.api.agent.sql_validator import HYPERTABLE_FUNCTIONS
        if table_in in HYPERTABLE_FUNCTIONS:
            time_clause = " WHERE hour >= now() - interval '30 days'"

        sql = (
            f"SELECT DISTINCT {column} FROM agent_views.{table_in}($1){time_clause} "
            f"LIMIT {n}"
        )
        v = validate_sql(sql, allowed_functions=allowed, row_limit=n)
        if not v.ok:
            AGENT_SQL_VALIDATIONS.labels(verdict=f"rejected:{v.error_kind}").inc()
            return {
                "error": f"validator rejected sample query: {v.error_kind}: {v.error}",
                "error_kind": v.error_kind,
            }
        AGENT_SQL_VALIDATIONS.labels(verdict="accepted").inc()
        try:
            r = await run_select(
                pool=pool,
                role=role,
                sql=v.sql,
                depot_ids=list(auth.visible_depot_ids),
                pool_label=label,
                row_cap=n,
            )
        except SqlExecutorRoleError as e:
            logger.error("agent role-swap failure pool=%s: %s", label, e)
            return {"error": "Internal authorisation error.", "error_kind": "role_error"}
        except SqlExecutorPlanError as e:
            return {"error": str(e), "error_kind": "plan_error"}
        except SqlExecutorTimeoutError as e:
            return {"error": str(e), "error_kind": "timeout"}
        except SqlExecutorError as e:
            return {"error": str(e), "error_kind": "exec_error"}
        return {
            "table": f"agent_views.{table_in}",
            "column": column,
            "values": [list(row.values())[0] if row else None for row in r.rows],
        }

    registry.register(
        "sample_values",
        description=(
            "Return up to N distinct values for one column on an agent_views table. "
            "Scoped to the caller's depots."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "table": {"type": "string"},
                "column": {"type": "string"},
                "n": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["table", "column"],
        },
        fn=_sample_values,
    )

    # ── run_select_static / run_select_ts ──────────────────────────────
    def _make_runner(pool: asyncpg.Pool, allowed, role: str, label: str):
        async def _runner(*, sql: str, **_: Any) -> dict:
            v = validate_sql(sql, allowed_functions=allowed)
            if not v.ok:
                AGENT_SQL_VALIDATIONS.labels(verdict=f"rejected:{v.error_kind}").inc()
                logger.info(
                    "agent sql validator rejected pool=%s kind=%s",
                    label, v.error_kind,
                )
                return {
                    "error": v.error,
                    "error_kind": v.error_kind,
                    "hint": (
                        "Re-emit the SQL fixing the issue above. "
                        "Common fixes: use agent_views.<fn>($1) instead of "
                        "public.<table>; add a time predicate on hour for "
                        "hypertable functions; remove DML/DDL; never write "
                        "a literal UUID for $1."
                    ),
                }
            AGENT_SQL_VALIDATIONS.labels(verdict="accepted").inc()

            try:
                r = await run_select(
                    pool=pool,
                    role=role,
                    sql=v.sql,
                    depot_ids=list(auth.visible_depot_ids),
                    pool_label=label,
                )
            except SqlExecutorRoleError as e:
                # Defence in depth signal — operator alert.
                logger.error("agent role-swap failure pool=%s: %s", label, e)
                return {"error": "Internal authorisation error.", "error_kind": "role_error"}
            except SqlExecutorPlanError as e:
                return {"error": str(e), "error_kind": "plan_error"}
            except SqlExecutorTimeoutError as e:
                return {"error": str(e), "error_kind": "timeout"}
            except SqlExecutorError as e:
                return {"error": str(e), "error_kind": "exec_error"}
            return {
                "rows": r.rows,
                "row_count": r.row_count,
                "truncated": r.truncated,
                "duration_ms": r.duration_ms,
                "functions_used": list(v.functions_used),
            }
        return _runner

    registry.register(
        "run_select_ts",
        description=(
            "Validate and execute a parameter-free SELECT against the TimescaleDB "
            "pool's agent_views.* functions. The server binds $1 to the caller's "
            "visible depots; you must reference it as $1 in every "
            "agent_views.<fn>($1) call. Returns up to 500 rows."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sql": {"type": "string", "maxLength": 4000},
            },
            "required": ["sql"],
        },
        fn=_make_runner(ts_pool, AGENT_VIEWS_FUNCTIONS_TS, "agent_reader_ts", "ts"),
    )

    registry.register(
        "run_select_static",
        description=(
            "Like run_select_ts but against the Supabase (static) pool. Use this "
            "for vehicles, drivers, depots, chargers, schedules_recent."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "sql": {"type": "string", "maxLength": 4000},
            },
            "required": ["sql"],
        },
        fn=_make_runner(
            static_pool, AGENT_VIEWS_FUNCTIONS_STATIC, "agent_reader_static", "static"
        ),
    )

    # ── current_time ────────────────────────────────────────────────────
    async def _current_time(**_: Any) -> dict:
        return {
            "utc_now_iso": datetime.now(timezone.utc).isoformat(),
            "note": (
                "Use this to anchor relative time references. Depot-local "
                "times require joining depots.timezone — query "
                "agent_views.depots($1) on the static pool."
            ),
        }

    registry.register(
        "current_time",
        description="Return the server's current UTC timestamp as ISO-8601.",
        input_schema={"type": "object", "properties": {}, "required": []},
        fn=_current_time,
    )

    # ── lookup_entity ───────────────────────────────────────────────────
    async def _lookup_entity(*, kind: str, text: str, **_: Any) -> dict:
        if kind not in ("driver", "vehicle", "depot", "rfid"):
            return {"error": f"kind must be one of driver|vehicle|depot|rfid; got {kind!r}"}
        mention = EntityMention(kind=kind, text=text)
        try:
            resolved = await resolve_entities([mention], auth, static_pool)
        except Exception as e:
            logger.exception("lookup_entity failed")
            return {"error": f"resolve failed: {e}"}
        out: list[dict] = []
        for r in resolved:
            primary = {
                "kind": r.kind,
                "display": r.display,
                "primary_id": str(r.primary_id) if r.primary_id else None,
            }
            candidates = [
                {
                    "kind": c.kind,
                    "display": c.display,
                    "primary_id": str(c.primary_id) if c.primary_id else None,
                }
                for c in (r.candidates or [])
            ]
            out.append({**primary, "candidates": candidates})
        return {"results": out}

    registry.register(
        "lookup_entity",
        description=(
            "Resolve a free-text name to candidate IDs. Use this to map "
            "'John Smith' or 'BUS-014' to a driver_id / vehicle_id before "
            "querying agent_views.* by UUID. Auto-scoped to your depots."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["driver", "vehicle", "depot", "rfid"]},
                "text": {"type": "string"},
            },
            "required": ["kind", "text"],
        },
        fn=_lookup_entity,
    )

    # ── emit_final_answer (terminator) ──────────────────────────────────
    async def _emit_final_answer(*, text: str, row_evidence: Any = 0, **_: Any) -> dict:
        # Defensively coerce row_evidence: the LLM occasionally passes
        # a stringified count ("42 rows") or null instead of the integer
        # the schema declares. A raw `int(row_evidence)` would raise
        # ValueError/TypeError, the runtime would catch it as a generic
        # tool failure, and the user would just see another retry — no
        # cleanly classified failure. Clamp to a sane non-negative int
        # so the terminator always returns a well-formed result.
        try:
            evidence = int(row_evidence)
        except (TypeError, ValueError):
            evidence = 0
        return {"text": str(text or ""), "row_evidence": max(0, evidence)}

    registry.register(
        EMIT_FINAL_ANSWER_TOOL,
        description=(
            "TERMINATOR. Call this exactly once when you have the answer. "
            "`text` is the natural-language reply the user will see. "
            "`row_evidence` is the count of rows that informed it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "maxLength": 4000},
                "row_evidence": {"type": "integer", "minimum": 0, "default": 0},
            },
            "required": ["text"],
        },
        fn=_emit_final_answer,
    )

    return registry


# Public name list, used by the runtime to construct allowed_tools.
SQL_AGENT_TOOL_NAMES: tuple[str, ...] = (
    "list_tables",
    "describe_table",
    "sample_values",
    "run_select_ts",
    "run_select_static",
    "current_time",
    "lookup_entity",
    EMIT_FINAL_ANSWER_TOOL,
)
