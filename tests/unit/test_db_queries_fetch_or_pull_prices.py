"""Unit tests for src/db/queries.py::fetch_or_pull_prices_by_zone.

The read-through cache pattern has three legs:

1. Full cache hit → no API call.
2. Cache miss → ENTSO-E API call → persist to ``electricity_prices``
   → re-read returns the merged map.
3. API failure → return whatever the cache had (partial map or empty).

These tests exercise all three with a fake DB connection and a
mocked ENTSO-E adapter. The full SQL behaviour of ``fetch_prices_by_zone``
(forward-fill, market_type filter, etc.) is covered by the integration
suite — this file only validates the cache-vs-API plumbing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db.queries import fetch_or_pull_prices_by_zone


ZONE = "10YLT-1001A0008Q"


def _utc(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


class _FakeDB:
    """asyncpg.Pool stand-in supporting ``fetch`` and ``executemany``."""

    def __init__(self) -> None:
        # rows returned by SELECT against electricity_prices, keyed by
        # the SELECT'd column shape. The helper only does two kinds of
        # SELECT: the initial cache read (via fetch_prices_by_zone, which
        # asks for ``time, lmp_price_mwh``) and the existence-check
        # (which asks for ``time``). We track both as lists of dicts.
        self.price_rows: list[dict] = []
        self.existing_times: list[datetime] = []
        self.executemany_calls: list[tuple[str, list]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict]:
        if "lmp_price_mwh" in query:
            return list(self.price_rows)
        # existence-check returns just {"time": ts}
        return [{"time": t} for t in self.existing_times]

    async def executemany(self, query: str, rows: list[tuple]) -> None:
        self.executemany_calls.append((query, list(rows)))


def _make_entsoe_price(ts: datetime, eur_mwh: float):
    """Build a minimal ENTSOEPrice without importing the real class —
    keeps the test independent of the adapter's import surface."""
    from src.adapters.entsoe.prices import ENTSOEPrice
    return ENTSOEPrice(
        timestamp=ts, price_eur_mwh=eur_mwh, bidding_zone=ZONE,
    )


@pytest.mark.asyncio
async def test_full_cache_hit_skips_api(monkeypatch):
    """Every expected hour already in electricity_prices → adapter
    never gets called."""
    db = _FakeDB()
    # Two hours of cached prices in the response of the initial fetch.
    db.price_rows = [
        {"time": _utc(2026, 5, 19, 13), "lmp_price_mwh": 200.0},  # €0.20/kWh
        {"time": _utc(2026, 5, 19, 14), "lmp_price_mwh": 300.0},  # €0.30/kWh
    ]
    monkeypatch.setenv("EUROPEAN_ELECTRICITY_API", "fake-token")

    with patch(
        "src.adapters.entsoe.prices.ENTSOEAdapter.get_day_ahead_prices",
        new=AsyncMock(side_effect=AssertionError("API should not be called")),
    ):
        result = await fetch_or_pull_prices_by_zone(
            db, ZONE, _utc(2026, 5, 19, 13), _utc(2026, 5, 19, 15),
        )

    assert _utc(2026, 5, 19, 13) in result
    assert _utc(2026, 5, 19, 14) in result
    assert result[_utc(2026, 5, 19, 13)] == pytest.approx(0.20)
    assert db.executemany_calls == []


@pytest.mark.asyncio
async def test_cache_miss_calls_api_persists_and_re_reads(monkeypatch):
    """electricity_prices is empty → adapter fetches → rows are
    persisted (executemany with INSERT) → result reflects the API
    data."""
    monkeypatch.setenv("EUROPEAN_ELECTRICITY_API", "fake-token")

    # _FakeDB.fetch is shared between the initial read and the
    # existence-check + final read. To distinguish the three calls,
    # advance the state manually.
    class _StatefulDB(_FakeDB):
        def __init__(self) -> None:
            super().__init__()
            self._read_count = 0

        async def fetch(self, query: str, *args: Any) -> list[dict]:
            if "lmp_price_mwh" in query:
                self._read_count += 1
                # First read: empty cache.
                if self._read_count == 1:
                    return []
                # Second read (after persist): show the rows we inserted.
                return [
                    {"time": _utc(2026, 5, 19, 13), "lmp_price_mwh": 200.0},
                    {"time": _utc(2026, 5, 19, 14), "lmp_price_mwh": 300.0},
                ]
            return []  # existence-check: no existing rows yet

    db = _StatefulDB()
    fake_api_result = [
        _make_entsoe_price(_utc(2026, 5, 19, 13), 200.0),
        _make_entsoe_price(_utc(2026, 5, 19, 14), 300.0),
    ]

    with patch(
        "src.adapters.entsoe.prices.ENTSOEAdapter.get_day_ahead_prices",
        new=AsyncMock(return_value=fake_api_result),
    ):
        result = await fetch_or_pull_prices_by_zone(
            db, ZONE, _utc(2026, 5, 19, 13), _utc(2026, 5, 19, 15),
        )

    assert len(db.executemany_calls) == 1
    query, rows = db.executemany_calls[0]
    assert "INSERT INTO electricity_prices" in query
    assert len(rows) == 2
    # Stored as €/MWh (×1000 conversion).
    assert any(r[2] == 200.0 for r in rows)
    assert any(r[2] == 300.0 for r in rows)
    # And the re-read produced the populated dict.
    assert result[_utc(2026, 5, 19, 13)] == pytest.approx(0.20)
    assert result[_utc(2026, 5, 19, 14)] == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_cache_miss_with_no_token_skips_api(monkeypatch, caplog):
    """No EUROPEAN_ELECTRICITY_API env → don't try the API, return
    whatever the cache has."""
    monkeypatch.delenv("EUROPEAN_ELECTRICITY_API", raising=False)
    db = _FakeDB()  # empty cache

    with patch(
        "src.adapters.entsoe.prices.ENTSOEAdapter.get_day_ahead_prices",
        new=AsyncMock(side_effect=AssertionError("must not be called")),
    ):
        result = await fetch_or_pull_prices_by_zone(
            db, ZONE, _utc(2026, 5, 19, 13), _utc(2026, 5, 19, 14),
        )

    assert result == {}
    assert db.executemany_calls == []


