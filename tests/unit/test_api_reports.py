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

from src.api import main as _main
from src.api.main import app
from src.api.reports import (
    SessionRow,
    aggregate_energy_rows,
    compute_energy_totals,
    csv_columns,
    stream_rows_as_csv,
)
from src.notifications.email_client import FakeEmailClient
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
        if "FROM sites" in query:
            return static_records.get("depot_row")
        return None

    async def static_fetch(query: str, *args, **kwargs):
        if "FROM charging_stations" in query:
            return static_records.get("charger_rows", [])
        if "FROM rfid_cards" in query:
            rows = static_records.get("rfid_card_rows", [])
            # Mirror the production WHERE clause: scope to the requested
            # depot when site_id is present in the query and a depot arg
            # was passed in. Cards are keyed by ``card_id``; the depot
            # filter uses an optional ``site_id`` field on the fixture
            # row (rows missing site_id are treated as in-scope).
            requested_card_ids = set(args[0]) if args else set()
            requested_site_id = args[1] if "site_id" in query and len(args) > 1 else None
            filtered = []
            for row in rows:
                if requested_card_ids and row["card_id"] not in requested_card_ids:
                    continue
                if requested_site_id is not None:
                    row_site = row.get("site_id")
                    if row_site is not None and row_site != requested_site_id:
                        continue
                filtered.append(row)
            return filtered
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
    name: str = "Test Depot",
    timezone: str = "America/Los_Angeles",
    currency: str = "USD",
    under_cap_rate: Optional[float] = 0.20,
):
    billing_metadata = {}
    if under_cap_rate is not None:
        billing_metadata["under_cap_rate"] = under_cap_rate
    return {
        "name": name,
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

    def test_avg_kw_uses_only_sessions_with_duration(self):
        """avg_kw must use the same subset as duration_hours."""
        sessions = [
            SessionRow(
                start_time=_utc(datetime(2024, 3, 5, 9, 0), "America/Los_Angeles"),
                end_time=_utc(datetime(2024, 3, 5, 11, 0), "America/Los_Angeles"),
                energy_kwh=100.0,
                cost_total=20.0,
                vehicle_id="bus_1",
                charger_id="charger_a",
                driver_id=None,
                card_id=None,
            ),
            SessionRow(
                start_time=_utc(datetime(2024, 3, 6, 9, 0), "America/Los_Angeles"),
                end_time=None,
                energy_kwh=80.0,
                cost_total=16.0,
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
            under_cap_rate=0.20,
            currency="USD",
            from_date=date(2024, 3, 1),
            to_date=date(2024, 3, 31),
        )
        assert len(rows) == 1
        # Only the 100 kWh session has a duration (2h), so avg_kw is 50.
        assert rows[0]["avg_kw"] == pytest.approx(50.0)
        # Total energy remains across all sessions for the energy rollup.
        assert rows[0]["energy_kwh"] == pytest.approx(180.0)

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

    def test_group_by_card_merges_same_label_across_tags(self):
        """Two cards sharing a label collapse into one row by merge key."""
        tz = "America/Los_Angeles"
        sessions = [
            SessionRow(
                start_time=_utc(datetime(2024, 1, 5, 9, 0), tz),
                end_time=_utc(datetime(2024, 1, 5, 11, 0), tz),
                energy_kwh=50.0,
                cost_total=10.0,
                vehicle_id=None,
                charger_id=None,
                driver_id=None,
                card_id="uuid-a",
                card_label="Driver John",
                card_id_tag="TAG_AAA",
            ),
            SessionRow(
                start_time=_utc(datetime(2024, 1, 10, 9, 0), tz),
                end_time=_utc(datetime(2024, 1, 10, 11, 0), tz),
                energy_kwh=30.0,
                cost_total=6.0,
                vehicle_id=None,
                charger_id=None,
                driver_id=None,
                card_id="uuid-b",
                card_label="Driver John",
                card_id_tag="TAG_BBB",
            ),
            SessionRow(
                start_time=_utc(datetime(2024, 1, 12, 9, 0), tz),
                end_time=_utc(datetime(2024, 1, 12, 11, 0), tz),
                energy_kwh=20.0,
                cost_total=4.0,
                vehicle_id=None,
                charger_id=None,
                driver_id=None,
                card_id="uuid-c",
                card_label="Driver Jane",
                card_id_tag="TAG_CCC",
            ),
        ]
        rows = aggregate_energy_rows(
            sessions,
            timezone=tz,
            group_by="card",
            under_cap_rate=0.20,
            currency="USD",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 31),
        )
        by_label = {r["card_label"]: r for r in rows}
        assert set(by_label) == {"Driver John", "Driver Jane"}
        assert by_label["Driver John"]["session_count"] == 2
        assert by_label["Driver John"]["energy_kwh"] == pytest.approx(80.0)
        assert by_label["Driver John"]["cost"]["amount"] == pytest.approx(16.0)
        # card_id on the merged row is one of the underlying UUIDs.
        assert by_label["Driver John"]["card_id"] in {"uuid-a", "uuid-b"}
        # Different label → separate row.
        assert by_label["Driver Jane"]["energy_kwh"] == pytest.approx(20.0)

    def test_group_by_card_falls_back_to_id_tag_when_label_missing(self):
        """No label → group by id_tag so two tag scans collapse anyway."""
        tz = "America/Los_Angeles"
        sessions = [
            SessionRow(
                start_time=_utc(datetime(2024, 1, 5, 9, 0), tz),
                end_time=_utc(datetime(2024, 1, 5, 11, 0), tz),
                energy_kwh=40.0,
                cost_total=8.0,
                vehicle_id=None,
                charger_id=None,
                driver_id=None,
                card_id="uuid-a",
                card_label=None,
                card_id_tag="TAG_SHARED",
            ),
            SessionRow(
                start_time=_utc(datetime(2024, 1, 6, 9, 0), tz),
                end_time=_utc(datetime(2024, 1, 6, 11, 0), tz),
                energy_kwh=10.0,
                cost_total=2.0,
                vehicle_id=None,
                charger_id=None,
                driver_id=None,
                card_id="uuid-b",
                card_label=None,
                card_id_tag="TAG_SHARED",
            ),
        ]
        rows = aggregate_energy_rows(
            sessions,
            timezone=tz,
            group_by="card",
            under_cap_rate=0.20,
            currency="USD",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 31),
        )
        assert len(rows) == 1
        assert rows[0]["energy_kwh"] == pytest.approx(50.0)
        # Label falls back to the id_tag because no label is set.
        assert rows[0]["card_label"] == "TAG_SHARED"

    def test_group_by_card_unassigned_when_card_id_is_null(self):
        """NULL card_id stays in the single 'unassigned' bucket."""
        tz = "America/Los_Angeles"
        sessions = [
            SessionRow(
                start_time=_utc(datetime(2024, 1, 5, 9, 0), tz),
                end_time=_utc(datetime(2024, 1, 5, 11, 0), tz),
                energy_kwh=10.0,
                cost_total=2.0,
                vehicle_id=None,
                charger_id=None,
                driver_id=None,
                card_id=None,
            ),
            SessionRow(
                start_time=_utc(datetime(2024, 1, 6, 9, 0), tz),
                end_time=_utc(datetime(2024, 1, 6, 11, 0), tz),
                energy_kwh=15.0,
                cost_total=3.0,
                vehicle_id=None,
                charger_id=None,
                driver_id=None,
                card_id=None,
            ),
        ]
        rows = aggregate_energy_rows(
            sessions,
            timezone=tz,
            group_by="card",
            under_cap_rate=0.20,
            currency="USD",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 31),
        )
        assert len(rows) == 1
        assert rows[0]["card_id"] == "unassigned"
        assert rows[0]["card_label"] == "unassigned"
        assert rows[0]["session_count"] == 2


