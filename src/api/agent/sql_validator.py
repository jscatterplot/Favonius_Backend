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
AGENT_VIEWS_FUNCTIONS_TS: frozenset[str] = frozenset(
    {
        "sessions",
        "optimization_runs",
        "alerts",
        "prices_hourly",
        "building_load_hourly",
        "connector_status_latest",
    }
)

AGENT_VIEWS_FUNCTIONS_STATIC: frozenset[str] = frozenset(
    {
        "depots",
        "vehicles",
        "chargers",
        "drivers",
        "schedules_recent",
    }
)

# Functions that REQUIRE a time predicate in the user's WHERE. These are
# the hypertable-backed ones; without a bounded scan the query can chew
# through a year of telemetry rolling-up before LIMIT applies. The
# validator looks for ANY comparison referencing one of these columns:
HYPERTABLE_FUNCTIONS: frozenset[str] = frozenset(
    {
        "prices_hourly",
        "building_load_hourly",
    }
)
# The two hypertable functions (prices_hourly, building_load_hourly)
# both expose exactly one time column: ``hour``. Older drafts of this
# set included ``start_time``/``end_time``/``time`` to match
# table-level columns, but those names exist on the non-hypertable
# functions (sessions has start_time/end_time; raw telemetry has
# time) — accepting them as bounds let an LLM-written query satisfy
# the missing_time_filter guard with a predicate on a column that
# doesn't actually exist on the bounded function. The query then
# failed at EXPLAIN time, wasting an iteration. Narrowed to the
# actual column the validator is guarding.
HYPERTABLE_TIME_COLUMNS: frozenset[str] = frozenset({"hour"})

# Forbidden schemas — anything resolving here is an instant reject.
FORBIDDEN_SCHEMAS: frozenset[str] = frozenset(
    {
        "pg_catalog",
        "pg_temp",
        "pg_toast",
        "information_schema",
        "auth",  # Supabase auth schema
        "storage",  # Supabase storage schema
        "vault",  # Supabase vault schema
        "supabase_functions",
        "supabase_migrations",
    }
)

# Functions that must never be callable from agent SQL, even outside a
# FROM clause. Each is either a known data-exfiltration vector, a
# server-state mutator, or a way to defeat the statement_timeout cap.
# sqlglot typed Func nodes (e.g. version() → CurrentVersion) use keys that
# differ from the SQL function name; map key → name for the denylist check.
TYPED_FUNC_KEYS: dict[str, str] = {
    "currentversion": "version",
}

DANGEROUS_FUNCTIONS: frozenset[str] = frozenset(
    {
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_disconnect",
        "copy",
        "current_setting",
        "set_config",
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "txid_current",
        "pg_current_xact_id",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "version",
        "pg_backend_pid",
        "pg_export_snapshot",
        "pg_advisory_lock",
        "pg_advisory_xact_lock",
    }
)

