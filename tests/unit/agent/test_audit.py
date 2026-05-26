"""Tests for :mod:`src.api.agent.audit`.

The audit writers are tiny — each is a single SQL statement — and the
goal of these tests is to pin (a) the exact SQL shape (so a future
schema change can't silently drop a column) and (b) the parameter
ordering and type coercion (so UUIDs reach asyncpg as the right
shape). Pool I/O is mocked end-to-end; no DB is required.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from src.api.agent.audit import (
    agent_runs_close,
    agent_runs_open,
    agent_runs_step,
    classify_failure,
    sql_audit_target_type,
    write_agent_query_audit,
)
from src.api.agent.auth_context import AuthContext
from src.api.agent.sql_executor import SqlExecutorError, SqlExecutorTimeoutError
from src.api.agent_workflows.runtime import ToolNotAllowedError
from src.api.agent_workflows.tools import ToolNotRegisteredError

pytestmark = pytest.mark.asyncio


def _auth(
    *,
    organization_id: str | None = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    role: str = "customer_admin",
    user_id: str = "11111111-1111-1111-1111-111111111111",
) -> AuthContext:
    """Build a minimal :class:`AuthContext` for audit tests."""
    return AuthContext(
        user_id=UUID(user_id),
        organization_id=UUID(organization_id) if organization_id else None,
        role=role,  # type: ignore[arg-type]
        visible_depot_ids=[],
    )


def _make_pool(*, fetchval_return: Any = None) -> Any:
    """Build a fake asyncpg pool whose ``acquire()`` yields a mock conn.

    The yielded connection exposes ``execute`` and ``fetchval`` as
    :class:`AsyncMock`. ``fetchval`` returns ``fetchval_return`` so
    tests can pin what the INSERT … RETURNING produced.
    """
    pool = MagicMock()
    conn = MagicMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=fetchval_return)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    pool._conn = conn  # surface the inner conn so tests can assert calls
    return pool


class TestAgentRunsOpen:
    async def test_inserts_one_row_and_returns_run_id(self):
        new_run_id = uuid4()
        pool = _make_pool(fetchval_return=str(new_run_id))
        auth = _auth()

        run_id = await agent_runs_open(pool, auth, "How much did John charge last month?")

        assert run_id == new_run_id
        # Exactly one INSERT.
        pool._conn.fetchval.assert_awaited_once()
        sql, *params = pool._conn.fetchval.await_args.args
        assert "INSERT INTO agent_runs" in sql
        assert "RETURNING run_id" in sql
        assert "'running'" in sql  # placeholder status hardcoded
        assert params == [
            str(auth.user_id),
            str(auth.organization_id),
            None,  # depot_id
            "How much did John charge last month?",
        ]

    async def test_organization_id_none_is_passed_through_for_favonius_admin(self):
        pool = _make_pool(fetchval_return=str(uuid4()))
        auth = _auth(organization_id=None, role="favonius_admin")

        await agent_runs_open(pool, auth, "test")

        params = pool._conn.fetchval.await_args.args[1:]
        # organization_id positional — second param after user_id, must be None.
        assert params[0] == str(auth.user_id)
        assert params[1] is None

    async def test_uuid_returned_from_db_is_rewrapped_as_uuid_object(self):
        # asyncpg returns UUID objects; we still want a UUID even if a
        # string slips through (e.g. mocked db, type coercion at boundary).
        new_run_id = uuid4()
        pool = _make_pool(fetchval_return=new_run_id)  # raw UUID, not str
        auth = _auth()

        run_id = await agent_runs_open(pool, auth, "test")

        assert isinstance(run_id, UUID)
        assert run_id == new_run_id


class TestAgentRunsStep:
    async def test_appends_step_to_steps_json(self):
        pool = _make_pool()
        run_id = uuid4()

        await agent_runs_step(pool, run_id, "extract_plan", {"intent": "consumption_by_user"})

        pool._conn.execute.assert_awaited_once()
        sql, blob, run_id_str = pool._conn.execute.await_args.args
        assert "UPDATE agent_runs" in sql
        assert "steps_json = steps_json || $1::jsonb" in sql
        assert "WHERE run_id = $2::uuid" in sql
        # The blob is a single-element array so the JSONB || operator
        # extends the trace rather than replacing it.
        parsed = json.loads(blob)
        assert parsed == [{"name": "extract_plan", "payload": {"intent": "consumption_by_user"}}]
        assert run_id_str == str(run_id)

    async def test_serializes_uuid_payload_via_default_str(self):
        pool = _make_pool()
        payload = {"driver_id": uuid4()}

        await agent_runs_step(pool, uuid4(), "resolve_entities", payload)

        blob = pool._conn.execute.await_args.args[1]
        parsed = json.loads(blob)
        # UUID becomes a string thanks to ``json.dumps(default=str)``.
        assert isinstance(parsed[0]["payload"]["driver_id"], str)

    async def test_multiple_calls_accumulate_via_jsonb_concat_operator(self):
        # Each call is one UPDATE; the test fixture's mock execute is
        # awaited each time, so we verify the SQL semantics (||) plus
        # the call count rather than the resulting array.
        pool = _make_pool()
        run_id = uuid4()

        await agent_runs_step(pool, run_id, "extract_plan", {})
        await agent_runs_step(pool, run_id, "compile", {"sql": "SELECT 1"})

        assert pool._conn.execute.await_count == 2
        for call in pool._conn.execute.await_args_list:
            sql = call.args[0]
            assert "steps_json = steps_json || $1::jsonb" in sql


class TestAgentRunsClose:
    async def test_sets_status_and_intent_and_duration(self):
        pool = _make_pool()
        run_id = uuid4()
        reply = {"intent": "consumption_by_user", "text": "..."}

        await agent_runs_close(pool, run_id, "success", reply)

        pool._conn.execute.assert_awaited_once()
        sql, *params = pool._conn.execute.await_args.args
        assert "UPDATE agent_runs" in sql
        assert "SET status" in sql
        assert "COALESCE($2, final_intent)" in sql
        # failure_reason is COALESCE-preserved (a None re-close can't erase a
        # recorded reason), same idempotency contract as duration_ms.
        assert "failure_reason = COALESCE($3, failure_reason)" in sql
        assert "duration_ms" in sql and "COALESCE" in sql
        # A success close passes failure_reason=None (param $3).
        assert params == ["success", "consumption_by_user", None, str(run_id)]

    async def test_failure_reason_is_written_as_param_three(self):
        pool = _make_pool()
        run_id = uuid4()
        reply = {"intent": "sql_general"}

        await agent_runs_close(pool, run_id, "error", reply, failure_reason="validator_rejected")

        sql, *params = pool._conn.execute.await_args.args
        assert "failure_reason = COALESCE($3, failure_reason)" in sql
        assert params == ["error", "sql_general", "validator_rejected", str(run_id)]

    async def test_extracts_intent_from_object_with_attribute(self):
        pool = _make_pool()
        reply = MagicMock()
        reply.intent = "consumption_by_user"

        await agent_runs_close(pool, uuid4(), "success", reply)

        params = pool._conn.execute.await_args.args[1:]
        assert params[0] == "success"
        assert params[1] == "consumption_by_user"

    async def test_none_reply_passes_none_for_final_intent(self):
        pool = _make_pool()

        await agent_runs_close(pool, uuid4(), "error", None)

        params = pool._conn.execute.await_args.args[1:]
        assert params[0] == "error"
        # COALESCE keeps the existing final_intent when None is passed.
        assert params[1] is None

    async def test_idempotent_repeated_close_executes_safely(self):
        pool = _make_pool()
        run_id = uuid4()
        reply = {"intent": "consumption_by_user"}

        await agent_runs_close(pool, run_id, "success", reply)
        await agent_runs_close(pool, run_id, "success", reply)

        # Both calls run; the COALESCE on duration_ms in SQL guarantees
        # the second call does not reset the captured duration.
        assert pool._conn.execute.await_count == 2


class TestWriteAgentQueryAudit:
    async def test_writes_one_audit_log_row_with_agent_query_action(self):
        pool = _make_pool()
        auth = _auth()
        run_id = uuid4()

        with patch("src.api.agent.audit.write_admin_audit_row", new=AsyncMock()) as mock_writer:
            await write_agent_query_audit(
                pool,
                auth,
                run_id,
                intent="consumption_by_user",
                row_count=23,
            )

        mock_writer.assert_awaited_once()
        passed_pool, passed_row = mock_writer.await_args.args
        assert passed_pool is pool
        assert passed_row.action == "agent.query"
        assert passed_row.target_type == "charging_sessions"
        assert passed_row.target_id == str(run_id)
        assert passed_row.actor_user_id == str(auth.user_id)
        assert passed_row.actor_role == "customer_admin"
        assert passed_row.organization_id == str(auth.organization_id)

    async def test_metadata_carries_intent_and_row_count_only(self):
        pool = _make_pool()
        auth = _auth()
        run_id = uuid4()

        with patch("src.api.agent.audit.write_admin_audit_row", new=AsyncMock()) as mock_writer:
            await write_agent_query_audit(
                pool,
                auth,
                run_id,
                intent="consumption_by_user",
                row_count=147,
            )

        passed_row = mock_writer.await_args.args[1]
        assert passed_row.metadata == {"intent": "consumption_by_user", "row_count": 147}

    async def test_optional_depot_id_is_stringified_when_supplied(self):
        pool = _make_pool()
        auth = _auth()
        depot_id = uuid4()

        with patch("src.api.agent.audit.write_admin_audit_row", new=AsyncMock()) as mock_writer:
            await write_agent_query_audit(
                pool,
                auth,
                uuid4(),
                intent="consumption_by_user",
                row_count=1,
                depot_id=depot_id,
            )

        passed_row = mock_writer.await_args.args[1]
        assert passed_row.depot_id == str(depot_id)

    async def test_default_depot_id_is_none_for_multi_depot_queries(self):
        pool = _make_pool()
        auth = _auth()

        with patch("src.api.agent.audit.write_admin_audit_row", new=AsyncMock()) as mock_writer:
            await write_agent_query_audit(
                pool,
                auth,
                uuid4(),
                intent="consumption_by_user",
                row_count=0,
            )

        passed_row = mock_writer.await_args.args[1]
        assert passed_row.depot_id is None

    async def test_favonius_admin_writes_null_organization_id(self):
        pool = _make_pool()
        auth = _auth(organization_id=None, role="favonius_admin")

        with patch("src.api.agent.audit.write_admin_audit_row", new=AsyncMock()) as mock_writer:
            await write_agent_query_audit(
                pool,
                auth,
                uuid4(),
                intent="consumption_by_user",
                row_count=5,
            )

        passed_row = mock_writer.await_args.args[1]
        assert passed_row.organization_id is None

    async def test_sql_general_target_type_reflects_functions_accessed(self):
        pool = _make_pool()
        auth = _auth()
        run_id = uuid4()

        with patch("src.api.agent.audit.write_admin_audit_row", new=AsyncMock()) as mock_writer:
            await write_agent_query_audit(
                pool,
                auth,
                run_id,
                intent="sql_general",
                row_count=12,
                target_type=sql_audit_target_type(["optimization_runs", "alerts"]),
                functions_accessed=["optimization_runs", "alerts"],
            )

        passed_row = mock_writer.await_args.args[1]
        assert passed_row.target_type == "optimization_runs,alerts"
        assert passed_row.metadata == {
            "intent": "sql_general",
            "row_count": 12,
            "functions_accessed": ["optimization_runs", "alerts"],
        }


# ── classify_failure (S3.5 taxonomy) ─────────────────────────────────────────


def _tc(name: str, *, ok: bool, error_kind: str | None = None, row_count: int | None = None) -> Any:
    """A run-time-shaped ToolCall stand-in (duck-typed by classify_failure)."""
    result: dict[str, Any] = {}
    if error_kind is not None:
        result["error_kind"] = error_kind
    if row_count is not None:
        result["row_count"] = row_count
    return SimpleNamespace(name=name, ok=ok, result=result, arguments={}, error=error_kind)


def _qa(
    *,
    status: str = "success",
    tool_calls: list | None = None,
    empty_result: bool = False,
    text: str | None = None,
) -> Any:
    """A QAResult-shaped terminal object (duck-typed by classify_failure).

    ``text`` defaults to ``None`` (attribute present but unset) so the
    classifier's missing-text branch is exercised by the existing success
    cases; pass an explicit string to test the blank-terminator path.
    """
    return SimpleNamespace(
        status=status, tool_calls=tool_calls or [], empty_result=empty_result, text=text
    )


class BudgetExceededError(Exception):
    """Forward-declared S4 exception; classify_failure matches it by name.

    The name must match exactly what budget.py (S4) will raise — that is the
    whole point of the name-based hook in classify_failure.
    """


class LLMExtractionError(RuntimeError):
    """Stand-in for ``src.api.agent.llm.LLMExtractionError``.

    classify_failure matches it by class name so ``audit.py`` need not import
    ``src.api.agent.llm`` (which loads the Anthropic SDK at module import). The
    real class is a ``RuntimeError`` subclass; this local mirror lets the audit
    unit tests run in an llm-free environment, which is the whole point of the
    name-based hook.
    """


class TestClassifyFailure:
    """One assertion per failure_reason value showing the mapping works."""

    async def test_validator_rejected_from_failed_run_select_kind(self):
        # The §S3.5 done-when case: a validator rejection the model never
        # recovered from (ran out of iterations) classifies as validator_rejected.
        qa = _qa(
            status="max_iterations",
            tool_calls=[_tc("run_select_ts", ok=False, error_kind="parse_error")],
        )
        assert classify_failure(qa) == "validator_rejected"

    async def test_executor_timeout_from_exception(self):
        assert classify_failure(SqlExecutorTimeoutError("statement_timeout fired")) == (
            "executor_timeout"
        )

    async def test_executor_timeout_from_run_select_kind(self):
        qa = _qa(
            status="max_iterations",
            tool_calls=[_tc("run_select_ts", ok=False, error_kind="timeout")],
        )
        assert classify_failure(qa) == "executor_timeout"

    async def test_empty_result_flag_takes_precedence_over_success(self):
        qa = _qa(
            status="success",
            tool_calls=[_tc("run_select_ts", ok=True, row_count=0)],
            empty_result=True,
        )
        assert classify_failure(qa) == "empty_result"

    async def test_budget_exceeded_matched_by_class_name(self):
        assert classify_failure(BudgetExceededError("over monthly ceiling")) == "budget_exceeded"

    async def test_tool_error_from_disallowed_tool(self):
        assert classify_failure(ToolNotAllowedError("nope")) == "tool_error"

    async def test_tool_error_from_unregistered_tool(self):
        assert classify_failure(ToolNotRegisteredError("missing")) == "tool_error"

    async def test_tool_error_from_executor_role_error_kind(self):
        qa = _qa(
            status="max_iterations",
            tool_calls=[_tc("run_select_static", ok=False, error_kind="role_error")],
        )
        assert classify_failure(qa) == "tool_error"

    async def test_tool_error_from_generic_sql_executor_error(self):
        assert classify_failure(SqlExecutorError("boom")) == "tool_error"

    async def test_llm_error_from_anthropic_sdk_exception(self):
        # Match by module so audit.py needn't import the SDK.
        fake = type("APIStatusError", (Exception,), {})
        fake.__module__ = "anthropic"
        assert classify_failure(fake("rate limited")) == "llm_error"

    async def test_llm_error_from_model_giving_up(self):
        qa = _qa(status="no_terminator", tool_calls=[])
        assert classify_failure(qa) == "llm_error"

    async def test_other_from_unattributable_exception(self):
        assert classify_failure(ValueError("unexpected")) == "other"

    async def test_other_from_bare_error_status_token(self):
        assert classify_failure("error") == "other"

    async def test_none_for_no_signal(self):
        assert classify_failure(None) is None

    async def test_none_for_successful_qa_result(self):
        qa = _qa(status="success", tool_calls=[_tc("run_select_ts", ok=True, row_count=5)])
        assert classify_failure(qa) is None

    async def test_none_for_graceful_status_tokens(self):
        for token in ("success", "running", "disambiguation", "not_found"):
            assert classify_failure(token) is None

    async def test_recovered_validator_rejection_on_success_is_not_a_failure(self):
        # A rejection the model recovered from (final status success) records
        # no failure_reason — success wins over a stale earlier tool error.
        qa = _qa(
            status="success",
            tool_calls=[
                _tc("run_select_ts", ok=False, error_kind="parse_error"),
                _tc("run_select_ts", ok=True, row_count=3),
            ],
        )
        assert classify_failure(qa) is None

    async def test_terminator_failed_is_tool_error_despite_stale_run_select(self):
        # A turn that hit a validator rejection mid-way, recovered, then failed
        # to emit a valid final answer is a terminator (tool_error) failure —
        # the stale run_select rejection must not shadow the terminal cause.
        qa = _qa(
            status="terminator_failed",
            tool_calls=[_tc("run_select_ts", ok=False, error_kind="parse_error")],
        )
        assert classify_failure(qa) == "tool_error"

    async def test_terminator_failed_is_tool_error_with_no_tool_calls(self):
        assert classify_failure(_qa(status="terminator_failed", tool_calls=[])) == "tool_error"

    async def test_validator_rejected_from_failed_sample_values(self):
        # sample_values shares the validate→execute path, so an unrecovered
        # rejection there is attributable too (not a fall-through to llm_error).
        qa = _qa(
            status="max_iterations",
            tool_calls=[_tc("sample_values", ok=False, error_kind="parse_error")],
        )
        assert classify_failure(qa) == "validator_rejected"

    async def test_executor_timeout_from_failed_sample_values(self):
        qa = _qa(
            status="max_iterations",
            tool_calls=[_tc("sample_values", ok=False, error_kind="timeout")],
        )
        assert classify_failure(qa) == "executor_timeout"

    async def test_llm_error_from_extraction_error(self):
        # The consumption path's extractor failure is an LLM-origin error, not
        # an unattributable "other". Matched by class name so audit.py need not
        # import the (SDK-pulling) llm module.
        assert classify_failure(LLMExtractionError("no tool call on retry")) == "llm_error"

    async def test_blank_terminator_success_is_llm_error(self):
        # run_qa_turn can report success with empty text (emit_final_answer has
        # no min length); the controller falls back to not_found. The turn
        # produced no usable answer → llm_error, not a NULL failure_reason.
        qa = _qa(
            status="success",
            text="   ",
            tool_calls=[_tc("run_select_ts", ok=True, row_count=4)],
        )
        assert classify_failure(qa) == "llm_error"

    async def test_nonblank_terminator_success_is_not_a_failure(self):
        qa = _qa(
            status="success",
            text="John charged 412 kWh last month.",
            tool_calls=[_tc("run_select_ts", ok=True, row_count=4)],
        )
        assert classify_failure(qa) is None

    async def test_tool_error_from_failed_sql_tool_without_error_kind(self):
        # sample_values can fail with only an ``error`` envelope (invalid n,
        # unknown table) and no error_kind. A turn that dies after such a
        # failure is a tool_error, not a fall-through to llm_error.
        qa = _qa(
            status="max_iterations",
            tool_calls=[_tc("sample_values", ok=False, error_kind=None)],
        )
        assert classify_failure(qa) == "tool_error"

    async def test_tool_error_from_failed_non_sql_tool(self):
        # A failed non-SQL tool (e.g. lookup_entity) the model never recovered
        # from is a tool-path failure even though the terminal status is the
        # generic "model gave up" marker.
        qa = _qa(
            status="max_iterations",
            tool_calls=[_tc("lookup_entity", ok=False)],
        )
        assert classify_failure(qa) == "tool_error"

    async def test_llm_error_when_model_gives_up_with_no_failed_tools(self):
        # Guardrail for the _any_tool_failed fallback: a clean run that simply
        # exhausted iterations (no failed tool) stays llm_error.
        qa = _qa(
            status="max_iterations",
            tool_calls=[_tc("run_select_ts", ok=True, row_count=2)],
        )
        assert classify_failure(qa) == "llm_error"

    async def test_audit_module_does_not_import_anthropic_sdk(self):
        # Regression guard (Codex P2): audit.py must stay importable without the
        # Anthropic SDK. It classifies LLMExtractionError / SDK errors by name,
        # so it must not transitively import ``anthropic`` at module load.
        import sys

        assert "src.api.agent.audit" in sys.modules
        audit_mod = sys.modules["src.api.agent.audit"]
        # The module's own globals must not hold a direct handle to the SDK or
        # the llm module (the two import paths that pull anthropic in).
        assert "anthropic" not in vars(audit_mod)
        assert "LLMExtractionError" not in vars(audit_mod)