class TestComputeEnergyTotals:
    """Tests for :func:`compute_energy_totals`."""

    def test_totals_sum_energy_count_and_cost(self):
        tz = "America/Los_Angeles"
        sessions = [
            SessionRow(
                start_time=_utc(datetime(2024, 1, 5, 9, 0), tz),
                end_time=_utc(datetime(2024, 1, 5, 11, 0), tz),
                energy_kwh=100.0,
                cost_total=20.0,
                vehicle_id=None,
                charger_id=None,
                driver_id=None,
                card_id=None,
            ),
            SessionRow(
                start_time=_utc(datetime(2024, 2, 5, 9, 0), tz),
                end_time=_utc(datetime(2024, 2, 5, 11, 0), tz),
                energy_kwh=50.0,
                cost_total=None,  # forces estimate
                vehicle_id=None,
                charger_id=None,
                driver_id=None,
                card_id=None,
            ),
        ]
        totals = compute_energy_totals(
            sessions,
            timezone=tz,
            under_cap_rate=0.20,
            currency="USD",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 2, 29),
        )
        assert totals["session_count"] == 2
        assert totals["energy_kwh"] == pytest.approx(150.0)
        # 20.0 + (50.0 × 0.20) = 30.0
        assert totals["cost"]["amount"] == pytest.approx(30.0)
        assert totals["cost"]["currency"] == "USD"
        assert totals["cost"]["estimated"] is True
        # avg_kw = 150 kWh / 4 hr = 37.5
        assert totals["avg_kw"] == pytest.approx(37.5)

    def test_totals_skip_sessions_outside_window(self):
        tz = "America/Los_Angeles"
        sessions = [
            SessionRow(
                start_time=_utc(datetime(2023, 12, 30, 9, 0), tz),
                end_time=_utc(datetime(2023, 12, 30, 11, 0), tz),
                energy_kwh=999.0,
                cost_total=999.0,
                vehicle_id=None,
                charger_id=None,
                driver_id=None,
                card_id=None,
            ),
            SessionRow(
                start_time=_utc(datetime(2024, 1, 5, 9, 0), tz),
                end_time=_utc(datetime(2024, 1, 5, 11, 0), tz),
                energy_kwh=10.0,
                cost_total=2.0,
                vehicle_id=None,
                charger_id=None,
                driver_id=None,
                card_id=None,
            ),
        ]
        totals = compute_energy_totals(
            sessions,
            timezone=tz,
            under_cap_rate=None,
            currency="USD",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 31),
        )
        assert totals["session_count"] == 1
        assert totals["energy_kwh"] == pytest.approx(10.0)

    def test_totals_empty_when_no_sessions(self):
        totals = compute_energy_totals(
            [],
            timezone="UTC",
            under_cap_rate=None,
            currency="EUR",
            from_date=date(2024, 1, 1),
            to_date=date(2024, 1, 31),
        )
        assert totals == {
            "energy_kwh": 0.0,
            "session_count": 0,
            "avg_kw": 0.0,
            "cost": {"amount": 0.0, "currency": "EUR", "estimated": False},
        }


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

    def test_csv_columns_card_grouping_includes_label(self):
        cols = csv_columns("card")
        assert cols[1] == "card_id"
        assert cols[2] == "card_label"

    def test_stream_rows_as_csv_with_totals_appends_total_row(self):
        rows = [
            {
                "bucket": "2024-01",
                "vehicle_id": "bus_1",
                "energy_kwh": 100.0,
                "session_count": 1,
                "avg_kw": 50.0,
                "cost": {"amount": 20.0, "currency": "USD", "estimated": False},
            },
        ]
        totals = {
            "energy_kwh": 100.0,
            "session_count": 1,
            "avg_kw": 50.0,
            "cost": {"amount": 20.0, "currency": "USD", "estimated": False},
        }
        body = "".join(
            stream_rows_as_csv(rows, group_by="vehicle", totals=totals)
        )
        reader = list(csv.reader(io.StringIO(body)))
        assert len(reader) == 3  # header + 1 row + TOTAL
        assert reader[-1][0] == "TOTAL"
        assert reader[-1][1] == ""  # grouping cell blank on totals
        assert float(reader[-1][2]) == 100.0
        assert int(reader[-1][3]) == 1
        assert reader[-1][4] == ""  # avg_kw blank on totals
        assert float(reader[-1][5]) == 20.0
        assert reader[-1][6] == "USD"

    def test_stream_rows_as_csv_card_grouping_emits_label_column(self):
        rows = [
            {
                "bucket": "2024-01",
                "card_id": "uuid-a",
                "card_label": "Driver John",
                "energy_kwh": 100.0,
                "session_count": 1,
                "avg_kw": 50.0,
                "cost": {"amount": 20.0, "currency": "USD", "estimated": False},
            },
        ]
        body = "".join(stream_rows_as_csv(rows, group_by="card"))
        reader = list(csv.reader(io.StringIO(body)))
        # Header includes card_id + card_label.
        assert reader[0][1] == "card_id"
        assert reader[0][2] == "card_label"
        assert reader[1][1] == "uuid-a"
        assert reader[1][2] == "Driver John"

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
                "charger_id": ocpp_id,
                "driver_id": "driver_x",
                "card_id": "card_1",
            },
            {
                "start_time": _utc(datetime(2024, 1, 20, 14, 0), tz),
                "end_time": _utc(datetime(2024, 1, 20, 16, 0), tz),
                "energy_delivered_kwh": 80.0,
                "cost_total": None,
                "vehicle_id": "bus_1",
                "charger_id": ocpp_id,
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
        # Depot name and totals now flow through the response.
        assert body["depot_name"] == "Test Depot"
        assert body["totals"]["session_count"] == 2
        assert body["totals"]["energy_kwh"] == pytest.approx(200.0)
        assert body["totals"]["cost"]["amount"] == pytest.approx(40.0)

    def test_group_by_card_returns_labels_and_merges_same_label_tags(self, client):
        """Two physical tags with one label collapse into a single row."""
        depot_id = str(uuid4())
        ocpp_id = "ocpp_a"
        charger_uuid = str(uuid4())
        card_uuid_a = str(uuid4())
        card_uuid_b = str(uuid4())
        card_uuid_c = str(uuid4())
        tz = "America/Los_Angeles"

        ts_records = [
            {
                "start_time": _utc(datetime(2024, 1, 5, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 5, 11, 0), tz),
                "energy_delivered_kwh": 50.0,
                "cost_total": 10.0,
                "vehicle_id": None,
                "charger_id": ocpp_id,
                "driver_id": None,
                "card_id": card_uuid_a,
            },
            {
                "start_time": _utc(datetime(2024, 1, 10, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 10, 11, 0), tz),
                "energy_delivered_kwh": 30.0,
                "cost_total": 6.0,
                "vehicle_id": None,
                "charger_id": ocpp_id,
                "driver_id": None,
                "card_id": card_uuid_b,
            },
            {
                "start_time": _utc(datetime(2024, 1, 12, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 12, 11, 0), tz),
                "energy_delivered_kwh": 20.0,
                "cost_total": 4.0,
                "vehicle_id": None,
                "charger_id": ocpp_id,
                "driver_id": None,
                "card_id": card_uuid_c,
            },
        ]
        pools = _make_pool(
            static_records={
                "depot_row": _depot_row(timezone=tz),
                "charger_rows": [{"charger_id": charger_uuid, "ocpp_id": ocpp_id}],
                "rfid_card_rows": [
                    {"card_id": card_uuid_a, "label": "Driver John", "id_tag": "TAG_A"},
                    {"card_id": card_uuid_b, "label": "Driver John", "id_tag": "TAG_B"},
                    {"card_id": card_uuid_c, "label": "Driver Jane", "id_tag": "TAG_C"},
                ],
            },
            ts_records=ts_records,
        )
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly",
                params={
                    "from": "2024-01-01",
                    "to": "2024-01-31",
                    "group_by": "card",
                },
            )

        assert response.status_code == status.HTTP_200_OK
        rows = response.json()["rows"]
        by_label = {r["card_label"]: r for r in rows}
        assert set(by_label) == {"Driver John", "Driver Jane"}
        # John's two tags collapsed into one row totalling 80 kWh / $16.
        assert by_label["Driver John"]["session_count"] == 2
        assert by_label["Driver John"]["energy_kwh"] == pytest.approx(80.0)
        assert by_label["Driver John"]["cost"]["amount"] == pytest.approx(16.0)
        # Jane stays separate.
        assert by_label["Driver Jane"]["session_count"] == 1
        assert by_label["Driver Jane"]["energy_kwh"] == pytest.approx(20.0)
        # Totals cross all buckets.
        totals = response.json()["totals"]
        assert totals["session_count"] == 3
        assert totals["energy_kwh"] == pytest.approx(100.0)
        assert totals["cost"]["amount"] == pytest.approx(20.0)

    def test_card_from_other_depot_does_not_leak_label(self, client):
        """Card UUID belonging to another depot is NOT enriched.

        Models a legacy/imported charging_sessions row whose ``card_id``
        points at an ``rfid_cards`` row owned by a different ``site_id``.
        The lookup must be depot-scoped so the other tenant's ``label``
        never appears in this depot's report.
        """
        depot_id = str(uuid4())
        other_depot_id = str(uuid4())
        ocpp_id = "ocpp_a"
        charger_uuid = str(uuid4())
        leaked_card_uuid = str(uuid4())
        local_card_uuid = str(uuid4())
        tz = "America/Los_Angeles"

        ts_records = [
            {
                "start_time": _utc(datetime(2024, 1, 5, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 5, 11, 0), tz),
                "energy_delivered_kwh": 40.0,
                "cost_total": 8.0,
                "vehicle_id": None,
                "charger_id": ocpp_id,
                "driver_id": None,
                "card_id": leaked_card_uuid,
            },
            {
                "start_time": _utc(datetime(2024, 1, 6, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 6, 11, 0), tz),
                "energy_delivered_kwh": 25.0,
                "cost_total": 5.0,
                "vehicle_id": None,
                "charger_id": ocpp_id,
                "driver_id": None,
                "card_id": local_card_uuid,
            },
        ]
        pools = _make_pool(
            static_records={
                "depot_row": _depot_row(timezone=tz),
                "charger_rows": [{"charger_id": charger_uuid, "ocpp_id": ocpp_id}],
                "rfid_card_rows": [
                    # Owned by another depot — must NOT leak through.
                    {
                        "card_id": leaked_card_uuid,
                        "label": "Other Tenant Driver",
                        "id_tag": "TAG_OTHER",
                        "site_id": other_depot_id,
                    },
                    {
                        "card_id": local_card_uuid,
                        "label": "Local Driver",
                        "id_tag": "TAG_LOCAL",
                        "site_id": depot_id,
                    },
                ],
            },
            ts_records=ts_records,
        )
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly",
                params={
                    "from": "2024-01-01",
                    "to": "2024-01-31",
                    "group_by": "card",
                },
            )

        assert response.status_code == status.HTTP_200_OK
        rows = response.json()["rows"]
        labels = {r["card_label"] for r in rows}
        assert "Other Tenant Driver" not in labels
        assert "TAG_OTHER" not in labels
        # The local card is enriched normally; the leaked-card row falls
        # back to grouping by raw UUID (so card_label == card_id UUID).
        assert "Local Driver" in labels
        leaked_row = next(r for r in rows if r["card_id"] == leaked_card_uuid)
        assert leaked_row["card_label"] == leaked_card_uuid

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
                "charger_id": ocpp_id,
                "driver_id": None,
                "card_id": None,
            },
            {
                "start_time": _utc(datetime(2024, 1, 6, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 6, 11, 0), tz),
                "energy_delivered_kwh": 40.0,
                "cost_total": 8.0,
                "vehicle_id": "bus_2",
                "charger_id": ocpp_id,
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

    def test_report_dimensions_are_normalized_before_aggregation(self, client):
        depot_id = str(uuid4())
        ocpp_id = "ocpp_a"
        charger_uuid = str(uuid4())
        vehicle_uuid = uuid4()
        tz = "America/Los_Angeles"

        ts_records = [
            {
                "start_time": _utc(datetime(2024, 1, 5, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 5, 11, 0), tz),
                "energy_delivered_kwh": 60.0,
                "cost_total": 12.0,
                "vehicle_id": vehicle_uuid,
                "charger_id": ocpp_id,
                "driver_id": "",
                "card_id": "",
            },
            {
                "start_time": _utc(datetime(2024, 1, 6, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 6, 11, 0), tz),
                "energy_delivered_kwh": 40.0,
                "cost_total": 8.0,
                "vehicle_id": vehicle_uuid,
                "charger_id": ocpp_id,
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
            vehicle_response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly",
                params={"from": "2024-01-01", "to": "2024-01-31", "group_by": "vehicle"},
            )
            driver_response = client.get(
                f"/reports/depots/{depot_id}/energy/monthly",
                params={"from": "2024-01-01", "to": "2024-01-31", "group_by": "driver"},
            )

        assert vehicle_response.status_code == status.HTTP_200_OK
        vehicle_rows = vehicle_response.json()["rows"]
        assert len(vehicle_rows) == 1
        assert vehicle_rows[0]["vehicle_id"] == str(vehicle_uuid)

        assert driver_response.status_code == status.HTTP_200_OK
        driver_rows = driver_response.json()["rows"]
        assert len(driver_rows) == 1
        assert driver_rows[0]["driver_id"] == "unassigned"

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
                "charger_id": ocpp_id,
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
                "charger_id": ocpp_id,
                "driver_id": "driver_x",
                "card_id": "card_1",
            },
            {
                "start_time": _utc(datetime(2024, 1, 6, 9, 0), tz),
                "end_time": _utc(datetime(2024, 1, 6, 11, 0), tz),
                "energy_delivered_kwh": 60.0,
                "cost_total": 12.0,
                "vehicle_id": "bus_2",
                "charger_id": ocpp_id,
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
                "charger_id": ocpp_id,
                "driver_id": "driver_x",
                "card_id": "card_1",
            },
            {
                "start_time": _utc(datetime(2024, 2, 6, 9, 0), tz),
                "end_time": _utc(datetime(2024, 2, 6, 11, 0), tz),
                "energy_delivered_kwh": 80.0,
                "cost_total": None,
                "vehicle_id": "bus_1",
                "charger_id": ocpp_id,
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
        json_body = json_response.json()
        json_rows = json_body["rows"]
        csv_text = csv_response.text
        reader = list(csv.reader(io.StringIO(csv_text)))
        # 1 header + N data rows + 1 TOTAL row.
        assert len(reader) == 1 + len(json_rows) + 1
        # Walk every JSON row and confirm the corresponding CSV row matches
        # field-for-field.
        for json_row, csv_row in zip(json_rows, reader[1:-1]):
            assert csv_row[0] == json_row["bucket"]
            assert float(csv_row[1]) == pytest.approx(json_row["energy_kwh"])
            assert int(csv_row[2]) == json_row["session_count"]
            assert float(csv_row[3]) == pytest.approx(json_row["avg_kw"])
            assert float(csv_row[4]) == pytest.approx(json_row["cost"]["amount"])
            assert csv_row[5] == json_row["cost"]["currency"]
            assert (csv_row[6] == "true") == json_row["cost"]["estimated"]
        # Final row carries cross-bucket totals matching the JSON ``totals`` block.
        totals = json_body["totals"]
        total_row = reader[-1]
        assert total_row[0] == "TOTAL"
        assert float(total_row[1]) == pytest.approx(totals["energy_kwh"])
        assert int(total_row[2]) == totals["session_count"]
        assert total_row[3] == ""  # avg_kw blank on totals
        assert float(total_row[4]) == pytest.approx(totals["cost"]["amount"])
        assert total_row[5] == totals["cost"]["currency"]

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


# ── Energy transactions endpoint tests ──────────────────────────────────────


def _txn_record(
    *,
    session_id: str,
    started_at: datetime,
    ended_at: Optional[datetime],
    energy_kwh: Optional[float],
    cost_total: Optional[float],
    vehicle_id: Optional[str] = None,
    ocpp_id: str = "ocpp_a",
    driver_id: Optional[str] = None,
    card_id: Optional[str] = None,
) -> dict:
    """Build a fake asyncpg.Record-shaped dict for the transactions query."""
    return {
        "session_id": session_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "energy_delivered_kwh": energy_kwh,
        "cost_total": cost_total,
        "vehicle_id": vehicle_id,
        "ocpp_id": ocpp_id,
        "driver_id": driver_id,
        "card_id": card_id,
    }


class TestEnergyTransactionsEndpoint:
    """Tests for ``GET /reports/depots/{depot_id}/energy/transactions``."""

    def _setup_pools(
        self,
        *,
        ocpp_id: str = "ocpp_a",
        charger_uuid: Optional[str] = None,
        ts_records: Optional[list[dict]] = None,
        timezone_name: str = "Europe/Vilnius",
        currency: str = "EUR",
        under_cap_rate: float = 0.20,
    ):
        charger_uuid = charger_uuid or str(uuid4())
        captured: dict[str, Any] = {}

        ts_records = ts_records or []

        ts_conn = AsyncMock()

        async def ts_fetch(query: str, *args, **kwargs):
            captured["query"] = query
            captured["args"] = args
            return ts_records

        ts_conn.fetch.side_effect = ts_fetch

        static_conn = AsyncMock()

        async def static_fetchrow(query: str, *args, **kwargs):
            if "FROM sites" in query:
                return _depot_row(
                    timezone=timezone_name, currency=currency, under_cap_rate=under_cap_rate
                )
            return None

        async def static_fetch(query: str, *args, **kwargs):
            if "FROM charging_stations" in query:
                return [{"charger_id": charger_uuid, "ocpp_id": ocpp_id}]
            return []

        static_conn.fetchrow.side_effect = static_fetchrow
        static_conn.fetch.side_effect = static_fetch

        static_pool = MagicMock()
        static_pool.acquire.return_value.__aenter__.return_value = static_conn
        static_pool.acquire.return_value.__aexit__.return_value = None

        ts_pool = MagicMock()
        ts_pool.acquire.return_value.__aenter__.return_value = ts_conn
        ts_pool.acquire.return_value.__aexit__.return_value = None

        pools = MagicMock()
        pools.static = static_pool
        pools.ts = ts_pool
        return pools, charger_uuid, captured

    def test_returns_one_row_per_session_in_camelcase(self, client):
        depot_id = str(uuid4())
        session_a = str(uuid4())
        session_b = str(uuid4())
        vehicle_uuid = str(uuid4())
        driver_uuid = str(uuid4())
        card_uuid = str(uuid4())
        tz = "Europe/Vilnius"

        # Newest first (ORDER BY started_at DESC, session_id DESC)
        records = [
            _txn_record(
                session_id=session_a,
                started_at=_utc(datetime(2026, 5, 4, 23, 14, 32), tz),
                ended_at=_utc(datetime(2026, 5, 5, 6, 42, 11), tz),
                energy_kwh=84.3,
                cost_total=17.28,
                vehicle_id=vehicle_uuid,
                driver_id=None,
                card_id=card_uuid,
            ),
            _txn_record(
                session_id=session_b,
                started_at=_utc(datetime(2026, 5, 3, 8, 0, 0), tz),
                ended_at=_utc(datetime(2026, 5, 3, 9, 0, 0), tz),
                energy_kwh=22.0,
                cost_total=None,  # → estimated
                vehicle_id=None,
                driver_id=driver_uuid,
                card_id=None,
            ),
        ]
        pools, _, _ = self._setup_pools(ts_records=records, timezone_name=tz, currency="EUR")

        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={"from": "2026-05-01", "to": "2026-05-31"},
            )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        # Top-level camelCase shape.
        assert body["depotId"] == depot_id
        assert body["currency"] == "EUR"
        assert body["from"] == "2026-05-01"
        assert body["to"] == "2026-05-31"
        assert body["nextCursor"] is None
        rows = body["rows"]
        assert len(rows) == 2

        # First row (cost present → estimated=false).
        first = rows[0]
        assert first["sessionId"] == session_a
        assert first["startedAt"].endswith("Z")
        assert first["endedAt"].endswith("Z")
        assert first["vehicleId"] == vehicle_uuid
        assert first["driverId"] is None
        assert first["cardId"] == card_uuid  # never the literal "unassigned"
        assert first["energyKwh"] == pytest.approx(84.3)
        # duration = 7h27m59s → 448 minutes
        assert first["durationMinutes"] == 448
        # avg = 84.3 / (448/60) ≈ 11.29 → rounded to 2 decimals
        assert first["avgKw"] == pytest.approx(round(84.3 / (448 / 60.0), 2))
        assert first["cost"] == {"amount": 17.28, "currency": "EUR", "estimated": False}

        # Second row (cost missing → estimated, amount = energy × under_cap_rate)
        second = rows[1]
        assert second["sessionId"] == session_b
        assert second["cardId"] is None
        assert second["cost"]["estimated"] is True
        # 22 kWh × 0.20 = 4.40
        assert second["cost"]["amount"] == pytest.approx(4.40)
        assert second["cost"]["currency"] == "EUR"

    def test_empty_range_returns_empty_rows(self, client):
        depot_id = str(uuid4())
        pools, _, _ = self._setup_pools(ts_records=[])

        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={"from": "2026-05-01", "to": "2026-05-31"},
            )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["rows"] == []
        assert body["nextCursor"] is None

    def test_active_session_duration_against_now(self, client):
        depot_id = str(uuid4())
        tz = "Europe/Vilnius"
        # Started 30 minutes ago, still active.
        now = datetime.now(ZoneInfo("UTC"))
        started = now.replace(microsecond=0) - timedelta_minutes(30)

        records = [
            _txn_record(
                session_id=str(uuid4()),
                started_at=started,
                ended_at=None,
                energy_kwh=0.0,
                cost_total=None,
            )
        ]
        pools, _, _ = self._setup_pools(ts_records=records, timezone_name=tz)
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={"from": "2026-05-01", "to": "2026-05-31"},
            )

        body = response.json()
        row = body["rows"][0]
        assert row["endedAt"] is None
        # Active session with no energy → 0 cost (estimated).
        assert row["energyKwh"] == 0.0
        assert row["cost"]["estimated"] is True
        assert row["cost"]["amount"] == 0.0
        # Duration: 30 ± 1 min (small wall-clock drift between request and check)
        assert 29 <= row["durationMinutes"] <= 31
        # avg = 0 / duration → 0
        assert row["avgKw"] == 0.0

    def test_manual_authorize_session_card_id_null_not_string(self, client):
        depot_id = str(uuid4())
        tz = "Europe/Vilnius"
        records = [
            _txn_record(
                session_id=str(uuid4()),
                started_at=_utc(datetime(2026, 5, 4, 12, 0), tz),
                ended_at=_utc(datetime(2026, 5, 4, 13, 0), tz),
                energy_kwh=10.0,
                cost_total=2.0,
                card_id=None,
            )
        ]
        pools, _, _ = self._setup_pools(ts_records=records, timezone_name=tz)
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={"from": "2026-05-01", "to": "2026-05-31"},
            )

        body = response.json()
        assert body["rows"][0]["cardId"] is None
        assert "unassigned" not in str(body)

    def test_filter_by_card_id_passes_uuid_to_sql(self, client):
        depot_id = str(uuid4())
        card_uuid = str(uuid4())
        tz = "Europe/Vilnius"

        pools, _, captured = self._setup_pools(ts_records=[], timezone_name=tz)
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={
                    "from": "2026-05-01",
                    "to": "2026-05-31",
                    "card_id": card_uuid,
                },
            )

        assert response.status_code == status.HTTP_200_OK
        # The card_id UUID must have been bound as a parameter.
        assert card_uuid in captured["args"]
        assert "cs.card_id =" in captured["query"]

    def test_filter_by_charger_id_resolves_to_station_id(self, client):
        depot_id = str(uuid4())
        ocpp_id = "abb-001"
        charger_uuid = str(uuid4())
        tz = "Europe/Vilnius"

        pools, _, captured = self._setup_pools(
            ts_records=[], timezone_name=tz, ocpp_id=ocpp_id, charger_uuid=charger_uuid
        )
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={
                    "from": "2026-05-01",
                    "to": "2026-05-31",
                    "charger_id": charger_uuid,
                },
            )

        assert response.status_code == status.HTTP_200_OK
        # Backend should have rewritten charger_id (UUID) → station_id (OCPP).
        assert ocpp_id in captured["args"]

    def test_charger_id_in_other_depot_returns_empty_without_db_call(self, client):
        depot_id = str(uuid4())
        other_charger_uuid = str(uuid4())

        pools, _, captured = self._setup_pools(ts_records=[])
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={
                    "from": "2026-05-01",
                    "to": "2026-05-31",
                    "charger_id": other_charger_uuid,
                },
            )

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["rows"] == []
        assert body["nextCursor"] is None
        # The SQL fetch should be short-circuited.
        assert "query" not in captured

    def test_search_matches_card_id_uuid_prefix(self, client):
        depot_id = str(uuid4())
        card_uuid = "09d29997-1111-2222-3333-444444444444"
        tz = "Europe/Vilnius"

        records = [
            _txn_record(
                session_id=str(uuid4()),
                started_at=_utc(datetime(2026, 5, 4, 12, 0), tz),
                ended_at=_utc(datetime(2026, 5, 4, 13, 0), tz),
                energy_kwh=10.0,
                cost_total=2.0,
                card_id=card_uuid,
            )
        ]
        pools, _, captured = self._setup_pools(ts_records=records, timezone_name=tz)
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={
                    "from": "2026-05-01",
                    "to": "2026-05-31",
                    "search": "09d29997",
                },
            )

        assert response.status_code == status.HTTP_200_OK
        # ILIKE pattern wrapping with % around the trimmed needle.
        assert "%09d29997%" in captured["args"]
        body = response.json()
        assert len(body["rows"]) == 1
        assert body["rows"][0]["cardId"] == card_uuid

    def test_pagination_emits_next_cursor_when_more_rows_exist(self, client):
        depot_id = str(uuid4())
        tz = "Europe/Vilnius"

        # 11 newest-first rows; limit=10 → first 10 returned + nextCursor pointing
        # at the 10th row's (started_at, session_id).
        records = []
        for i in range(11):
            records.append(
                _txn_record(
                    session_id=str(uuid4()),
                    started_at=_utc(datetime(2026, 5, 10, 12, 0) - timedelta_minutes(i), tz),
                    ended_at=_utc(datetime(2026, 5, 10, 13, 0) - timedelta_minutes(i), tz),
                    energy_kwh=1.0,
                    cost_total=0.10,
                )
            )

        pools, _, _ = self._setup_pools(ts_records=records, timezone_name=tz)
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={"from": "2026-05-01", "to": "2026-05-31", "limit": 10},
            )

        body = response.json()
        assert len(body["rows"]) == 10
        assert body["nextCursor"] is not None
        # Cursor is base64-encoded JSON containing startedAt + sessionId of the
        # last-returned row.
        import base64
        import json as _json

        decoded = _json.loads(base64.urlsafe_b64decode(body["nextCursor"]).decode("utf-8"))
        assert decoded["sessionId"] == body["rows"][-1]["sessionId"]
        assert decoded["startedAt"] == body["rows"][-1]["startedAt"]

    def test_pagination_returns_null_cursor_on_last_page(self, client):
        depot_id = str(uuid4())
        tz = "Europe/Vilnius"

        records = [
            _txn_record(
                session_id=str(uuid4()),
                started_at=_utc(datetime(2026, 5, 10, 12, 0), tz),
                ended_at=_utc(datetime(2026, 5, 10, 13, 0), tz),
                energy_kwh=1.0,
                cost_total=0.10,
            )
        ]
        pools, _, _ = self._setup_pools(ts_records=records, timezone_name=tz)
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={"from": "2026-05-01", "to": "2026-05-31", "limit": 10},
            )

        body = response.json()
        assert len(body["rows"]) == 1
        assert body["nextCursor"] is None

    def test_invalid_cursor_returns_400(self, client):
        depot_id = str(uuid4())
        pools, _, _ = self._setup_pools(ts_records=[])
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={
                    "from": "2026-05-01",
                    "to": "2026-05-31",
                    "cursor": "not-base64!!!",
                },
            )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_invalid_date_returns_400(self, client):
        depot_id = str(uuid4())
        pools, _, _ = self._setup_pools(ts_records=[])
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={"from": "not-a-date", "to": "2026-05-31"},
            )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_to_before_from_returns_400(self, client):
        depot_id = str(uuid4())
        pools, _, _ = self._setup_pools(ts_records=[])
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={"from": "2026-05-31", "to": "2026-05-01"},
            )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_range_over_one_year_returns_400(self, client):
        depot_id = str(uuid4())
        pools, _, _ = self._setup_pools(ts_records=[])
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={"from": "2024-01-01", "to": "2026-01-01"},
            )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_invalid_vehicle_id_returns_400(self, client):
        depot_id = str(uuid4())
        pools, _, _ = self._setup_pools(ts_records=[])
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={
                    "from": "2026-05-01",
                    "to": "2026-05-31",
                    "vehicle_id": "not-a-uuid",
                },
            )
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_limit_out_of_range_returns_400(self, client):
        depot_id = str(uuid4())
        pools, _, _ = self._setup_pools(ts_records=[])
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={
                    "from": "2026-05-01",
                    "to": "2026-05-31",
                    "limit": 100000,
                },
            )
        # FastAPI's Query(le=...) returns 422 for validation failures.
        assert response.status_code in (
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    def test_cross_org_denial(self, client):
        depot_id = str(uuid4())
        pools, _, _ = self._setup_pools(ts_records=[])

        async def _denied(*args, **kwargs):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: you do not have permission for this depot",
            )

        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", side_effect=_denied
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={"from": "2026-05-01", "to": "2026-05-31"},
            )

        assert response.status_code == status.HTTP_403_FORBIDDEN
        detail = response.json()["detail"]
        assert detail["error_code"] == "CROSS_ORG_DENIED"

    def test_session_at_2330_local_does_not_bleed_into_next_month(self, client):
        depot_id = str(uuid4())
        tz = "Europe/Vilnius"
        # 23:30 local on Apr 30 should still be inside [2026-04-01, 2026-04-30].
        records = [
            _txn_record(
                session_id=str(uuid4()),
                started_at=_utc(datetime(2026, 4, 30, 23, 30), tz),
                ended_at=_utc(datetime(2026, 5, 1, 0, 30), tz),
                energy_kwh=10.0,
                cost_total=2.0,
            )
        ]
        pools, _, captured = self._setup_pools(ts_records=records, timezone_name=tz)
        with patch("src.api.main.db_pools", pools), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(
                f"/reports/depots/{depot_id}/energy/transactions",
                params={"from": "2026-04-01", "to": "2026-04-30"},
            )
        # Filter assertion: query must include the depot timezone in args.
        assert tz in captured["args"]
        body = response.json()
        assert len(body["rows"]) == 1


