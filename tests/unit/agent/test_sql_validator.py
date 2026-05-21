"""Unit tests for the depot chat agent's SQL validator.

The validator is the security gate between the LLM and the read-only
Postgres role — bugs here are tenant-isolation failures. Coverage target:
≥ 95% on ``src/api/agent/sql_validator.py``.

Cases are grouped by error_kind so a regression points at the failing
rule. Each rule has at least one ACCEPT and one REJECT case.
"""

from __future__ import annotations

import pytest

from src.api.agent.sql_validator import (
    AGENT_VIEWS_FUNCTIONS_STATIC,
    AGENT_VIEWS_FUNCTIONS_TS,
    DEFAULT_ROW_LIMIT,
    MAX_ROW_LIMIT,
    ValidationResult,
    validate_sql,
)


TS = AGENT_VIEWS_FUNCTIONS_TS
STATIC = AGENT_VIEWS_FUNCTIONS_STATIC


def _ok(sql: str, *, allowed=TS, row_limit=DEFAULT_ROW_LIMIT) -> ValidationResult:
    r = validate_sql(sql, allowed_functions=allowed, row_limit=row_limit)
    assert r.ok, f"expected accept but got {r.error_kind}: {r.error}"
    return r


def _rej(sql: str, *, kind: str, allowed=TS) -> ValidationResult:
    r = validate_sql(sql, allowed_functions=allowed)
    assert not r.ok, f"expected reject ({kind}) but got accept; canonical={r.sql!r}"
    assert r.error_kind == kind, (
        f"expected error_kind={kind!r}, got {r.error_kind!r}; detail: {r.error}"
    )
    return r


# ── Happy paths ─────────────────────────────────────────────────────────


class TestHappyPaths:
    def test_minimal_select(self):
        r = _ok("SELECT * FROM agent_views.sessions($1)")
        assert r.functions_used == ("sessions",)
        assert " LIMIT 500" in r.sql.upper()

    def test_with_filter_and_groupby(self):
        r = _ok(
            "SELECT depot_id, SUM(energy_kwh) FROM agent_views.sessions($1) "
            "WHERE start_time >= now() - interval '7 days' "
            "GROUP BY depot_id"
        )
        assert r.functions_used == ("sessions",)

    def test_static_pool_allowed(self):
        r = _ok(
            "SELECT depot_id, name FROM agent_views.depots($1)",
            allowed=STATIC,
        )
        assert r.functions_used == ("depots",)

    def test_hypertable_with_time_predicate(self):
        r = _ok(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour >= now() - interval '7 days'"
        )
        assert r.functions_used == ("prices_hourly",)

    def test_join_two_agent_views_functions(self):
        # Self-join on the same pool is allowed.
        r = _ok(
            "SELECT s.depot_id, COUNT(*) "
            "FROM agent_views.sessions($1) s "
            "JOIN agent_views.alerts($1) a ON a.depot_id = s.depot_id "
            "GROUP BY s.depot_id"
        )
        assert set(r.functions_used) == {"sessions", "alerts"}


# ── Single-statement / parse rules ──────────────────────────────────────


class TestStatementCount:
    def test_multi_statement_semicolon(self):
        _rej("SELECT 1; SELECT 2", kind="multi_statement")

    def test_unparseable(self):
        _rej("SELECT FROM WHERE", kind="parse_error")

    def test_empty(self):
        _rej("", kind="empty_sql")

    def test_too_long(self):
        long = "SELECT 1, " + ("'x', " * 2000) + "1 FROM agent_views.sessions($1)"
        _rej(long, kind="too_long")


# ── Root must be SELECT / SELECT-set-op ──────────────────────────────────


class TestRootIsSelect:
    def test_insert_rejected(self):
        _rej("INSERT INTO public.x VALUES (1)", kind="non_select")

    def test_update_rejected(self):
        _rej("UPDATE public.x SET y = 1", kind="non_select")

    def test_delete_rejected(self):
        _rej("DELETE FROM public.x", kind="non_select")

    def test_drop_rejected(self):
        _rej("DROP TABLE public.x", kind="non_select")

    def test_truncate_rejected(self):
        _rej("TRUNCATE TABLE public.x", kind="non_select")

    def test_alter_rejected(self):
        _rej("ALTER TABLE public.x ADD COLUMN z text", kind="non_select")


