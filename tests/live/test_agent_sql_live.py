"""Nightly agent-SQL LIVE shadow suite (PLAN.md §S3.5).

This is the drift detector the canned-trace golden gate
(``tests/golden/test_agent_sql_golden.py``) structurally cannot be: it drives a
**real Sonnet** over the real ``POST /agent/turn`` endpoint — the model authors
the SQL itself — and asserts only on the natural-language answer + the
``agent_runs.status`` row. A model or prompt regression that a replayed
``llm_trace`` would mask shows up here.

Scenarios come from ``tests/golden/agent_sql_live.yaml`` (5 questions copied
verbatim from the 20-question gate, minus ``llm_trace`` /
``expected.agent_views_used``). The mechanics mirror the S3 real-DB integration
test (``tests/integration/agent_sql/test_sql_mode_real_db.py``): a real HS256
JWT, the frozen ``graph_snapshot`` re-anchored to the live clock and loaded into
a rolled-back savepoint on the TimescaleDB + Supabase pair. To stay DRY this
imports that test's pure helpers (re-anchor, JWT mint, endpoint app) and the
golden harness's snapshot loaders rather than re-implementing them; it does NOT
modify either (the canned-trace suite is untouched — this is purely additive).

Skips when ``ANTHROPIC_API_KEY`` is unset (so local ``pytest`` burns no tokens)
and when the DB pair is unreachable (CI fails instead — see the conftest). The
nightly workflow (``.github/workflows/agent-sql-shadow.yml``) runs it with both
present and posts a one-line pass/fail summary to Slack.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

# Snapshot loaders + fixed defaults from the golden harness (read-only reuse —
# the same surface the S3 integration test consumes; the harness is untouched).
from tests.golden.test_agent_sql_golden import (
    _DEFAULT_ORG_ID,
    _depot_org_map,
    _load_static_snapshot,
    _load_ts_snapshot,
    _parse_scenario_now,
    _TxPool,
)

# Pure "real /agent/turn" helpers from the S3 integration test (DRY: the
# re-anchor logic in particular has subtle whole-day-shift correctness we do
# not want to duplicate).
from tests.integration.agent_sql.test_sql_mode_real_db import (
    _TEST_JWT_SECRET,
    _build_endpoint_app,
    _mint_jwt,
    _reanchor,
)

pytestmark = [pytest.mark.agent_sql_live, pytest.mark.asyncio]

_SCENARIOS_PATH = Path(__file__).resolve().parents[1] / "golden" / "agent_sql_live.yaml"


def _load_live_scenarios() -> list[dict[str, Any]]:
    """Load the shadow scenarios, failing loudly if the fixture is broken.

    A missing / unparseable / empty fixture is a broken drift detector, not a
    benign skip: an empty parametrize set collects zero tests, which pytest
    reports as a pass (exit 0), so the nightly job would post a green/skip line
    while silently testing nothing. Raising here turns that into a collection
    error the shadow run surfaces instead. (The legitimate "don't burn tokens
    locally" skip is handled separately by the ANTHROPIC_API_KEY fixture.)
    """
    if not _SCENARIOS_PATH.is_file():
        raise RuntimeError(
            f"shadow scenarios fixture missing: {_SCENARIOS_PATH} — the nightly "
            "drift suite has nothing to run"
        )
    raw = yaml.safe_load(_SCENARIOS_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(
            f"shadow scenarios fixture {_SCENARIOS_PATH} did not parse to a "
            f"non-empty list (got {type(raw).__name__})"
        )
    return raw


_LIVE_SCENARIOS = _load_live_scenarios()
_LIVE_IDS = [str(s.get("id", f"scenario_{i}")) for i, s in enumerate(_LIVE_SCENARIOS)]


@pytest.fixture(autouse=True)
def _skip_without_anthropic_key() -> None:
    """Skip every test here unless a real Anthropic key is present."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        pytest.skip("ANTHROPIC_API_KEY unset — skipping live shadow suite")


@pytest.fixture
def sql_mode_env(monkeypatch: pytest.MonkeyPatch):
    """Enable SQL mode (open allowlist) + the known JWT secret for one test.

    Mirrors the S3 integration test's fixture: clears the planner's lru-cached
    env reads before and after so the toggle doesn't leak, and resets the
    Anthropic client singleton so the turn builds a fresh client from the live
    ``ANTHROPIC_API_KEY``.
    """
    from src.api.agent import llm as agent_llm
    from src.api.agent import planner

    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    monkeypatch.delenv("AGENT_SQL_ORG_ALLOWLIST", raising=False)  # open to all orgs
    monkeypatch.setenv("JWT_SECRET_KEY", _TEST_JWT_SECRET)
    monkeypatch.delenv("JWT_SECRET_KEY_PREVIOUS", raising=False)
    planner.is_sql_mode_enabled.cache_clear()
    planner._sql_org_allowlist_tokens.cache_clear()
    agent_llm._reset_client_for_tests()
    try:
        yield
    finally:
        planner.is_sql_mode_enabled.cache_clear()
        planner._sql_org_allowlist_tokens.cache_clear()
        agent_llm._reset_client_for_tests()


async def _run_live_scenario(scenario: dict[str, Any], *, static_pool: Any, ts_pool: Any) -> None:
    """Drive one shadow scenario through the real /agent/turn endpoint."""
    sid = str(scenario["id"])
    question = str(scenario["question"])
    expected = scenario["expected"]
    scenario_now = _parse_scenario_now(scenario.get("scenario_now"))
    real_now = datetime.now(timezone.utc)
    snapshot = _reanchor(scenario["graph_snapshot"], scenario_now=scenario_now, real_now=real_now)
    org_map = _depot_org_map(snapshot, _DEFAULT_ORG_ID)

    token = _mint_jwt(user_id=uuid4(), org_id=_DEFAULT_ORG_ID, role="customer_admin")

    # One open transaction per pool → SAVEPOINT for the executor's nested
    # read-only transaction; both roll back so the run leaves no trace.
    async with ts_pool.acquire() as ts_conn, static_pool.acquire() as static_conn:
        ts_tx = ts_conn.transaction()
        static_tx = static_conn.transaction()
        await ts_tx.start()
        await static_tx.start()
        try:
            await _load_static_snapshot(static_conn, snapshot, org_map)
            await _load_ts_snapshot(ts_conn, snapshot, real_now, org_map)

            app = _build_endpoint_app(_TxPool(static_conn), _TxPool(ts_conn))
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test", timeout=120.0
            ) as client:
                resp = await client.post(
                    "/agent/turn",
                    json={"message": question},
                    headers={"Authorization": f"Bearer {token}"},
                )

            assert resp.status_code == 200, f"[{sid}] {resp.status_code}: {resp.text}"
            body = resp.json()
            text = body.get("text") or ""

            want_status = expected.get("status", "success")
            assert (
                body.get("status") == want_status
            ), f"[{sid}] expected {want_status!r}, got {body.get('status')!r}: {text!r}"

            for needle in expected["final_answer_must_include"]:
                assert needle in text, f"[{sid}] answer must include {needle!r} — got {text!r}"
            for needle in expected["final_answer_must_not_include"]:
                assert (
                    needle not in text
                ), f"[{sid}] answer must NOT include {needle!r} — got {text!r}"
        finally:
            await static_tx.rollback()
            await ts_tx.rollback()


@pytest.mark.usefixtures("sql_mode_env")
@pytest.mark.parametrize("scenario", _LIVE_SCENARIOS, ids=_LIVE_IDS or None)
async def test_agent_sql_live(
    scenario: dict[str, Any], sql_live_static_pool: Any, sql_live_ts_pool: Any
) -> None:
    """Run one shadow scenario end-to-end against real Sonnet + real DB pair."""
    await _run_live_scenario(scenario, static_pool=sql_live_static_pool, ts_pool=sql_live_ts_pool)
