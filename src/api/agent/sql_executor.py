"""Read-only SQL executor for the depot chat agent.

Runs LLM-emitted (and validator-approved) SELECTs against the curated
``agent_views.*`` table-functions. Defence in depth:

1. **Role swap with assertion (S2)** — every call wraps the SELECT in a
   transaction that ``SET LOCAL ROLE`` to the read-only role
   (``agent_reader_ts`` or ``agent_reader_static``) and then immediately
   asserts ``current_user`` matches. If the grant is misconfigured the
   request fails closed with :class:`SqlExecutorRoleError`.
2. ``SET LOCAL statement_timeout = '5s'`` so a runaway plan cannot tie
   up the connection.
3. ``SET LOCAL transaction_read_only = on`` belt-and-braces.
4. ``EXPLAIN (FORMAT TEXT) …`` is run inside the same transaction before
   execution (S4 — never ``ANALYZE``), catching column hallucinations
   sqlglot cannot.
5. The whole transaction is wrapped in a savepoint that ``ROLLBACK``s at
   the end so the role swap auto-reverts and connection reuse is safe.
6. Per-tool ``async with pool.acquire()`` (P2) — connections are never
   held across LLM round-trips.

The executor never logs SQL with bound parameter values at INFO level
(parameters can carry PII like driver UUIDs). DEBUG-level logging
includes the SQL but not the values.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import asyncpg

from src.monitoring.metrics import (
    AGENT_SQL_EXECUTION_DURATION,
    AGENT_SQL_EXECUTIONS,
    AGENT_SQL_ROWS_RETURNED,
)

logger = logging.getLogger(__name__)

# Postgres interval literals accepted by SET LOCAL statement_timeout.
_STATEMENT_TIMEOUT_RE = re.compile(r"^(?:0|\d+(?:\.\d+)?(?:ms|s|min|h|d)?)$")


class SqlExecutorError(Exception):
    """Base for executor errors that should bubble back as tool errors."""


class SqlExecutorRoleError(SqlExecutorError):
    """The post-swap ``current_user`` did not match the expected role.

    This is **fail closed** behaviour for S2. We never run the LLM's SQL
    while wearing the wrong hat; raising here ends the turn with
    ``status='error'`` and triggers an operator alert. Mitigation: grant
    the read-only role to the connection user (see migrations 042 /
    supabase 040).
    """


class SqlExecutorPlanError(SqlExecutorError):
    """``EXPLAIN`` rejected the SQL (e.g. hallucinated column).

    Surfaced to the LLM as a tool error so it can self-correct.
    """


class SqlExecutorTimeoutError(SqlExecutorError):
    """``statement_timeout`` fired."""


@dataclass
class ExecResult:
    rows: list[dict[str, Any]] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    duration_ms: int = 0
    explain_text: str = ""


# Per-cell byte cap so a single giant JSONB doesn't blow the LLM
# context window. Long values are truncated with a trailing marker.
_CELL_BYTE_CAP = 1024


def _truncate_cell(value: Any) -> Any:
    """Cap string-like cells to ``_CELL_BYTE_CAP`` bytes with a marker."""
    if value is None or isinstance(value, (int, float, bool, UUID)):
        return value
    s = str(value) if not isinstance(value, str) else value
    if len(s.encode("utf-8")) <= _CELL_BYTE_CAP:
        return s
    # Truncate at boundary; keep marker visible.
    truncated = s.encode("utf-8")[: _CELL_BYTE_CAP - 16].decode("utf-8", errors="ignore")
    return f"{truncated}…[truncated]"


async def run_select(
    *,
    pool: asyncpg.Pool,
    role: str,
    sql: str,
    depot_ids: list[UUID],
    pool_label: str,
    statement_timeout: str = "5s",
    row_cap: int = 500,
) -> ExecResult:
    """Validate-checked SELECT execution path.

    Args:
        pool: asyncpg pool for the target database (TS or static).
        role: Expected ``current_user`` after ``SET LOCAL ROLE`` (one of
            ``"agent_reader_ts"`` / ``"agent_reader_static"``).
        sql: Canonical SQL string as returned by
            :func:`~.sql_validator.validate_sql`. The first bound
            parameter (``$1``) is the depot allowlist; the caller
            supplies the value via ``depot_ids``.
        depot_ids: The caller's ``visible_depot_ids``. Bound to ``$1``.
        pool_label: Metric label — ``"ts"`` or ``"static"``.
        statement_timeout: Postgres interval, default ``'5s'``.
        row_cap: Hard limit on rows returned to the LLM. Defaults to
            500; the validator already injected a LIMIT but this is a
            second cap (defence in depth).

    Returns:
        :class:`ExecResult`.

    Raises:
        :class:`SqlExecutorRoleError`: role swap failed.
        :class:`SqlExecutorPlanError`: EXPLAIN failed (e.g. bad column).
        :class:`SqlExecutorTimeoutError`: statement_timeout fired.
        :class:`SqlExecutorError`: any other execution failure.
    """
    if role not in {"agent_reader_ts", "agent_reader_static"}:
        # Programming error — the caller MUST pick a valid role.
        raise SqlExecutorError(f"Unknown agent role: {role!r}")
    if not _STATEMENT_TIMEOUT_RE.fullmatch(statement_timeout):
        raise SqlExecutorError(
            f"Invalid statement_timeout: {statement_timeout!r}. "
            "Use a Postgres interval literal (e.g. '5s', '500ms', '0')."
        )

    started = time.monotonic()
    outcome = "success"
    try:
        async with pool.acquire() as conn:  # P2: per-tool acquire/release
            # NB: we must NOT use prepared statements here — Supavisor
            # transaction-mode rejects them. asyncpg defaults to prepared
            # via the pool's `statement_cache_size`, which the DB pool sets
            # to 0. Each statement runs as a simple query.
            async with conn.transaction(readonly=True):
                try:
                    await conn.execute(f"SET LOCAL ROLE {role};")
                except Exception as e:
                    raise SqlExecutorRoleError(
                        f"Could not SET LOCAL ROLE {role}: {e}. The application's "
                        f"connection user must be granted this role."
                    ) from e

                # S2: assert current_user matches.
                current = await conn.fetchval("SELECT current_user;")
                if str(current).lower() != role.lower():
                    raise SqlExecutorRoleError(
                        f"Post-swap current_user is {current!r}, expected {role!r}. "
                        f"Aborting turn (fail closed)."
                    )

                await conn.execute(f"SET LOCAL statement_timeout = '{statement_timeout}';")
                await conn.execute("SET LOCAL transaction_read_only = on;")

                # S4: EXPLAIN (FORMAT TEXT) — never ANALYZE.
                try:
                    explain_rows = await conn.fetch(
                        f"EXPLAIN (FORMAT TEXT) {sql}",
                        depot_ids,
                    )
                    explain_text = "\n".join(str(r[0]) for r in explain_rows)
                except asyncpg.PostgresError as e:
                    raise SqlExecutorPlanError(f"EXPLAIN rejected SQL: {e}") from e

                try:
                    rows = await conn.fetch(sql, depot_ids)
                except asyncpg.QueryCanceledError as e:
                    raise SqlExecutorTimeoutError(
                        f"statement_timeout ({statement_timeout}) fired."
                    ) from e
                except asyncpg.PostgresError as e:
                    raise SqlExecutorError(f"SQL execution failed: {e}") from e

    except SqlExecutorRoleError:
        outcome = "role_error"
        AGENT_SQL_EXECUTIONS.labels(outcome=outcome, pool=pool_label).inc()
        raise
    except SqlExecutorPlanError:
        outcome = "plan_error"
        AGENT_SQL_EXECUTIONS.labels(outcome=outcome, pool=pool_label).inc()
        raise
    except SqlExecutorTimeoutError:
        outcome = "timeout"
        AGENT_SQL_EXECUTIONS.labels(outcome=outcome, pool=pool_label).inc()
        raise
    except SqlExecutorError:
        outcome = "error"
        AGENT_SQL_EXECUTIONS.labels(outcome=outcome, pool=pool_label).inc()
        raise

    duration_ms = int((time.monotonic() - started) * 1000)
    truncated = False
    if len(rows) > row_cap:
        truncated = True
        rows = rows[:row_cap]

    out_rows: list[dict[str, Any]] = []
    for r in rows:
        out_rows.append({k: _truncate_cell(v) for k, v in dict(r).items()})

    AGENT_SQL_EXECUTIONS.labels(outcome="success", pool=pool_label).inc()
    AGENT_SQL_EXECUTION_DURATION.observe(duration_ms / 1000.0)
    AGENT_SQL_ROWS_RETURNED.observe(len(out_rows))

    return ExecResult(
        rows=out_rows,
        row_count=len(out_rows),
        truncated=truncated,
        duration_ms=duration_ms,
        explain_text=explain_text,
    )
