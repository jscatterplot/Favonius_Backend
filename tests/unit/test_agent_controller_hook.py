"""The chat controller must invoke the automation-suggestion hook after a
successful consumption turn — and only then.

Drives ``run_turn`` with its heavy collaborators mocked (no DB, no LLM, no
network) so we can assert the single wiring contract: the hook is awaited once
on the success path and not at all on a refuse path. The hook's own behaviour
(flag-gating, best-effort, timeout) is covered in
``test_automation_suggestions_emit.py``.
"""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from src.api.agent import controller as ctrl
from src.api.agent.auth_context import ResolvedEntity, ResolvedTimeWindow
from src.api.agent.plan import EntityMention, QueryPlan, TimeWindow


class _FakeLLM:
    async def extract_plan(self, message, *, two_model_enabled=False):
        return QueryPlan(
            intent="consumption_by_user",
            subjects=[EntityMention(kind="driver", text="John")],
            time_window=TimeWindow(kind="relative", relative="last_week"),
        )

    async def format_answer(
        self, plan, resolved, window, rows, *, result_summary=None, two_model_enabled=False
    ):
        return "John used 42 kWh last week."


def _fake_auth():
    return SimpleNamespace(
        user_id=uuid4(),
        organization_id=uuid4(),
        role="customer_admin",
        visible_depot_ids=[uuid4()],
    )


def _patches(stack: ExitStack, *, auth, hook: AsyncMock):
    """Patch every collaborator run_turn touches up to the success return."""
    p = lambda name, **kw: stack.enter_context(patch.object(ctrl, name, **kw))  # noqa: E731

    p("build_auth_context", new=AsyncMock(return_value=auth))
    p("agent_runs_open", new=AsyncMock(return_value=uuid4()))
    p("agent_runs_step", new=AsyncMock())
    p("agent_runs_close", new=AsyncMock())
    p("write_agent_query_audit", new=AsyncMock())
    p("_emit_answer_safe", new=AsyncMock())
    p("is_sql_mode_enabled", new=MagicMock(return_value=False))
    p("resolve_org_two_model_enabled", new=AsyncMock(return_value=False))
    p("configured_default_model", new=MagicMock(return_value="m"))
    p("pick_model", new=MagicMock(return_value="m"))
    p(
        "resolve_entities",
        new=AsyncMock(
            return_value=[
                ResolvedEntity(
                    kind="driver", display="John", primary_id=uuid4(), card_ids=[], candidates=[]
                )
            ]
        ),
    )
    p("load_depot_timezones", new=AsyncMock(return_value={}))
    p(
        "resolve_time_window",
        new=MagicMock(
            return_value=ResolvedTimeWindow(
                start_utc=datetime(2026, 5, 18, tzinfo=timezone.utc),
                end_utc=datetime(2026, 5, 25, tzinfo=timezone.utc),
                timezone="UTC",
            )
        ),
    )
    p("compile_consumption_by_user", new=MagicMock(return_value=("SELECT 1", [])))
    p("summarize_consumption_rows", new=MagicMock(return_value={}))
    p("maybe_emit_automation_suggestion", new=hook)


def test_hook_awaited_after_successful_consumption_turn():
    auth = _fake_auth()
    hook = AsyncMock()
    ts_pool = SimpleNamespace(fetch=AsyncMock(return_value=[]))
    static_pool = object()
    token = {"sub": str(uuid4()), "email": "ops@example.com"}

    with ExitStack() as stack:
        _patches(stack, auth=auth, hook=hook)
        reply = asyncio.run(
            ctrl.run_turn(
                "how much did John charge last week", token, static_pool, ts_pool, _FakeLLM()
            )
        )

    assert reply.status == "success"
    hook.assert_awaited_once()
    # Threaded the right pool/auth/token through.
    kwargs = hook.await_args.kwargs
    assert kwargs["ts_pool"] is ts_pool
    assert kwargs["auth"] is auth
    assert kwargs["token_payload"] is token


def test_hook_not_called_on_refuse():
    auth = _fake_auth()
    hook = AsyncMock()
    ts_pool = SimpleNamespace(fetch=AsyncMock(return_value=[]))

    with ExitStack() as stack:
        _patches(stack, auth=auth, hook=hook)
        # Override the planner to refuse this turn.
        stack.enter_context(
            patch.object(
                ctrl,
                "planner_classify",
                new=MagicMock(return_value=SimpleNamespace(route="refuse", reason="x")),
            )
        )
        reply = asyncio.run(
            ctrl.run_turn("hello", {"sub": str(uuid4())}, object(), ts_pool, _FakeLLM())
        )

    assert reply.status == "not_found"
    hook.assert_not_awaited()


def test_hook_failure_does_not_break_successful_turn():
    """Defense-in-depth: a leaking post-success hook must not 502 the turn.

    Even though maybe_emit_* is designed never to raise, the controller guards
    the call locally so a hypothetical leak can't 502 the turn or re-close the
    already-success run as error.
    """
    auth = _fake_auth()
    boom = AsyncMock(side_effect=RuntimeError("hook leaked"))
    ts_pool = SimpleNamespace(fetch=AsyncMock(return_value=[]))

    with ExitStack() as stack:
        _patches(stack, auth=auth, hook=boom)
        reply = asyncio.run(
            ctrl.run_turn(
                "how much did John charge last week",
                {"sub": str(uuid4())},
                object(),
                ts_pool,
                _FakeLLM(),
            )
        )
        # The turn still succeeds despite the leaking hook.
        assert reply.status == "success"
        boom.assert_awaited_once()
        # The run was closed as success and never re-closed as error.
        close_statuses = [c.args[2] for c in ctrl.agent_runs_close.await_args_list]
        assert "success" in close_statuses
        assert "error" not in close_statuses
