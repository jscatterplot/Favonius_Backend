"""Unit tests for odometer-delta distance (src/core/billing/distance.py).

The pure ``_distance_from_bounds`` carries all the edge-case logic, so most
tests hit it directly; a couple exercise ``compute_distances_for_depot`` with a
fake pool to confirm the seeding + row-merge behaviour.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.core.billing.distance import (
    MAX_DISTANCE_KM_PER_WINDOW,
    VehicleDistance,
    _distance_from_bounds,
    compute_distances_for_depot,
)

_START = datetime(2026, 5, 18, 0, 0, tzinfo=timezone.utc)
_END = datetime(2026, 5, 25, 0, 0, tzinfo=timezone.utc)


# ── _distance_from_bounds ─────────────────────────────────────────────────────


def test_monotonic_delta():
    d, reset = _distance_from_bounds(
        first_odo=1000.0, last_odo=1250.0, min_odo=1000.0, max_odo=1250.0, n=5
    )
    assert d == pytest.approx(250.0)
    assert reset is False


def test_exactly_two_readings():
    d, reset = _distance_from_bounds(
        first_odo=100.0, last_odo=142.0, min_odo=100.0, max_odo=142.0, n=2
    )
    assert d == pytest.approx(42.0)
    assert reset is False


def test_fewer_than_two_readings_is_unknown():
    d, reset = _distance_from_bounds(
        first_odo=100.0, last_odo=100.0, min_odo=100.0, max_odo=100.0, n=1
    )
    assert d is None
    assert reset is False


def test_zero_readings_is_unknown():
    d, reset = _distance_from_bounds(first_odo=None, last_odo=None, min_odo=None, max_odo=None, n=0)
    assert d is None


def test_no_movement_is_zero_not_none():
    # Two identical readings = parked all week = a real, measured 0 km.
    d, reset = _distance_from_bounds(
        first_odo=500.0, last_odo=500.0, min_odo=500.0, max_odo=500.0, n=3
    )
    assert d == pytest.approx(0.0)
    assert reset is False


def test_reset_recovered():
    # Ran 1000→1200 (max), odometer reset, ran 0→50 (last). Distance = 200 + 50.
    d, reset = _distance_from_bounds(
        first_odo=1000.0, last_odo=50.0, min_odo=0.0, max_odo=1200.0, n=10
    )
    assert d == pytest.approx(250.0)
    assert reset is True


def test_rollover_recovered():
    # 32-bit-ish rollover: first near a ceiling, last small, min=last, max=first.
    d, reset = _distance_from_bounds(
        first_odo=999_900.0, last_odo=120.0, min_odo=120.0, max_odo=999_900.0, n=8
    )
    # (max-first) = 0, (last-min) = 0 → recovered 0; coherent (no negative).
    assert d == pytest.approx(0.0)
    assert reset is True


def test_reset_incoherent_is_unknown():
    # A backwards delta whose recovery would exceed the sanity ceiling → unknown.
    d, reset = _distance_from_bounds(
        first_odo=50.0,
        last_odo=10.0,
        min_odo=0.0,
        max_odo=MAX_DISTANCE_KM_PER_WINDOW + 5000.0,
        n=4,
    )
    assert d is None
    assert reset is True


def test_monotonic_above_ceiling_is_unknown():
    # A single absurd jump (garbage CAN value) is rejected, not trusted.
    d, reset = _distance_from_bounds(
        first_odo=0.0,
        last_odo=MAX_DISTANCE_KM_PER_WINDOW + 1.0,
        min_odo=0.0,
        max_odo=MAX_DISTANCE_KM_PER_WINDOW + 1.0,
        n=2,
    )
    assert d is None
    assert reset is True


# ── compute_distances_for_depot (fake pool) ───────────────────────────────────


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, sql, *args):
        return self._rows


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, rows):
        self._conn = _FakeConn(rows)

    def acquire(self):
        return _Acquire(self._conn)


@pytest.mark.asyncio
async def test_empty_vehicle_list_returns_empty():
    out = await compute_distances_for_depot(_FakePool([]), vehicle_ids=[], start=_START, end=_END)
    assert out == {}


@pytest.mark.asyncio
async def test_seeds_unknown_for_vehicles_without_readings():
    # v2 has no row in the query result → must still appear, as unknown.
    rows = [
        {
            "vehicle_id": "v1",
            "first_odo": 1000.0,
            "last_odo": 1300.0,
            "min_odo": 1000.0,
            "max_odo": 1300.0,
            "n": 4,
        }
    ]
    out = await compute_distances_for_depot(
        _FakePool(rows), vehicle_ids=["v1", "v2"], start=_START, end=_END
    )
    assert out["v1"].distance_km == pytest.approx(300.0)
    assert out["v1"].reading_count == 4
    assert isinstance(out["v2"], VehicleDistance)
    assert out["v2"].distance_km is None
    assert out["v2"].reading_count == 0


@pytest.mark.asyncio
async def test_reset_flagged_through_pool():
    rows = [
        {
            "vehicle_id": "v1",
            "first_odo": 1000.0,
            "last_odo": 50.0,
            "min_odo": 0.0,
            "max_odo": 1200.0,
            "n": 6,
        }
    ]
    out = await compute_distances_for_depot(
        _FakePool(rows), vehicle_ids=["v1"], start=_START, end=_END
    )
    assert out["v1"].distance_km == pytest.approx(250.0)
    assert out["v1"].had_reset is True
