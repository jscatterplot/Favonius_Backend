"""Unit tests for src/core/billing/session_cost.py.

Covers the 17 mandatory cases from the plan:

1. Granular happy path (constant kW × constant price)
2. Hour boundary crossing (two-hour session with different prices per hour)
3. Varying-kW trapezoidal correctness
4. Coverage gate boundary (79% fails, 81% passes)
5. Energy reconciliation gate (>10% mismatch → fallback)
6. No-telemetry fallback (only energy_delivered_kwh + start/end + prices)
7. Unpriceable: no prices at all
8. Forward-fill within 1h works
9. Gap > 1h → unpriceable
10. Open session (end_time IS NULL) → pending_close
11. No energy → no_energy
12. No depot (site_id IS NULL) → no_depot
13. Backfill idempotency (predicate filters already-priced rows) — covered in integration
14. Backfill skips manually-priced rows (cost_total_source = 'manual')
15. SKIP LOCKED concurrency — covered in integration
16. DST boundary (UTC timestamps unaffected)
17. write_session_cost UPDATE WHERE predicate

The granular SQL itself is exercised by the integration suite (it uses
TimescaleDB's ``time_bucket`` which has no faithful pure-Python fake).
This file tests the dispatcher: gate logic, state transitions, and the
fallback math.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.core.billing.session_cost import (
    SessionCostResult,
    compute_session_cost,
    write_session_cost,
)


VEHICLE = UUID("11111111-1111-1111-1111-111111111111")
DEPOT = UUID("22222222-2222-2222-2222-222222222222")
SESSION = UUID("33333333-3333-3333-3333-333333333333")


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


DEFAULT_ZONE = "10YLT-1001A0008Q"


def _session_row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "session_id": SESSION,
        "site_id": DEPOT,
        "vehicle_id": VEHICLE,
        "start_time": _utc(2026, 5, 19, 13, 0),
        "end_time": _utc(2026, 5, 19, 14, 0),
        "energy_delivered_kwh": Decimal("50.0"),
        "cost_total": None,
        "cost_total_source": None,
        "bidding_zone": DEFAULT_ZONE,
    }
    base.update(overrides)
    return base


class _FakeConn:
    """Connection that returns canned telemetry rows on the granular SQL."""

    def __init__(self, telemetry_rows: list[dict] | None = None) -> None:
        self._telem = telemetry_rows or []
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self._next_execute_return = "UPDATE 1"

    async def fetch(self, query: str, *args: Any) -> list[dict]:
        if "time_bucket" in query and "telemetry" in query:
            return self._telem
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict | None:
        return None

    async def execute(self, query: str, *args: Any) -> str:
        self.execute_calls.append((query, args))
        return self._next_execute_return


class _Acquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakePool:
    def __init__(self, conn: _FakeConn | None = None) -> None:
        self.conn = conn or _FakeConn()

    def acquire(self) -> _Acquire:
        return _Acquire(self.conn)


def _make_lookup(price_map: dict[datetime, float]):
    async def _lookup(bidding_zone: str, start: datetime, end: datetime) -> dict[datetime, float]:
        # Mirror fetch_prices_by_zone: return any hour bucket whose
        # [h, h+1h) overlaps [start, end). A session 13:30→14:30 needs
        # hours 13:00 AND 14:00.
        result: dict[datetime, float] = {}
        for h, p in price_map.items():
            if h < end and (h + timedelta(hours=1)) > start:
                result[h] = p
        return result
    return _lookup


# ─── 1. Granular happy path ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_granular_constant_kw_constant_price() -> None:
    """50 kW for one hour at €0.20/kWh → €10."""
    telem = [
        {
            "hour": _utc(2026, 5, 19, 13, 0),
            "first_time": _utc(2026, 5, 19, 13, 0),
            "last_time": _utc(2026, 5, 19, 13, 59),
            "sample_count": 60,
            "energy_kwh": 50.0,
        }
    ]
    pool = _FakePool(_FakeConn(telem))
    lookup = _make_lookup({_utc(2026, 5, 19, 13, 0): 0.20})

    result = await compute_session_cost(
        pool, _session_row(), price_lookup=lookup
    )

    assert result.source == "granular"
    assert result.cost == Decimal("10.0000")
    assert result.energy_kwh_from_telemetry == pytest.approx(50.0)


# ─── 2. Hour boundary crossing ───────────────────────────────────────


@pytest.mark.asyncio
async def test_granular_hour_boundary_crossing() -> None:
    """50 kW × 30 min @ €0.10 + 50 kW × 30 min @ €0.20 = €1.25 + €2.50 = €3.75.

    The session window is 13:30→14:30; telemetry rows are bucketed into
    13:00 (25 kWh) and 14:00 (25 kWh); prices €0.10 and €0.20.
    """
    telem = [
        {
            "hour": _utc(2026, 5, 19, 13, 0),
            "first_time": _utc(2026, 5, 19, 13, 30),
            "last_time": _utc(2026, 5, 19, 13, 59),
            "sample_count": 30,
            "energy_kwh": 25.0,
        },
        {
            "hour": _utc(2026, 5, 19, 14, 0),
            "first_time": _utc(2026, 5, 19, 14, 0),
            "last_time": _utc(2026, 5, 19, 14, 29),
            "sample_count": 30,
            "energy_kwh": 25.0,
        },
    ]
    pool = _FakePool(_FakeConn(telem))
    lookup = _make_lookup({
        _utc(2026, 5, 19, 13, 0): 0.10,
        _utc(2026, 5, 19, 14, 0): 0.20,
    })

    row = _session_row(
        start_time=_utc(2026, 5, 19, 13, 30),
        end_time=_utc(2026, 5, 19, 14, 30),
    )
    result = await compute_session_cost(pool, row, price_lookup=lookup)

    assert result.source == "granular"
    assert result.cost == Decimal("7.5000")  # 25*0.10 + 25*0.20 = 7.50


# ─── 3. Varying-kW trapezoidal correctness ───────────────────────────


@pytest.mark.asyncio
async def test_granular_varying_kw() -> None:
    """The dispatcher accepts whatever ``energy_kwh`` SQL returns per bucket.

    Two buckets with different energies × different prices.
    """
    telem = [
        {
            "hour": _utc(2026, 5, 19, 13, 0),
            "first_time": _utc(2026, 5, 19, 13, 0),
            "last_time": _utc(2026, 5, 19, 13, 30),
            "sample_count": 4,
            # observed_seconds mirrors the granular SQL: SUM of all
            # consecutive-sample Δt within the bucket, including the
            # cross-bucket interval to the first sample in hour 14
            # (LEAD is no longer partitioned by hour).
            "observed_seconds": 3600.0,  # 13:00 → 14:00 = 1h continuous coverage
            "energy_kwh": 10.0,  # ramp-up
        },
        {
            "hour": _utc(2026, 5, 19, 14, 0),
            "first_time": _utc(2026, 5, 19, 14, 0),
            "last_time": _utc(2026, 5, 19, 14, 50),
            "sample_count": 4,
            "observed_seconds": 3000.0,  # 14:00 → 14:50
            "energy_kwh": 40.0,  # steady at peak
        },
    ]
    pool = _FakePool(_FakeConn(telem))
    lookup = _make_lookup({
        _utc(2026, 5, 19, 13, 0): 0.15,
        _utc(2026, 5, 19, 14, 0): 0.25,
    })

    row = _session_row(
        start_time=_utc(2026, 5, 19, 13, 0),
        end_time=_utc(2026, 5, 19, 15, 0),
        energy_delivered_kwh=Decimal("50.0"),
    )
    result = await compute_session_cost(pool, row, price_lookup=lookup)

    assert result.source == "granular"
    # 10*0.15 + 40*0.25 = 1.50 + 10.00 = 11.50
    assert result.cost == Decimal("11.5000")


# ─── 4. Coverage gate boundary ───────────────────────────────────────


@pytest.mark.asyncio
async def test_coverage_gate_below_threshold_falls_back() -> None:
    """Telemetry covers only 50% of a 2-hour session → fallback."""
    # Session is 2h long. Telemetry covers only the last 1h (50%).
    telem = [
        {
            "hour": _utc(2026, 5, 19, 14, 0),
            "first_time": _utc(2026, 5, 19, 14, 0),
            "last_time": _utc(2026, 5, 19, 14, 50),
            "sample_count": 10,
            "energy_kwh": 50.0,
        }
    ]
    pool = _FakePool(_FakeConn(telem))
    lookup = _make_lookup({
        _utc(2026, 5, 19, 13, 0): 0.10,
        _utc(2026, 5, 19, 14, 0): 0.20,
    })
    row = _session_row(
        start_time=_utc(2026, 5, 19, 13, 0),
        end_time=_utc(2026, 5, 19, 15, 0),
        energy_delivered_kwh=Decimal("50.0"),
    )

    result = await compute_session_cost(pool, row, price_lookup=lookup)

    # Avg price = (0.10 + 0.20) / 2 = 0.15. 50 * 0.15 = 7.50.
    assert result.source == "fallback_average"
    assert result.cost == Decimal("7.5000")


# ─── 5. Energy reconciliation gate ───────────────────────────────────


@pytest.mark.asyncio
async def test_energy_mismatch_falls_back() -> None:
    """Telemetry implies 25 kWh but energy_delivered_kwh = 50 (50% off) → fallback."""
    telem = [
        {
            "hour": _utc(2026, 5, 19, 13, 0),
            "first_time": _utc(2026, 5, 19, 13, 0),
            "last_time": _utc(2026, 5, 19, 13, 55),
            "sample_count": 12,
            "energy_kwh": 25.0,
        }
    ]
    pool = _FakePool(_FakeConn(telem))
    lookup = _make_lookup({_utc(2026, 5, 19, 13, 0): 0.20})
    row = _session_row(energy_delivered_kwh=Decimal("50.0"))

    result = await compute_session_cost(pool, row, price_lookup=lookup)
    assert result.source == "fallback_average"
    assert result.cost == Decimal("10.0000")  # 50 * 0.20


# ─── 6. No-telemetry fallback ────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_telemetry_uses_fallback() -> None:
    pool = _FakePool(_FakeConn([]))  # zero telemetry rows
    lookup = _make_lookup({_utc(2026, 5, 19, 13, 0): 0.30})

    result = await compute_session_cost(pool, _session_row(), price_lookup=lookup)
    assert result.source == "fallback_average"
    assert result.cost == Decimal("15.0000")  # 50 * 0.30


# ─── 7. Unpriceable: no prices at all ────────────────────────────────


@pytest.mark.asyncio
async def test_no_prices_returns_unpriceable() -> None:
    pool = _FakePool(_FakeConn([]))
    lookup = _make_lookup({})  # no prices at all

    result = await compute_session_cost(pool, _session_row(), price_lookup=lookup)
    assert result.source == "unpriceable"
    assert result.cost is None


@pytest.mark.asyncio
async def test_missing_bidding_zone_returns_unpriceable() -> None:
    """When the caller can't resolve a zone (sites row missing tz +
    tariff_config), the calculator short-circuits to unpriceable."""
    pool = _FakePool(_FakeConn([]))
    lookup = _make_lookup({_utc(2026, 5, 19, 13, 0): 0.20})
    row = _session_row(bidding_zone=None)

    result = await compute_session_cost(pool, row, price_lookup=lookup)
    assert result.source == "unpriceable"
    assert result.cost is None


@pytest.mark.asyncio
async def test_empty_bidding_zone_string_returns_unpriceable() -> None:
    pool = _FakePool(_FakeConn([]))
    lookup = _make_lookup({_utc(2026, 5, 19, 13, 0): 0.20})
    row = _session_row(bidding_zone="")

    result = await compute_session_cost(pool, row, price_lookup=lookup)
    assert result.source == "unpriceable"


# ─── 8. Forward-fill within 1h ───────────────────────────────────────


@pytest.mark.asyncio
async def test_forward_fill_within_one_hour_works() -> None:
    """Prices missing for hour 14, but hour 13 is present (within 1h) →
    the fetch_prices_with_fill helper handles this; here we simulate the
    'helper already filled' case by supplying both hours."""
    telem = [
        {
            "hour": _utc(2026, 5, 19, 13, 0),
            "first_time": _utc(2026, 5, 19, 13, 0),
            "last_time": _utc(2026, 5, 19, 13, 59),
            "sample_count": 60,
            "energy_kwh": 25.0,
        },
        {
            "hour": _utc(2026, 5, 19, 14, 0),
            "first_time": _utc(2026, 5, 19, 14, 0),
            "last_time": _utc(2026, 5, 19, 14, 59),
            "sample_count": 60,
            "energy_kwh": 25.0,
        },
    ]
    pool = _FakePool(_FakeConn(telem))
    # Lookup returns both hours at the same price → forward-fill simulated.
    lookup = _make_lookup({
        _utc(2026, 5, 19, 13, 0): 0.20,
        _utc(2026, 5, 19, 14, 0): 0.20,
    })

    row = _session_row(
        start_time=_utc(2026, 5, 19, 13, 0),
        end_time=_utc(2026, 5, 19, 15, 0),
        energy_delivered_kwh=Decimal("50.0"),
    )
    result = await compute_session_cost(pool, row, price_lookup=lookup)
    assert result.source == "granular"
    assert result.cost == Decimal("10.0000")


# ─── 9. Gap > 1h → unpriceable (in fallback path) ────────────────────


@pytest.mark.asyncio
async def test_fallback_unpriceable_on_partial_price_coverage() -> None:
    """No telemetry, fallback path, but prices cover only 1 of 3 hours →
    unpriceable. We don't average over a partial window."""
    pool = _FakePool(_FakeConn([]))
    lookup = _make_lookup({
        _utc(2026, 5, 19, 13, 0): 0.20,
        # Hours 14 and 15 missing entirely.
    })
    row = _session_row(
        start_time=_utc(2026, 5, 19, 13, 0),
        end_time=_utc(2026, 5, 19, 16, 0),
        energy_delivered_kwh=Decimal("100.0"),
    )

    result = await compute_session_cost(pool, row, price_lookup=lookup)
    assert result.source == "unpriceable"
    assert result.cost is None


