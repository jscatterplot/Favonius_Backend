"""Unit tests for the Navirec backfill CLI (fakes only — no DB).

Covers the future-dated-row guard and depot-UUID normalization in
``run_backfill``. End-to-end idempotency against a real DB is covered by
``tests/integration/test_navirec_integration.py``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from scripts.backfill_vehicle_telemetry_from_navirec import run_backfill

_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakeConn:
    def __init__(self, vehicle_rows):
        self._vehicle_rows = vehicle_rows
        self.written: list[tuple] = []

    async def fetchval(self, sql, *args):
        if "pg_try_advisory_lock" in sql:
            return True
        if "pg_advisory_unlock" in sql:
            return True
        return None

    async def fetch(self, sql, *args):
        if "FROM vehicles" in sql:
            return self._vehicle_rows
        if "INSERT INTO vehicle_telemetry" in sql:
            rows = [tuple(r) for r in zip(*args)] if args else []
            self.written.extend(rows)
            return [1] * len(rows)
        return []


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
    def __init__(self, vehicles, history):
        self._vehicles = vehicles
        self._history = history

    async def iter_vehicles(self):
        for v in self._vehicles:
            yield v

    async def iter_vehicle_history(self, *, vehicle_id, start_iso, end_iso):
        for h in self._history.get(vehicle_id, []):
            yield h


@pytest.mark.asyncio
async def test_backfill_skips_future_dated_rows():
    depot = str(uuid.uuid4())
    vehicle = str(uuid.uuid4())
    conn = FakeConn([{"vehicle_id": vehicle, "depot_id": depot, "license_plate": "ABC123"}])
    history = {
        "nav-1": [
            {"soc": 50, "timestamp": (_NOW - timedelta(hours=1)).isoformat()},  # past → kept
            {"soc": 60, "timestamp": (_NOW + timedelta(hours=5)).isoformat()},  # future → dropped
        ]
    }
    client = FakeClient([{"id": "nav-1", "licensePlate": "ABC123"}], history)

    summary = await run_backfill(
        FakePool(conn),
        FakePool(conn),
        client,
        since=_NOW - timedelta(days=1),
        until=_NOW,
        execute=True,
    )
    assert summary["planned_rows"] == {depot: 1}
    assert summary["written"] == {depot: 1}
    assert len(conn.written) == 1  # only the past row landed


@pytest.mark.asyncio
async def test_backfill_normalizes_uppercase_depot_id():
    depot = str(uuid.uuid4())  # canonical lowercase, as Postgres returns
    vehicle = str(uuid.uuid4())
    conn = FakeConn([{"vehicle_id": vehicle, "depot_id": depot, "license_plate": "ABC123"}])
    history = {"nav-1": [{"soc": 50, "timestamp": (_NOW - timedelta(hours=1)).isoformat()}]}
    client = FakeClient([{"id": "nav-1", "licensePlate": "ABC123"}], history)

    # Operator passes an UPPERCASE UUID; it must still match site_id::text.
    summary = await run_backfill(
        FakePool(conn),
        FakePool(conn),
        client,
        since=_NOW - timedelta(days=1),
        until=_NOW,
        depot_id=depot.upper(),
        execute=True,
    )
    assert summary["matched_vehicles"] == 1
    assert summary["written"] == {depot: 1}
