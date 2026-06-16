"""Unit tests for bounded latest-vehicle telemetry queries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from src.db.queries import (
    _MERGED_VEHICLE_TELEMETRY_SQL,
    _TELEMETRY_ONLY_VEHICLE_TELEMETRY_SQL,
    default_vehicle_telemetry_recency_floor,
    fetch_freshest_vehicle_telemetry,
    latest_telemetry_by_vehicles,
    latest_telemetry_for_depot_vehicles,
)


@pytest.mark.unit
class TestFetchFreshestVehicleTelemetry:
    @pytest.mark.asyncio
    async def test_uses_merged_sql_with_recency_floor(self):
        vehicle_id = uuid4()
        floor = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        fetcher = AsyncMock()
        fetcher.fetch = AsyncMock(
            return_value=[
                {
                    "vehicle_id": str(vehicle_id),
                    "time": floor + timedelta(hours=1),
                    "soc": 0.5,
                    "charging_kw": 80.0,
                    "charger_id": str(uuid4()),
                    "is_plugged": True,
                    "energy_kwh": 12.0,
                }
            ]
        )

        rows = await fetch_freshest_vehicle_telemetry(
            fetcher, [vehicle_id], recency_floor=floor
        )

        fetcher.fetch.assert_awaited_once_with(
            _MERGED_VEHICLE_TELEMETRY_SQL, [vehicle_id], floor
        )
        assert "time > $2" in _MERGED_VEHICLE_TELEMETRY_SQL
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_falls_back_when_vehicle_telemetry_missing(self):
        vehicle_id = uuid4()
        floor = default_vehicle_telemetry_recency_floor()
        fetcher = AsyncMock()
        fetcher.fetch = AsyncMock(
            side_effect=[
                asyncpg.UndefinedTableError("vehicle_telemetry"),
                [
                    {
                        "vehicle_id": str(vehicle_id),
                        "time": floor + timedelta(minutes=5),
                        "soc": 0.4,
                        "charging_kw": None,
                        "charger_id": None,
                        "is_plugged": None,
                        "energy_kwh": None,
                    }
                ],
            ]
        )

        rows = await fetch_freshest_vehicle_telemetry(
            fetcher, [str(vehicle_id)], recency_floor=floor
        )

        assert fetcher.fetch.await_count == 2
        assert fetcher.fetch.await_args_list[1].args[0] == _TELEMETRY_ONLY_VEHICLE_TELEMETRY_SQL
        assert "time > $2" in _TELEMETRY_ONLY_VEHICLE_TELEMETRY_SQL
        assert rows[0]["soc"] == 0.4

    @pytest.mark.asyncio
    async def test_latest_telemetry_by_vehicles_shapes_dict(self):
        vehicle_id = uuid4()
        seen_at = datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc)
        charger_id = uuid4()
        fetcher = AsyncMock()
        fetcher.fetch = AsyncMock(
            return_value=[
                {
                    "vehicle_id": str(vehicle_id),
                    "time": seen_at,
                    "soc": 0.61,
                    "charging_kw": 72.0,
                    "charger_id": str(charger_id),
                    "is_plugged": True,
                    "energy_kwh": 3.5,
                }
            ]
        )

        result = await latest_telemetry_by_vehicles(fetcher, [str(vehicle_id)])

        row = result[str(vehicle_id)]
        assert row["last_seen_at"] == seen_at
        assert row["current_soc"] == 0.61
        assert row["current_power_kw"] == 72.0
        assert row["charger_id"] == str(charger_id)

    @pytest.mark.asyncio
    async def test_latest_telemetry_for_depot_vehicles_shapes_list(self):
        vehicle_id = uuid4()
        seen_at = datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc)
        fetcher = AsyncMock()
        fetcher.fetch = AsyncMock(
            return_value=[
                {
                    "vehicle_id": str(vehicle_id),
                    "time": seen_at,
                    "soc": 0.55,
                    "charging_kw": 40.0,
                    "charger_id": None,
                    "is_plugged": False,
                    "energy_kwh": None,
                }
            ]
        )

        rows = await latest_telemetry_for_depot_vehicles(fetcher, vehicle_ids=[str(vehicle_id)])

        assert rows[0]["vehicle_id"] == str(vehicle_id)
        assert rows[0]["last_seen_at"] == seen_at
        assert rows[0]["power_kw"] == 40.0
