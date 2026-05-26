"""Real-DB budget-refusal integration test (PLAN.md S4, step C item 11).

Unlike the S3 real-Anthropic test, this exercises the **budget refusal** path,
which by construction never calls the model: ``check_and_reserve`` runs before
``_get_client()``, so an over-budget org is refused with ZERO Anthropic code
touched. Hence no ``ANTHROPIC_API_KEY`` is required — only the TS+static DB
pair (skips when unreachable, like every real-db test here; CI fails instead).

Setup (inside a rolled-back savepoint on each pool):

* an ``organizations`` row with ``agent_token_budget_monthly = 1000`` (the
  per-org ceiling), and
* an ``agent_token_usage`` row with 1500 tokens already spent this period.

A ``sql_general`` question then POSTs to ``/agent/turn`` and must come back
refused.

Asserts: HTTP 200 with the refusal payload (``status='refused'``,
``reason='monthly_budget_exceeded'``), the ``agent_runs`` row recorded
``status='refused'`` + ``failure_reason='budget_exceeded'``, the
``favonius_agent_sql_budget_refused_total`` metric incremented, and
``_get_client`` was never called (no Anthropic interaction).
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from src.api.agent import budget
from src.api.agent.budget import TokenBudgetTracker, current_period_yyyymm
from tests.golden.test_agent_sql_golden import _TxPool
from tests.integration.agent_sql.test_sql_mode_real_db import (
    _TEST_JWT_SECRET,
    _build_endpoint_app,
    _mint_jwt,
)

pytestmark = [pytest.mark.integration, pytest.mark.agent_sql_real, pytest.mark.asyncio]

# A question the planner routes to sql_general (the \bfault anti-pattern pulls it
# off the consumption fast path), so it reaches the budget gate.
_SQL_QUESTION = "Which chargers were faulted yesterday?"


@pytest.fixture
def budget_sql_env(monkeypatch: pytest.MonkeyPatch):
    """Enable SQL mode (open allowlist) + known JWT secret; trip-wire _get_client.

    No ANTHROPIC key is set: the refusal must short-circuit before any client is
    built, so ``_get_client`` is replaced with a mock that raises if called.
    Yields that mock so the test can assert it stayed untouched.
    """
    from src.api.agent import llm as agent_llm
    from src.api.agent import planner

    monkeypatch.setenv("AGENT_SQL_MODE_ENABLED", "true")
    monkeypatch.delenv("AGENT_SQL_ORG_ALLOWLIST", raising=False)  # open to all orgs
    monkeypatch.setenv("JWT_SECRET_KEY", _TEST_JWT_SECRET)
    monkeypatch.delenv("JWT_SECRET_KEY_PREVIOUS", raising=False)
    planner.is_sql_mode_enabled.cache_clear()
    planner._sql_org_allowlist_tokens.cache_clear()
    no_client = MagicMock(side_effect=AssertionError("refusal path must not call _get_client"))
    monkeypatch.setattr(agent_llm, "_get_client", no_client)
    try:
        yield no_client
    finally:
        planner.is_sql_mode_enabled.cache_clear()
        planner._sql_org_allowlist_tokens.cache_clear()


async def test_over_budget_org_is_refused_without_anthropic(
    budget_sql_env,
    monkeypatch: pytest.MonkeyPatch,
    sql_real_static_pool,
    sql_real_ts_pool,
) -> None:
    org_id = uuid4()
    period = current_period_yyyymm()
    token = _mint_jwt(user_id=uuid4(), org_id=org_id, role="customer_admin")

    async with (
        sql_real_ts_pool.acquire() as ts_conn,
        sql_real_static_pool.acquire() as static_conn,
    ):
        ts_tx = ts_conn.transaction()
        static_tx = static_conn.transaction()
        await ts_tx.start()
        await static_tx.start()
        try:
            # Per-org ceiling = 1000 on a fresh org …
            await static_conn.execute(
                "INSERT INTO organizations (id, name, agent_token_budget_monthly) "
                "VALUES ($1::uuid, $2, $3)",
                str(org_id),
                "S4 Budget Test Org",
                1000,
            )
            # … and 1500 tokens already spent this period — already over budget.
            await ts_conn.execute(
                "INSERT INTO agent_token_usage "
                "(organization_id, period_yyyymm, input_tokens, output_tokens) "
                "VALUES ($1::uuid, $2, $3, $4)",
                str(org_id),
                period,
                1500,
                0,
            )

            tx_static = _TxPool(static_conn)
            tx_ts = _TxPool(ts_conn)
            # Point the budget tracker at the savepoint conns (the module
            # singleton would otherwise lazily borrow the lifespan pools).
            monkeypatch.setattr(
                budget, "_TRACKER", TokenBudgetTracker(static_pool=tx_static, ts_pool=tx_ts)
            )

            metric = budget_refused_metric_value(org_id)

            app = _build_endpoint_app(tx_static, tx_ts)
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test", timeout=30.0
            ) as client:
                resp = await client.post(
                    "/agent/turn",
                    json={"message": _SQL_QUESTION},
                    headers={"Authorization": f"Bearer {token}"},
                )

            # Controller convention: a graceful refusal is HTTP 200 with the
            # refusal payload (not a 4xx/5xx).
            assert resp.status_code == 200, f"{resp.status_code}: {resp.text}"
            body = resp.json()
            assert body.get("status") == "refused", body
            assert body.get("reason") == "monthly_budget_exceeded", body
            assert body.get("intent") == "sql_general", body

            # agent_runs row (same rolled-back ts transaction).
            run_id = body.get("run_id")
            assert run_id, "reply carried no run_id"
            row = await ts_conn.fetchrow(
                "SELECT status, failure_reason FROM agent_runs WHERE run_id = $1::uuid",
                str(run_id),
            )
            assert row is not None
            assert row["status"] == "refused", dict(row)
            assert row["failure_reason"] == "budget_exceeded", dict(row)

            # Metric incremented by exactly one for this org.
            assert budget_refused_metric_value(org_id) - metric == 1

            # The refusal must not have touched Anthropic at all.
            budget_sql_env.assert_not_called()
        finally:
            await static_tx.rollback()
            await ts_tx.rollback()


def budget_refused_metric_value(org_id) -> float:
    """Current favonius_agent_sql_budget_refused_total for this org label."""
    from src.monitoring.metrics import AGENT_SQL_BUDGET_REFUSED

    return AGENT_SQL_BUDGET_REFUSED.labels(organization_id=str(org_id))._value.get()
