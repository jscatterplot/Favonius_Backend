"""SQL validator for the depot chat agent's text-to-SQL path.

Security gate between LLM-emitted SQL and the read-only Postgres role.
Parses the SQL with sqlglot, walks the AST, enforces:

* exactly one statement, root is a ``SELECT`` (or set-operation of selects)
* every FROM/JOIN target is an ``agent_views.<allowed_name>($1)`` call
* the placeholder is exactly ``$1`` — no literal UUIDs the LLM could
  forge to peek at another tenant
* no DML anywhere (including inside CTEs)
* no references to ``pg_*`` / ``information_schema`` / ``auth`` / ``storage``
  / ``vault`` / ``supabase_*`` schemas
* no calls to dangerous functions outside the ``agent_views.*`` allowlist
* hypertable-backed functions require a time predicate in the WHERE clause
* ``LIMIT`` injection / cap

The validator does NOT execute the SQL — that's the executor's job. It
returns a :class:`ValidationResult` whose ``ok=True`` form carries a
canonical, rewritten SQL string the executor will pass through
``EXPLAIN (FORMAT TEXT) …`` (S4 — never ANALYZE) and then run.

Rejections come back with a structured ``error_kind`` so the orchestrator
can feed it to the LLM as a ``tool_result(is_error=True)`` for a retry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)


# ── Allow / deny lists ────────────────────────────────────────────────────

# All names case-insensitive — Postgres folds unquoted identifiers to lower.
AGENT_VIEWS_FUNCTIONS_TS: frozenset[str] = frozenset({
    "sessions",
    "optimization_runs",
    "alerts",
    "prices_hourly",
    "building_load_hourly",
    "connector_status_latest",
})

AGENT_VIEWS_FUNCTIONS_STATIC: frozenset[str] = frozenset({
    "depots",
    "vehicles",
    "chargers",
    "drivers",
    "schedules_recent",
})

# Functions that REQUIRE a time predicate in the user's WHERE. These are
# the hypertable-backed ones; without a bounded scan the query can chew
# through a year of telemetry rolling-up before LIMIT applies. The
# validator looks for ANY comparison referencing one of these columns:
HYPERTABLE_FUNCTIONS: frozenset[str] = frozenset({
    "prices_hourly",
    "building_load_hourly",
})
HYPERTABLE_TIME_COLUMNS: frozenset[str] = frozenset({
    "hour", "start_time", "end_time", "time",
})

# Forbidden schemas — anything resolving here is an instant reject.
FORBIDDEN_SCHEMAS: frozenset[str] = frozenset({
    "pg_catalog",
    "pg_temp",
    "pg_toast",
    "information_schema",
    "auth",        # Supabase auth schema
    "storage",     # Supabase storage schema
    "vault",       # Supabase vault schema
    "supabase_functions",
    "supabase_migrations",
})

# Functions that must never be callable from agent SQL, even outside a
# FROM clause. Each is either a known data-exfiltration vector, a
# server-state mutator, or a way to defeat the statement_timeout cap.
# sqlglot typed Func nodes (e.g. version() → CurrentVersion) use keys that
# differ from the SQL function name; map key → name for the denylist check.
TYPED_FUNC_KEYS: dict[str, str] = {
    "currentversion": "version",
}

DANGEROUS_FUNCTIONS: frozenset[str] = frozenset({
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export",
    "dblink", "dblink_exec", "dblink_connect", "dblink_disconnect",
    "copy",
    "current_setting", "set_config",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "txid_current", "pg_current_xact_id",
    "pg_terminate_backend", "pg_cancel_backend",
    "pg_reload_conf",
    "version",
    "pg_backend_pid",
    "pg_export_snapshot",
    "pg_advisory_lock", "pg_advisory_xact_lock",
})

DEFAULT_ROW_LIMIT = 500
MAX_ROW_LIMIT = 500  # absolute ceiling — user-supplied LIMIT is capped to this


# ── Result type ──────────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    """Outcome of a single validator pass.

    On ``ok=True``, ``sql`` is the canonical, LIMIT-injected SQL safe to
    pass to the executor's :func:`~.sql_executor.run_select`. On
    ``ok=False``, ``error_kind`` is one of the documented rejection
    codes and ``error`` carries a short human-readable explanation
    suitable to surface to the LLM as a tool-result error.
    """

    ok: bool
    sql: str = ""
    error: Optional[str] = None
    error_kind: Optional[str] = None
    functions_used: tuple[str, ...] = ()


def _reject(kind: str, msg: str) -> ValidationResult:
    return ValidationResult(ok=False, error=msg, error_kind=kind)


# ── Public entrypoint ────────────────────────────────────────────────────


def validate_sql(
    sql: str,
    *,
    allowed_functions: frozenset[str],
    row_limit: int = DEFAULT_ROW_LIMIT,
) -> ValidationResult:
    """Validate one LLM-emitted SELECT against the agent_views.* surface.

    Args:
        sql: Raw SQL the LLM produced.
        allowed_functions: The set of function names allowed for THIS
            pool (TS or static). The caller passes
            :data:`AGENT_VIEWS_FUNCTIONS_TS` or
            :data:`AGENT_VIEWS_FUNCTIONS_STATIC`.
        row_limit: Hard ceiling on the row count returned. Defaults to
            500 — must not exceed :data:`MAX_ROW_LIMIT`.

    Returns:
        A :class:`ValidationResult`.
    """
    if not isinstance(sql, str) or not sql.strip():
        return _reject("empty_sql", "Empty SQL.")
    if len(sql) > 8000:
        return _reject("too_long", "SQL exceeds 8000-character cap.")

    row_limit = min(max(1, row_limit), MAX_ROW_LIMIT)

    # 1. Parse + multi-statement check.
    try:
        trees = sqlglot.parse(sql, dialect="postgres")
    except Exception as e:  # sqlglot raises ParseError, TokenError, etc.
        return _reject("parse_error", f"Could not parse SQL: {e}")
    trees = [t for t in trees if t is not None]
    if len(trees) == 0:
        return _reject("empty_sql", "No statement parsed from input.")
    if len(trees) > 1:
        return _reject("multi_statement", "Only one statement allowed per call.")
    tree = trees[0]

    # 2. Root must be SELECT or a set-operation of SELECTs (UNION etc.).
    # sqlglot 30 split Intersect/Except out of Union; allow all three so a
    # legitimate set-op over two agent_views functions parses without a
    # spurious non_select rejection.
    _select_set_op_types: tuple[type, ...] = (exp.Select, exp.Union)
    for cls_name in ("Intersect", "Except", "SetOperation"):
        if hasattr(exp, cls_name):
            _select_set_op_types = _select_set_op_types + (getattr(exp, cls_name),)
    if not isinstance(tree, _select_set_op_types):
        return _reject(
            "non_select",
            f"Only SELECT statements are allowed; got {type(tree).__name__}.",
        )

    # 3. Reject DML anywhere — covers `WITH x AS (DELETE …) SELECT …`.
    for node in tree.walk():
        if isinstance(node, (exp.Delete, exp.Insert, exp.Update, exp.Merge,
                             exp.Drop, exp.Create, exp.Alter,
                             exp.TruncateTable, exp.Command)):
            return _reject(
                "non_select",
                f"DML/DDL not allowed (found {type(node).__name__}).",
            )

    # 4. Schema allowlist + table-function allowlist.
    functions_used: list[str] = []
    for node in tree.find_all(exp.Table):
        # The Table node represents either a plain table (e.g. public.x)
        # or a table-function call (e.g. agent_views.sessions($1)).
        # In sqlglot, a table-function's Table has empty `name` and the
        # actual function name lives on a nested Anonymous.
        db = (node.db or "").lower()
        name = (node.name or "").lower()

        if db in FORBIDDEN_SCHEMAS or name in FORBIDDEN_SCHEMAS:
            return _reject(
                "forbidden_schema",
                f"Schema not allowed: {db or name}.",
            )

        if db != "agent_views":
            return _reject(
                "table_not_allowed",
                f"FROM/JOIN target {node.sql(dialect='postgres')!r} is not in "
                f"agent_views.*. Only agent_views.<fn>($1) calls are allowed.",
            )

        # Find the nested Anonymous representing the function call.
        anon = node.find(exp.Anonymous)
        if anon is None:
            # Plain `FROM agent_views.something` without ($1) — not a
            # function call, so neither a table-function nor a view we
            # exposed. (We exposed only functions.)
            return _reject(
                "missing_argument",
                f"agent_views target {node.sql(dialect='postgres')!r} must be "
                f"called as agent_views.<fn>($1).",
            )

        fn_name = anon.name.lower()
        if fn_name not in allowed_functions:
            return _reject(
                "table_not_allowed",
                f"Function agent_views.{fn_name} is not in the allowlist for "
                f"this pool. Allowed: {sorted(allowed_functions)}.",
            )

        # Verify exactly one argument, and that argument is the
        # placeholder `$1`. sqlglot represents `$1` as
        #   Parameter(this=Literal(value=1, is_string=False)).
        args = anon.args.get("expressions") or []
        if len(args) != 1:
            return _reject(
                "bad_function_argument",
                f"agent_views.{fn_name} must take exactly one argument ($1); "
                f"got {len(args)}.",
            )
        arg = args[0]
        if not (isinstance(arg, exp.Parameter)
                and isinstance(arg.this, exp.Literal)
                and str(arg.this.name) == "1"):
            return _reject(
                "bad_function_argument",
                f"agent_views.{fn_name} argument must be the placeholder $1, "
                f"got {arg.sql(dialect='postgres')!r}. The server binds the "
                f"caller's visible depots — never write a literal UUID list.",
            )

        functions_used.append(fn_name)

    if not functions_used:
        return _reject(
            "no_data_source",
            "No agent_views.* function was referenced. At minimum the query "
            "must SELECT FROM one of: "
            f"{', '.join(sorted(allowed_functions))}.",
        )

    # 5. Dangerous functions called outside table-context.
    for node in tree.find_all(exp.Anonymous):
        parent = node.parent
        # Skip the table-function nodes — those are the legitimate
        # FROM/JOIN targets we already validated above.
        if isinstance(parent, exp.Table):
            continue
        if node.name.lower() in DANGEROUS_FUNCTIONS:
            return _reject(
                "dangerous_fn",
                f"Function {node.name!r} not permitted.",
            )

    # Also walk built-in funcs sqlglot resolved (Func subclasses) to be
    # paranoid. Most dangerous functions parse as Anonymous because they
    # are postgres-specific, but a few (e.g. version() → CurrentVersion)
    # are typed. CURRENT_USER is informational — allow — but flag the rest.
    # This is a defence-in-depth list; the main protection is the role
    # swap + grants.
    for node in tree.find_all(exp.Func):
        if isinstance(node, exp.Anonymous):
            continue
        parent = node.parent
        if isinstance(parent, exp.Table):
            continue
        fn_name = TYPED_FUNC_KEYS.get((node.key or "").lower(), (node.key or "").lower())
        if fn_name in DANGEROUS_FUNCTIONS:
            return _reject(
                "dangerous_fn",
                f"Function {fn_name!r} not permitted.",
            )

    # 6. Hypertable-backed functions need a time predicate. Apply per
    # SELECT-branch — a UNION with one bounded and one unbounded branch
    # would otherwise pass with the unbounded scan intact.
    if functions_used and any(fn in HYPERTABLE_FUNCTIONS for fn in functions_used):
        for select_node in _iter_selects(tree):
            if not _select_touches_hypertable(select_node):
                continue
            where = select_node.args.get("where")
            if where is None or not _has_bounding_time_predicate(where):
                fn_name = next(
                    (fn for fn in functions_used if fn in HYPERTABLE_FUNCTIONS),
                    "hypertable",
                )
                return _reject(
                    "missing_time_filter",
                    f"agent_views.{fn_name} requires a bounding time predicate "
                    f"on one of {sorted(HYPERTABLE_TIME_COLUMNS)} in EVERY "
                    f"SELECT branch that references it (e.g. "
                    f"WHERE hour >= now() - interval '7 days'). Comparisons "
                    f"where both sides are columns (like `hour = hour`) do "
                    f"NOT count.",
                )

    # 7. LIMIT injection / cap.
    limit_node = tree.args.get("limit")
    if isinstance(tree, exp.Union):
        # Union-level limit: walk to the outermost. sqlglot puts LIMIT on
        # the Union when it's a trailing top-level limit.
        pass

    if limit_node is None:
        tree.set("limit", exp.Limit(expression=exp.Literal.number(row_limit)))
    else:
        # Cap user-supplied LIMIT to row_limit.
        expr = limit_node.expression
        if isinstance(expr, exp.Literal) and not expr.is_string:
            try:
                user_n = int(str(expr.name))
            except ValueError:
                user_n = row_limit
            if user_n > row_limit or user_n < 1:
                limit_node.set("expression", exp.Literal.number(row_limit))
        else:
            # Non-literal LIMIT (e.g. a parameter) is rejected — keeps the
            # cap deterministic.
            return _reject(
                "non_literal_limit",
                "LIMIT must be a non-negative integer literal "
                f"(≤ {row_limit}).",
            )

    canonical = tree.sql(dialect="postgres")
    return ValidationResult(
        ok=True,
        sql=canonical,
        functions_used=tuple(functions_used),
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _iter_selects(tree: exp.Expression) -> "list[exp.Select]":
    """Yield every Select node in a tree, including those nested inside
    set-operations (Union / Intersect / Except). Top-down."""
    out: list[exp.Select] = []
    for node in tree.walk():
        if isinstance(node, exp.Select):
            out.append(node)
    return out


def _select_touches_hypertable(select: exp.Select) -> bool:
    """True iff this Select directly references a hypertable function in
    its FROM/JOIN chain (not via a sub-SELECT — those carry their own
    SELECT node and get checked separately)."""
    for tbl in _iter_select_from_tables(select):
        if (tbl.db or "").lower() != "agent_views":
            continue
        anon = tbl.find(exp.Anonymous)
        if anon and anon.name.lower() in HYPERTABLE_FUNCTIONS:
            return True
    return False


def _iter_select_from_tables(select: exp.Select):
    """Yield Table nodes in this SELECT's top-level FROM/JOIN only."""
    from_clause = select.args.get("from_")
    if from_clause is not None:
        yield from _iter_from_join_tables(from_clause)
    for join in select.args.get("joins") or []:
        yield from _iter_from_join_tables(join)