# Convenience helper local to this module.
def timedelta_minutes(n: int):
    """timedelta(minutes=n) shorthand used only by the transactions tests."""
    from datetime import timedelta

    return timedelta(minutes=n)


# ── reports.approve delivery + report export (PDF/CSV) ───────────────────────


def _approved_report_row(kind: str = "monthly_consumption", *, status: str = "approved"):
    return {
        "id": str(uuid4()),
        "depot_id": str(uuid4()),
        "title": "Monthly electricity consumption — 2026-04",
        "kind": kind,
        "status": status,
        "group_by": "card",
        "period_start": datetime(2026, 4, 1, tzinfo=ZoneInfo("UTC")),
        "period_end": datetime(2026, 5, 1, tzinfo=ZoneInfo("UTC")),
        "data": {"group_by": "card", "rows": [], "totals": None, "depot_name": "HRX"},
    }


def _single_conn_pool(fetchrow_row):
    pool = MagicMock()
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=fetchrow_row)
    conn.transaction = MagicMock()
    conn.transaction.return_value.__aenter__.return_value = None
    conn.transaction.return_value.__aexit__.return_value = None
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    pool.ts = pool
    pool.static = pool
    return pool


class TestMarkReportApproved:
    """_mark_report_approved flips status and resolves export_url."""

    def test_monthly_consumption_sets_export_url(self):
        import asyncio

        approved_at = datetime(2026, 5, 2, tzinfo=ZoneInfo("UTC"))
        conn = AsyncMock()

        async def _fetchrow(query, *args):
            if "FOR UPDATE" in query:
                return {"kind": "monthly_consumption", "data": {"rows": []}}
            return {"approved_at": approved_at}

        conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        out = asyncio.run(
            _main._mark_report_approved(
                conn, depot_id="dep", report_id="rep-1", approved_by="u@x.com"
            )
        )
        assert out["kind"] == "monthly_consumption"
        assert out["exportUrl"] == "/depots/dep/reports/rep-1/export"
        assert out["approvedAt"] == approved_at

    def test_non_consumption_has_no_export_url(self):
        import asyncio

        conn = AsyncMock()

        async def _fetchrow(query, *args):
            if "FOR UPDATE" in query:
                return {"kind": "incident", "data": None}
            return {"approved_at": datetime(2026, 5, 2, tzinfo=ZoneInfo("UTC"))}

        conn.fetchrow = AsyncMock(side_effect=_fetchrow)
        out = asyncio.run(
            _main._mark_report_approved(conn, depot_id="dep", report_id="rep-1", approved_by=None)
        )
        assert out["exportUrl"] is None

    def test_missing_report_returns_none(self):
        import asyncio

        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        out = asyncio.run(
            _main._mark_report_approved(conn, depot_id="dep", report_id="rep-1", approved_by=None)
        )
        assert out is None


