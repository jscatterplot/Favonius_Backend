"""Tests for ``src.api.agent.resolve.resolve_time_window``.

The function turns a relative phrase or absolute date pair into UTC
bounds, anchored in the depot's local timezone. The DST cases below
are not bonus coverage — they are the reason the resolver runs in
depot TZ and not in UTC. ``Europe/Vilnius`` is the v0 pilot's TZ; its
2026 spring-forward weekend is March 29.

The tests pass a fixed ``now`` rather than relying on freezegun so
the suite has no extra dependency.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from src.api.agent.plan import TimeWindow
from src.api.agent.resolve import (
    _clear_tz_cache,
    load_depot_timezones,
    resolve_time_window,
)

VILNIUS = "Europe/Vilnius"
NEW_YORK = "America/New_York"

DEPOT_VILNIUS = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
DEPOT_KAUNAS = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
DEPOT_NYC = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")


def _vilnius_only() -> dict[UUID, str]:
    return {DEPOT_VILNIUS: VILNIUS, DEPOT_KAUNAS: VILNIUS}


# A fixed "now" mid-April 2026: Wednesday April 15, 12:00 UTC. In Vilnius
# this is post-DST (UTC+3). All "this_*" / "last_*" assertions below
# reference this anchor.
NOW_APR_15 = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Each relative literal
# --------------------------------------------------------------------------- #


class TestRelativeLiterals:
    def test_today(self):
        result = resolve_time_window(
            TimeWindow(kind="relative", relative="today"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
            now=NOW_APR_15,
        )
        # Vilnius local Apr 15 = UTC Apr 14 21:00 → Apr 15 21:00 (UTC+3).
        assert result.start_utc == _utc(2026, 4, 14, 21)
        assert result.end_utc == _utc(2026, 4, 15, 21)
        assert result.timezone == VILNIUS

    def test_yesterday(self):
        result = resolve_time_window(
            TimeWindow(kind="relative", relative="yesterday"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
            now=NOW_APR_15,
        )
        assert result.start_utc == _utc(2026, 4, 13, 21)
        assert result.end_utc == _utc(2026, 4, 14, 21)

    def test_this_week(self):
        # Apr 15 2026 is a Wednesday → ISO week starts Mon Apr 13.
        result = resolve_time_window(
            TimeWindow(kind="relative", relative="this_week"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
            now=NOW_APR_15,
        )
        assert result.start_utc == _utc(2026, 4, 12, 21)
        assert result.end_utc == _utc(2026, 4, 19, 21)

    def test_last_week(self):
        # last_week = Mon Apr 6 → Mon Apr 13 in Vilnius.
        result = resolve_time_window(
            TimeWindow(kind="relative", relative="last_week"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
            now=NOW_APR_15,
        )
        assert result.start_utc == _utc(2026, 4, 5, 21)
        assert result.end_utc == _utc(2026, 4, 12, 21)
        # Plain non-DST week: 7 × 24 h = 168 h.
        assert (result.end_utc - result.start_utc) == timedelta(days=7)

    def test_this_month(self):
        result = resolve_time_window(
            TimeWindow(kind="relative", relative="this_month"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
            now=NOW_APR_15,
        )
        # April 1 (UTC+3, post-DST) → May 1 (UTC+3).
        assert result.start_utc == _utc(2026, 3, 31, 21)
        assert result.end_utc == _utc(2026, 4, 30, 21)

    def test_last_month(self):
        # March in Vilnius spans the DST transition: starts UTC+2,
        # ends UTC+3, so 30d23h elapsed instead of 31d.
        result = resolve_time_window(
            TimeWindow(kind="relative", relative="last_month"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
            now=NOW_APR_15,
        )
        assert result.start_utc == _utc(2026, 2, 28, 22)
        assert result.end_utc == _utc(2026, 3, 31, 21)
        assert result.end_utc - result.start_utc == timedelta(days=31) - timedelta(hours=1)


class TestRelativeYearBoundaries:
    def test_last_month_in_january_rolls_to_previous_year(self):
        jan_15 = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        result = resolve_time_window(
            TimeWindow(kind="relative", relative="last_month"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
            now=jan_15,
        )
        # Vilnius is UTC+2 in December and January (no DST).
        assert result.start_utc == _utc(2025, 11, 30, 22)
        assert result.end_utc == _utc(2025, 12, 31, 22)

    def test_this_month_in_december_keeps_same_year(self):
        dec_15 = datetime(2026, 12, 15, 12, 0, tzinfo=timezone.utc)
        result = resolve_time_window(
            TimeWindow(kind="relative", relative="this_month"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
            now=dec_15,
        )
        # Vilnius UTC+2 in December.
        assert result.start_utc == _utc(2026, 11, 30, 22)
        # End rolls into January next year.
        assert result.end_utc == _utc(2026, 12, 31, 22)


# --------------------------------------------------------------------------- #
# DST boundary
# --------------------------------------------------------------------------- #


class TestDSTBoundary:
    def test_last_week_spanning_spring_forward(self):
        # Wednesday Apr 1, 2026 12:00 UTC is in the week of Mon Mar 30,
        # so "last_week" = Mon Mar 23 → Mon Mar 30, which contains
        # the spring-forward Sunday (Mar 29). The local week spans
        # exactly 7 days, but UTC sees one hour fewer because clocks
        # jumped from 03:00 to 04:00 mid-week.
        anchor = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
        result = resolve_time_window(
            TimeWindow(kind="relative", relative="last_week"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
            now=anchor,
        )
        # March 23 00:00 +02:00 → March 22 22:00 UTC.
        assert result.start_utc == _utc(2026, 3, 22, 22)
        # March 30 00:00 +03:00 → March 29 21:00 UTC.
        assert result.end_utc == _utc(2026, 3, 29, 21)
        # 7 days local == 7 × 24 h - 1 h = 167 h in UTC.
        assert result.end_utc - result.start_utc == timedelta(hours=167)

    def test_last_week_spanning_fall_back(self):
        # Fall-back in 2026 is Sunday Oct 25 (clocks go 04:00 → 03:00).
        # "last_week" anchored Wed Oct 28 covers Mon Oct 19 → Mon Oct 26,
        # which contains the fall-back Sunday and is therefore one
        # hour longer than 168 h.
        anchor = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
        result = resolve_time_window(
            TimeWindow(kind="relative", relative="last_week"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
            now=anchor,
        )
        assert result.end_utc - result.start_utc == timedelta(hours=169)


# --------------------------------------------------------------------------- #
# Absolute window
# --------------------------------------------------------------------------- #


class TestAbsoluteWindow:
    def test_full_calendar_month(self):
        result = resolve_time_window(
            TimeWindow(kind="absolute", from_iso="2026-03-01", to_iso="2026-04-01"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
            now=NOW_APR_15,  # ignored for absolute mode
        )
        # March in Vilnius: starts at UTC+2, ends at UTC+3.
        assert result.start_utc == _utc(2026, 2, 28, 22)
        assert result.end_utc == _utc(2026, 3, 31, 21)
        assert result.timezone == VILNIUS

    def test_absolute_ignores_now(self):
        # Whatever ``now`` says, the absolute bounds are fixed.
        far_future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        a = resolve_time_window(
            TimeWindow(kind="absolute", from_iso="2026-04-01", to_iso="2026-05-01"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
            now=NOW_APR_15,
        )
        b = resolve_time_window(
            TimeWindow(kind="absolute", from_iso="2026-04-01", to_iso="2026-05-01"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
            now=far_future,
        )
        assert a.start_utc == b.start_utc
        assert a.end_utc == b.end_utc


# --------------------------------------------------------------------------- #
# Multi-timezone contract
# --------------------------------------------------------------------------- #


class TestMultiTimezone:
    def test_multiple_timezones_raises(self):
        # Contract: when visible depots span multiple IANA TZs, the
        # resolver raises and the compiler must group by TZ. This
        # keeps a single ResolvedTimeWindow tied to a single TZ.
        depot_tzs = {DEPOT_VILNIUS: VILNIUS, DEPOT_NYC: NEW_YORK}
        with pytest.raises(ValueError) as excinfo:
            resolve_time_window(
                TimeWindow(kind="relative", relative="today"),
                [DEPOT_VILNIUS, DEPOT_NYC],
                depot_tzs,
                now=NOW_APR_15,
            )
        msg = str(excinfo.value)
        assert "multiple timezones" in msg
        assert NEW_YORK in msg
        assert VILNIUS in msg

    def test_same_timezone_across_multiple_depots_passes(self):
        # Two depots, one IANA TZ → fine, the resolver picks that TZ.
        depot_tzs = {DEPOT_VILNIUS: VILNIUS, DEPOT_KAUNAS: VILNIUS}
        result = resolve_time_window(
            TimeWindow(kind="relative", relative="today"),
            [DEPOT_VILNIUS, DEPOT_KAUNAS],
            depot_tzs,
            now=NOW_APR_15,
        )
        assert result.timezone == VILNIUS

    def test_empty_timezone_map_raises(self):
        with pytest.raises(ValueError) as excinfo:
            resolve_time_window(
                TimeWindow(kind="relative", relative="today"),
                [DEPOT_VILNIUS],
                {},
                now=NOW_APR_15,
            )
        assert "no depot timezones" in str(excinfo.value)

    def test_visible_depot_without_tz_in_map_is_ignored(self):
        # A depot present in visible_depot_ids but missing from
        # depot_timezones is treated as "no TZ for that depot" — the
        # remaining ones still drive resolution.
        depot_tzs = {DEPOT_VILNIUS: VILNIUS}
        result = resolve_time_window(
            TimeWindow(kind="relative", relative="today"),
            [DEPOT_VILNIUS, DEPOT_NYC],  # NYC has no TZ in the map
            depot_tzs,
            now=NOW_APR_15,
        )
        assert result.timezone == VILNIUS


# --------------------------------------------------------------------------- #
# Argument hygiene
# --------------------------------------------------------------------------- #


class TestArgumentHygiene:
    def test_naive_now_raises(self):
        # Passing a naive datetime is a bug — fail loudly rather than
        # silently coerce to UTC.
        naive = datetime(2026, 4, 15, 12, 0)
        with pytest.raises(ValueError):
            resolve_time_window(
                TimeWindow(kind="relative", relative="today"),
                [DEPOT_VILNIUS],
                _vilnius_only(),
                now=naive,
            )

    def test_default_now_uses_wall_clock(self):
        # Just checks the function runs end-to-end without ``now``;
        # the bounds are not asserted because they depend on the
        # actual wall clock. The point is that we don't blow up on
        # the default path.
        result = resolve_time_window(
            TimeWindow(kind="relative", relative="today"),
            [DEPOT_VILNIUS],
            _vilnius_only(),
        )
        assert result.timezone == VILNIUS
        assert result.end_utc - result.start_utc <= timedelta(hours=25)
        assert result.end_utc > result.start_utc


# --------------------------------------------------------------------------- #
# Depot-timezone helper
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestLoadDepotTimezones:
    @pytest.fixture(autouse=True)
    def _reset_cache(self):
        _clear_tz_cache()
        yield
        _clear_tz_cache()

    async def test_returns_id_to_timezone_map(self, fake_static_pool):
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {"id": DEPOT_VILNIUS, "timezone": VILNIUS},
                {"id": DEPOT_KAUNAS, "timezone": VILNIUS},
            ]
        )

        result = await load_depot_timezones(fake_static_pool, [DEPOT_VILNIUS, DEPOT_KAUNAS])

        assert result == {DEPOT_VILNIUS: VILNIUS, DEPOT_KAUNAS: VILNIUS}
        # The visible-depot list went into $1 unmodified.
        call = fake_static_pool.fetch.await_args
        assert call.args[1] == [DEPOT_VILNIUS, DEPOT_KAUNAS]

    async def test_skips_rows_with_null_timezone(self, fake_static_pool):
        fake_static_pool.fetch = AsyncMock(
            return_value=[
                {"id": DEPOT_VILNIUS, "timezone": VILNIUS},
                {"id": DEPOT_KAUNAS, "timezone": None},
            ]
        )

        result = await load_depot_timezones(fake_static_pool, [DEPOT_VILNIUS, DEPOT_KAUNAS])

        assert result == {DEPOT_VILNIUS: VILNIUS}

    async def test_empty_visible_depots_short_circuits(self, fake_static_pool):
        result = await load_depot_timezones(fake_static_pool, [])

        assert result == {}
        fake_static_pool.fetch.assert_not_awaited()

    async def test_caches_within_process(self, fake_static_pool):
        fake_static_pool.fetch = AsyncMock(
            return_value=[{"id": DEPOT_VILNIUS, "timezone": VILNIUS}]
        )

        a = await load_depot_timezones(fake_static_pool, [DEPOT_VILNIUS])
        b = await load_depot_timezones(fake_static_pool, [DEPOT_VILNIUS])

        assert a == b
        # Cache hit on second call: only one fetch.
        assert fake_static_pool.fetch.await_count == 1

    async def test_cache_keyed_by_visible_depot_set(self, fake_static_pool):
        # Different visible-depot sets are cached independently.
        fake_static_pool.fetch = AsyncMock(
            side_effect=[
                [{"id": DEPOT_VILNIUS, "timezone": VILNIUS}],
                [
                    {"id": DEPOT_VILNIUS, "timezone": VILNIUS},
                    {"id": DEPOT_KAUNAS, "timezone": VILNIUS},
                ],
            ]
        )

        a = await load_depot_timezones(fake_static_pool, [DEPOT_VILNIUS])
        b = await load_depot_timezones(fake_static_pool, [DEPOT_VILNIUS, DEPOT_KAUNAS])

        assert a == {DEPOT_VILNIUS: VILNIUS}
        assert b == {DEPOT_VILNIUS: VILNIUS, DEPOT_KAUNAS: VILNIUS}
        assert fake_static_pool.fetch.await_count == 2

    async def test_string_uuids_are_coerced(self, fake_static_pool):
        # asyncpg returns native UUIDs, but a stringified row should
        # still land in the dict keyed by ``UUID``.
        depot_str = str(uuid4())
        fake_static_pool.fetch = AsyncMock(return_value=[{"id": depot_str, "timezone": VILNIUS}])

        result = await load_depot_timezones(fake_static_pool, [UUID(depot_str)])

        assert UUID(depot_str) in result
