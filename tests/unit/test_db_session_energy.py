"""Unit tests for :func:`src.db.queries.get_session_energy_kwh`.

Covers the two-source resolution strategy:
* Option A — return ``charging_sessions.energy_delivered_kwh`` when present.
* Option B — fall back to ``telemetry.energy_kwh`` register delta when the
  stored value is missing (in-flight sessions). telemetry_samples was retired
  in migration 045; energy_kwh is now a first-class column on the wide table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.db import queries as db_queries
from src.db.queries import (
    ENERGY_SOURCE_SESSION_METER,
    ENERGY_SOURCE_TELEMETRY_REGISTER,
    get_session_energy_kwh,
)


def _session_row(
    *,
    transaction_id: int | None = 1001,
    energy_delivered_kwh: Decimal | None = None,
    end_time: datetime | None = None,
) -> dict:
    return {
        "session_id": uuid4(),
        "transaction_id": transaction_id,
        "start_time": datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc),
        "end_time": end_time,
        "energy_delivered_kwh": energy_delivered_kwh,
    }


@pytest.mark.asyncio
async def test_returns_stored_energy_when_session_closed():
    db = AsyncMock()
    db.fetchrow = AsyncMock(
        return_value=_session_row(
            energy_delivered_kwh=Decimal("42.500"),
            end_time=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
        )
    )

    result = await get_session_energy_kwh(db, transaction_id=1001)

    assert result == {"energy_kwh": 42.5, "source": ENERGY_SOURCE_SESSION_METER}
    # Only the session lookup ran — no telemetry_samples query needed.
    assert db.fetchrow.await_count == 1
    assert "charging_sessions" in db.fetchrow.await_args_list[0].args[0]


@pytest.mark.asyncio
async def test_falls_back_to_telemetry_register_delta_for_open_session():
    db = AsyncMock()
    db.fetchrow = AsyncMock(
        side_effect=[
            _session_row(energy_delivered_kwh=None, end_time=None),
            {"start_kwh": 1200.000, "end_kwh": 1234.567, "has_rollover": False},
        ]
    )

    result = await get_session_energy_kwh(db, transaction_id=1001)

    assert result is not None
    assert result["source"] == ENERGY_SOURCE_TELEMETRY_REGISTER
    assert result["energy_kwh"] == pytest.approx(34.567, abs=1e-6)

    fallback_query = db.fetchrow.await_args_list[1].args[0]
    # telemetry_samples retired (mig 045) — fallback now reads telemetry.energy_kwh
    assert "FROM telemetry" in fallback_query
    assert "energy_kwh IS NOT NULL" in fallback_query
    # energy_kwh is already normalised to kWh in the write path; no unit CASE needed
    assert "telemetry_samples" not in fallback_query


@pytest.mark.asyncio
async def test_session_id_lookup_uses_session_id_column():
    db = AsyncMock()
    sid = uuid4()
    db.fetchrow = AsyncMock(
        return_value=_session_row(
            energy_delivered_kwh=Decimal("10.000"),
            end_time=datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc),
        )
    )

    result = await get_session_energy_kwh(db, session_id=sid)

    assert result == {"energy_kwh": 10.0, "source": ENERGY_SOURCE_SESSION_METER}
    call = db.fetchrow.await_args_list[0]
    assert "WHERE session_id = $1" in call.args[0]
    assert call.args[1] == sid


@pytest.mark.asyncio
async def test_returns_none_when_session_not_found():
    db = AsyncMock()
    db.fetchrow = AsyncMock(return_value=None)

    result = await get_session_energy_kwh(db, transaction_id=9999)

    assert result is None
    assert db.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_returns_none_when_no_stored_and_no_telemetry():
    db = AsyncMock()
    db.fetchrow = AsyncMock(
        side_effect=[
            _session_row(energy_delivered_kwh=None, end_time=None),
            {"start_kwh": None, "end_kwh": None, "has_rollover": False},
        ]
    )

    result = await get_session_energy_kwh(db, transaction_id=1001)

    assert result is None


@pytest.mark.asyncio
async def test_returns_none_when_session_lacks_transaction_id():
    # OCPP 2.0.1 rows can have transaction_id=None; without it we cannot
    # join telemetry, so the fallback path is unavailable.
    db = AsyncMock()
    db.fetchrow = AsyncMock(
        return_value=_session_row(transaction_id=None, energy_delivered_kwh=None)
    )

    result = await get_session_energy_kwh(db, session_id=uuid4())

    assert result is None
    # No telemetry fallback query should be issued.
    assert db.fetchrow.await_count == 1


@pytest.mark.asyncio
async def test_rollover_flag_returns_none(caplog):
    # Meter rollover / reset is surfaced as unavailable.
    db = AsyncMock()
    db.fetchrow = AsyncMock(
        side_effect=[
            _session_row(energy_delivered_kwh=None, end_time=None),
            {"start_kwh": 100.0, "end_kwh": 110.0, "has_rollover": True},
        ]
    )

    with caplog.at_level("WARNING", logger=db_queries.__name__):
        result = await get_session_energy_kwh(db, transaction_id=1001)

    assert result is None
    assert any("Invalid telemetry register progression" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_zero_delta_is_returned_not_none():
    # A session with a single register reading (or no power drawn) has
    # start == end == 0 delta. That's a legitimate 0.0 kWh, not missing data.
    db = AsyncMock()
    db.fetchrow = AsyncMock(
        side_effect=[
            _session_row(energy_delivered_kwh=None, end_time=None),
            {"start_kwh": 1234.5, "end_kwh": 1234.5, "has_rollover": False},
        ]
    )

    result = await get_session_energy_kwh(db, transaction_id=1001)

    assert result == {"energy_kwh": 0.0, "source": ENERGY_SOURCE_TELEMETRY_REGISTER}


@pytest.mark.asyncio
async def test_requires_exactly_one_identifier():
    db = AsyncMock()

    with pytest.raises(ValueError):
        await get_session_energy_kwh(db)

    with pytest.raises(ValueError):
        await get_session_energy_kwh(db, session_id=uuid4(), transaction_id=1001)

    db.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_query_passes_session_window_bounds():
    start = datetime(2026, 5, 11, 9, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    session = _session_row(energy_delivered_kwh=None, end_time=end)
    session["start_time"] = start

    db = AsyncMock()
    db.fetchrow = AsyncMock(
        side_effect=[session, {"start_kwh": 2.0, "end_kwh": 5.0, "has_rollover": False}]
    )

    await get_session_energy_kwh(db, transaction_id=1001)

    fallback_call = db.fetchrow.await_args_list[1]
    # (query, transaction_id, start_time, end_time)
    assert fallback_call.args[1] == 1001
    assert fallback_call.args[2] == start
    assert fallback_call.args[3] == end
    # COALESCE($3, NOW()) — open sessions pass end_time=None and let SQL pick NOW().
    assert "COALESCE($3, NOW())" in fallback_call.args[0]


@pytest.mark.asyncio
async def test_lowercase_kwh_is_not_divided_by_thousand():
    db = AsyncMock()
    db.fetchrow = AsyncMock(
        side_effect=[
            _session_row(energy_delivered_kwh=None, end_time=None),
            {"start_kwh": 10.0, "end_kwh": 60.0, "has_rollover": False},
        ]
    )

    result = await get_session_energy_kwh(db, transaction_id=1001)

    assert result == {"energy_kwh": 50.0, "source": ENERGY_SOURCE_TELEMETRY_REGISTER}
    fallback_query = db.fetchrow.await_args_list[1].args[0]
    assert "energy_kwh IS NOT NULL" in fallback_query
