"""Pytest plumbing shared by the tests/golden/ gates.

Currently this provides everything the agent-SQL golden harness
(``test_agent_sql_golden.py``) needs:

* the ``agent_sql_golden`` marker (registered in :func:`pytest_configure`),
* the ``--diag`` flag — on a scenario failure, the harness prints the LLM
  trace and the SQL it executed,
* the ``agent_sql_ts_pool`` / ``agent_sql_static_pool`` fixtures — asyncpg
  pools against the TimescaleDB + Supabase test pair, mirroring the
  skip-locally / fail-in-CI behaviour of
  ``tests/golden/workflows/conftest.py``.

The workflow gate keeps its own ``conftest.py`` under
``tests/golden/workflows/`` (with its single-pool fixture); this file does
not touch it.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio

try:
    import asyncpg
except ImportError:  # pragma: no cover - asyncpg is a hard dep, but be defensive
    asyncpg = None  # type: ignore[assignment]


# Local default mirrors the workflow gate's port (5433). The static pool
# defaults to a sibling database on the same instance so a single local
# TimescaleDB container can host both halves of the pair.
_DEFAULT_TS_URL = "postgresql://favonius_test:test_password@localhost:5433/favonius_test"
_DEFAULT_STATIC_URL = "postgresql://favonius_test:test_password@localhost:5433/favonius_static"


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``agent_sql_golden`` marker.

    Also listed in ``pytest.ini`` so ``--strict-markers`` is satisfied in
    every invocation; registering it here too keeps the marker alive if this
    conftest is loaded by a downstream consumer's pytest run.
    """
    config.addinivalue_line(
        "markers",
        "agent_sql_golden: parametrised agent-SQL eval scenarios (real TS+static DB pair required)",
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add ``--diag`` — dump the LLM trace + executed SQL for failed scenarios."""
    group = parser.getgroup("agent_sql_golden")
    group.addoption(
        "--diag",
        action="store_true",
        default=False,
        help=(
            "agent-SQL golden: on scenario failure, print the replayed LLM "
            "trace and the SQL the harness executed (diagnostic mode)."
        ),
    )


def _ts_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", _DEFAULT_TS_URL)


def _static_database_url() -> str:
    """Static (Supabase) test DB URL.

    Honours ``TEST_STATIC_DATABASE_URL`` when set; otherwise derives a
    sibling ``favonius_static`` database from ``TEST_DATABASE_URL`` so a
    single local container serves both pools.
    """
    explicit = os.environ.get("TEST_STATIC_DATABASE_URL")
    if explicit:
        return explicit
    ts = os.environ.get("TEST_DATABASE_URL")
    if ts:
        base, _, _dbname = ts.rpartition("/")
        if base:
            return f"{base}/favonius_static"
    return _DEFAULT_STATIC_URL


def _ci_requires_db() -> bool:
    """True when CI expects the agent-SQL gate to talk to a real database."""
    for var in ("CI", "GITHUB_ACTIONS"):
        if (os.environ.get(var) or "").strip().lower() in ("1", "true", "yes"):
            return True
    return False


async def _make_pool(url: str, label: str):
    """Create an asyncpg pool, skipping locally / failing in CI when unreachable.

    ``statement_cache_size=0`` matches the production DB pools (Supavisor
    transaction-mode rejects prepared statements), which is also what the
    sql_executor's role-swap path assumes.
    """
    if asyncpg is None:
        msg = "asyncpg not installed; agent-SQL golden tests require it"
        if _ci_requires_db():
            pytest.fail(msg)
        pytest.skip(msg)
    try:
        return await asyncpg.create_pool(
            url,
            min_size=1,
            max_size=4,
            command_timeout=15,
            statement_cache_size=0,
        )
    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        msg = f"agent-SQL golden: {label} test database unavailable: {exc}"
        if _ci_requires_db():
            pytest.fail(msg)
        pytest.skip(msg)


@pytest_asyncio.fixture
async def agent_sql_ts_pool():
    """Function-scoped asyncpg pool against the TimescaleDB test database.

    Function scope (not session) matches the workflow-golden conftest: each
    test opens its own transaction and rolls it back, and pytest-asyncio
    spins a fresh event loop per function.
    """
    pool = await _make_pool(_ts_database_url(), "TimescaleDB")
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def agent_sql_static_pool():
    """Function-scoped asyncpg pool against the Supabase (static) test database."""
    pool = await _make_pool(_static_database_url(), "static")
    try:
        yield pool
    finally:
        await pool.close()