class TestDeliverReportToApprover:
    """_deliver_report_to_approver emails monthly_consumption reports to the approver."""

    def test_monthly_consumption_sends_to_approver(self):
        import asyncio

        email = FakeEmailClient()
        with patch.object(_main, "report_email_client", email), patch.object(
            _main, "report_email_from", "noreply@favonius.io"
        ):
            out = asyncio.run(
                _main._deliver_report_to_approver(
                    report_row=_approved_report_row(),
                    kind="monthly_consumption",
                    export_url="/depots/dep/reports/rep-1/export",
                    to_email="boss@depot.example",
                )
            )
        assert out["status"] == "sent"
        assert out["emailAddress"] == "boss@depot.example"
        assert out["format"] == "pdf"
        assert len(email.sent) == 1

    def test_non_consumption_returns_none(self):
        import asyncio

        with patch.object(_main, "report_email_client", FakeEmailClient()):
            out = asyncio.run(
                _main._deliver_report_to_approver(
                    report_row=_approved_report_row(kind="incident"),
                    kind="incident",
                    export_url=None,
                    to_email="boss@depot.example",
                )
            )
        assert out is None

    def test_no_email_returns_none(self):
        import asyncio

        with patch.object(_main, "report_email_client", FakeEmailClient()):
            out = asyncio.run(
                _main._deliver_report_to_approver(
                    report_row=_approved_report_row(),
                    kind="monthly_consumption",
                    export_url="/x",
                    to_email=None,
                )
            )
        assert out is None

    def test_no_email_client_returns_none(self):
        import asyncio

        with patch.object(_main, "report_email_client", None):
            out = asyncio.run(
                _main._deliver_report_to_approver(
                    report_row=_approved_report_row(),
                    kind="monthly_consumption",
                    export_url="/x",
                    to_email="boss@depot.example",
                )
            )
        assert out is None

    def test_delivery_failure_is_caught_not_raised(self):
        import asyncio

        def _boom(_message, _n):
            raise RuntimeError("provider exploded")

        with (
            patch.object(_main, "report_email_client", FakeEmailClient(script=_boom)),
            patch.object(_main, "report_email_from", "noreply@favonius.io"),
        ):
            out = asyncio.run(
                _main._deliver_report_to_approver(
                    report_row=_approved_report_row(),
                    kind="monthly_consumption",
                    export_url="/depots/dep/reports/rep-1/export",
                    to_email="boss@depot.example",
                )
            )
        # Best-effort: the exception is swallowed and surfaced as a failed delivery.
        assert out["status"] == "failed"
        assert out["error"]


