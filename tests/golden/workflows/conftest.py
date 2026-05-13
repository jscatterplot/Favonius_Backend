"""Pytest plumbing for the workflow eval harness.

Exposes:

* the ``workflow_golden`` marker (registered in :func:`pytest_configure`)
* the ``workflow_test_db_pool`` fixture — a session-scoped asyncpg pool
  against the test database. Skips the whole module if the DB is
  unreachable, matching the integration-agent conftest pattern.

The actual scenario discovery + parametrisation lives in
``test_workflow_golden.py`` (one parametrised test per YAML file). Doing
it there rather than in a plugin keeps the discovery rule visible in
the test module and lets pytest's normal collection + ``-k`` selection
work without extra wiring.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import pytest_asyncio

try:
    import asyncpg
except ImportError:  # pragma: no cover - asyncpg is a hard dep, but be defensive
    asyncpg = None  # type: ignore[assignment]


WORKFLOW_GOLDEN_DIR = Path(__file__).parent
EXAMPLES_DIR = WORKFLOW_GOLDEN_DIR / "_examples"


def pytest_configure(config: pytest.Config) -> None:
    """Register the ``workflow_golden`` marker.

    Listed in ``pyproject.toml`` so ``--strict-markers`` is happy in
    every test run, but we also register it here so the marker survives
    when this conftest is loaded outside of the project (e.g. in a
    downstream consumer's pytest invocation).
    """
    config.addinivalue_line(
        "markers",
        "workflow_golden: parametrised workflow eval scenarios (real DB required)",
    )


def _test_database_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://favonius_test:test_password@localhost:5433/favonius_test",
    )


def _workflow_golden_ci_requires_db() -> bool:
    """True when CI expects the workflow-golden gate to talk to a real database."""
    ci = (os.environ.get("CI") or "").strip().lower()
    if ci in ("1", "true", "yes"):
        return True
    ga = (os.environ.get("GITHUB_ACTIONS") or "").strip().lower()
    return ga in ("1", "true", "yes")


@pytest_asyncio.fixture
async def workflow_test_db_pool():
    """Function-scoped asyncpg pool for one workflow-golden test.

    Function scope (rather than session) matches the integration-agent
    conftest pattern: pytest-asyncio creates a new event loop per
    function by default, and a session-scoped async pool ties resources
    to a loop that gets torn down between tests. Each test only needs a
    short-lived pool — the runner opens its own transaction per
    scenario and rolls it back immediately.

    Skips when the test DB is unreachable locally; in CI (``CI`` /
    ``GITHUB_ACTIONS``) a connection failure fails the job instead of
    skipping.
    """
    if asyncpg is None:
        pytest.skip("asyncpg not installed; workflow-golden tests require it")

    try:
        pool = await asyncpg.create_pool(
            _test_database_url(),
            min_size=1,
            max_size=4,
            command_timeout=15,
        )
    except (OSError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
        if _workflow_golden_ci_requires_db():
            pytest.fail(f"workflow-golden tests: test database unavailable in CI: {exc}")
        pytest.skip(f"workflow-golden tests: test database unavailable: {exc}")

    try:
        yield pool
    finally:
        await pool.close()