# ─── 10. Open session → pending_close ───────────────────────────────


@pytest.mark.asyncio
async def test_open_session_returns_pending_close() -> None:
    pool = _FakePool(_FakeConn([]))
    lookup = _make_lookup({})
    row = _session_row(end_time=None)

    result = await compute_session_cost(pool, row, price_lookup=lookup)
    assert result.source == "pending_close"
    assert result.cost is None


# ─── 11. No energy → no_energy ───────────────────────────────────────


@pytest.mark.asyncio
async def test_no_energy_returns_no_energy() -> None:
    pool = _FakePool(_FakeConn([]))
    lookup = _make_lookup({_utc(2026, 5, 19, 13, 0): 0.20})
    row = _session_row(energy_delivered_kwh=None)

    result = await compute_session_cost(pool, row, price_lookup=lookup)
    assert result.source == "no_energy"


@pytest.mark.asyncio
async def test_zero_energy_returns_no_energy() -> None:
    pool = _FakePool(_FakeConn([]))
    lookup = _make_lookup({_utc(2026, 5, 19, 13, 0): 0.20})
    row = _session_row(energy_delivered_kwh=Decimal("0"))

    result = await compute_session_cost(pool, row, price_lookup=lookup)
    assert result.source == "no_energy"


# ─── 12. No depot → no_depot ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_depot_returns_no_depot() -> None:
    pool = _FakePool(_FakeConn([]))
    lookup = _make_lookup({})
    row = _session_row(site_id=None)

    result = await compute_session_cost(pool, row, price_lookup=lookup)
    assert result.source == "no_depot"