def _iter_from_join_tables(node: exp.Expression):
    """Walk a FROM/JOIN fragment; do not descend into nested SELECTs."""
    if isinstance(node, (exp.Subquery, exp.Select)):
        return
    if isinstance(node, exp.Table):
        yield node
        return
    for child in node.iter_expressions():
        yield from _iter_from_join_tables(child)


def _has_bounding_time_predicate(where: exp.Where) -> bool:
    """True iff the WHERE contains a comparison that bounds one of
    HYPERTABLE_TIME_COLUMNS against a non-column expression.

    Tautologies like ``hour = hour`` are rejected: BOTH sides must
    NOT be the same Column reference for the comparison to count.
    BETWEEN and IN counts as bounding even if the children include
    column refs — those still constrain the range to a finite set.

    Predicates inside EXISTS/subquery branches are ignored — they
    bound a nested scan, not this SELECT's hypertable FROM target.
    """
    for col in _iter_where_columns(where.this):
        if (col.name or "").lower() not in HYPERTABLE_TIME_COLUMNS:
            continue
        comparison = _ancestor_comparison(col)
        if comparison is None:
            continue
        if isinstance(comparison, (exp.Between, exp.In)):
            return True
        # Binary comparison: at least one side must not be a Column.
        left = comparison.this
        right = comparison.args.get("expression")
        if left is None or right is None:
            continue
        if isinstance(left, exp.Column) and isinstance(right, exp.Column):
            continue  # `hour = hour` style tautology — does not bound
        return True
    return False


def _iter_where_columns(node: exp.Expression | None):
    """Yield Column nodes in a WHERE predicate, excluding subqueries."""
    if node is None:
        return
    if isinstance(node, (exp.Subquery, exp.Select, exp.Exists)):
        return
    if isinstance(node, exp.Column):
        yield node
    for child in node.iter_expressions():
        yield from _iter_where_columns(child)


def _ancestor_comparison(node: exp.Expression) -> exp.Expression | None:
    """Walk parents up to the nearest comparison-like node (or None)."""
    cur = node.parent
    while cur is not None and not isinstance(cur, exp.Where):
        if isinstance(
            cur,
            (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ, exp.Between, exp.In),
        ):
            return cur
        cur = cur.parent
    return None
