"""Real-DB + real-Anthropic integration test for agent SQL mode (PLAN.md S3).

Unlike the S2 golden gate (``tests/golden/test_agent_sql_golden.py``), which
replays a canned ``llm_trace`` through a ``FakeAnthropicClient``, this test
exercises the *integration layer*: a real HS256 JWT, the real
``POST /agent/turn`` endpoint, the real
``planner → run_qa_turn → sql_validator → sql_executor`` chain, and a **real
Anthropic SDK call**. The model authors the SQL itself; we assert only on the
natural-language answer + the ``agent_runs.status`` row.

One test per eval category, each seeded from a real S2 scenario in
``tests/golden/agent_sql.yaml``:

  * ``energy_cost``    → ``en_05``  ("top 5 vehicles by total cost in the last 30 days")
  * ``ops_status``     → ``ops_11`` ("which alerts are still active right now")
  * ``pricing_market`` → ``pm_15``  ("5 highest electricity prices last week")

Each test (1) mints a real JWT for a test org+operator, (2) loads the seed's
``graph_snapshot`` into a rolled-back savepoint on the real TimescaleDB +
Supabase pair (reusing the S2 loaders), (3) POSTs the seed's question to
``/agent/turn``, (4) asserts the seed's ``expected.final_answer_must_include`` /
``_must_not_include`` substrings AND ``agent_runs.status = 'success'``.

Skips when ``ANTHROPIC_API_KEY`` is unset (so local pytest doesn't burn tokens)
and when the DB pair is unreachable (CI fails instead — see the conftest).

# S3 followups (design decisions surfaced while building this — not blockers):
#  * Re-anchoring fixtures to real now(): the S2 fixtures are pinned to
#    2026-05-25 and the canned traces use absolute date literals, but a *real*
#    model anchors relative windows ("last week", "last 30 days") to the live
#    clock (the current_time tool + SQL now()). Loading the frozen 2026-05 dates
#    verbatim would make every rolling-window question miss on any run date other
#    than ~2026-05-25. We therefore shift every fixture timestamp by a whole-day
#    delta = (today - scenario_now.date) so the windows line up on any CI date.
#    The three seeds use rolling ("last 30 days"/"last week") or status-based
#    ("active right now") windows, for which a whole-day shift is exact.
#    Calendar-month seeds (en_01/04/06, pm_16) are deliberately NOT used here — a
#    raw day shift can cross a month boundary and break "last month".
#  * Real-LLM substring fragility: the assertions are the same substrings the
#    canned S2 answers satisfy. Robust seeds were chosen (vehicle ids /
#    alert_type / price values), but "EUR/kWh" (pm_15) is the one
#    phrasing-sensitive token — if a future Sonnet revision renders it
#    "EUR per kWh" this is the line to revisit.
#  * Could not be executed against a live key in the authoring sandbox; the data
#    path is identical to the green S2 gate except the LLM is live, so the first
#    CI run with ANTHROPIC_API_KEY set is the real proof.
"""

from __future__ import annotations

import copy
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.agent.router import get_static_pool, get_ts_pool
from src.api.agent.router import router as agent_router
from tests.golden.test_agent_sql_golden import (
    _ALL_SCENARIOS,
    _DEFAULT_ORG_ID,
    _depot_org_map,
    _load_static_snapshot,
    _load_ts_snapshot,
    _parse_scenario_now,
    _TxPool,
)

pytestmark = [pytest.mark.integration, pytest.mark.agent_sql_real, pytest.mark.asyncio]


# HS256 secret used both to mint the test JWT and (via JWT_SECRET_KEY) to verify
# it through the real ``src.security.auth.verify_token`` path.
_TEST_JWT_SECRET = "s3-integration-test-secret"

# One seed scenario per eval category (ids in tests/golden/agent_sql.yaml).
_SEED_BY_CATEGORY: dict[str, str] = {
    "energy_cost": "en_05",
    "ops_status": "ops_11",
    "pricing_market": "pm_15",
}

_BY_ID: dict[str, dict[str, Any]] = {str(s.get("id")): s for s in _ALL_SCENARIOS}

# Timestamp fields per graph_snapshot section, for re-anchoring to real now().
_TS_FIELDS: dict[str, tuple[str, ...]] = {
    "sessions": ("start_time", "end_time"),
    "optimization_runs": ("run_time", "horizon_start", "horizon_end"),
    "alerts": ("created_at",),
    "electricity_prices": ("time",),
    "building_load": ("time",),
    "connector_status": ("timestamp",),
}


@pytest.fixture(autouse=True)
def _skip_without_anthropic_key() -> None:
    """Skip every test in this module unless a real Anthropic key is present."""
    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        pytest.skip("ANTHROPIC_API_KEY unset — skipping real-Anthropic integration test")


@pytest.fixture
def sql_mode_env(monkeypatch: pytest.MonkeyPatch):
    """Enable SQL mode + a known JWT secret for one test.

    Clears the planner's lru-cached env reads before and after so the toggle
    does not leak into other tests, and resets the Anthropic client singleton
    so the turn builds a fresh client from the live ``ANTHROPIC_API_KEY``.
    """
    from src.api.agent import llm as agent_llm
    from src.api.agent import planner

    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    monkeypatch.setenv("JWT_SECRET_KEY", _TEST_JWT_SECRET)
    monkeypatch.delenv("JWT_SECRET_KEY_PREVIOUS", raising=False)
    planner.is_sql_mode_enabled.cache_clear()
    agent_llm._reset_client_for_tests()
    try:
        yield
    finally:
        planner.is_sql_mode_enabled.cache_clear()
        agent_llm._reset_client_for_tests()


