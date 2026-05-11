"""Regression tests for open-session lookups excluding imported rows.

Historical XLSX backfills can persist rows with ``end_time IS NULL`` when the
source status is ``Charging``. These rows must never be treated as active live
sessions by fleet list endpoints.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.db import queries as db_queries


@pytest.mark.asyncio
async def test_open_sessions_by_stations_filters_to_live_source():
    db = AsyncMock()
    db.fetch = AsyncMock(
        return_value=[
            {
                "station_id": "CP-1",
                "session_id": "s1",
                "vehicle_id": None,
                "started_at": None,
                "current_power_kw": None,
                "current_soc": None,
                "target_soc": None,
                "estimated_end_at": None,
            }
        ]
    )

    result = await db_queries.open_sessions_by_stations(db, ["CP-1"])

    assert "CP-1" in result
    query = db.fetch.await_args.args[0]
    assert "source = 'live'" in query
    # ``id_token AS id_tag`` is part of the projection so the chargers
    # endpoint can render the raw RFID when the cards-only auth path
    # leaves ``vehicle_id`` null (HRX cards-only fleet pattern).
    assert "id_token" in query
    assert "AS id_tag" in query


@pytest.mark.asyncio
async def test_open_session_by_vehicles_filters_to_live_source():
    db = AsyncMock()
    db.fetch = AsyncMock(
        return_value=[
            {
                "vehicle_id": "veh-1",
                "session_id": "s1",
                "ocpp_id": "CP-1",
                "started_at": None,
                "current_power_kw": None,
            }
        ]
    )

    result = await db_queries.open_session_by_vehicles(db, ["00000000-0000-0000-0000-000000000001"])

    assert "veh-1" in result
    query = db.fetch.await_args.args[0]
    assert "source = 'live'" in query
