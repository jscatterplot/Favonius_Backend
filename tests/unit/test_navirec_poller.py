"""Unit tests for the Navirec poller (fakes only — no DB).

Covers plate-map ambiguity dropping, the advisory-lock write contract (with
true-insert counting via the unnest+RETURNING upsert), per-depot failure
isolation, unmatched-plate counting, and stale / future-dated dropping. The
advisory-lock semantics against a real Postgres are covered by the integration
suite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.navirec.mapping import VehicleTelemetryReading
from src.adapters.navirec.poller import (
    _UPSERT_SQL,
    _reading_to_row,
    build_plate_map,
    poll_once,
    resolve_readings,
    write_depot_readings,
)

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakeConn:
    def __init__(self, *, lock_result=True, raise_for_vehicle=None, recorder=None, fetch_rows=None):
        self.lock_result = lock_result
        self.raise_for_vehicle = raise_for_vehicle
        self.recorder = recorder if recorder is not None else []
        self.fetch_rows = fetch_rows or []
        self.unlock_called = False
        self.insert_calls = 0

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock" in sql:
            return self.lock_result
        if "pg_advisory_unlock" in sql:
            self.unlock_called = True
            return True
        return None

    async def fetch(self, sql, *args):
        if "INSERT INTO vehicle_telemetry" in sql:
            self.insert_calls += 1
            # args are the 7 per-column arrays; transpose back into row tuples.
            rows = [tuple(r) for r in zip(*args)] if args else []
            if self.raise_for_vehicle is not None and any(
                r[1] == self.raise_for_vehicle for r in rows
            ):
                raise RuntimeError("simulated write failure")
            self.recorder.extend(rows)
            return [1] * len(rows)  # one RETURNING row per actual insert
        return self.fetch_rows  # build_plate_map's SELECT


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)


class FakeClient:
    def __init__(self, raws):
        self._raws = raws

    async def iter_vehicles(self):
        for r in self._raws:
            yield r


def _reading(plate, soc=0.5, *, time=_NOW):
    return VehicleTelemetryReading(vehicle_plate=plate, soc=soc, time=time)


# --------------------------------------------------------------------------
# _reading_to_row / upsert column contract
# --------------------------------------------------------------------------
def test_reading_to_row_includes_odometer_last():
    r = VehicleTelemetryReading(vehicle_plate="X", soc=0.5, time=_NOW, odometer_km=12345.0)
    row = _reading_to_row("vX", r)
    # (time, vehicle_id, soc, lat, lon, source, raw_fields, odometer_km)
    assert len(row) == 8
    assert row[0] == _NOW
    assert row[1] == "vX"
    assert row[2] == 0.5
    assert row[5] == "navirec"
    assert row[7] == 12345.0


def test_reading_to_row_odometer_none_when_absent():
    r = VehicleTelemetryReading(vehicle_plate="X", soc=0.5, time=_NOW)
    assert _reading_to_row("vX", r)[7] is None


def test_upsert_sql_column_and_array_counts_match():
    # The unnest() array count must match the column list, or the insert fails
    # at runtime. Guard the 8-column / 8-array contract here.
    assert _UPSERT_SQL.count("::") == 8  # eight typed unnest arrays
    assert "odometer_km" in _UPSERT_SQL
    assert _UPSERT_SQL.count("$") == 8


# --------------------------------------------------------------------------
# resolve_readings
# --------------------------------------------------------------------------
def test_resolve_groups_by_depot_and_counts_unmatched():
    plate_map = {"AAA": ("vA", "dA"), "BBB": ("vB", "dB")}
    readings = [_reading("AAA"), _reading("BBB"), _reading("CCC")]
    rows_by_depot, unmatched = resolve_readings(readings, plate_map)
    assert unmatched == 1
    assert set(rows_by_depot) == {"dA", "dB"}
    assert rows_by_depot["dA"][0][1] == "vA"  # vehicle_id column


# --------------------------------------------------------------------------
# build_plate_map
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_build_plate_map_drops_ambiguous():
    rows = [
        {"vehicle_id": "v1", "depot_id": "d1", "license_plate": "ABC 123"},
        {"vehicle_id": "v2", "depot_id": "d1", "license_plate": "abc-123"},  # collides with v1
        {"vehicle_id": "v3", "depot_id": "d2", "license_plate": "XYZ-9"},
        {
            "vehicle_id": "v3",
            "depot_id": "d2",
            "license_plate": "xyz9",
        },  # same vehicle, not ambiguous
    ]
    pool = FakePool(FakeConn(fetch_rows=rows))
    plate_map = await build_plate_map(pool)
    assert "ABC123" not in plate_map  # ambiguous → dropped
    assert plate_map["XYZ9"] == ("v3", "d2")


# --------------------------------------------------------------------------
# write_depot_readings
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_write_acquires_lock_and_counts_true_inserts():
    conn = FakeConn(lock_result=True)
    rows = [(_NOW, "vA", 0.5, None, None, "navirec", "{}")]
    written = await write_depot_readings(FakePool(conn), "dA", rows)
    assert written == 1
    assert conn.recorder == rows
    assert conn.unlock_called is True


@pytest.mark.asyncio
async def test_write_skips_when_lock_unavailable():
    conn = FakeConn(lock_result=False)
    rows = [(_NOW, "vA", 0.5, None, None, "navirec", "{}")]
    written = await write_depot_readings(FakePool(conn), "dA", rows)
    assert written is None
    assert conn.insert_calls == 0
    assert conn.unlock_called is False  # never acquired → never unlocks


@pytest.mark.asyncio
async def test_write_empty_rows_is_noop():
    conn = FakeConn(lock_result=True)
    assert await write_depot_readings(FakePool(conn), "dA", []) == 0
    assert conn.insert_calls == 0


# --------------------------------------------------------------------------
# poll_once
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_poll_once_isolates_depot_failure():
    plate_map = {"PA": ("vA", "dA"), "PB": ("vB", "dB")}
    raws = [
        {"plate": "PA", "soc": 50, "timestamp": _NOW.isoformat()},
        {"plate": "PB", "soc": 60, "timestamp": _NOW.isoformat()},
    ]
    conn = FakeConn(lock_result=True, raise_for_vehicle="vB")
    summary = await poll_once(None, FakePool(conn), FakeClient(raws), plate_map=plate_map, now=_NOW)
    assert summary["written"] == {"dA": 1}
    assert summary["failed_depots"] == ["dB"]
    # depot A's row landed; depot B's never did
    assert [r[1] for r in conn.recorder] == ["vA"]


@pytest.mark.asyncio
async def test_poll_once_drops_stale_beyond_window():
    plate_map = {"PA": ("vA", "dA"), "PB": ("vB", "dB")}
    raws = [
        {"plate": "PA", "soc": 50, "timestamp": (_NOW - timedelta(hours=48)).isoformat()},
        {"plate": "PB", "soc": 60, "timestamp": _NOW.isoformat()},
    ]
    conn = FakeConn(lock_result=True)
    summary = await poll_once(None, FakePool(conn), FakeClient(raws), plate_map=plate_map, now=_NOW)
    assert summary["stale_dropped"] == 1
    assert summary["written"] == {"dB": 1}
    assert "dA" not in summary["written"]


@pytest.mark.asyncio
async def test_poll_once_drops_future_dated():
    # Clock-skewed future timestamp would always win freshest-wins → must drop.
    plate_map = {"PA": ("vA", "dA"), "PB": ("vB", "dB")}
    raws = [
        {"plate": "PA", "soc": 50, "timestamp": (_NOW + timedelta(hours=2)).isoformat()},
        {"plate": "PB", "soc": 60, "timestamp": _NOW.isoformat()},
    ]
    conn = FakeConn(lock_result=True)
    summary = await poll_once(None, FakePool(conn), FakeClient(raws), plate_map=plate_map, now=_NOW)
    assert summary["stale_dropped"] == 1
    assert summary["written"] == {"dB": 1}
    assert "dA" not in summary["written"]


@pytest.mark.asyncio
async def test_poll_once_drops_future_timestamps():
    # Even a small forward skew is dropped (any age < 0).
    plate_map = {"PA": ("vA", "dA"), "PB": ("vB", "dB")}
    raws = [
        {"plate": "PA", "soc": 50, "timestamp": (_NOW + timedelta(minutes=5)).isoformat()},
        {"plate": "PB", "soc": 60, "timestamp": _NOW.isoformat()},
    ]
    conn = FakeConn(lock_result=True)
    summary = await poll_once(None, FakePool(conn), FakeClient(raws), plate_map=plate_map, now=_NOW)
    assert summary["stale_dropped"] == 1
    assert summary["written"] == {"dB": 1}
    assert "dA" not in summary["written"]


@pytest.mark.asyncio
async def test_poll_once_counts_unmatched():
    raws = [{"plate": "ZZZ", "soc": 50, "timestamp": _NOW.isoformat()}]
    conn = FakeConn(lock_result=True)
    summary = await poll_once(None, FakePool(conn), FakeClient(raws), plate_map={}, now=_NOW)
    assert summary["unmatched"] == 1
    assert summary["written"] == {}


@pytest.mark.asyncio
async def test_poll_once_sanitizes_non_finite_raw_fields():
    # NaN/Infinity in raw_fields must not produce invalid jsonb (would fail the
    # whole depot batch insert in Postgres).
    import json

    plate_map = {"PA": ("vA", "dA")}
    raws = [
        {"plate": "PA", "soc": 50, "timestamp": _NOW.isoformat(), "weird": float("nan")},
    ]
    conn = FakeConn(lock_result=True)
    await poll_once(None, FakePool(conn), FakeClient(raws), plate_map=plate_map, now=_NOW)
    payload = conn.recorder[0][6]  # raw_fields JSON column
    assert "NaN" not in payload and "Infinity" not in payload
    assert json.loads(payload)["weird"] is None
