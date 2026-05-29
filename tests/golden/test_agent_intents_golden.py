"""Golden gate for the deterministic fast-path intents (readiness + savings).

Unlike the agent-SQL gate, these intents make **no** LLM call — the planner
routes to them and a deterministic handler answers from real SQL. So each
scenario seeds its ``graph_snapshot`` into the same TimescaleDB + Supabase
test pair, calls the real handler
(``src.api.agent.controller._run_readiness_turn`` /
``_run_savings_turn``) with the scenario's ``scenario_now`` injected, and
asserts the rendered answer. Both DBs roll back per scenario.

This gate is what catches cross-pool SQL / schema drift in the readiness
queries (departures ⋈ vehicles, the merged SoC query, the latest-plan read)
and the savings aggregation — the per-vehicle verdict and window maths are
covered separately by the unit tests
(``tests/unit/agent/test_readiness_intent.py`` /
``test_savings_window.py``).

Requires the TS + static test pair (``tests/golden/conftest.py``); skips
locally when unreachable, runs in the agent-sql-golden CI job (which applies
all migrations, including 047, and the Supabase bootstrap that creates
``schedules``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from src.api.agent.audit import agent_runs_open
from src.api.agent.auth_context import AuthContext
from src.api.agent.controller import _run_readiness_turn, _run_savings_turn
from src.api.agent.resolve import _clear_tz_cache
from tests.golden._snapshot import TxPool, coerce_uuid, load_static, load_ts, parse_scenario_now

_HERE = Path(__file__).parent
_DEFAULT_ORG = UUID("bb000000-0000-4000-8000-0000000000bb")
_DEFAULT_USER = UUID("aa000000-0000-4000-8000-0000000000aa")


def _load(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, list) else []


_READINESS = _load(_HERE / "agent_readiness.yaml")
_SAVINGS = _load(_HERE / "agent_savings.yaml")
_READINESS_IDS = [str(s.get("id", i)) for i, s in enumerate(_READINESS)]
_SAVINGS_IDS = [str(s.get("id", i)) for i, s in enumerate(_SAVINGS)]


def _auth(snapshot: dict[str, Any]) -> AuthContext:
    visible = [coerce_uuid(d["depot_id"]) for d in snapshot.get("depots") or []]
    return AuthContext(
        user_id=_DEFAULT_USER,
        organization_id=_DEFAULT_ORG,
        role="customer_operator",
        visible_depot_ids=visible,
    )


async def _noop_step(*_a: Any, **_k: Any) -> None:
    return None


async def _run_scenario(scenario, *, ts_pool, static_pool, kind: str):
    # The depot-timezone helper memoises per process keyed by the depot-id set;
    # clear it so a scenario reusing a depot id can't inherit a prior tz.
    _clear_tz_cache()
    scenario_now = parse_scenario_now(scenario.get("scenario_now"))
    snapshot = scenario["graph_snapshot"]
    auth = _auth(snapshot)
    message = scenario.get("message") or scenario.get("question") or ""

    async with ts_pool.acquire() as ts_conn, static_pool.acquire() as static_conn:
        ts_tx = ts_conn.transaction()
        static_tx = static_conn.transaction()
        await ts_tx.start()
        await static_tx.start()
        try:
            await load_static(static_conn, snapshot, _DEFAULT_ORG, scenario_now)
            await load_ts(ts_conn, snapshot, scenario_now)
            tsf, stf = TxPool(ts_conn), TxPool(static_conn)
            run_id = await agent_runs_open(tsf, auth, message)
            if kind == "readiness":
                reply = await _run_readiness_turn(
                    run_id=run_id,
                    message=message,
                    auth=auth,
                    static_pool=stf,
                    ts_pool=tsf,
                    sse=None,
                    emit_step=_noop_step,
                    now=scenario_now,
                )
            else:
                reply = await _run_savings_turn(
                    run_id=run_id,
                    message=message,
                    auth=auth,
                    static_pool=stf,
                    ts_pool=tsf,
                    sse=None,
                    emit_step=_noop_step,
                    now=scenario_now,
                )
        finally:
            await static_tx.rollback()
            await ts_tx.rollback()
    return reply


def _assert_expected(scenario: dict[str, Any], reply: Any) -> None:
    expected = scenario["expected"]
    assert reply.status == expected.get("status", "success"), (
        f"[{scenario['id']}] status {reply.status!r} != {expected.get('status', 'success')!r}; "
        f"text={reply.text!r}"
    )
    text = reply.text or ""
    for needle in expected.get("answer_must_include", []):
        assert needle in text, f"[{scenario['id']}] answer missing {needle!r} — got {text!r}"
    for needle in expected.get("answer_must_not_include", []):
        assert (
            needle not in text
        ), f"[{scenario['id']}] answer must not include {needle!r} — {text!r}"


# ── No-DB invariants ─────────────────────────────────────────────────────────


def test_scenarios_present_and_well_formed() -> None:
    for label, scenarios in (("readiness", _READINESS), ("savings", _SAVINGS)):
        assert scenarios, f"no {label} golden scenarios found"
        ids = [s.get("id") for s in scenarios]
        assert len(ids) == len(set(ids)), f"duplicate {label} ids: {ids}"
        for s in scenarios:
            assert "graph_snapshot" in s and "expected" in s, f"{label} {s.get('id')!r} malformed"


# ── The gates (real DB; skip locally) ────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _READINESS, ids=_READINESS_IDS or None)
async def test_readiness_golden(scenario, agent_sql_ts_pool, agent_sql_static_pool) -> None:
    reply = await _run_scenario(
        scenario, ts_pool=agent_sql_ts_pool, static_pool=agent_sql_static_pool, kind="readiness"
    )
    _assert_expected(scenario, reply)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", _SAVINGS, ids=_SAVINGS_IDS or None)
async def test_savings_golden(scenario, agent_sql_ts_pool, agent_sql_static_pool) -> None:
    reply = await _run_scenario(
        scenario, ts_pool=agent_sql_ts_pool, static_pool=agent_sql_static_pool, kind="savings"
    )
    _assert_expected(scenario, reply)