@pytest.mark.asyncio
async def test_api_failure_returns_partial_cache(monkeypatch):
    """API raises → log + return whatever was in the cache (which
    may be empty). The cost-calc path treats an empty map as
    ``unpriceable``."""
    monkeypatch.setenv("EUROPEAN_ELECTRICITY_API", "fake-token")
    db = _FakeDB()
    db.price_rows = [
        {"time": _utc(2026, 5, 19, 13), "lmp_price_mwh": 200.0},
    ]

    with patch(
        "src.adapters.entsoe.prices.ENTSOEAdapter.get_day_ahead_prices",
        new=AsyncMock(side_effect=RuntimeError("ENTSO-E rate limit")),
    ):
        result = await fetch_or_pull_prices_by_zone(
            db, ZONE, _utc(2026, 5, 19, 13), _utc(2026, 5, 19, 15),
        )

    # Hour 13 served from cache; hour 14 absent because API failed.
    assert _utc(2026, 5, 19, 13) in result
    # No persist happened.
    assert db.executemany_calls == []


@pytest.mark.asyncio
async def test_existing_rows_not_re_inserted(monkeypatch):
    """Cache has hour 13 only; window covers 13:00–16:00 — hour 14 is
    served by 1h forward-fill but hour 15 is a real gap. API returns
    all 3 hours. The existence-check reports hour 13 already in DB, so
    only hours 14 and 15 are inserted (idempotent skip of duplicates)."""
    monkeypatch.setenv("EUROPEAN_ELECTRICITY_API", "fake-token")

    class _MixedDB(_FakeDB):
        def __init__(self) -> None:
            super().__init__()
            self._read_count = 0

        async def fetch(self, query: str, *args: Any) -> list[dict]:
            if "lmp_price_mwh" in query:
                self._read_count += 1
                if self._read_count == 1:
                    # Initial cache read: only hour 13 — hour 14 will be
                    # forward-filled, hour 15 will be the real gap.
                    return [
                        {"time": _utc(2026, 5, 19, 13), "lmp_price_mwh": 200.0},
                    ]
                return [
                    {"time": _utc(2026, 5, 19, 13), "lmp_price_mwh": 200.0},
                    {"time": _utc(2026, 5, 19, 14), "lmp_price_mwh": 250.0},
                    {"time": _utc(2026, 5, 19, 15), "lmp_price_mwh": 300.0},
                ]
            # existence-check: only hour 13 is already in the table.
            return [{"time": _utc(2026, 5, 19, 13)}]

    db = _MixedDB()
    fake_api_result = [
        _make_entsoe_price(_utc(2026, 5, 19, 13), 200.0),
        _make_entsoe_price(_utc(2026, 5, 19, 14), 250.0),
        _make_entsoe_price(_utc(2026, 5, 19, 15), 300.0),
    ]

    with patch(
        "src.adapters.entsoe.prices.ENTSOEAdapter.get_day_ahead_prices",
        new=AsyncMock(return_value=fake_api_result),
    ):
        await fetch_or_pull_prices_by_zone(
            db, ZONE, _utc(2026, 5, 19, 13), _utc(2026, 5, 19, 16),
        )

    assert len(db.executemany_calls) == 1, "exactly one INSERT batch"
    _, rows = db.executemany_calls[0]
    # Hour 13 is the existing row — it must NOT be re-inserted.
    inserted_times = {r[0] for r in rows}
    assert _utc(2026, 5, 19, 13) not in inserted_times
    assert _utc(2026, 5, 19, 14) in inserted_times
    assert _utc(2026, 5, 19, 15) in inserted_times
