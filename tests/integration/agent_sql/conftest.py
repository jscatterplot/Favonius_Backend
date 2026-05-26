"""Fixtures + marker for the agent-SQL real-DB integration test (PLAN.md S3).

Provides the TimescaleDB + Supabase test-pair pools and registers the
``agent_sql_real`` marker.

The pools reuse the URL + pool helpers from ``tests/golden/conftest.py`` so the
connection contract is byte-for-byte the same as the S2 golden gate — in
particular ``statement_cache_size=0``, which the role-swap executor
(``src/api/agent/sql_executor.py``) relies on (Supavisor transaction-mode
rejects prepared statements). Skips locally when the pair is unreachable;
fails the job in CI (``CI`` / ``GITHUB_ACTIONS``), matching the golden gate.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from tests.golden.conftest import (
    _make_pool,
    _static_database_url,
    _ts_database_url,
)


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``agent_sql_real`` marker.

    Also listed in ``pytest.ini`` so ``--strict-markers`` is satisfied; the
    duplicate registration here keeps the marker alive if this conftest is
    loaded by a downstream consumer's pytest run.
    """
    config.addinivalue_line(
        "markers",
        "agent_sql_real: agent-SQL real-DB + real-Anthropic integration "
        "(TS+static pair AND ANTHROPIC_API_KEY required)",
    )


@pytest_asyncio.fixture
async def sql_real_ts_pool():
    """Function-scoped asyncpg pool against the TimescaleDB test database."""
    pool = await _make_pool(_ts_database_url(), "TimescaleDB")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def sql_real_static_pool():
    """Function-scoped asyncpg pool against the Supabase (static) test database."""
    pool = await _make_pool(_static_database_url(), "static")
    try:
        yield pool
    finally:
        await pool.close()