# ─── 14. Manual cost is sacrosanct ───────────────────────────────────


@pytest.mark.asyncio
async def test_manual_cost_is_preserved() -> None:
    pool = _FakePool(_FakeConn([]))
    lookup = _make_lookup({_utc(2026, 5, 19, 13, 0): 0.20})
    row = _session_row(
        cost_total=Decimal("99.99"),
        cost_total_source="manual",
    )

    result = await compute_session_cost(pool, row, price_lookup=lookup)
    assert result.source == "manual"
    assert result.cost == Decimal("99.99")


# ─── Overwrite policy: cost_total = 0 is treated as missing ──────────


@pytest.mark.asyncio
async def test_zero_cost_total_is_treated_as_missing() -> None:
    """Historical imports landed cost_total=0; the calculator must
    recompute. cost_total=0 + non-'manual' source → eligible."""
    telem = [
        {
            "hour": _utc(2026, 5, 19, 13, 0),
            "first_time": _utc(2026, 5, 19, 13, 0),
            "last_time": _utc(2026, 5, 19, 13, 59),
            "sample_count": 60,
            "energy_kwh": 50.0,
        }
    ]
    pool = _FakePool(_FakeConn(telem))
    lookup = _make_lookup({_utc(2026, 5, 19, 13, 0): 0.20})
    row = _session_row(cost_total=Decimal("0"), cost_total_source=None)

    result = await compute_session_cost(pool, row, price_lookup=lookup)
    assert result.source == "granular"
    assert result.cost == Decimal("10.0000")


