"""Unit tests for :class:`WorkflowScheduler` (pre-departure trigger).

Drives the scheduler one tick at a time using a fake :class:`DatabasePools`
so we never touch a real DB. Validates:

  - earliest_departure - lead_time_min ≤ now fires the workflow.
  - Fresh decision row inside dedupe window suppresses the run.
  - No departures in window → no run.
  - tick() returns the number of depots that fired.
  - Trigger time AFTER now → no run.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

import pytest

from src.core.workflow_scheduler import WorkflowScheduler, WorkflowSchedulerConfig

DEPOT_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
DEPOT_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ORG_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")


# ── Fakes ────────────────────────────────────────────────────────────────


class _Cursor:
    def __init__(self, payload: Any) -> None:
        self.payload = payload


class _FakeConn:
    """Stateful fake: scripted responses keyed by SQL substring."""

    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan = plan
        self.executed: list[tuple[str, tuple]] = []

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        if "FROM sites" in query and "ORDER BY created_at" in query:
            return self.plan.get("list_depots", [])
        return []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        if "MIN(s.departure_time)" in query:
            depot_id_arg = args[0]
            return {"earliest": self.plan.get("earliest", {}).get(str(depot_id_arg))}
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "organization_id::text" in query and "sites" in query:
            return str(self.plan.get("org_for", {}).get(str(args[0]), ORG_ID))
        if "FROM workflow_decisions" in query and "depot_id = $2::uuid" in query:
            depot_id_arg = args[1]
            return self.plan.get("has_fresh_decision", {}).get(str(depot_id_arg))
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.executed.append((query, args))
        return "OK"


class _Acquire:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self.conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakePool:
    def __init__(self, plan: dict[str, Any]) -> None:
        self._conn = _FakeConn(plan)

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)

    async def fetchrow(self, q: str, *a: Any) -> Any:
        return await self._conn.fetchrow(q, *a)

    async def fetchval(self, q: str, *a: Any) -> Any:
        return await self._conn.fetchval(q, *a)

    async def fetch(self, q: str, *a: Any) -> list[Any]:
        return await self._conn.fetch(q, *a)

    async def execute(self, q: str, *a: Any) -> str:
        return await self._conn.execute(q, *a)


class _Pools:
    def __init__(self, plan: dict[str, Any]) -> None:
        self.static = _FakePool(plan)
        self.ts = _FakePool(plan)


def _fixed_clock(t: datetime):
    return lambda: t


# ── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fires_when_lead_time_reached(monkeypatch):
    now = datetime(2026, 5, 13, 4, 5, tzinfo=timezone.utc)
    earliest_departure = datetime(2026, 5, 13, 5, 0, tzinfo=timezone.utc)
    # lead_time=60 → trigger at 04:00 ≤ now (04:05) ✔︎

    plan = {
        "list_depots": [{"depot_id": str(DEPOT_A), "timezone": "UTC"}],
        "earliest": {str(DEPOT_A): earliest_departure},
        "has_fresh_decision": {str(DEPOT_A): None},
        "org_for": {str(DEPOT_A): ORG_ID},
    }
    pools = _Pools(plan)
    sched = WorkflowScheduler(
        pools=pools,  # type: ignore[arg-type]
        config=WorkflowSchedulerConfig(
            poll_interval_seconds=60,
            departure_lookahead_hours=24,
            lead_time_minutes=60,
            window_hours=4,
            dedupe_window_seconds=1800,
        ),
        clock=_fixed_clock(now),
    )
    # Patch execute_readiness_workflow to bypass the real tool bundle.
    import src.core.workflow_scheduler as ws

    async def fake_execute(**kwargs):
        from src.core.workflows.readiness import ReadinessDecision, ReadinessOutput
        return ReadinessDecision(
            decision_id=uuid4(),
            workflow_name="daily_readiness_check",
            workflow_version="v1",
            depot_id=kwargs["depot_id"],
            organization_id=kwargs["organization_id"],
            triggered_by=kwargs["triggered_by"],
            triggered_by_user_id=None,
            inputs_hash="00" * 32,
            tool_calls=[],
            output=ReadinessOutput(
                depot_id=str(kwargs["depot_id"]),
                window="2026-05-13T05:00 → 09:00",
                coverage={"vehicles_checked": 1, "chargers_checked": 1, "routes_checked": 1},
                status="all_clear",
            ),
        )

    monkeypatch.setattr(ws, "execute_readiness_workflow", fake_execute)

    ran = await sched.tick()
    assert ran == 1


@pytest.mark.asyncio
async def test_does_not_fire_when_trigger_time_in_future():
    now = datetime(2026, 5, 13, 3, 0, tzinfo=timezone.utc)
    earliest_departure = datetime(2026, 5, 13, 6, 0, tzinfo=timezone.utc)
    # lead_time=60 → trigger at 05:00, after now → skip.

    plan = {
        "list_depots": [{"depot_id": str(DEPOT_A), "timezone": "UTC"}],
        "earliest": {str(DEPOT_A): earliest_departure},
        "has_fresh_decision": {str(DEPOT_A): None},
        "org_for": {str(DEPOT_A): ORG_ID},
    }
    pools = _Pools(plan)
    sched = WorkflowScheduler(pools=pools, clock=_fixed_clock(now))  # type: ignore[arg-type]
    ran = await sched.tick()
    assert ran == 0


@pytest.mark.asyncio
async def test_skips_when_fresh_decision_exists():
    now = datetime(2026, 5, 13, 4, 30, tzinfo=timezone.utc)
    earliest_departure = datetime(2026, 5, 13, 5, 0, tzinfo=timezone.utc)
    plan = {
        "list_depots": [{"depot_id": str(DEPOT_A), "timezone": "UTC"}],
        "earliest": {str(DEPOT_A): earliest_departure},
        "has_fresh_decision": {str(DEPOT_A): 1},  # already ran
        "org_for": {str(DEPOT_A): ORG_ID},
    }
    pools = _Pools(plan)
    sched = WorkflowScheduler(pools=pools, clock=_fixed_clock(now))  # type: ignore[arg-type]
    ran = await sched.tick()
    assert ran == 0


@pytest.mark.asyncio
async def test_no_departures_no_run():
    now = datetime(2026, 5, 13, 4, 0, tzinfo=timezone.utc)
    plan = {
        "list_depots": [{"depot_id": str(DEPOT_A), "timezone": "UTC"}],
        "earliest": {str(DEPOT_A): None},
        "has_fresh_decision": {str(DEPOT_A): None},
        "org_for": {str(DEPOT_A): ORG_ID},
    }
    pools = _Pools(plan)
    sched = WorkflowScheduler(pools=pools, clock=_fixed_clock(now))  # type: ignore[arg-type]
    ran = await sched.tick()
    assert ran == 0


@pytest.mark.asyncio
async def test_per_depot_isolation(monkeypatch):
    """A failure on depot A doesn't block depot B."""
    now = datetime(2026, 5, 13, 4, 30, tzinfo=timezone.utc)
    earliest_departure = datetime(2026, 5, 13, 5, 0, tzinfo=timezone.utc)

    plan = {
        "list_depots": [
            {"depot_id": str(DEPOT_A), "timezone": "UTC"},
            {"depot_id": str(DEPOT_B), "timezone": "UTC"},
        ],
        "earliest": {
            str(DEPOT_A): earliest_departure,
            str(DEPOT_B): earliest_departure,
        },
        "has_fresh_decision": {str(DEPOT_A): None, str(DEPOT_B): None},
        "org_for": {str(DEPOT_A): ORG_ID, str(DEPOT_B): ORG_ID},
    }
    pools = _Pools(plan)
    sched = WorkflowScheduler(pools=pools, clock=_fixed_clock(now))  # type: ignore[arg-type]
    import src.core.workflow_scheduler as ws

    async def fake_execute(**kwargs):
        if kwargs["depot_id"] == DEPOT_A:
            raise RuntimeError("depot A blew up")
        from src.core.workflows.readiness import ReadinessDecision, ReadinessOutput
        return ReadinessDecision(
            decision_id=uuid4(),
            workflow_name="daily_readiness_check",
            workflow_version="v1",
            depot_id=kwargs["depot_id"],
            organization_id=kwargs["organization_id"],
            triggered_by=kwargs["triggered_by"],
            triggered_by_user_id=None,
            inputs_hash="00" * 32,
            tool_calls=[],
            output=ReadinessOutput(
                depot_id=str(kwargs["depot_id"]),
                window="2026-05-13T05:00 → 09:00",
                coverage={"vehicles_checked": 0, "chargers_checked": 0, "routes_checked": 0},
                status="all_clear",
            ),
        )

    monkeypatch.setattr(ws, "execute_readiness_workflow", fake_execute)
    ran = await sched.tick()
    assert ran == 1  # only depot B succeeded


@pytest.mark.asyncio
async def test_start_and_stop_are_idempotent():
    plan = {"list_depots": [], "earliest": {}, "has_fresh_decision": {}, "org_for": {}}
    pools = _Pools(plan)
    sched = WorkflowScheduler(
        pools=pools,  # type: ignore[arg-type]
        config=WorkflowSchedulerConfig(poll_interval_seconds=1),
    )
    await sched.start()
    await sched.start()  # double-start tolerated
    await asyncio.sleep(0)
    await sched.stop()
    await sched.stop()  # double-stop tolerated