class TestReportsApproveCommand:
    """reports.approve flips status and (monthly_consumption) reports a delivery."""

    @patch("src.api.main.get_audit_logger", return_value=None)
    def test_approve_returns_delivery_for_monthly_consumption(self, _audit, client):
        report_id = str(uuid4())
        depot_id = str(uuid4())
        approved_at = datetime(2026, 5, 2, tzinfo=ZoneInfo("UTC"))
        pool = _single_conn_pool(None)
        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main._mark_report_approved",
            new_callable=AsyncMock,
            return_value={
                "kind": "monthly_consumption",
                "exportUrl": f"/depots/{depot_id}/reports/{report_id}/export",
                "approvedAt": approved_at,
                "reportRow": _approved_report_row(),
            },
        ), patch(
            "src.api.main._deliver_report_to_approver",
            new_callable=AsyncMock,
            return_value={
                "emailAddress": "a@x.com",
                "format": "pdf",
                "status": "sent",
                "providerMessageId": "fake-1",
                "error": None,
            },
        ):
            resp = client.post(
                "/commands/execute",
                json={
                    "command": "reports.approve",
                    "depot_id": depot_id,
                    "params": {"reportId": report_id},
                    "dry_run": False,
                },
                headers={"Authorization": "Bearer test"},
            )
        assert resp.status_code == status.HTTP_200_OK, resp.text
        result = resp.json()["result"]
        assert result["status"] == "approved"
        assert result["exportUrl"].endswith("/export")
        assert result["delivery"]["status"] == "sent"