# ── DML in CTE (the classic bypass) ──────────────────────────────────────


class TestDmlInCte:
    def test_delete_in_cte(self):
        _rej(
            "WITH d AS (DELETE FROM x RETURNING *) SELECT * FROM d",
            kind="non_select",
        )

    def test_insert_in_cte(self):
        _rej(
            "WITH i AS (INSERT INTO x VALUES (1) RETURNING *) "
            "SELECT * FROM agent_views.sessions($1)",
            kind="non_select",
        )

    def test_update_in_cte(self):
        _rej(
            "WITH u AS (UPDATE x SET y = 1 RETURNING *) SELECT 1",
            kind="non_select",
        )


# ── Schema allowlist ─────────────────────────────────────────────────────


class TestSchemaAllowlist:
    def test_public_schema_rejected(self):
        _rej("SELECT * FROM public.charging_sessions", kind="table_not_allowed")

    def test_pg_catalog_rejected(self):
        _rej("SELECT * FROM pg_catalog.pg_class", kind="forbidden_schema")

    def test_information_schema_rejected(self):
        _rej(
            "SELECT * FROM information_schema.tables",
            kind="forbidden_schema",
        )

    def test_auth_schema_rejected(self):
        _rej("SELECT * FROM auth.users", kind="forbidden_schema")

    def test_storage_schema_rejected(self):
        _rej("SELECT * FROM storage.objects", kind="forbidden_schema")

    def test_no_schema_at_all(self):
        _rej("SELECT * FROM sessions", kind="table_not_allowed")


# ── UNION-style evasion ──────────────────────────────────────────────────


class TestUnionEvasion:
    def test_union_with_forbidden(self):
        _rej(
            "SELECT depot_id FROM agent_views.sessions($1) "
            "UNION SELECT id FROM public.audit_log",
            kind="table_not_allowed",
        )

    def test_intersect_with_forbidden(self):
        _rej(
            "SELECT depot_id FROM agent_views.sessions($1) "
            "INTERSECT SELECT id FROM public.audit_log",
            kind="table_not_allowed",
        )

    def test_union_of_two_allowed(self):
        _ok(
            "SELECT depot_id FROM agent_views.sessions($1) "
            "UNION SELECT depot_id FROM agent_views.alerts($1)"
        )

    def test_no_data_source_at_all(self):
        # Pure constants — no agent_views reference.
        _rej("SELECT 1 UNION SELECT 2", kind="no_data_source")


# ── Function-argument enforcement (the S1 keystone) ─────────────────────


class TestFunctionArguments:
    def test_no_args_rejected(self):
        _rej(
            "SELECT * FROM agent_views.sessions()",
            kind="bad_function_argument",
        )

    def test_literal_uuid_array_rejected(self):
        _rej(
            "SELECT * FROM agent_views.sessions("
            "ARRAY['00000000-0000-0000-0000-000000000000'::uuid])",
            kind="bad_function_argument",
        )

    def test_wrong_placeholder_index_rejected(self):
        _rej(
            "SELECT * FROM agent_views.sessions($2)",
            kind="bad_function_argument",
        )

    def test_too_many_args_rejected(self):
        _rej(
            "SELECT * FROM agent_views.sessions($1, $2)",
            kind="bad_function_argument",
        )


# ── Function name allowlist ──────────────────────────────────────────────


class TestFunctionAllowlist:
    def test_unknown_function_in_ts_pool(self):
        _rej(
            "SELECT * FROM agent_views.depots($1)",
            kind="table_not_allowed",
            allowed=TS,  # depots lives on static, not ts
        )

    def test_unknown_function_in_static_pool(self):
        _rej(
            "SELECT * FROM agent_views.sessions($1)",
            kind="table_not_allowed",
            allowed=STATIC,
        )

    def test_case_insensitive_function_name(self):
        # Postgres folds unquoted identifiers to lower; validator must too.
        _ok("SELECT * FROM agent_views.SESSIONS($1)")
        _ok("SELECT * FROM agent_views.Sessions($1)")