def _shift(value: Any, delta: timedelta) -> datetime:
    """Parse an ISO/datetime fixture timestamp and shift it by ``delta`` (UTC)."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt + delta


def _reanchor(
    snapshot: dict[str, Any], *, scenario_now: datetime, real_now: datetime
) -> dict[str, Any]:
    """Return a deep copy of ``snapshot`` with every timestamp shifted to now().

    See the module "S3 followups" note for the rationale. The shift granularity
    is whole days so rolling-window and "yesterday" questions line up against
    the live clock; a zero delta (running on 2026-05-25) is a no-op copy.
    """
    snap = copy.deepcopy(snapshot)
    delta = timedelta(days=(real_now.date() - scenario_now.date()).days)
    if delta == timedelta(0):
        return snap
    for section, fields in _TS_FIELDS.items():
        for row in snap.get(section) or []:
            for field in fields:
                if row.get(field) is not None:
                    row[field] = _shift(row[field], delta)
    return snap


def _mint_jwt(*, user_id: UUID, org_id: UUID, role: str) -> str:
    """Mint a real HS256 Supabase-shaped JWT the real verify_token accepts."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "aud": "authenticated",
        "role": "authenticated",
        # Non-staff domain so get_user_role does NOT auto-promote to
        # favonius_admin (which would widen visible_depot_ids to all depots and
        # defeat the org-scoped read this test relies on).
        "email": "operator@example.test",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        "app_metadata": {"favonius_role": role, "organization_id": str(org_id)},
        "user_metadata": {"email_verified": True},
    }
    return pyjwt.encode(payload, _TEST_JWT_SECRET, algorithm="HS256")


def _build_endpoint_app(static_tx_pool: Any, ts_tx_pool: Any) -> FastAPI:
    """Mount the real agent router with pools pointed at the savepoint conns."""
    app = FastAPI()
    app.include_router(agent_router)
    app.dependency_overrides[get_static_pool] = lambda: static_tx_pool
    app.dependency_overrides[get_ts_pool] = lambda: ts_tx_pool
    return app


async def _run_seed(seed_id: str, *, static_pool: Any, ts_pool: Any) -> None:
    """Drive one seed scenario through the real /agent/turn endpoint."""
    scenario = _BY_ID.get(seed_id)
    assert scenario is not None, f"seed scenario {seed_id!r} not found in agent_sql.yaml"

    question = str(scenario["question"])
    expected = scenario["expected"]
    scenario_now = _parse_scenario_now(scenario.get("scenario_now"))
    real_now = datetime.now(timezone.utc)
    snapshot = _reanchor(scenario["graph_snapshot"], scenario_now=scenario_now, real_now=real_now)
    org_map = _depot_org_map(snapshot, _DEFAULT_ORG_ID)

    token = _mint_jwt(user_id=uuid4(), org_id=_DEFAULT_ORG_ID, role="customer_admin")

    # One open transaction per pool, becomes a SAVEPOINT for the executor's
    # nested read-only transaction; both roll back so the run leaves no trace.
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

            assert resp.status_code == 200, f"[{seed_id}] {resp.status_code}: {resp.text}"
            body = resp.json()
            text = body.get("text") or ""

            assert (
                body.get("status") == "success"
            ), f"[{seed_id}] expected success, got {body.get('status')!r}: {text!r}"
            assert (
                body.get("intent") == "sql_general"
            ), f"[{seed_id}] expected sql_general route, got {body.get('intent')!r}"

            for needle in expected["final_answer_must_include"]:
                assert needle in text, f"[{seed_id}] answer must include {needle!r} — got {text!r}"
            for needle in expected["final_answer_must_not_include"]:
                assert (
                    needle not in text
                ), f"[{seed_id}] answer must NOT include {needle!r} — got {text!r}"

            # agent_runs row lives in the same (rolled-back) ts transaction.
            run_id = body.get("run_id")
            assert run_id, f"[{seed_id}] reply carried no run_id"
            status_row = await ts_conn.fetchval(
                "SELECT status FROM agent_runs WHERE run_id = $1::uuid", str(run_id)
            )
            assert (
                status_row == "success"
            ), f"[{seed_id}] agent_runs.status = {status_row!r} (expected 'success')"
        finally:
            await static_tx.rollback()
            await ts_tx.rollback()


@pytest.mark.usefixtures("sql_mode_env")
async def test_sql_mode_energy_cost_real_db(sql_real_static_pool, sql_real_ts_pool) -> None:
    """energy_cost — top-5 vehicles by total cost in the last 30 days (seed en_05)."""
    await _run_seed("en_05", static_pool=sql_real_static_pool, ts_pool=sql_real_ts_pool)


@pytest.mark.usefixtures("sql_mode_env")
async def test_sql_mode_ops_status_real_db(sql_real_static_pool, sql_real_ts_pool) -> None:
    """ops_status — alerts still active right now (seed ops_11)."""
    await _run_seed("ops_11", static_pool=sql_real_static_pool, ts_pool=sql_real_ts_pool)


@pytest.mark.usefixtures("sql_mode_env")
async def test_sql_mode_pricing_market_real_db(sql_real_static_pool, sql_real_ts_pool) -> None:
    """pricing_market — 5 highest electricity prices last week (seed pm_15)."""
    await _run_seed("pm_15", static_pool=sql_real_static_pool, ts_pool=sql_real_ts_pool)