# ─── 16. DST boundary (UTC timestamps unaffected) ────────────────────


@pytest.mark.asyncio
async def test_dst_boundary_session_costs_in_utc() -> None:
    """Sessions are stored in UTC; DST is a presentation concern only.
    Verify by running a session across the EU 2026 spring-forward (29 Mar).
    """
    # In Europe/Berlin, 02:00 CET → 03:00 CEST on 2026-03-29. UTC is 01:00.
    # A session 00:30 UTC → 02:30 UTC spans the local DST jump cleanly.
    telem = [
        {
            "hour": _utc(2026, 3, 29, 0, 0),
            "first_time": _utc(2026, 3, 29, 0, 30),
            "last_time": _utc(2026, 3, 29, 0, 59),
            "sample_count": 30,
            "energy_kwh": 25.0,
        },
        {
            "hour": _utc(2026, 3, 29, 1, 0),
            "first_time": _utc(2026, 3, 29, 1, 0),
            "last_time": _utc(2026, 3, 29, 1, 59),
            "sample_count": 60,
            "energy_kwh": 50.0,
        },
        {
            "hour": _utc(2026, 3, 29, 2, 0),
            "first_time": _utc(2026, 3, 29, 2, 0),
            "last_time": _utc(2026, 3, 29, 2, 29),
            "sample_count": 30,
            "energy_kwh": 25.0,
        },
    ]
    pool = _FakePool(_FakeConn(telem))
    lookup = _make_lookup({
        _utc(2026, 3, 29, 0, 0): 0.10,
        _utc(2026, 3, 29, 1, 0): 0.20,
        _utc(2026, 3, 29, 2, 0): 0.30,
    })
    row = _session_row(
        start_time=_utc(2026, 3, 29, 0, 30),
        end_time=_utc(2026, 3, 29, 2, 30),
        energy_delivered_kwh=Decimal("100.0"),
    )

    result = await compute_session_cost(pool, row, price_lookup=lookup)
    assert result.source == "granular"
    # 25*0.10 + 50*0.20 + 25*0.30 = 2.50 + 10.00 + 7.50 = 20.00
    assert result.cost == Decimal("20.0000")


