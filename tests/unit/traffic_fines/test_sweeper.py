"""Unit tests for the traffic-fine deadline re-check sweep."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest

from src.core.traffic_fines import repository, sweeper
from src.core.traffic_fines.sweeper import run_one_sweep

NOW = datetime(2026, 5, 30, 6, 0, 0, tzinfo=timezone.utc)


class _FakeTx:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *a: Any) -> bool:
        return False


class _FakeConn:
    def transaction(self) -> _FakeTx:
        return _FakeTx()


class _FakeAcquire:
    async def __aenter__(self) -> _FakeConn:
        return _FakeConn()

    async def __aexit__(self, *a: Any) -> bool:
        return False


class _FakePool:
    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire()


def _candidate(deadline_dt: datetime) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "depot_id": uuid4(),
        "organization_id": uuid4(),
        "fine_reference": "X-1",
        "extraction": {
            "is_traffic_fine": True,
            "fine_reference": "X-1",
            "currency": "EUR",
            "full_amount": 100.0,
            "early_payment_amount": 80.0,
            "early_payment_deadline": deadline_dt.isoformat(),
        },
    }


async def test_sweep_raises_only_for_due_fines(monkeypatch: pytest.MonkeyPatch) -> None:
    due = _candidate(NOW + timedelta(hours=12))  # inside the 48h window
    far = _candidate(NOW + timedelta(days=10))  # outside (defensive — re-checked)
    raised: list[Any] = []
    marked: list[Any] = []

    async def fake_candidates(pool: Any, *, window_hours: float, limit: int = 200) -> list[dict]:
        return [due, far]

    async def fake_tz(pool: Any, did: Any) -> Any:
        return None

    async def fake_mark(conn: Any, fid: Any, *, alert_id: Any, evaluation: Any) -> None:
        marked.append(fid)

    async def fake_raise(conn: Any, **kw: Any) -> Any:
        raised.append(kw["fine_id"])
        return uuid4()

    monkeypatch.setattr(repository, "sweep_candidates", fake_candidates)
    monkeypatch.setattr(repository, "get_depot_timezone", fake_tz)
    monkeypatch.setattr(repository, "mark_alerted", fake_mark)
    monkeypatch.setattr(sweeper, "raise_fine_alert", fake_raise)

    n = await run_one_sweep(_FakePool(), _FakePool(), now=NOW)

    assert n == 1
    assert raised == [due["id"]]
    assert marked == [due["id"]]


async def test_sweep_skips_unparseable_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    bad = {
        "id": uuid4(),
        "depot_id": uuid4(),
        "organization_id": uuid4(),
        "fine_reference": "Y-1",
        "extraction": {"unexpected_key": "x"},  # extra="forbid" -> ValidationError
    }

    async def fake_candidates(pool: Any, *, window_hours: float, limit: int = 200) -> list[dict]:
        return [bad]

    async def fake_raise(conn: Any, **kw: Any) -> Any:  # pragma: no cover - must not run
        raise AssertionError("should not alert on unparseable extraction")

    monkeypatch.setattr(repository, "sweep_candidates", fake_candidates)
    monkeypatch.setattr(sweeper, "raise_fine_alert", fake_raise)

    n = await run_one_sweep(_FakePool(), _FakePool(), now=NOW)
    assert n == 0