class TestExportReportFormat:
    """GET /depots/{id}/reports/{id}/export — PDF (default) or CSV."""

    def test_default_format_is_pdf(self, client):
        row = _approved_report_row()
        with patch("src.api.main.db_pools", _single_conn_pool(row)):
            resp = client.get(f"/depots/{row['depot_id']}/reports/{row['id']}/export")
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.headers["content-type"].startswith("application/pdf")
        assert resp.content[:4] == b"%PDF"

    def test_csv_format(self, client):
        row = _approved_report_row()
        with patch("src.api.main.db_pools", _single_conn_pool(row)):
            resp = client.get(f"/depots/{row['depot_id']}/reports/{row['id']}/export?format=csv")
        assert resp.status_code == status.HTTP_200_OK, resp.text
        assert resp.headers["content-type"].startswith("text/csv")

    def test_invalid_format_returns_422(self, client):
        row = _approved_report_row()
        with patch("src.api.main.db_pools", _single_conn_pool(row)):
            resp = client.get(f"/depots/{row['depot_id']}/reports/{row['id']}/export?format=xml")
        assert resp.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    def test_not_approved_returns_404(self, client):
        row = _approved_report_row(status="draft")
        with patch("src.api.main.db_pools", _single_conn_pool(row)):
            resp = client.get(f"/depots/{row['depot_id']}/reports/{row['id']}/export")
        assert resp.status_code == status.HTTP_404_NOT_FOUND