# ── Dangerous functions ──────────────────────────────────────────────────


class TestDangerousFunctions:
    def test_pg_read_file_rejected(self):
        _rej(
            "SELECT pg_read_file('/etc/passwd') FROM agent_views.sessions($1)",
            kind="dangerous_fn",
        )

    def test_pg_sleep_rejected(self):
        _rej(
            "SELECT pg_sleep(100) FROM agent_views.sessions($1)",
            kind="dangerous_fn",
        )

    def test_set_config_rejected(self):
        _rej(
            "SELECT set_config('role', 'postgres', false) "
            "FROM agent_views.sessions($1)",
            kind="dangerous_fn",
        )

    def test_dblink_rejected(self):
        _rej(
            "SELECT dblink('host=evil', 'SELECT 1') FROM agent_views.sessions($1)",
            kind="dangerous_fn",
        )

    def test_lo_import_rejected(self):
        _rej(
            "SELECT lo_import('/etc/passwd') FROM agent_views.sessions($1)",
            kind="dangerous_fn",
        )


# ── Time-predicate enforcement on hypertable functions ──────────────────


class TestTimePredicate:
    def test_prices_without_where_rejected(self):
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1)",
            kind="missing_time_filter",
        )

    def test_prices_with_unrelated_where_rejected(self):
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1) WHERE depot_id IS NOT NULL",
            kind="missing_time_filter",
        )

    def test_building_load_without_time_rejected(self):
        _rej(
            "SELECT * FROM agent_views.building_load_hourly($1)",
            kind="missing_time_filter",
        )

    def test_building_load_with_between_accepted(self):
        _ok(
            "SELECT * FROM agent_views.building_load_hourly($1) "
            "WHERE hour BETWEEN now() - interval '1 day' AND now()"
        )

    def test_prices_with_hour_gte_accepted(self):
        _ok(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour >= now() - interval '7 days'"
        )

    def test_sessions_does_not_require_time(self):
        # sessions is not in HYPERTABLE_FUNCTIONS
        _ok("SELECT * FROM agent_views.sessions($1)")

    def test_tautology_hour_eq_hour_rejected(self):
        # `WHERE hour = hour` is a column-column comparison; it does not
        # bound the time range.
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1) WHERE hour = hour",
            kind="missing_time_filter",
        )

    def test_union_unbounded_branch_rejected(self):
        # Bounded first branch + unbounded second branch over a hypertable
        # MUST be rejected — each branch is checked independently.
        _rej(
            "SELECT hour FROM agent_views.prices_hourly($1) "
            "WHERE hour >= now() - interval '7 days' "
            "UNION "
            "SELECT hour FROM agent_views.prices_hourly($1)",
            kind="missing_time_filter",
        )

    def test_union_both_bounded_accepted(self):
        _ok(
            "SELECT hour FROM agent_views.prices_hourly($1) "
            "WHERE hour >= now() - interval '1 day' "
            "UNION "
            "SELECT hour FROM agent_views.prices_hourly($1) "
            "WHERE hour < now() - interval '7 days'"
        )

    def test_or_true_bypass_rejected(self):
        # `WHERE hour >= ... OR 1=1` — the OR-TRUE neutralises the bound.
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour >= now() - interval '7 days' OR 1=1",
            kind="missing_time_filter",
        )

    def test_or_boolean_true_bypass_rejected(self):
        _rej(
            "SELECT * FROM agent_views.building_load_hourly($1) "
            "WHERE hour >= now() - interval '7 days' OR TRUE",
            kind="missing_time_filter",
        )

    def test_self_ref_arithmetic_tautology_rejected(self):
        # `hour >= hour - interval '1 day'` — time column on both sides.
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour >= hour - interval '1 day'",
            kind="missing_time_filter",
        )

    def test_self_ref_function_wrap_tautology_rejected(self):
        # `date_trunc('day', hour) = hour` — same column, both sides.
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE date_trunc('day', hour) = hour",
            kind="missing_time_filter",
        )

    def test_and_combined_predicates_accepted(self):
        # AND-combining the bound with another predicate is fine.
        _ok(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour >= now() - interval '1 day' AND depot_id IS NOT NULL"
        )


