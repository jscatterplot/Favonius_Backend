"""Tests for :mod:`src.api.agent.budget`.

This module covers budget *resolution* — the platform default, the per-org
column value, their precedence, and the fail-open behaviour. The enforcement
layer (``check_and_reserve`` / ``record_actual``) and its accept/reject/
hydration/rollover cases land in the S4-C suite and extend this file then.

Pool I/O is mocked end-to-end; no DB is required.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.api.agent.budget import (
    DEFAULT_TOKEN_BUDGET_MONTHLY,
    PER_TURN_OVERHEAD_TOKENS,
    Reservation,
    TokenBudgetTracker,
    _parse_positive_int,
    _PeriodCounter,
    current_period_yyyymm,
    estimate_turn_tokens,
    resolve_org_token_budget,
)

_PERIOD = "202605"  # fixed period for deterministic (org, period) counter keys

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


# ── Enforcement: TokenBudgetTracker ─────────────────────────────────────────


def _ts_pool(
    *,
    hydrate_row: Any = None,
    fetchrow_fn: Optional[Callable[..., Any]] = None,
    executemany_raises: bool = False,
) -> Any:
    """Fake TS asyncpg pool: ``fetchrow`` for hydration, ``executemany`` for flush.

    ``hydrate_row`` is the agent_token_usage row returned on cold start (or
    ``None``); ``fetchrow_fn`` overrides it with a per-call side effect (used by
    the period-rollover test to gate the row on the queried period).
    """
    pool = MagicMock()
    conn = MagicMock()
    if fetchrow_fn is not None:
        conn.fetchrow = AsyncMock(side_effect=fetchrow_fn)
    else:
        conn.fetchrow = AsyncMock(return_value=hydrate_row)
    conn.executemany = AsyncMock(
        side_effect=RuntimeError("flush boom") if executemany_raises else None
    )

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    pool._conn = conn
    return pool


def _tracker(
    *,
    ceiling: Any = "1000",
    hydrate_row: Any = None,
    fetchrow_fn: Optional[Callable[..., Any]] = None,
    period: str = _PERIOD,
    flush_every_writes: int = 1000,
    executemany_raises: bool = False,
) -> TokenBudgetTracker:
    """Build a tracker over fake pools with a fixed period + high flush threshold.

    ``flush_every_writes=1000`` (and the default 30 s window) means a single
    ``record_actual`` never schedules a background flush, keeping reconcile
    tests deterministic; flush tests call ``flush()`` directly.
    """
    return TokenBudgetTracker(
        static_pool=_make_pool(fetchval_return=ceiling),
        ts_pool=_ts_pool(
            hydrate_row=hydrate_row,
            fetchrow_fn=fetchrow_fn,
            executemany_raises=executemany_raises,
        ),
        period_provider=lambda: period,
        flush_every_writes=flush_every_writes,
    )


@pytest.mark.asyncio
class TestCheckAndReserve:
    async def test_accept_under_ceiling(self):
        tr = _tracker(ceiling="1000")
        assert await tr.check_and_reserve(uuid4(), 500) is not None

    async def test_accept_at_ceiling_minus_one(self):
        # est lands total at exactly ceiling - 1.
        tr = _tracker(ceiling="1000")
        r = await tr.check_and_reserve(uuid4(), 999)
        assert r is not None and r.est_tokens == 999

    async def test_accept_at_exactly_ceiling(self):
        # Boundary: total == ceiling is allowed; only > ceiling is refused.
        tr = _tracker(ceiling="1000")
        assert await tr.check_and_reserve(uuid4(), 1000) is not None

    async def test_reject_over_ceiling(self):
        tr = _tracker(ceiling="1000")
        assert await tr.check_and_reserve(uuid4(), 1001) is None

    async def test_reject_when_existing_usage_plus_estimate_exceeds(self):
        # Hydrated base 900 + est 200 = 1100 > 1000 → refuse.
        tr = _tracker(ceiling="1000", hydrate_row={"input_tokens": 900, "output_tokens": 0})
        assert await tr.check_and_reserve(uuid4(), 200) is None

    async def test_per_org_column_beats_platform_default(self):
        # Column ceiling 2000: a 2500-token turn is refused even though it is
        # far below the 10M platform default — proving the column is the ceiling.
        tr_small = _tracker(ceiling="2000")
        assert await tr_small.check_and_reserve(uuid4(), 2500) is None
        # Same turn with NO column value falls back to the 10M default → accept.
        tr_default = _tracker(ceiling=None)
        assert await tr_default.check_and_reserve(uuid4(), 2500) is not None

    async def test_cold_start_hydration_matches_db_row(self):
        org = uuid4()
        tr = _tracker(ceiling="100000", hydrate_row={"input_tokens": 400, "output_tokens": 300})

        await tr.check_and_reserve(org, 10)

        counter = tr._counters[(str(org), _PERIOD)]
        assert counter.committed_input == 400
        assert counter.committed_output == 300
        # total counts the hydrated 700 + the 10-token reservation.
        assert counter.total == 710
        assert counter.reserved == 10

    async def test_period_rollover_previous_month_does_not_constrain(self):
        # The DB has 1500 tokens, but only for the PREVIOUS period (202604).
        def _row_for_previous_period_only(_sql, _org, period, *_a, **_k):
            if period == "202604":
                return {"input_tokens": 1500, "output_tokens": 0}
            return None

        # Current period 202605: hydration finds no row → usage 0 → 800 accepted
        # under the 1000 ceiling despite last month's 1500.
        current = _tracker(
            ceiling="1000", fetchrow_fn=_row_for_previous_period_only, period="202605"
        )
        assert await current.check_and_reserve(uuid4(), 800) is not None

        # Same data viewed as 202604 WOULD constrain (1500 + 800 > 1000) — proves
        # the row is real and the isolation above is period-scoped, not absence.
        previous = _tracker(
            ceiling="1000", fetchrow_fn=_row_for_previous_period_only, period="202604"
        )
        assert await previous.check_and_reserve(uuid4(), 800) is None

    async def test_none_org_returns_noop_reservation_never_refuses(self):
        tr = _tracker(ceiling="1000")
        r = await tr.check_and_reserve(None, 10**9)  # absurd estimate
        assert r is not None and r.organization_id is None
        # No counter is created for a non-org-scoped turn.
        assert tr._counters == {}

    async def test_concurrent_cold_start_reserve_against_hydrated_baseline(self):
        # Regression for the hydration race (Bugbot High / Codex P2): two
        # concurrent first-turn requests for the SAME cold (org, period) must
        # both evaluate against the hydrated baseline (900), not a zero baseline.
        # With base 900 and a 1000 ceiling, exactly one 60-token reservation
        # fits; the other is refused. Without the hydration lock both would race
        # past the in-flight DB read against 0 and BOTH would be admitted.
        org = uuid4()

        async def _slow_fetchrow(*_args, **_kwargs):
            await asyncio.sleep(0.02)  # widen the race window
            return {"input_tokens": 900, "output_tokens": 0}

        tr = _tracker(ceiling="1000", fetchrow_fn=_slow_fetchrow)

        results = await asyncio.gather(
            tr.check_and_reserve(org, 60),
            tr.check_and_reserve(org, 60),
        )

        accepted = [r for r in results if r is not None]
        refused = [r for r in results if r is None]
        assert len(accepted) == 1, f"expected exactly one accept, got {results}"
        assert len(refused) == 1
        # The DB was read exactly once despite two concurrent cold-start callers.
        assert tr._explicit_ts_pool._conn.fetchrow.await_count == 1


@pytest.mark.asyncio
class TestRecordActual:
    async def test_reconcile_frees_reservation_headroom(self):
        org = uuid4()
        tr = _tracker(ceiling="1000")

        r = await tr.check_and_reserve(org, 800)
        assert r is not None
        # A second 300-token turn won't fit while 800 is reserved.
        assert await tr.check_and_reserve(org, 300) is None

        # Real usage was far less than the 800 estimate; reconcile frees it.
        tr.record_actual(r, 100, 50)
        counter = tr._counters[(str(org), _PERIOD)]
        assert counter.reserved == 0
        assert counter.committed_input == 100 and counter.committed_output == 50

        # Now an 800-token turn fits (150 committed + 800 = 950 <= 1000).
        assert await tr.check_and_reserve(org, 800) is not None

    async def test_none_or_noop_reservation_is_ignored(self):
        tr = _tracker(ceiling="1000")
        tr.record_actual(None, 100, 100)  # no crash
        tr.record_actual(
            Reservation(organization_id=None, period_yyyymm=_PERIOD, est_tokens=0), 5, 5
        )
        assert tr._counters == {}

    async def test_record_actual_accumulates_unflushed_for_persistence(self):
        org = uuid4()
        tr = _tracker(ceiling="100000")
        r = await tr.check_and_reserve(org, 100)
        tr.record_actual(r, 120, 40)
        counter = tr._counters[(str(org), _PERIOD)]
        assert counter.unflushed_input == 120 and counter.unflushed_output == 40

    async def test_record_actual_zero_releases_reservation(self):
        # The controller calls record_actual(reservation, 0, 0) when setup fails
        # before the LLM loop (Codex P1): the rough estimate must be released so
        # it doesn't leak and wrongly refuse future turns.
        org = uuid4()
        tr = _tracker(ceiling="1000")
        r = await tr.check_and_reserve(org, 800)
        assert tr._counters[(str(org), _PERIOD)].reserved == 800

        tr.record_actual(r, 0, 0)

        counter = tr._counters[(str(org), _PERIOD)]
        assert counter.reserved == 0
        assert counter.committed_input == 0 and counter.committed_output == 0
        # Full headroom restored — a subsequent full-ceiling turn fits.
        assert await tr.check_and_reserve(org, 1000) is not None


@pytest.mark.asyncio
class TestFlush:
    async def test_flush_writes_append_style_delta_and_resets(self):
        org = uuid4()
        tr = _tracker(ceiling="100000")
        r = await tr.check_and_reserve(org, 100)
        tr.record_actual(r, 120, 40)

        await tr.flush()

        em = tr._explicit_ts_pool._conn.executemany
        em.assert_awaited_once()
        rows = em.await_args.args[1]
        assert rows == [(str(org), _PERIOD, 120, 40)]
        counter = tr._counters[(str(org), _PERIOD)]
        assert counter.unflushed_input == 0 and counter.unflushed_output == 0

    async def test_flush_noop_when_nothing_pending(self):
        tr = _tracker(ceiling="100000")
        await tr.flush()
        tr._explicit_ts_pool._conn.executemany.assert_not_awaited()

    async def test_flush_rolls_back_deltas_on_db_error(self):
        org = uuid4()
        tr = _tracker(ceiling="100000", executemany_raises=True)
        r = await tr.check_and_reserve(org, 100)
        tr.record_actual(r, 120, 40)

        await tr.flush()  # best-effort: must not raise

        counter = tr._counters[(str(org), _PERIOD)]
        # Deltas rolled back so the next flush retries them.
        assert counter.unflushed_input == 120 and counter.unflushed_output == 40


@pytest.mark.asyncio
class TestReconcile:
    def _reconcile_pool(self, *, executemany_raises: bool = False) -> tuple[Any, Any]:
        """A TS pool whose ``fetchrow`` return can be flipped between phases."""
        pool = MagicMock()
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=None)  # cold start: no row yet
        conn.executemany = AsyncMock(
            side_effect=RuntimeError("flush boom") if executemany_raises else None
        )

        @asynccontextmanager
        async def _acquire():
            yield conn

        pool.acquire = _acquire
        pool._conn = conn
        return pool, conn

    async def test_flushes_then_rehydrates_committed_from_db(self):
        org = uuid4()
        ts_pool, conn = self._reconcile_pool()
        tr = TokenBudgetTracker(
            static_pool=_make_pool(fetchval_return="100000"),
            ts_pool=ts_pool,
            period_provider=lambda: _PERIOD,
            flush_every_writes=1000,
        )
        r = await tr.check_and_reserve(org, 100)  # cold hydrate -> committed 0
        tr.record_actual(r, 50, 30)  # committed 80, unflushed 50/30

        # The DB now holds the cross-worker total (our 50/30 once flushed + another
        # worker's 200/100); reconcile must fold that into committed.
        conn.fetchrow = AsyncMock(return_value={"input_tokens": 250, "output_tokens": 130})

        await tr.reconcile()

        conn.executemany.assert_awaited()  # flushed our delta first
        counter = tr._counters[(str(org), _PERIOD)]
        assert counter.unflushed_input == 0 and counter.unflushed_output == 0
        # committed := DB(250/130) + unflushed(0/0) — picks up the other worker.
        assert counter.committed_input == 250 and counter.committed_output == 130

    async def test_preserves_local_unflushed_when_flush_fails(self):
        org = uuid4()
        ts_pool, conn = self._reconcile_pool(executemany_raises=True)
        tr = TokenBudgetTracker(
            static_pool=_make_pool(fetchval_return="100000"),
            ts_pool=ts_pool,
            period_provider=lambda: _PERIOD,
            flush_every_writes=1000,
        )
        r = await tr.check_and_reserve(org, 100)
        tr.record_actual(r, 50, 30)
        # DB read for the re-hydrate sees no persisted total (our flush failed).
        conn.fetchrow = AsyncMock(return_value={"input_tokens": 0, "output_tokens": 0})

        await tr.reconcile()  # flush raises internally -> deltas rolled back

        counter = tr._counters[(str(org), _PERIOD)]
        # committed := DB(0) + unflushed(50/30): local actuals are NOT lost.
        assert counter.committed_input == 50 and counter.committed_output == 30
        assert counter.unflushed_input == 50 and counter.unflushed_output == 30

    async def test_skips_keys_still_hydrating(self):
        # A key present in _counters but NOT in _hydrated is mid cold-start
        # hydration; reconcile must not re-read/clobber it (Bugbot).
        org = uuid4()
        ts_pool, conn = self._reconcile_pool()
        conn.fetchrow = AsyncMock(return_value={"input_tokens": 999, "output_tokens": 999})
        tr = TokenBudgetTracker(
            static_pool=_make_pool(fetchval_return="100000"),
            ts_pool=ts_pool,
            period_provider=lambda: _PERIOD,
            flush_every_writes=1000,
        )
        key = (str(org), _PERIOD)
        tr._counters[key] = _PeriodCounter(committed_input=111, committed_output=22)
        # deliberately NOT added to tr._hydrated

        await tr.reconcile()

        counter = tr._counters[key]
        assert counter.committed_input == 111 and counter.committed_output == 22
        conn.fetchrow.assert_not_awaited()  # no re-read for a still-hydrating key

    async def test_evicts_settled_past_period_keys(self):
        org = uuid4()
        ts_pool, _conn = self._reconcile_pool()
        tr = TokenBudgetTracker(
            static_pool=_make_pool(fetchval_return="100000"),
            ts_pool=ts_pool,
            period_provider=lambda: _PERIOD,  # current = 202605
            flush_every_writes=1000,
        )
        settled_past = (str(org), "202504")  # fully flushed, no reservation
        current = (str(org), _PERIOD)  # current period
        busy_past = (str(uuid4()), "202504")  # past but has an in-flight reservation
        tr._counters[settled_past] = _PeriodCounter(committed_input=500)
        tr._counters[current] = _PeriodCounter(committed_input=10)
        tr._counters[busy_past] = _PeriodCounter(reserved=50)
        tr._hydrated.update({settled_past, current, busy_past})

        await tr.reconcile()

        assert settled_past not in tr._counters and settled_past not in tr._hydrated
        assert current in tr._counters  # current period is never evicted
        assert busy_past in tr._counters  # in-flight reservation keeps it alive


class TestEstimateAndPeriodHelpers:
    def test_estimate_turn_tokens_prompt_plus_overhead(self):
        # ~4 chars/token: 16000 chars -> 4000 prompt tokens + fixed overhead.
        assert estimate_turn_tokens("x" * 16000) == 4000 + PER_TURN_OVERHEAD_TOKENS

    def test_estimate_turn_tokens_empty_prompt_is_just_overhead(self):
        assert estimate_turn_tokens("") == PER_TURN_OVERHEAD_TOKENS

    def test_current_period_yyyymm_format(self):
        assert current_period_yyyymm(datetime(2026, 5, 26, tzinfo=timezone.utc)) == "202605"
        assert current_period_yyyymm(datetime(2026, 1, 2, tzinfo=timezone.utc)) == "202601"
