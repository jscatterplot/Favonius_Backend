"""Unit tests for the energy reporting endpoints.

Covers ``GET /reports/depots/{depot_id}/energy/monthly``,
``GET /reports/depots/{depot_id}/energy/sessions``, and the streaming
CSV variant. Aggregation correctness is verified directly against the
pure-Python helper in :mod:`src.api.reports`; endpoint tests use
``TestClient`` with mocked DB pools and ``verify_depot_access`` to
exercise the wiring (param parsing, NULL collapsing, cross-org denial,
CSV row-for-row equivalence).
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.reports import (
    SessionRow,
    aggregate_energy_rows,
    csv_columns,
    stream_rows_as_csv,
)
from src.security.tenant_mirror import ensure_tenant_mirrored


# ── Fixtures and helpers ────────────────────────────────────────────────────


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def admin_user():
    return {"sub": str(uuid4()), "app_metadata": {"favonius_role": "favonius_admin"}}


@pytest.fixture(autouse=True)
def override_auth(admin_user):
    """Inject admin auth so verify_depot_access bypasses the tenant DB check."""
    prev = app.dependency_overrides.get(ensure_tenant_mirrored)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: admin_user
    yield
    if prev is not None:
        app.dependency_overrides[ensure_tenant_mirrored] = prev
    else:
        app.dependency_overrides.pop(ensure_tenant_mirrored, None)


def _make_pool(static_records: dict[str, Any], ts_records: list[dict]):
    """Construct a fake db_pools object with two acquire-able async pools."""

    static_conn = AsyncMock()

    async def static_fetchrow(query: str, *args, **kwargs):
        if "FROM depots" in query:
            return static_records.get("depot_row")
        return None

    async def static_fetch(query: str, *args, **kwargs):
        if "FROM chargers" in query:
            return static_records.get("charger_rows", [])
        return []

    static_conn.fetchrow.side_effect = static_fetchrow
    static_conn.fetch.side_effect = static_fetch

    ts_conn = AsyncMock()

    async def ts_fetch(query: str, *args, **kwargs):
        return ts_records

    ts_conn.fetch.side_effect = ts_fetch

    static_pool = MagicMock()
    static_pool.acquire.return_value.__aenter__.return_value = static_conn
    static_pool.acquire.return_value.__aexit__.return_value = None

    ts_pool = MagicMock()
    ts_pool.acquire.return_value.__aenter__.return_value = ts_conn
    ts_pool.acquire.return_value.__aexit__.return_value = None

    pools = MagicMock()
    pools.static = static_pool
    pools.ts = ts_pool
    return pools


def _depot_row(
    *,
    timezone: str = "America/Los_Angeles",
    currency: str = "USD",
    under_cap_rate: Optional[float] = 0.20,
):
    billing_metadata = {}
    if under_cap_rate is not None:
        billing_metadata["under_cap_rate"] = under_cap_rate
    return {
        "timezone": timezone,
        "currency": currency,
        "billing_metadata": billing_metadata,
    }


def _utc(dt_local: datetime, tz: str) -> datetime:
    """Produce a UTC datetime for a wall-clock time in tz."""
    return dt_local.replace(tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC"))


# ── Pure aggregation helper tests ───────────────────────────────────────────


class TestAggregateEnergyRows:
    """Unit tests for :func:`aggregate_energy_rows`."""

    def _two_vehicle_three_session_fixture(self) -> list[SessionRow]:
        tz = "America/Los_Angeles"
        return [
            SessionRow(
                start_time=_utc(datetime(2024, 1, 5, 9, 0), tz),
                end_time=_utc(datetime(2024, 1, 5, 11, 0), tz),
                energy_kwh=120.0,
                cost_total=24.0,
                vehicle_id="bus_1",
                charger_id="charger_a",
                driver_id="driver_x",
                card_id="card_1",
            ),
            SessionRow(
                start_time=_utc(datetime(2024, 1, 20, 14, 0), tz),
                end_time=_utc(datetime(2024, 1, 20, 16, 0), tz),
                energy_kwh=80.0,
                cost_total=None,  # forces estimation
                vehicle_id="bus_1",
                charger_id="charger_a",
                driver_id=None,  # null driver -> unassigned
                card_id="card_1",
            ),
            SessionRow(
                start_time=_utc(datetime(2024, 2, 10, 8, 0), tz),
                end_time=_utc(datetime(2024, 2, 10, 10, 0), tz),
                energy_kwh=200.0,
                cost_total=42.0,
                vehicle_id="bus_2",
                charger_id="charger_b",
                driver_id="driver_y",
                card_id="card_2",
            ),
        ]

    def test_no_grouping_buckets_by_month(self):
        rows = aggregate_energy_rows(
            self._two_vehicle_three_session_fixture(),
            timezone="America/Los_Angeles",
            group_by=None,
            under_cap_rate=0.20,
            currency="USD",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 2, 29),
        )
        assert [r["bucket"] for r in rows] == ["2024-01", "2024-02"]

        jan = rows[0]
        assert jan["session_count"] == 2
        assert jan["energy_kwh"] == pytest.approx(200.0)
        assert jan["avg_kw"] == pytest.approx(50.0)  # 200 kWh / 4 hours
        # Jan: one session with cost_total=24, one with None → estimated.
        # Estimated portion: 80 kWh × 0.20 = $16. Total: $24 + $16 = $40.
        assert jan["cost"]["amount"] == pytest.approx(40.0)
        assert jan["cost"]["currency"] == "USD"
        assert jan["cost"]["estimated"] is True

        feb = rows[1]
        assert feb["session_count"] == 1
        assert feb["energy_kwh"] == pytest.approx(200.0)
        assert feb["cost"]["amount"] == pytest.approx(42.0)
        assert feb["cost"]["estimated"] is False

    def test_group_by_vehicle(self):
        rows = aggregate_energy_rows(
            self._two_vehicle_three_session_fixture(),
            timezone="America/Los_Angeles",
            group_by="vehicle",
            under_cap_rate=0.20,
            currency="USD",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 2, 29),
        )
        keys = [(r["bucket"], r["vehicle_id"]) for r in rows]
        assert keys == [
            ("2024-01", "bus_1"),
            ("2024-02", "bus_2"),
        ]
        bus_1_jan = next(r for r in rows if r["vehicle_id"] == "bus_1")
        assert bus_1_jan["energy_kwh"] == pytest.approx(200.0)
        assert bus_1_jan["session_count"] == 2

    def test_null_driver_collapses_into_unassigned(self):
        rows = aggregate_energy_rows(
            self._two_vehicle_three_session_fixture(),
            timezone="America/Los_Angeles",
            group_by="driver",
            under_cap_rate=0.20,
            currency="USD",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 2, 29),
        )
        # Two distinct named drivers + one unassigned bucket in January.
        driver_ids = {r["driver_id"] for r in rows}
        assert "unassigned" in driver_ids
        assert {"driver_x", "driver_y"} <= driver_ids
        unassigned_row = next(r for r in rows if r["driver_id"] == "unassigned")
        assert unassigned_row["bucket"] == "2024-01"
        assert unassigned_row["session_count"] == 1
        assert unassigned_row["energy_kwh"] == pytest.approx(80.0)

    def test_depot_tz_bucketing_for_session_at_2330_local(self):
        """A session at 23:30 local on Jan 31 must NOT bleed into February.

        The same UTC instant — 07:30 Feb 1 UTC — is January in
        America/Los_Angeles but February in UTC. Bucketing in UTC
        would put it in the wrong month.
        """
        tz = "America/Los_Angeles"
        sessions = [
            SessionRow(
                start_time=_utc(datetime(2024, 1, 31, 23, 30), tz),
                end_time=_utc(datetime(2024, 2, 1, 0, 30), tz),
                energy_kwh=50.0,
                cost_total=10.0,
                vehicle_id="bus_1",
                charger_id="charger_a",
                driver_id=None,
                card_id=None,
            ),
        ]
        rows = aggregate_energy_rows(
            sessions,
            timezone=tz,
            group_by=None,
            under_cap_rate=0.20,
            currency="USD",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 2, 29),
        )
        assert len(rows) == 1
        assert rows[0]["bucket"] == "2024-01"

    def test_empty_sessions_returns_empty_rows(self):
        rows = aggregate_energy_rows(
            [],
            timezone="America/Los_Angeles",
            group_by=None,
            under_cap_rate=0.20,
            currency="USD",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 31),
        )
        assert rows == []

    def test_missing_under_cap_rate_keeps_estimated_flag(self):
        """When rate is unknown but cost is missing, row still flags estimated."""
        sessions = [
            SessionRow(
                start_time=_utc(datetime(2024, 3, 5, 9, 0), "America/Los_Angeles"),
                end_time=_utc(datetime(2024, 3, 5, 11, 0), "America/Los_Angeles"),
                energy_kwh=100.0,
                cost_total=None,
                vehicle_id="bus_1",
                charger_id="charger_a",
                driver_id=None,
                card_id=None,
            ),
        ]
        rows = aggregate_energy_rows(
            sessions,
            timezone="America/Los_Angeles",
            group_by=None,
            under_cap_rate=None,
            currency="USD",
            from_date=date(2024, 3, 1),
            to_date=date(2024, 3, 31),
        )
        assert len(rows) == 1
        assert rows[0]["cost"]["estimated"] is True
        assert rows[0]["cost"]["amount"] == pytest.approx(0.0)

    def test_invalid_group_by_raises(self):
        with pytest.raises(ValueError):
            aggregate_energy_rows(
                [],
                timezone="UTC",
                group_by="bogus",
                under_cap_rate=None,
                currency="USD",
                from_date=date(2024, 1, 1),
                to_date=date(2024, 1, 1),
            )


# ── CSV helper tests ────────────────────────────────────────────────────────


class TestCsvSerialization:
    """Tests for :func:`stream_rows_as_csv` formatting."""

    def test_csv_columns_no_grouping(self):
        assert csv_columns(None) == (
            "bucket",
            "energy_kwh",
            "session_count",
            "avg_kw",
            "cost_amount",
            "cost_currency",
            "cost_estimated",
        )

    def test_csv_columns_with_grouping(self):
        assert csv_columns("vehicle")[1] == "vehicle_id"

    def test_stream_rows_as_csv_matches_json_rows(self):
        rows = [
            {
                "bucket": "2024-01",
                "vehicle_id": "bus_1",
                "energy_kwh": 200.0,
                "session_count": 2,
                "avg_kw": 50.0,
                "cost": {"amount": 40.0, "currency": "USD", "estimated": True},
            },
            {
                "bucket": "2024-02",
                "vehicle_id": "bus_2",
                "energy_kwh": 200.0,
                "session_count": 1,
                "avg_kw": 100.0,
                "cost": {"amount": 42.0, "currency": "USD", "estimated": False},
            },
        ]
        body = "".join(stream_rows_as_csv(rows, group_by="vehicle"))
        reader = list(csv.reader(io.StringIO(body)))
        assert reader[0][0] == "bucket"
        assert reader[0][1] == "vehicle_id"
        assert len(reader) == 1 + len(rows)
        assert reader[1][:3] == ["2024-01", "bus_1", "200.0"]
        assert reader[1][-1] == "true"
        assert reader[2][-1] == "false"


# ── Endpoint integration tests ──────────────────────────────────────────────


class TestEnergyReportMonthlyEndpoint:
    """Tests for ``GET /reports/depots/{depot_id}/energy/monthly``."""

    def test_returns_aggregated_rows(self, client):
        depot_id = str(uuid4())
        ocpp_id = "ocpp_a"
        charger_uuid = str(uuid4())
        tz = "America/Los_Angeles"

        ts_records = [
            {
                "start_time": _utc(datetime(2024, 1, 5, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 5, 11, 0), tz),
                "energy_delivered_kwh": 120.0,
                "cost_total": 24.0,
                "vehicle_id": "bus_1",
                "charger_id": charger_uuid,
                "driver_id": "driver_x",
                "card_id": "card_1",
            },
            {
                "start_time": _utc(datetime(2024, 1, 20, 14, 0), tz),
                "end_time": _utc(datetime(2024, 1, 20, 16, 0), tz),
                "energy_delivered_kwh": 80.0,
                "cost_total": None,
                "vehicle_id": "bus_1",
                "charger_id": charger_uuid,
                "driver_id": None,
                "card_id": "card_1",
            },
        ]
        pools = _make_pool(
            static_records={
                "depot_row": _depot_row(timezone=tz),
                "charger_rows": [{"charger_id": charger_uuid, "ocpp_id": ocpp_id}],
            },
            ts_records=ts_records,
        )
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly",
                params={"from": "2024-01-01", "to": "2024-01-31"},
            )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["depot_id"] == depot_id
        assert body["currency"] == "USD"
        assert body["from"] == "2024-01-01"
        assert body["to"] == "2024-01-31"
        assert len(body["rows"]) == 1
        row = body["rows"][0]
        assert row["bucket"] == "2024-01"
        assert row["session_count"] == 2
        assert row["energy_kwh"] == pytest.approx(200.0)
        assert row["cost"]["estimated"] is True
        assert row["cost"]["amount"] == pytest.approx(40.0)

    def test_empty_data_returns_rows_array(self, client):
        depot_id = str(uuid4())
        pools = _make_pool(
            static_records={
                "depot_row": _depot_row(),
                "charger_rows": [],
            },
            ts_records=[],
        )
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly",
                params={"from": "2024-01-01", "to": "2024-01-31"},
            )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["depot_id"] == depot_id
        assert body["currency"] == "USD"
        assert body["from"] == "2024-01-01"
        assert body["to"] == "2024-01-31"
        assert body["rows"] == []

    def test_cross_org_denial(self, client):
        depot_id = str(uuid4())
        pools = _make_pool(static_records={}, ts_records=[])

        async def _denied(*args, **kwargs):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: you do not have permission for this depot",
            )

        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", side_effect=_denied
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly",
                params={"from": "2024-01-01", "to": "2024-01-31"},
            )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        body = response.json()
        # FastAPI wraps detail dict directly
        detail = body.get("detail")
        assert isinstance(detail, dict)
        assert detail["error_code"] == "CROSS_ORG_DENIED"

    def test_null_driver_collapses_into_unassigned_via_endpoint(self, client):
        depot_id = str(uuid4())
        ocpp_id = "ocpp_a"
        charger_uuid = str(uuid4())
        tz = "America/Los_Angeles"

        ts_records = [
            {
                "start_time": _utc(datetime(2024, 1, 5, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 5, 11, 0), tz),
                "energy_delivered_kwh": 60.0,
                "cost_total": 12.0,
                "vehicle_id": "bus_1",
                "charger_id": charger_uuid,
                "driver_id": None,
                "card_id": None,
            },
            {
                "start_time": _utc(datetime(2024, 1, 6, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 6, 11, 0), tz),
                "energy_delivered_kwh": 40.0,
                "cost_total": 8.0,
                "vehicle_id": "bus_2",
                "charger_id": charger_uuid,
                "driver_id": None,
                "card_id": None,
            },
        ]
        pools = _make_pool(
            static_records={
                "depot_row": _depot_row(timezone=tz),
                "charger_rows": [{"charger_id": charger_uuid, "ocpp_id": ocpp_id}],
            },
            ts_records=ts_records,
        )
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly",
                params={"from": "2024-01-01", "to": "2024-01-31", "group_by": "driver"},
            )

        assert response.status_code == status.HTTP_200_OK
        rows = response.json()["rows"]
        assert len(rows) == 1
        assert rows[0]["driver_id"] == "unassigned"
        assert rows[0]["session_count"] == 2

    def test_session_at_2330_local_does_not_bleed_into_next_month(self, client):
        depot_id = str(uuid4())
        ocpp_id = "ocpp_a"
        charger_uuid = str(uuid4())
        tz = "America/Los_Angeles"

        ts_records = [
            {
                # 23:30 PST on Jan 31 == 07:30 UTC on Feb 1.
                "start_time": _utc(datetime(2024, 1, 31, 23, 30), tz),
                "end_time": _utc(datetime(2024, 2, 1, 0, 30), tz),
                "energy_delivered_kwh": 50.0,
                "cost_total": 10.0,
                "vehicle_id": "bus_1",
                "charger_id": charger_uuid,
                "driver_id": None,
                "card_id": None,
            },
        ]
        pools = _make_pool(
            static_records={
                "depot_row": _depot_row(timezone=tz),
                "charger_rows": [{"charger_id": charger_uuid, "ocpp_id": ocpp_id}],
            },
            ts_records=ts_records,
        )
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly",
                params={"from": "2024-01-01", "to": "2024-02-29"},
            )

        rows = response.json()["rows"]
        assert len(rows) == 1
        assert rows[0]["bucket"] == "2024-01"

    def test_invalid_date_returns_400(self, client):
        depot_id = str(uuid4())
        pools = _make_pool(
            static_records={"depot_row": _depot_row()},
            ts_records=[],
        )
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly",
                params={"from": "not-a-date", "to": "2024-01-31"},
            )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_invalid_group_by_returns_400(self, client):
        depot_id = str(uuid4())
        pools = _make_pool(
            static_records={"depot_row": _depot_row()},
            ts_records=[],
        )
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly",
                params={
                    "from": "2024-01-01",
                    "to": "2024-01-31",
                    "group_by": "bogus",
                },
            )
        assert response.status_code == status.HTTP_400_BAD_REQUEST


class TestEnergyReportSessionsEndpoint:
    """Sessions endpoint defaults to vehicle grouping."""

    def test_default_grouping_is_vehicle(self, client):
        depot_id = str(uuid4())
        ocpp_id = "ocpp_a"
        charger_uuid = str(uuid4())
        tz = "America/Los_Angeles"

        ts_records = [
            {
                "start_time": _utc(datetime(2024, 1, 5, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 5, 11, 0), tz),
                "energy_delivered_kwh": 120.0,
                "cost_total": 24.0,
                "vehicle_id": "bus_1",
                "charger_id": charger_uuid,
                "driver_id": "driver_x",
                "card_id": "card_1",
            },
            {
                "start_time": _utc(datetime(2024, 1, 6, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 6, 11, 0), tz),
                "energy_delivered_kwh": 60.0,
                "cost_total": 12.0,
                "vehicle_id": "bus_2",
                "charger_id": charger_uuid,
                "driver_id": "driver_y",
                "card_id": "card_2",
            },
        ]
        pools = _make_pool(
            static_records={
                "depot_row": _depot_row(timezone=tz),
                "charger_rows": [{"charger_id": charger_uuid, "ocpp_id": ocpp_id}],
            },
            ts_records=ts_records,
        )
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/sessions",
                params={"from": "2024-01-01", "to": "2024-01-31"},
            )

        assert response.status_code == status.HTTP_200_OK
        rows = response.json()["rows"]
        assert {r["vehicle_id"] for r in rows} == {"bus_1", "bus_2"}


class TestEnergyReportCsvEndpoint:
    """Tests for ``GET /reports/depots/{depot_id}/energy/monthly.csv``."""

    def test_csv_body_matches_json_rows(self, client):
        depot_id = str(uuid4())
        ocpp_id = "ocpp_a"
        charger_uuid = str(uuid4())
        tz = "America/Los_Angeles"

        ts_records = [
            {
                "start_time": _utc(datetime(2024, 1, 5, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 5, 11, 0), tz),
                "energy_delivered_kwh": 120.0,
                "cost_total": 24.0,
                "vehicle_id": "bus_1",
                "charger_id": charger_uuid,
                "driver_id": "driver_x",
                "card_id": "card_1",
            },
            {
                "start_time": _utc(datetime(2024, 2, 6, 9, 0), tz),
                "end_time": _utc(datetime(2024, 2, 6, 11, 0), tz),
                "energy_delivered_kwh": 80.0,
                "cost_total": None,
                "vehicle_id": "bus_1",
                "charger_id": charger_uuid,
                "driver_id": "driver_x",
                "card_id": "card_1",
            },
        ]
        pools = _make_pool(
            static_records={
                "depot_row": _depot_row(timezone=tz),
                "charger_rows": [{"charger_id": charger_uuid, "ocpp_id": ocpp_id}],
            },
            ts_records=ts_records,
        )

        params = {"from": "2024-01-01", "to": "2024-02-29"}
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            json_response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly", params=params
            )
            csv_response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly.csv", params=params
            )

        assert csv_response.status_code == status.HTTP_200_OK
        assert csv_response.headers["content-type"].startswith("text/csv")
        json_rows = json_response.json()["rows"]
        csv_text = csv_response.text
        reader = list(csv.reader(io.StringIO(csv_text)))
        assert len(reader) == 1 + len(json_rows)
        # Walk every JSON row and confirm the corresponding CSV row matches
        # field-for-field.
        for json_row, csv_row in zip(json_rows, reader[1:]):
            assert csv_row[0] == json_row["bucket"]
            assert float(csv_row[1]) == pytest.approx(json_row["energy_kwh"])
            assert int(csv_row[2]) == json_row["session_count"]
            assert float(csv_row[3]) == pytest.approx(json_row["avg_kw"])
            assert float(csv_row[4]) == pytest.approx(json_row["cost"]["amount"])
            assert csv_row[5] == json_row["cost"]["currency"]
            assert (csv_row[6] == "true") == json_row["cost"]["estimated"]

    def test_csv_cross_org_denial(self, client):
        depot_id = str(uuid4())

        async def _denied(*args, **kwargs):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied",
            )

        pools = _make_pool(static_records={}, ts_records=[])
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", side_effect=_denied
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly.csv",
                params={"from": "2024-01-01", "to": "2024-01-31"},
            )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        detail = response.json()["detail"]
        assert detail["error_code"] == "CROSS_ORG_DENIED"