class TestOffset:
    def test_offset_nonzero_rejected(self):
        _rej(
            "SELECT * FROM agent_views.sessions($1) OFFSET 1000",
            kind="offset_not_allowed",
        )

    def test_offset_zero_accepted(self):
        # OFFSET 0 is benign; LIMIT still injected.
        _ok("SELECT * FROM agent_views.sessions($1) OFFSET 0")

    def test_offset_huge_rejected(self):
        _rej(
            "SELECT * FROM agent_views.sessions($1) OFFSET 1000000000",
            kind="offset_not_allowed",
        )

# ── LIMIT injection / cap ────────────────────────────────────────────────


class TestLimitHandling:
    def test_limit_injected_when_missing(self):
        r = _ok("SELECT * FROM agent_views.sessions($1)")
        assert " LIMIT 500" in r.sql.upper()

    def test_limit_capped_when_over(self):
        r = _ok(
            "SELECT * FROM agent_views.sessions($1) LIMIT 99999"
        )
        # canonical form must include the capped value
        assert " LIMIT 500" in r.sql.upper()
        assert "99999" not in r.sql

    def test_limit_preserved_when_under(self):
        r = _ok("SELECT * FROM agent_views.sessions($1) LIMIT 10")
        assert " LIMIT 10" in r.sql.upper()

    def test_non_literal_limit_rejected(self):
        _rej(
            "SELECT * FROM agent_views.sessions($1) LIMIT $1",
            kind="non_literal_limit",
        )

    def test_limit_zero_capped_to_default(self):
        # Treat LIMIT 0 as adversarial — coerce to default.
        r = _ok("SELECT * FROM agent_views.sessions($1) LIMIT 0")
        assert " LIMIT 500" in r.sql.upper()

    def test_custom_row_limit(self):
        r = _ok(
            "SELECT * FROM agent_views.sessions($1)",
            row_limit=50,
        )
        assert " LIMIT 50" in r.sql.upper()

    def test_row_limit_exceeding_max_is_clamped(self):
        # caller cannot ask for more rows than the absolute cap
        r = _ok(
            "SELECT * FROM agent_views.sessions($1)",
            row_limit=99999,
        )
        assert f" LIMIT {MAX_ROW_LIMIT}" in r.sql.upper()


# ── Canonical SQL preserves $1 ──────────────────────────────────────────


class TestCanonicalOutput:
    def test_dollar_placeholder_preserved(self):
        r = _ok("SELECT * FROM agent_views.sessions($1)")
        # canonical SQL must still pass through asyncpg's $N binding
        assert "$1" in r.sql

    def test_canonical_for_hypertable_keeps_predicate(self):
        r = _ok(
            "SELECT * FROM agent_views.prices_hourly($1) WHERE hour >= now()"
        )
        assert "hour" in r.sql.lower()
        assert "$1" in r.sql


# ── Latest review-feedback round ────────────────────────────────────────


class TestBindPlaceholderEnforcement:
    """Only $1 is bound by the executor; any $N for N != 1 must reject."""

    def test_dollar_two_in_where_rejected(self):
        _rej(
            "SELECT * FROM agent_views.sessions($1) WHERE depot_id = $2",
            kind="bad_placeholder",
        )

    def test_dollar_three_anywhere_rejected(self):
        _rej(
            "SELECT * FROM agent_views.sessions($1) "
            "WHERE start_time >= $3::timestamptz",
            kind="bad_placeholder",
        )


class TestSchemaQualifiedFunctionCalls:
    """Dot(Identifier, Anonymous) targets — auth.uid() etc. — must reject.

    The table-node check at step 4 covers FROM auth.x but NOT bare
    `SELECT auth.uid()` calls (sqlglot represents those as a Dot node,
    not a Table). Without this guard the role-swap is the only defence.
    """

    def test_auth_uid_call_rejected(self):
        _rej(
            "SELECT auth.uid() FROM agent_views.sessions($1)",
            kind="forbidden_schema",
        )

    def test_storage_function_call_rejected(self):
        _rej(
            "SELECT storage.objects() FROM agent_views.sessions($1)",
            kind="forbidden_schema",
        )

    def test_vault_function_call_rejected(self):
        _rej(
            "SELECT vault.decrypt('x') FROM agent_views.sessions($1)",
            kind="forbidden_schema",
        )

    def test_pg_catalog_dot_function_rejected(self):
        _rej(
            "SELECT pg_catalog.pg_read_file('x') FROM agent_views.sessions($1)",
            kind="forbidden_schema",
        )