# Positive allowlist of UNQUALIFIED function names that may appear in
# LLM-emitted SQL outside the table-function FROM slot. The intent:
# fail-closed for arbitrary user-defined functions (e.g.
# `public.some_internal_fn(...)`) which the role-swap might still allow
# execute permission on by accident, and which would expose data or
# side-effects outside the curated agent_views surface.
#
# Most Postgres / SQL builtins (COUNT, SUM, AVG, NOW, COALESCE, EXTRACT,
# date_trunc, …) sqlglot resolves to typed Func subclasses (exp.Count,
# exp.Now, exp.Coalesce, exp.Extract, exp.TimestampTrunc, …), NOT to
# exp.Anonymous — so they bypass this allowlist naturally. The
# entries below are the dialect-specific / extension functions that
# parse as Anonymous and that the LLM legitimately needs:
#
#  * `time_bucket` / `time_bucket_gapfill` — TimescaleDB time aggregation
#    (the agent_views.*_hourly functions wrap one but the LLM can
#    legitimately apply another to roll up further)
#  * `locf` / `interpolate` — TimescaleDB gap-fill helpers
#  * `first` / `last` — TimescaleDB ordered aggregates
#
# Plus a few common name-only aliases that sqlglot leaves as Anonymous
# in some versions: jsonb_object_keys, generate_series, …
#
# If the LLM legitimately needs a function not on this list, the
# rejection surfaces as `function_not_allowed` and an operator can
# extend the set. NEVER add `pg_*` here — that whole namespace is
# rejected wholesale by the `_unqualified pg_* call` check below.
ALLOWED_ANONYMOUS_FUNCTIONS: frozenset[str] = frozenset(
    {
        # TimescaleDB time-series helpers
        "time_bucket",
        "time_bucket_gapfill",
        "locf",
        "interpolate",
        "first",
        "last",
        # JSON helpers sqlglot sometimes leaves as Anonymous
        "jsonb_object_keys",
        "jsonb_each",
        "jsonb_each_text",
        "jsonb_array_elements",
        "jsonb_array_elements_text",
        # Set-returning helpers commonly needed for time-range generation
        "generate_series",
        # Conditional / coalesce-like
        "greatest",
        "least",
        "nullif",
        "coalesce",
        # Common numeric
        "abs",
        "sign",
        "mod",
        "round",
        "ceil",
        "ceiling",
        "floor",
        "trunc",
        # Common string utilities (most are typed, but some dialect-specific
        # ones land as Anonymous in older sqlglot builds)
        "length",
        "char_length",
        "lower",
        "upper",
        "initcap",
        "trim",
        "btrim",
        "ltrim",
        "rtrim",
        "replace",
        "regexp_replace",
        "substring",
        "substr",
        "left",
        "right",
        "concat",
        "concat_ws",
        "format",
        "to_char",
        "to_number",
        "to_date",
        "to_timestamp",
    }
)

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
    # Also reject row-locking SELECT variants (FOR UPDATE / FOR SHARE /
    # FOR NO KEY UPDATE / FOR KEY SHARE): the executor's read-only
    # transaction would refuse them at execute time, but blocking up
    # front gives the LLM a deterministic validation rejection it can
    # retry from, instead of burning a SQL-agent iteration on a runtime
    # error (P2 codex).
    for node in tree.walk():
        if isinstance(
            node,
            (
                exp.Delete,
                exp.Insert,
                exp.Update,
                exp.Merge,
                exp.Drop,
                exp.Create,
                exp.Alter,
                exp.TruncateTable,
                exp.Command,
            ),
        ):
            return _reject(
                "non_select",
                f"DML/DDL not allowed (found {type(node).__name__}).",
            )
        if isinstance(node, exp.Lock):
            return _reject(
                "lock_clause_not_allowed",
                f"Row-locking SELECT clauses (FOR UPDATE / FOR SHARE / "
                f"FOR NO KEY UPDATE / FOR KEY SHARE) are not permitted: "
                f"{node.sql(dialect='postgres')!r}. The agent runs in a "
                f"read-only transaction so locks have no useful effect "
                f"and the executor would reject them anyway.",
            )

    # 4. Schema allowlist + table-function allowlist.
    functions_used: list[str] = []
    allowed_placeholder_ids: set[int] = set()
    for node in tree.find_all(exp.Table):
        # The Table node represents either a plain table (e.g. public.x)
        # or a table-function call (e.g. agent_views.sessions($1)).
        # In sqlglot, a table-function's Table has empty `name` and the
        # actual function name lives on a nested Anonymous.
        db = (node.db or "").lower()
        name = (node.name or "").lower()

        if db in FORBIDDEN_SCHEMAS:
            return _reject(
                "forbidden_schema",
                f"Schema not allowed: {db}.",
            )
        # Table-function nodes often have empty `name` (function on Anonymous);
        # only treat `name` as a schema token when it is set (e.g. FROM auth).
        if name and name in FORBIDDEN_SCHEMAS:
            return _reject(
                "forbidden_schema",
                f"Schema not allowed: {name}.",
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
                f"agent_views.{fn_name} must take exactly one argument ($1); " f"got {len(args)}.",
            )
        arg = args[0]
        if not (
            isinstance(arg, exp.Parameter)
            and isinstance(arg.this, exp.Literal)
            and str(arg.this.name) == "1"
        ):
            return _reject(
                "bad_function_argument",
                f"agent_views.{fn_name} argument must be the placeholder $1, "
                f"got {arg.sql(dialect='postgres')!r}. The server binds the "
                f"caller's visible depots — never write a literal UUID list.",
            )
        # Record the legitimate $1 by identity so the broader Parameter
        # walk below can distinguish "the function arg slot" from "a
        # stray $1 elsewhere in the query".
        allowed_placeholder_ids.add(id(arg))

        functions_used.append(fn_name)

    if not functions_used:
        return _reject(
            "no_data_source",
            "No agent_views.* function was referenced. At minimum the query "
            "must SELECT FROM one of: "
            f"{', '.join(sorted(allowed_functions))}.",
        )

    # 4b. Reject any bind placeholder other than the single legitimate $1
    # used inside an agent_views.<fn>($1) call slot.
    #
    # The executor binds exactly one argument (the caller's
    # visible_depot_ids of type `uuid[]`). Two failure modes both end in
    # runtime parameter errors that waste loop iterations:
    #
    #   * `... WHERE foo = $2` — wrong index, no second bind value
    #   * `... WHERE depot_id = $1` (reusing $1 in a predicate) — the
    #     bind type is uuid[] but the LLM expected uuid, and even when
    #     the operator coerces the row leaks across tenants
    #
    # Both are rejected here so the LLM gets a tool_result(is_error=True)
    # at validate time instead of an asyncpg error at execute time.
    for param in tree.find_all(exp.Parameter):
        if id(param) in allowed_placeholder_ids:
            continue
        if not (isinstance(param.this, exp.Literal) and str(param.this.name) == "1"):
            return _reject(
                "bad_placeholder",
                f"Only the placeholder $1 is supported (bound to the caller's "
                f"depot list); got {param.sql(dialect='postgres')!r}. Use a "
                f"literal value or move the predicate into agent_views.<fn>($1).",
            )
        return _reject(
            "bad_placeholder",
            f"Extra $1 placeholder at {param.sql(dialect='postgres')!r}: "
            f"$1 is only valid as the first (and only) argument to "
            f"agent_views.<fn>($1). The server binds it to a uuid[] depot "
            f"list; reusing it elsewhere causes a parameter-type mismatch "
            f"at execute time.",
        )

    # 5. Schema-qualified function calls (Dot wrapping Anonymous) — reject
    # any prefix in FORBIDDEN_SCHEMAS. The Table-node check at step 4
    # blocks `FROM auth.x`, but it does NOT catch `SELECT auth.uid()`
    # because sqlglot represents that as Dot(Identifier, Anonymous), not
    # as a Table. Run BEFORE the dangerous_fn check so the rejection
    # surfaces the (broader, more specific) schema reason rather than
    # leaking which individual functions are on the denylist.
    for dot in tree.find_all(exp.Dot):
        left = dot.this
        if not isinstance(left, exp.Identifier):
            continue
        schema = (left.name or "").lower()
        if schema in FORBIDDEN_SCHEMAS:
            return _reject(
                "forbidden_schema",
                f"Schema-qualified function call to {schema}.* is not "
                f"permitted: {dot.sql(dialect='postgres')!r}.",
            )

    # 5b. Unqualified function calls — allowlist + denylist combo.
    # Most Postgres builtins parse as typed Func subclasses (exp.Count,
    # exp.Now, etc.) and bypass this check entirely. Anonymous calls are
    # dialect extensions or user-defined functions; we restrict them to
    # the positive ALLOWED_ANONYMOUS_FUNCTIONS set so user-defined
    # functions like `public.some_internal_fn()` cannot be reached even
    # if the role-swap happens to grant execute (P1 codex finding).
    for node in tree.find_all(exp.Anonymous):
        parent = node.parent
        # Skip the table-function nodes — those are the legitimate
        # FROM/JOIN targets we already validated above.
        if isinstance(parent, exp.Table):
            continue
        fname = (node.name or "").lower()
        if fname in DANGEROUS_FUNCTIONS:
            return _reject(
                "dangerous_fn",
                f"Function {node.name!r} not permitted.",
            )
        # Blanket-reject any unqualified pg_* call. The named denylist
        # above only catches a handful (pg_read_file, pg_ls_dir, pg_sleep,
        # …) — pg_relation_size, pg_total_relation_size, pg_database_size,
        # pg_stat_get_*, pg_advisory_lock and many more are not on it but
        # all violate the agent's "no system catalog access" contract.
        # Honest exception: nothing in agent_views.* uses pg_* prefixes,
        # so an LLM-emitted pg_X() call is always wrong.
        if fname.startswith("pg_"):
            return _reject(
                "dangerous_fn",
                f"Function {node.name!r} not permitted "
                f"(pg_* helpers are reserved for system catalog access).",
            )
        if fname not in ALLOWED_ANONYMOUS_FUNCTIONS:
            return _reject(
                "function_not_allowed",
                f"Function {node.name!r} is not in the agent's allowlist "
                f"of safe extensions. If the LLM needs a Postgres builtin, "
                f"verify it parses as a typed expression (sqlglot resolves "
                f"COUNT/SUM/NOW/EXTRACT/COALESCE/… as typed nodes that "
                f"bypass this check). If a dialect extension is genuinely "
                f"needed, add it to ALLOWED_ANONYMOUS_FUNCTIONS after "
                f"reviewing the side effects.",
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
    # would otherwise pass with the unbounded scan intact. AND apply per
    # hypertable: a query joining `prices_hourly` with `sessions` must
    # bound the time column attached to `prices_hourly`, not just any
    # column in HYPERTABLE_TIME_COLUMNS (the LLM could leak `prices_hourly`
    # by only bounding `sessions.start_time`).
    if functions_used and any(fn in HYPERTABLE_FUNCTIONS for fn in functions_used):
        for select_node in _iter_selects(tree):
            htable_refs = list(_hypertable_refs_in_select(select_node))
            if not htable_refs:
                continue
            where = select_node.args.get("where")
            all_aliases = list(_all_source_aliases(select_node))
            for alias, fname in htable_refs:
                if (
                    where is None
                    or not _has_bounding_time_predicate_for(where, alias, all_aliases)
                    or _has_or_true_bypass(where, alias, all_aliases)
                ):
                    return _reject(
                        "missing_time_filter",
                        f"agent_views.{fname} requires a bounding time predicate "
                        f"on its own time column (one of "
                        f"{sorted(HYPERTABLE_TIME_COLUMNS)}) in EVERY SELECT "
                        f"branch that references it. Bound the predicate to the "
                        f"hypertable's alias (e.g. `WHERE {alias}.hour >= "
                        f"now() - interval '7 days'`); bounds on a joined "
                        f"sibling table do NOT count, and comparisons where "
                        f"both sides are columns (like `hour = hour`) do NOT "
                        f"count either.",
                    )

    # 7. OFFSET reject + LIMIT injection / cap on every SELECT branch + root.
    #
    # OFFSET is rejected outright (not capped). The agent loop never needs
    # client-side pagination, and large OFFSETs force Postgres to skip
    # rows BEFORE the LIMIT applies — defeating the row-cap protection.
    # Check OFFSET on every Select branch (UNION branches can carry their
    # own OFFSET; they all need to be zero/absent).
    for select_node in _iter_selects(tree):
        offset_node = select_node.args.get("offset")
        if offset_node is not None and not _is_zero_literal(offset_node.expression):
            return _reject(
                "offset_not_allowed",
                "OFFSET is not permitted (a large value defeats the row "
                "cap by forcing a full scan). Use a more selective WHERE.",
            )
    root_offset = tree.args.get("offset") if not isinstance(tree, exp.Select) else None
    if root_offset is not None and not _is_zero_literal(root_offset.expression):
        return _reject(
            "offset_not_allowed",
            "OFFSET is not permitted (a large value defeats the row "
            "cap by forcing a full scan). Use a more selective WHERE.",
        )

    # Per-branch LIMITs on UNION/INTERSECT/EXCEPT run before the outer
    # LIMIT; uncapped branches can scan huge row sets within the timeout.
    # Cap each top-level branch AND the root — but DO NOT cap nested
    # subqueries (e.g. `WHERE x IN (SELECT y FROM …)`), since an injected
    # LIMIT there changes the query's semantics rather than just truncating
    # the user-visible output.
    for branch in _iter_top_level_branches(tree):
        rej = _apply_row_limit_on_node(branch, row_limit)
        if rej is not None:
            return rej
    # Plain SELECTs are already capped via the branch loop; only set-op
    # roots need an outer LIMIT (UNION/INTERSECT/EXCEPT).
    if not isinstance(tree, exp.Select):
        rej = _apply_row_limit_on_node(tree, row_limit)
        if rej is not None:
            return rej

    canonical = tree.sql(dialect="postgres")
    return ValidationResult(
        ok=True,
        sql=canonical,
        functions_used=tuple(functions_used),
    )


# ── Helpers ──────────────────────────────────────────────────────────────


def _is_zero_literal(expr: exp.Expression | None) -> bool:
    """True iff ``expr`` is the integer literal 0 (used for OFFSET 0)."""
    return isinstance(expr, exp.Literal) and not expr.is_string and str(expr.name) == "0"


def _apply_row_limit_on_node(
    node: exp.Expression,
    row_limit: int,
) -> Optional[ValidationResult]:
    """Inject or cap LIMIT on one AST node (Select, Union, etc.)."""
    limit_node = node.args.get("limit")
    if limit_node is None:
        node.set("limit", exp.Limit(expression=exp.Literal.number(row_limit)))
        return None
    expr = limit_node.expression
    if isinstance(expr, exp.Literal) and not expr.is_string:
        try:
            user_n = int(str(expr.name))
        except ValueError:
            user_n = row_limit
        if user_n > row_limit or user_n < 1:
            limit_node.set("expression", exp.Literal.number(row_limit))
        return None
    return _reject(
        "non_literal_limit",
        f"LIMIT must be a non-negative integer literal (≤ {row_limit}).",
    )


def _iter_selects(tree: exp.Expression) -> "list[exp.Select]":
    """Yield every Select node in a tree, including those nested inside
    set-operations (Union / Intersect / Except). Top-down."""
    out: list[exp.Select] = []
    for node in tree.walk():
        if isinstance(node, exp.Select):
            out.append(node)
    return out


def _iter_top_level_branches(tree: exp.Expression) -> "list[exp.Select]":
    """Yield the SELECT branches at the *user-visible* surface of a query.

    Walks set-operation (Union / Intersect / Except) nodes recursively to
    collect their direct SELECT operands, but does NOT recurse into
    Subquery / Exists / scalar-subquery contexts. Caller uses this for
    LIMIT injection — a LIMIT on a nested subquery would change the
    query's semantics rather than just truncating the user-visible
    output (e.g. ``WHERE x IN (SELECT y FROM …)`` becoming an arbitrary
    500-row IN-list).
    """
    # Collect all set-op classes available in the installed sqlglot (the
    # public API stabilised on Union but older/newer versions also expose
    # Intersect/Except/SetOperation). Without this, INTERSECT/EXCEPT
    # branches escape the LIMIT cap entirely.
    set_op_types: tuple[type, ...] = (exp.Union,)
    for cls_name in ("Intersect", "Except", "SetOperation"):
        if hasattr(exp, cls_name):
            set_op_types = set_op_types + (getattr(exp, cls_name),)

    out: list[exp.Select] = []

    def _visit(node: exp.Expression) -> None:
        if isinstance(node, set_op_types):
            left = node.args.get("this")
            right = node.args.get("expression")
            if left is not None:
                _visit(left)
            if right is not None:
                _visit(right)
            return
        if isinstance(node, exp.Select):
            out.append(node)

    _visit(tree)
    return out


def _hypertable_refs_in_select(
    select: exp.Select,
) -> "list[tuple[str, str]]":
    """Yield (alias_or_fname, fname) for each hypertable function in
    this Select's top-level FROM/JOIN. The alias falls back to the
    function name when none is declared — that's what the LLM has to
    reference in WHERE for the time bound to count.
    """
    out: list[tuple[str, str]] = []
    for tbl in _iter_select_from_tables(select):
        if (tbl.db or "").lower() != "agent_views":
            continue
        anon = tbl.find(exp.Anonymous)
        if anon is None:
            continue
        fname = (anon.name or "").lower()
        if fname not in HYPERTABLE_FUNCTIONS:
            continue
        alias = (tbl.alias or fname).lower()
        out.append((alias, fname))
    return out


def _all_source_aliases(select: exp.Select) -> "list[str]":
    """Yield aliases (or function names) for every FROM/JOIN source.
    Used to decide whether an UNQUALIFIED column reference is ambiguous
    in WHERE: if there's exactly one source, ``WHERE hour >= X`` can
    only refer to that source's column.
    """
    out: list[str] = []
    for tbl in _iter_select_from_tables(select):
        anon = tbl.find(exp.Anonymous)
        fname = (anon.name or "").lower() if anon else ""
        out.append((tbl.alias or fname or (tbl.name or "")).lower())
    return out


def _iter_select_from_tables(select: exp.Select):
    """Yield Table nodes in this SELECT's top-level FROM/JOIN only."""
    from_clause = select.args.get("from")
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


def _has_bounding_time_predicate_for(
    where: exp.Where,
    target_alias: str,
    all_aliases: "list[str]",
) -> bool:
    """True iff the WHERE contains a bounding time-column predicate
    attributable to ``target_alias`` (the alias/name of the hypertable
    function being guarded).

    A predicate counts when:
      * its time column belongs to HYPERTABLE_TIME_COLUMNS, AND
      * the column reference is qualified by ``target_alias``, OR
        unqualified AND ``target_alias`` is the only source in the
        SELECT's FROM/JOIN chain (so the reference is unambiguous), AND
      * the comparison is a genuine bound (not a self-reference,
        EXTRACT-wrapper, etc.), AND
      * the predicate is not inside an EXISTS/subquery branch.

    OR-TRUE bypass is detected separately by ``_has_or_true_bypass``.
    """
    only_source = len(all_aliases) == 1 and all_aliases[0] == target_alias
    for col in _iter_where_columns(where.this):
        if (col.name or "").lower() not in HYPERTABLE_TIME_COLUMNS:
            continue
        col_alias = (col.table or "").lower()
        if col_alias:
            if col_alias != target_alias:
                # Column belongs to a sibling table (e.g. sessions.start_time
                # when we're guarding prices_hourly) — does NOT bound us.
                continue
        else:
            # Unqualified column reference. Only counts if there's a single
            # source and it's the hypertable we're guarding — otherwise
            # the LLM hasn't disambiguated which table the bound applies to.
            if not only_source:
                continue
        comparison = _ancestor_comparison(col)
        if comparison is None:
            continue
        if not _is_genuine_bound(comparison, col):
            continue
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


def _is_genuine_bound(comparison: exp.Expression, time_col: exp.Column) -> bool:
    """True iff ``comparison`` bounds the time column without being a
    self-referential tautology.

    Rules:

    * Binary comparison (``>=`` etc.): the time column must appear on
      EXACTLY ONE side; the OTHER side must not contain a Column ref
      to the SAME time column (catches ``hour >= hour - interval '…'``
      and ``date_trunc('day', hour) = hour``).
    * ``BETWEEN``: the time column must appear on the subject side only.
      If EITHER bound also references the time column the predicate is
      self-referential (e.g. ``hour BETWEEN hour - interval '1d' AND
      hour + interval '1d'``) and does NOT bound the scan window.
    * ``IN``: the time column must be the subject side only — if any
      value in the IN-list also references the time column, the
      predicate degenerates (e.g. ``hour IN (hour)``).
    """
    target_name = (time_col.name or "").lower()

    if isinstance(comparison, exp.Between):
        subject = comparison.args.get("this")
        low = comparison.args.get("low")
        high = comparison.args.get("high")
        if subject is None or low is None or high is None:
            return False
        subj_has = bool(_columns_matching(subject, target_name))
        low_has = bool(_columns_matching(low, target_name))
        high_has = bool(_columns_matching(high, target_name))
        return subj_has and not (low_has or high_has)

    if isinstance(comparison, exp.In):
        subject = comparison.args.get("this")
        if subject is None:
            return False
        subj_has = bool(_columns_matching(subject, target_name))
        expressions = comparison.args.get("expressions") or []
        for v in expressions:
            if _columns_matching(v, target_name):
                return False
        # An IN against a subquery cannot be statically proven to bound the
        # window, but the subquery is opaque to the validator — treat as
        # bound only if there's no subquery branch.
        if comparison.args.get("query") is not None:
            return False
        return subj_has and bool(expressions)

    left = comparison.args.get("this")
    right = comparison.args.get("expression")
    if left is None or right is None:
        return False
    left_time_cols = _columns_matching(left, target_name)
    right_time_cols = _columns_matching(right, target_name)
    if left_time_cols and right_time_cols:
        # Time column on both sides — `hour = hour`, `hour >= hour - …`,
        # `date_trunc('day', hour) = hour`, etc. Not a bound.
        return False
    if not left_time_cols and not right_time_cols:
        # Neither side references the time column (shouldn't reach here
        # via _ancestor_comparison, but defensive).
        return False
    # The time-referencing side must be either the bare Column or
    # `date_trunc(_, hour)` (order-preserving). Anything else —
    # `EXTRACT(EPOCH FROM hour) > 0`, `date_part('hour', hour) >= 0`,
    # `MOD(EXTRACT(…), 24) = 5`, etc. — does NOT bound the time range
    # of the scan even though the column technically appears on one side.
    time_side = left if left_time_cols else right
    if not _side_is_order_preserving(time_side, target_name):
        return False
    return True


def _side_is_order_preserving(node: exp.Expression, time_col_name: str) -> bool:
    """True if ``node`` is the time column directly, or wrapped in a
    monotonic / order-preserving function that still lets a literal on
    the OTHER side actually bound the scan window.

    Accepts:
      * bare Column reference to the time column
      * ``date_trunc(<unit>, <time_col>)`` — truncates but preserves order
        (sqlglot may parse this as TimestampTrunc / DateTrunc / DatetimeTrunc
        depending on dialect / version; handle them all)
      * ``time_bucket(<interval>, <time_col>)`` — TimescaleDB analogue

    Rejects everything else (``EXTRACT``, ``date_part``, ``MOD``,
    arithmetic against the column, etc.). When in doubt, reject — the
    LLM can rewrite to the bare-column form.
    """
    if isinstance(node, exp.Column) and (node.name or "").lower() == time_col_name:
        return True
    # date_trunc parses differently across sqlglot versions / dialects:
    # TimestampTrunc, DateTrunc, DatetimeTrunc are all possible nodes.
    trunc_types: tuple[type, ...] = ()
    for cls_name in ("TimestampTrunc", "DateTrunc", "DatetimeTrunc"):
        if hasattr(exp, cls_name):
            trunc_types = trunc_types + (getattr(exp, cls_name),)
    if trunc_types and isinstance(node, trunc_types):
        inner = node.args.get("this")
        if isinstance(inner, exp.Column) and (inner.name or "").lower() == time_col_name:
            return True
        return False
    # time_bucket() typically parses as Anonymous with the column as last arg
    if isinstance(node, exp.Anonymous) and (node.name or "").lower() == "time_bucket":
        exprs = node.args.get("expressions") or []
        if exprs:
            last = exprs[-1]
            if isinstance(last, exp.Column) and (last.name or "").lower() == time_col_name:
                return True
        return False
    return False


def _columns_matching(node: exp.Expression, name: str) -> list[exp.Column]:
    """Return Column nodes (anywhere inside `node`) whose name matches."""
    out: list[exp.Column] = []
    if isinstance(node, exp.Column) and (node.name or "").lower() == name:
        out.append(node)
    for c in node.find_all(exp.Column):
        if (c.name or "").lower() == name and c is not node:
            out.append(c)
    return out


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


def _is_under(expr: exp.Expression, ancestor: exp.Expression) -> bool:
    """True if ``expr`` is ``ancestor`` or nested inside it."""
    cur: exp.Expression | None = expr
    while cur is not None:
        if cur is ancestor:
            return True
        cur = cur.parent
    return False


def _has_or_true_bypass(
    where: exp.Where,
    target_alias: str,
    all_aliases: "list[str]",
) -> bool:
    """Detect when the WHERE can be satisfied without an effective time bound.

    OR-TRUE only neutralizes a bound when an OR branch can be taken without
    satisfying another AND-joined effective bound. ``WHERE (hour >= X OR 1=1)
    AND hour >= Y`` is not a bypass; ``WHERE hour >= Y OR (hour >= X OR 1=1)``
    still is.

    Scoped to ``target_alias``: a sibling hypertable's time bound (e.g.
    ``bl.hour >= Y`` when guarding ``ph``) does not count as enforcing the
    target scan.
    """
    return _predicate_allows_unbounded_time(where.this, target_alias, all_aliases)


def _predicate_allows_unbounded_time(
    node: exp.Expression,
    target_alias: str,
    all_aliases: "list[str]",
) -> bool:
    """True iff this boolean subtree can be TRUE without a hypertable time bound."""
    if isinstance(node, exp.Paren):
        inner = node.this
        if inner is None:
            return True
        return _predicate_allows_unbounded_time(inner, target_alias, all_aliases)
    if isinstance(node, exp.And):
        left, right = node.this, node.expression
        if left is None or right is None:
            return True
        return _predicate_allows_unbounded_time(
            left, target_alias, all_aliases
        ) and _predicate_allows_unbounded_time(right, target_alias, all_aliases)
    if isinstance(node, exp.Or):
        left, right = node.this, node.expression
        if left is None or right is None:
            return True
        return _predicate_allows_unbounded_time(
            left, target_alias, all_aliases
        ) or _predicate_allows_unbounded_time(right, target_alias, all_aliases)
    if isinstance(node, exp.Not):
        inner = node.this
        if inner is None:
            return True
        if _is_unconditional_false(inner):
            # NOT(FALSE) ≡ TRUE — bypass.
            return True
        if _is_unconditional_true(inner):
            # NOT(TRUE) ≡ FALSE — the row is unreachable, so the NOT
            # branch cannot be satisfied. No bypass via this branch.
            return False
        # NOT(anything else) — the negation of an arbitrary leaf predicate
        # is still a non-time-bound predicate that can be TRUE for many
        # rows (`NOT(depot_id IS NOT NULL)` ≡ `depot_id IS NULL`,
        # `NOT(status = 'x')` ≡ `status != 'x'` etc.). Conservatively
        # treat it as "allows unbounded time"; that's what a bypass means
        # here. Returning False here would let
        # `WHERE hour >= … OR NOT(depot_id IS NOT NULL)` pass the guard
        # while leaving the OR branch fully unbounded.
        return True
    if _is_unconditional_true(node):
        return True
    if _leaf_enforces_hypertable_time_bound(node, target_alias, all_aliases):
        return False
    return True


def _leaf_enforces_hypertable_time_bound(
    node: exp.Expression,
    target_alias: str,
    all_aliases: "list[str]",
) -> bool:
    """True iff this leaf predicate genuinely bounds the target hypertable's time column."""
    only_source = len(all_aliases) == 1 and all_aliases[0] == target_alias
    for col in _iter_where_columns(node):
        if (col.name or "").lower() not in HYPERTABLE_TIME_COLUMNS:
            continue
        col_alias = (col.table or "").lower()
        if col_alias:
            if col_alias != target_alias:
                continue
        elif not only_source:
            continue
        comparison = _ancestor_comparison(col)
        if comparison is None or not _is_genuine_bound(comparison, col):
            continue
        if _is_under(comparison, node):
            return True
    return False


def _is_unconditional_true(node: exp.Expression | None) -> bool:
    """Best-effort check for SQL expressions that are always TRUE.

    Catches a small grammar of literal tautologies:

    * ``TRUE`` / non-zero integer literal
    * Self-comparison on columns: ``hour = hour``, ``hour >= hour``, etc.
    * Literal-vs-literal binary comparisons evaluated at parse time:
      ``1 = 1``, ``2 > 1``, ``1 < 2``, ``1 <> 0``, ``1 >= 1``, etc.

    Does not attempt arbitrary expression evaluation — anything more
    complex falls through to ``False`` (validator will accept; defence
    relies on role-swap + statement_timeout as backstop).
    """
    if node is None:
        return False
    # Unwrap parentheses — `(TRUE)`, `((1=1))`, etc. should be equivalent
    # to the inner expression. sqlglot keeps Paren in the tree, so the
    # NOT case in _predicate_allows_unbounded_time wouldn't recognise
    # `NOT(TRUE)` as `NOT TRUE` without this.
    while isinstance(node, exp.Paren):
        node = node.this
        if node is None:
            return False
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Literal) and not node.is_string:
        # `WHERE 1` style — non-zero numeric literal alone is truthy.
        try:
            return float(node.name) != 0
        except ValueError:
            return False
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        left = node.this
        right = node.expression
        if left is None or right is None:
            return False
        # column compared to itself (EQ / GTE / LTE are tautologies)
        if (
            isinstance(node, (exp.EQ, exp.GTE, exp.LTE))
            and isinstance(left, exp.Column)
            and isinstance(right, exp.Column)
        ):
            return left.sql(dialect="postgres").lower() == right.sql(dialect="postgres").lower()
        if isinstance(left, exp.Literal) and isinstance(right, exp.Literal):
            return _literal_comparison_holds(node, left, right)
    return False