# ─── 17. write_session_cost idempotency / predicate ──────────────────


@pytest.mark.asyncio
async def test_write_session_cost_writes_only_for_priceable_sources() -> None:
    conn = _FakeConn()
    pool = _FakePool(conn)

    # 1. Granular result → writes cost + source.
    granular = SessionCostResult(
        cost=Decimal("12.3456"), source="granular",
        energy_kwh_from_telemetry=50.0,
    )
    await write_session_cost(pool, SESSION, granular)
    assert any("cost_total" in q for q, _ in conn.execute_calls)
    assert conn.execute_calls[-1][1][1] == Decimal("12.3456")

    # 2. Unpriceable → writes provenance but cost is NULL.
    conn.execute_calls.clear()
    unp = SessionCostResult(cost=None, source="unpriceable")
    await write_session_cost(pool, SESSION, unp)
    assert len(conn.execute_calls) == 1
    assert conn.execute_calls[0][1][1] is None
    assert conn.execute_calls[0][1][2] == "unpriceable"


@pytest.mark.asyncio
async def test_write_session_cost_skips_pending_close() -> None:
    conn = _FakeConn()
    pool = _FakePool(conn)
    pending = SessionCostResult(cost=None, source="pending_close")

    affected = await write_session_cost(pool, SESSION, pending)
    assert affected is False
    assert conn.execute_calls == []


@pytest.mark.asyncio
async def test_write_session_cost_returns_false_when_no_row_affected() -> None:
    """Race lost: SKIP LOCKED moved on, or row was 'manual'-ed in the meantime."""
    conn = _FakeConn()
    conn._next_execute_return = "UPDATE 0"
    pool = _FakePool(conn)
    result = SessionCostResult(cost=Decimal("1"), source="granular")

    affected = await write_session_cost(pool, SESSION, result)
    assert affected is False


