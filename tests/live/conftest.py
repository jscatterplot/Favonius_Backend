"""Fixtures + marker for the nightly agent-SQL live shadow suite (PLAN.md S3.5).

Provides the TimescaleDB + Supabase test-pair pools the shadow test loads its
frozen graph_snapshots into (rolled-back savepoints, same pattern as the S3
integration test). The pools reuse the URL + pool helpers from
``tests/golden/conftest.py`` so the connection contract is byte-for-byte the
same as the golden gate — in particular ``statement_cache_size=0``, which the
role-swap executor relies on.

Skips locally when the pair is unreachable; fails the job in CI
(``CI`` / ``GITHUB_ACTIONS``). The live test additionally skips itself when
``ANTHROPIC_API_KEY`` is unset, so a local ``pytest`` run burns no tokens.
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
    """Register the ``agent_sql_live`` marker.

    Also listed in ``pytest.ini`` so ``--strict-markers`` is satisfied; the
    duplicate registration here keeps the marker alive if this conftest is
    loaded standalone.
    """
    config.addinivalue_line(
        "markers",
        "agent_sql_live: nightly agent-SQL live shadow suite (TS+static pair "
        "AND ANTHROPIC_API_KEY required; real /agent/turn + real Sonnet)",
    )


@pytest_asyncio.fixture
async def sql_live_ts_pool():
    """Function-scoped asyncpg pool against the TimescaleDB test database."""
    pool = await _make_pool(_ts_database_url(), "TimescaleDB")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def sql_live_static_pool():
    """Function-scoped asyncpg pool against the Supabase (static) test database."""
    pool = await _make_pool(_static_database_url(), "static")
    try:
        yield pool
    finally:
        await pool.close()