def _is_unconditional_false(node: exp.Expression | None) -> bool:
    """Best-effort check for SQL expressions that are always FALSE."""
    if node is None:
        return False
    while isinstance(node, exp.Paren):
        node = node.this
        if node is None:
            return False
    if isinstance(node, exp.Boolean):
        return not bool(node.this)
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            return float(node.name) == 0
        except ValueError:
            return False
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        left = node.this
        right = node.expression
        if left is None or right is None:
            return False
        if isinstance(left, exp.Literal) and isinstance(right, exp.Literal):
            return not _literal_comparison_holds(node, left, right)
    return False


def _literal_comparison_holds(op: exp.Expression, left: exp.Literal, right: exp.Literal) -> bool:
    """Evaluate a comparison between two literals at parse time.

    Both literals are coerced to floats when both are numeric strings;
    otherwise falls back to string comparison. Returns False on any
    coercion failure (we'd rather miss a tautology than wrongly reject).
    """
    try:
        if not left.is_string and not right.is_string:
            lv: float | str = float(left.name)
            rv: float | str = float(right.name)
        else:
            lv = str(left.name)
            rv = str(right.name)
    except (TypeError, ValueError):
        return False
    if isinstance(op, exp.EQ):
        return lv == rv
    if isinstance(op, exp.NEQ):
        return lv != rv
    if isinstance(op, exp.GT):
        return lv > rv  # type: ignore[operator]
    if isinstance(op, exp.GTE):
        return lv >= rv  # type: ignore[operator]
    if isinstance(op, exp.LT):
        return lv < rv  # type: ignore[operator]
    if isinstance(op, exp.LTE):
        return lv <= rv  # type: ignore[operator]
    return False