class TestSelfReferentialBetweenInRejected:
    """Codex P1 — `_is_genuine_bound` must reject self-references in
    BETWEEN/IN, otherwise the time-predicate guard is trivially bypassed.
    """

    def test_between_self_ref_low_rejected(self):
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour BETWEEN hour - interval '1 day' AND hour + interval '1 day'",
            kind="missing_time_filter",
        )

    def test_between_normal_bounds_accepted(self):
        _ok(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour BETWEEN now() - interval '7 days' AND now()"
        )

    def test_in_self_ref_rejected(self):
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1) WHERE hour IN (hour)",
            kind="missing_time_filter",
        )

    def test_in_with_literals_accepted(self):
        # `hour IN (timestamptz '...')` is a finite list — bound.
        _ok(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour IN (timestamptz '2026-05-01 00:00:00')"
        )

    def test_in_subquery_rejected(self):
        # Sub-SELECT in IN cannot be statically shown to bound the scan.
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour IN (SELECT hour FROM agent_views.prices_hourly($1))",
            kind="missing_time_filter",
        )


class TestBroaderUnconditionalTrueDetection:
    """Codex P1 — the OR-bypass detector must catch non-EQ tautologies."""

    def test_or_two_gt_one_rejected(self):
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour >= now() - interval '7 days' OR 2 > 1",
            kind="missing_time_filter",
        )

    def test_or_one_lt_two_rejected(self):
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour >= now() - interval '7 days' OR 1 < 2",
            kind="missing_time_filter",
        )

    def test_or_one_lte_one_rejected(self):
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour >= now() - interval '7 days' OR 1 <= 1",
            kind="missing_time_filter",
        )

    def test_or_one_neq_two_rejected(self):
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour >= now() - interval '7 days' OR 1 <> 2",
            kind="missing_time_filter",
        )


class TestNestedAndOrBypass:
    """Bugbot — inverted _is_under args bypass detection when the
    bounding comparison is nested inside an AND inside the OR branch.
    """

    def test_nested_and_with_or_true_rejected(self):
        _rej(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE (hour >= now() - interval '7 days' AND depot_id IS NOT NULL) "
            "OR 1=1",
            kind="missing_time_filter",
        )

    def test_and_combined_or_true_in_sibling_accepted(self):
        # OR-TRUE is ANDed against the bound, not ORed against it — the
        # time predicate is still independently enforced.
        _ok(
            "SELECT * FROM agent_views.prices_hourly($1) "
            "WHERE hour >= now() - interval '7 days' AND (status = 'x' OR 1=1)"
        )


class TestSubqueryRowCapNotInjected:
    """Codex P1 — LIMIT must only be injected on the top-level branches,
    not on subqueries nested in IN/EXISTS/scalar contexts.
    """

    def test_in_subquery_not_capped(self):
        r = _ok(
            "SELECT * FROM agent_views.sessions($1) "
            "WHERE driver_id IN (SELECT driver_id FROM agent_views.drivers($1))",
            allowed=TS | STATIC,
        )
        # The inner SELECT must NOT have its own LIMIT injected; only the
        # outer one. Verify by checking that exactly one LIMIT appears in
        # the canonical SQL (the outer one).
        assert r.sql.upper().count("LIMIT") == 1

    def test_union_branches_still_capped(self):
        # UNION branches DO get capped — they're top-level result sources.
        r = _ok(
            "SELECT depot_id FROM agent_views.sessions($1) "
            "UNION ALL "
            "SELECT depot_id FROM agent_views.optimization_runs($1)"
        )
        # Two branch LIMITs + one outer LIMIT = three.
        assert r.sql.upper().count("LIMIT") >= 2
