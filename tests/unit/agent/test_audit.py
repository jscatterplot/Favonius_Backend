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
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from src.api.agent.audit import (
    agent_runs_close,
    agent_runs_open,
    agent_runs_step,
    sql_audit_target_type,
    write_agent_query_audit,
)
from src.api.agent.auth_context import AuthContext

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
        assert "final_intent = COALESCE($2, final_intent)" in sql
        # Duration is COALESCE'd so a second close() doesn't reset it.
        assert "duration_ms  = COALESCE" in sql
        assert params == ["success", "consumption_by_user", str(run_id)]

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
