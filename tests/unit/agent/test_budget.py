"""Tests for :mod:`src.api.agent.budget`.

This module covers budget *resolution* — the platform default, the per-org
column value, their precedence, and the fail-open behaviour. The enforcement
layer (``check_and_reserve`` / ``record_actual``) and its accept/reject/
hydration/rollover cases land in the S4-C suite and extend this file then.

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
    _parse_positive_int,
    resolve_org_token_budget,
)

# Async tests are marked at the class level (TestResolveOrgTokenBudget) rather
# than module-wide, so the sync parse tests stay unmarked and don't trip
# pytest-asyncio's "marked async but not async" warning.


def _make_pool(*, fetchval_return: Any = None, raise_on_acquire: bool = False) -> Any:
    """Fake asyncpg pool whose ``acquire()`` yields a conn with ``fetchval``.

    ``fetchval`` returns ``fetchval_return`` — the org's
    ``agent_token_budget_monthly`` as the text asyncpg yields from the
    ``to_jsonb(o)->>...`` read, or ``None`` when the column is unset/absent.
    With ``raise_on_acquire`` the pool raises when acquired, to exercise the
    fail-open path.
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


@pytest.mark.asyncio
class TestResolveOrgTokenBudget:
    async def test_per_org_value_beats_platform_default(self):
        # A set column value wins over the hard-coded platform default.
        pool = _make_pool(fetchval_return="500000")

        result = await resolve_org_token_budget(pool, uuid4())

        assert result == 500_000
        assert result != DEFAULT_TOKEN_BUDGET_MONTHLY

    async def test_unset_value_uses_platform_default(self):
        pool = _make_pool(fetchval_return=None)  # column NULL / absent

        result = await resolve_org_token_budget(pool, uuid4())

        assert result == DEFAULT_TOKEN_BUDGET_MONTHLY

    @pytest.mark.parametrize("bad", ["0", "-5", "  ", "garbage", None])
    async def test_garbage_value_falls_back_to_platform_default(self, bad):
        pool = _make_pool(fetchval_return=bad)

        result = await resolve_org_token_budget(pool, uuid4())

        assert result == DEFAULT_TOKEN_BUDGET_MONTHLY

    async def test_none_org_short_circuits_to_default_without_db(self):
        pool = _make_pool(fetchval_return="123")

        result = await resolve_org_token_budget(pool, None)

        assert result == DEFAULT_TOKEN_BUDGET_MONTHLY
        # favonius_admin (org=None) must not hit the DB at all.
        pool._conn.fetchval.assert_not_awaited()

    async def test_none_pool_short_circuits_to_default(self):
        result = await resolve_org_token_budget(None, uuid4())
        assert result == DEFAULT_TOKEN_BUDGET_MONTHLY

    async def test_db_error_fails_open_to_default(self):
        pool = _make_pool(raise_on_acquire=True)

        result = await resolve_org_token_budget(pool, uuid4())

        assert result == DEFAULT_TOKEN_BUDGET_MONTHLY

    async def test_query_targets_organizations_column_resiliently(self):
        # Pin the SQL shape: it must read the column via to_jsonb(o)->>... so a
        # DB without supabase/044 returns NULL instead of raising UndefinedColumn.
        pool = _make_pool(fetchval_return=None)

        await resolve_org_token_budget(pool, uuid4())

        sql = pool._conn.fetchval.await_args.args[0]
        assert "to_jsonb(o) ->> 'agent_token_budget_monthly'" in sql
        assert "FROM organizations o" in sql