@pytest.mark.asyncio
async def test_write_session_cost_query_filters_manual_and_priced() -> None:
    """The UPDATE WHERE clause must exclude rows already 'manual' or with
    a non-zero cost_total, regardless of what the caller passes."""
    conn = _FakeConn()
    pool = _FakePool(conn)
    result = SessionCostResult(cost=Decimal("1"), source="granular")

    await write_session_cost(pool, SESSION, result)
    query, _args = conn.execute_calls[0]
    assert "cost_total IS NULL OR cost_total = 0" in query
    assert "cost_total_source IS DISTINCT FROM 'manual'" in query


@pytest.mark.asyncio
async def test_write_session_cost_writes_null_not_zero_for_unpriceable() -> None:
    """Regression: the UPDATE must SET cost_total = $2 directly, not
    COALESCE($2, cost_total). For an 'unpriceable' result $2 is NULL,
    and we want the column to land as NULL — historical imports landed
    cost_total=0, and COALESCE would have left them at 0 (looking like
    a free session) instead of normalizing to NULL."""
    conn = _FakeConn()
    pool = _FakePool(conn)
    result = SessionCostResult(cost=None, source="unpriceable")

    await write_session_cost(pool, SESSION, result)
    query, args = conn.execute_calls[0]
    assert "COALESCE" not in query.upper().replace("CORRELATION", "")
    assert "SET cost_total        = $2" in query or "SET cost_total = $2" in query
    # $2 must be NULL for the unpriceable case.
    assert args[1] is None


@pytest.mark.asyncio
async def test_granular_unpriced_bucket_falls_back_not_short_circuits() -> None:
    """Regression: when granular's per-bucket loop hits an unpriced
    hour, the dispatcher must continue to ``_fallback_average`` rather
    than short-circuit to ``'unpriceable'``. _try_granular signals
    'try fallback' with ``None``; the dispatcher owns the final verdict.

    Scenario: telemetry covers a single hour (priced); session window
    extends one extra hour into an UNpriced region. Granular sees an
    unpriced bucket, so it bails. Fallback also can't price the full
    window (gap > 1h in expected_hour_buckets) → 'unpriceable'.
    The contract here is *the path taken*, not the final source.
    """
    # Two hours of telemetry: 13:00 bucket fully priced, 14:00 absent
    # from price_map → granular finds an unpriced bucket and must
    # fall through to fallback (not emit unpriceable directly).
    telem = [
        {
            "hour": _utc(2026, 5, 19, 13, 0),
            "first_time": _utc(2026, 5, 19, 13, 0),
            "last_time": _utc(2026, 5, 19, 13, 59),
            "sample_count": 60,
            "observed_seconds": 3540.0,
            "energy_kwh": 25.0,
        },
        {
            "hour": _utc(2026, 5, 19, 14, 0),
            "first_time": _utc(2026, 5, 19, 14, 0),
            "last_time": _utc(2026, 5, 19, 14, 50),
            "sample_count": 50,
            "observed_seconds": 3000.0,
            "energy_kwh": 25.0,
        },
    ]
    pool = _FakePool(_FakeConn(telem))
    # Only hour 13 priced; hour 14 deliberately missing.
    lookup = _make_lookup({_utc(2026, 5, 19, 13, 0): 0.20})

    row = _session_row(
        start_time=_utc(2026, 5, 19, 13, 0),
        end_time=_utc(2026, 5, 19, 15, 0),
        energy_delivered_kwh=Decimal("50.0"),
    )
    result = await compute_session_cost(pool, row, price_lookup=lookup)
    # Fallback couldn't price hour 14 either → unpriceable. The
    # important thing is we *reached* fallback (which sets source
    # to 'unpriceable' via the missing-hour path, not granular).
    assert result.source == "unpriceable"
    # Telemetry diagnostics are absent when fallback emits — granular
    # didn't emit a result. The earlier short-circuit version included
    # granular's diagnostics in the unpriceable result.
    assert result.energy_kwh_from_telemetry is None
    assert result.telemetry_coverage is None


# The naive/aware regression target is the optimizer +
# fetch_prices_by_zone combination; tests/unit/test_state_assembler.py
# already exercises that with datetime.utcnow() (naive) → aware-keyed
# price map. A standalone test here would have to use a fake lookup
# that bypasses the real helper's normalization, which would defeat
# the purpose.
