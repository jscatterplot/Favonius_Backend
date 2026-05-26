"""Tests for :mod:`src.api.agent.budget`.

S4-A scope: budget *resolution* only — the env default, the per-org override,
their precedence, and the fail-open behaviour. The enforcement layer
(``check_and_reserve`` / ``record_actual``) and its accept/reject/hydration/
rollover cases land in S4-B/S4-C and extend this file then.

Pool I/O is mocked end-to-end; no DB is required.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.api.agent.budget import (
    DEFAULT_TOKEN_BUDGET_MONTHLY,
    TOKEN_BUDGET_ENV_VAR,
    _parse_positive_int,
    env_default_budget,
    resolve_org_token_budget,
)

# Async tests are marked at the class level (TestResolveOrgTokenBudget) rather
# than module-wide, so the sync parse/env tests stay unmarked and don't trip
# pytest-asyncio's "marked async but not async" warning.


def _make_pool(*, fetchval_return: Any = None, raise_on_acquire: bool = False) -> Any:
    """Fake asyncpg pool whose ``acquire()`` yields a conn with ``fetchval``.

    ``fetchval`` returns ``fetchval_return`` (the metadata override text, or
    ``None``). With ``raise_on_acquire`` the pool raises when acquired, to
    exercise the fail-open path.
    """
    pool = MagicMock()
    conn = MagicMock()
    conn.fetchval = AsyncMock(return_value=fetchval_return)

    if raise_on_acquire:

        @asynccontextmanager
        async def _acquire():
            raise RuntimeError("db down")
            yield conn  # pragma: no cover — unreachable, makes this an async gen

    else:

        @asynccontextmanager
        async def _acquire():
            yield conn

    pool.acquire = _acquire
    pool._conn = conn
    return pool


@pytest.fixture(autouse=True)
def _clear_env_cache(monkeypatch):
    """Reset the lru_cache around env_default_budget before AND after each test.

    The cache is process-global, so a value read by one test would otherwise
    leak into the next. Clearing on both sides keeps tests order-independent.
    """
    monkeypatch.delenv(TOKEN_BUDGET_ENV_VAR, raising=False)
    env_default_budget.cache_clear()
    yield
    env_default_budget.cache_clear()


class TestParsePositiveInt:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("5000000", 5_000_000),
            (5_000_000, 5_000_000),
            ("  42 ", 42),
            ("1", 1),
            ("10_000_000", 10_000_000),  # int() accepts underscores
        ],
    )
    def test_accepts_positive(self, raw, expected):
        assert _parse_positive_int(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   ", "0", "-1", "abc", "3.5", "1e6", "nan"])
    def test_rejects_non_positive_or_garbage(self, raw):
        assert _parse_positive_int(raw) is None


class TestEnvDefaultBudget:
    def test_unset_uses_hardcoded_default(self):
        assert env_default_budget() == DEFAULT_TOKEN_BUDGET_MONTHLY

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(TOKEN_BUDGET_ENV_VAR, "250000")
        env_default_budget.cache_clear()
        assert env_default_budget() == 250_000

    def test_garbage_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(TOKEN_BUDGET_ENV_VAR, "not-a-number")
        env_default_budget.cache_clear()
        assert env_default_budget() == DEFAULT_TOKEN_BUDGET_MONTHLY

    def test_zero_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(TOKEN_BUDGET_ENV_VAR, "0")
        env_default_budget.cache_clear()
        assert env_default_budget() == DEFAULT_TOKEN_BUDGET_MONTHLY


@pytest.mark.asyncio
class TestResolveOrgTokenBudget:
    async def test_per_org_override_beats_env_default(self, monkeypatch):
        # Env default deliberately different from the override so the win is
        # unambiguous.
        monkeypatch.setenv(TOKEN_BUDGET_ENV_VAR, "9000000")
        env_default_budget.cache_clear()
        pool = _make_pool(fetchval_return="500000")

        result = await resolve_org_token_budget(pool, uuid4())

        assert result == 500_000  # override wins, not the 9,000,000 env default

    async def test_no_override_uses_env_default(self, monkeypatch):
        monkeypatch.setenv(TOKEN_BUDGET_ENV_VAR, "750000")
        env_default_budget.cache_clear()
        pool = _make_pool(fetchval_return=None)  # metadata key absent

        result = await resolve_org_token_budget(pool, uuid4())

        assert result == 750_000

    async def test_no_override_no_env_uses_hardcoded_default(self):
        pool = _make_pool(fetchval_return=None)

        result = await resolve_org_token_budget(pool, uuid4())

        assert result == DEFAULT_TOKEN_BUDGET_MONTHLY

    @pytest.mark.parametrize("bad", ["0", "-5", "  ", "garbage", None])
    async def test_garbage_override_falls_back_to_env_default(self, monkeypatch, bad):
        monkeypatch.setenv(TOKEN_BUDGET_ENV_VAR, "600000")
        env_default_budget.cache_clear()
        pool = _make_pool(fetchval_return=bad)

        result = await resolve_org_token_budget(pool, uuid4())

        assert result == 600_000

    async def test_none_org_short_circuits_to_default_without_db(self):
        pool = _make_pool(fetchval_return="123")

        result = await resolve_org_token_budget(pool, None)

        assert result == DEFAULT_TOKEN_BUDGET_MONTHLY
        # favonius_admin (org=None) must not hit the DB at all.
        pool._conn.fetchval.assert_not_awaited()

    async def test_none_pool_short_circuits_to_default(self):
        result = await resolve_org_token_budget(None, uuid4())
        assert result == DEFAULT_TOKEN_BUDGET_MONTHLY

    async def test_db_error_fails_open_to_env_default(self, monkeypatch):
        monkeypatch.setenv(TOKEN_BUDGET_ENV_VAR, "333000")
        env_default_budget.cache_clear()
        pool = _make_pool(raise_on_acquire=True)

        result = await resolve_org_token_budget(pool, uuid4())

        assert result == 333_000

    async def test_query_targets_organizations_metadata_resiliently(self):
        # Pin the SQL shape: it must read the override via to_jsonb(o)->'metadata'
        # so a DB without supabase/044 returns NULL instead of raising.
        pool = _make_pool(fetchval_return=None)

        await resolve_org_token_budget(pool, uuid4())

        sql = pool._conn.fetchval.await_args.args[0]
        assert "to_jsonb(o) -> 'metadata' ->> 'agent_token_budget_monthly'" in sql
        assert "FROM organizations o" in sql
