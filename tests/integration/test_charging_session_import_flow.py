"""Integration tests for the historical charging-session XLSX backfill flow.

Stitches the two endpoints the Reports → Energy accounting bulk-import dialog
relies on:

    POST /admin/depots/{id}/charging-sessions/import
        ↓ writes to charging_sessions with site_id=depot_id, source='import'
    GET  /reports/depots/{id}/energy/monthly
        ↓ surfaces both live (station_id) and imported (site_id) rows

The DB layer is mocked via a small in-memory store so static and ts pool
acquires share state. This pins down the cross-endpoint contract — that the
``_fetch_session_rows`` WHERE clause picks up imported rows and the
aggregation produces the correct totals when live and imported rows are
mixed in the same period.

For pure aggregation correctness, see tests/unit/test_api_reports.py.
For per-row import edge cases, see tests/unit/test_charging_session_import.py.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock as _MagicMock

# Pyomo is a heavy optional dep not installed in the test venv. Stub it before
# any ``src.*`` imports trigger the controller_manager → optimizer → pyomo
# chain. Mirrors the same trick used in tests/unit/conftest.py.
if "pyomo" not in sys.modules:
    _pyomo_mock = _MagicMock()
    sys.modules["pyomo"] = _pyomo_mock
    sys.modules["pyomo.environ"] = _pyomo_mock
    sys.modules["pyomo.core"] = _pyomo_mock
    sys.modules["pyomo.opt"] = _pyomo_mock

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi import status
from fastapi.testclient import TestClient

from src.api.main import app
from src.security.tenant_mirror import ensure_tenant_mirrored


AUTH_HDR = {"Authorization": "Bearer test-token"}


# --------------------------------------------------------------------------- #
# In-memory store shared across POST/GET calls
# --------------------------------------------------------------------------- #


class _SessionStore:
    """Tiny mock charging_sessions table.

    Tracks live and imported rows in a shape the report query can read back.
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def insert_imported(
        self,
        *,
        station_id: str,
        site_id: str,
        start_time: datetime,
        end_time: datetime | None,
        energy_kwh: float,
        cost_total: float,
        vehicle_id: str | None,
        driver_id: str | None,
        card_id: str | None,
    ) -> str:
        session_id = str(uuid4())
        self.rows.append(
            {
                "session_id": session_id,
                "station_id": station_id,
                "site_id": site_id,
                "start_time": start_time,
                "end_time": end_time,
                "energy_delivered_kwh": energy_kwh,
                "cost_total": cost_total,
                "vehicle_id": vehicle_id,
                "driver_id": driver_id,
                "card_id": card_id,
                "source": "import",
            }
        )
        return session_id

    def insert_live(
        self,
        *,
        station_id: str,
        start_time: datetime,
        end_time: datetime | None,
        energy_kwh: float,
        cost_total: float,
        vehicle_id: str,
    ) -> None:
        """Simulate an OCPP-derived row: scoped by station_id, not site_id."""
        self.rows.append(
            {
                "session_id": str(uuid4()),
                "station_id": station_id,
                "site_id": None,  # live rows do not currently set site_id
                "start_time": start_time,
                "end_time": end_time,
                "energy_delivered_kwh": energy_kwh,
                "cost_total": cost_total,
                "vehicle_id": vehicle_id,
                "driver_id": None,
                "card_id": None,
                "source": "live",
            }
        )

    def select_for_report(
        self,
        *,
        ocpp_ids: list[str],
        site_id: str,
        from_date,
        to_date,
        tz: str,
    ) -> list[dict[str, Any]]:
        """Mirror the WHERE clause in _fetch_session_rows."""
        zi = ZoneInfo(tz)
        from_utc = datetime.combine(from_date, datetime.min.time()).replace(tzinfo=zi).astimezone(ZoneInfo("UTC"))
        # exclusive: < (to_date + 1 day) at depot tz
        from datetime import timedelta as _td
        to_exclusive_utc = (
            datetime.combine(to_date + _td(days=1), datetime.min.time())
            .replace(tzinfo=zi)
            .astimezone(ZoneInfo("UTC"))
        )

        out: list[dict[str, Any]] = []
        for row in self.rows:
            station_match = row["station_id"] in ocpp_ids
            site_match = row["site_id"] == site_id
            if not (station_match or site_match):
                continue
            st = row["start_time"]
            if st < from_utc or st >= to_exclusive_utc:
                continue
            out.append(
                {
                    "start_time": row["start_time"],
                    "end_time": row["end_time"],
                    "energy_delivered_kwh": row["energy_delivered_kwh"],
                    "cost_total": row["cost_total"],
                    "vehicle_id": row["vehicle_id"],
                    "charger_id": row["station_id"],  # report uses station_id alias
                    "driver_id": row["driver_id"],
                    "card_id": row["card_id"],
                }
            )
        out.sort(key=lambda r: r["start_time"])
        return out


# --------------------------------------------------------------------------- #
# Pool builder: static and ts share state via the SessionStore
# --------------------------------------------------------------------------- #


def _build_shared_pool(
    *,
    store: _SessionStore,
    depot_id: str,
    org_id: str,
    timezone_name: str,
    currency: str,
    charger_rows: list[dict[str, str]],
):
    """Build a fake db_pools where both static and ts mutate the same store."""

    placeholder_station = f"imported:{depot_id}"

    # ── Static connection: sites lookup, identity resolution, charger rows ──
    static_conn = AsyncMock()

    async def static_fetchrow(query: str, *args, **kwargs):
        if "FROM sites" in query and "organization_id" in query:
            # Import endpoint depot/org check.
            if str(args[0]) == depot_id and str(args[1]) == org_id:
                return {"timezone": timezone_name}
            return None
        if "FROM sites" in query:
            # Reports endpoint depot row.
            return {
                "timezone": timezone_name,
                "currency": currency,
                "billing_metadata": {"under_cap_rate": 0.20},
            }
        if "FROM vehicles" in query:
            # No vehicle id_tag matches in this fixture — frontend keeps the
            # raw "Opel Mokka" / "ED8503" cell as id_token.
            return None
        if "FROM rfid_cards" in query:
            return None
        return None

    async def static_fetch(query: str, *args, **kwargs):
        if "FROM charging_stations" in query:
            return charger_rows
        return []

    static_conn.fetchrow.side_effect = static_fetchrow
    static_conn.fetch.side_effect = static_fetch

    static_pool = MagicMock()
    static_pool.acquire.return_value.__aenter__.return_value = static_conn
    static_pool.acquire.return_value.__aexit__.return_value = None

    # ── Timeseries connection: INSERT (import) and SELECT (reports) ──
    ts_conn = AsyncMock()

    async def ts_fetchval(query: str, *args, **kwargs):
        if "INSERT INTO charging_sessions" in query:
            (
                station_id,
                vehicle_id,
                _id_token,
                driver_id,
                card_id,
                start_time,
                end_time,
                energy,
                revenue,
                site_id,
                _batch,
                _hash,
                _user_full,
                _station_owner,
                _import_status,
            ) = args
            assert station_id == placeholder_station, (
                "Import endpoint must use the deterministic placeholder station_id"
            )
            assert site_id == depot_id, "Import row must scope by site_id=depot_id"
            return store.insert_imported(
                station_id=station_id,
                site_id=site_id,
                start_time=start_time,
                end_time=end_time,
                energy_kwh=float(energy),
                cost_total=float(revenue),
                vehicle_id=vehicle_id,
                driver_id=driver_id,
                card_id=card_id,
            )
        return None

    async def ts_fetch(query: str, *args, **kwargs):
        if "FROM charging_sessions" in query:
            ocpp_ids = list(args[0])
            from_date = args[1]
            to_date = args[2]
            tz = args[3]
            site_id = args[4]
            return store.select_for_report(
                ocpp_ids=ocpp_ids,
                site_id=site_id,
                from_date=from_date,
                to_date=to_date,
                tz=tz,
            )
        return []

    ts_conn.fetchval.side_effect = ts_fetchval
    ts_conn.fetch.side_effect = ts_fetch

    ts_pool = MagicMock()
    ts_pool.acquire.return_value.__aenter__.return_value = ts_conn
    ts_pool.acquire.return_value.__aexit__.return_value = None

    pools = MagicMock()
    pools.static = static_pool
    pools.ts = ts_pool
    return pools, static_conn, ts_conn


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def bypass_geo_block():
    """GeoIP is unavailable in CI, so the middleware fail-closes on every request.

    Mirror the unit-test conftest helper so the testclient can reach handlers.
    """
    _not_blocked = MagicMock(blocked=False)
    with patch("src.security.geo_block.check_ip_blocked", return_value=_not_blocked):
        yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def clear_overrides():
    yield
    app.dependency_overrides.clear()


def _user(org_id: str, role: str = "customer_admin") -> dict:
    return {
        "sub": str(uuid4()),
        "app_metadata": {"favonius_role": role, "organization_id": org_id},
    }


def _override_token(user: dict):
    def _impl():
        return user

    return _impl


def _utc(dt_local: datetime, tz: str) -> datetime:
    return dt_local.replace(tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC"))


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def test_imported_rows_appear_alongside_live_rows_in_monthly_report(client):
    """End-to-end: POST 2 imported rows + 1 live row → /energy/monthly aggregates all three.

    Verifies the WHERE clause tweak: imported rows (selected by site_id) and
    live rows (selected by station_id) bucket into the same monthly summary.
    """
    depot_id = str(uuid4())
    org_id = str(uuid4())
    charger_uuid = str(uuid4())
    ocpp_id = "ocpp_charger_a"
    tz = "Europe/Vilnius"
    store = _SessionStore()

    # Pre-seed one live OCPP-derived row in the same period (2026-05).
    store.insert_live(
        station_id=ocpp_id,
        start_time=_utc(datetime(2026, 5, 6, 10, 0), tz),
        end_time=_utc(datetime(2026, 5, 6, 12, 0), tz),
        energy_kwh=50.0,
        cost_total=10.0,
        vehicle_id="bus_live",
    )

    pools, *_ = _build_shared_pool(
        store=store,
        depot_id=depot_id,
        org_id=org_id,
        timezone_name=tz,
        currency="EUR",
        charger_rows=[{"charger_id": charger_uuid, "ocpp_id": ocpp_id}],
    )

    app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

    with patch("src.api.main.db_pools", pools), patch(
        "src.api.main.verify_depot_access", new_callable=AsyncMock
    ), patch("src.api.main._audit_identity_write", new_callable=AsyncMock):
        # Import row #1: HRX example, Finished, 24.044 kWh.
        r1 = client.post(
            f"/admin/depots/{depot_id}/charging-sessions/import",
            headers=AUTH_HDR,
            json={
                "import_batch_id": str(uuid4()),
                "start_time_local": "2026-05-05 12:56",
                "end_time_local": "2026-05-11 01:17",
                "energy_delivered_kwh": 24.044,
                "revenue": 0,
                "id_tag": "ED8503",
                "status": "Finished",
                "transaction_type": "RFID",
                "user_full_name": "HRX Transport",
                "station_owner_full_name": "Gustas Diksa",
            },
        )
        assert r1.status_code == status.HTTP_201_CREATED, r1.text

        # Import row #2: nickname id_tag, Finished, 11.782 kWh.
        r2 = client.post(
            f"/admin/depots/{depot_id}/charging-sessions/import",
            headers=AUTH_HDR,
            json={
                "import_batch_id": str(uuid4()),
                "start_time_local": "2026-05-05 08:43",
                "end_time_local": "2026-05-11 05:27",
                "energy_delivered_kwh": 11.782,
                "revenue": 0,
                "id_tag": "Opel Mokka",
                "status": "Finished",
                "transaction_type": "RFID",
                "user_full_name": "HRX Transport",
                "station_owner_full_name": "Gustas Diksa",
            },
        )
        assert r2.status_code == status.HTTP_201_CREATED, r2.text

        # Now read back via /reports/depots/{id}/energy/monthly.
        report = client.get(
            f"/reports/depots/{depot_id}/energy/monthly",
            params={"from": "2026-05-01", "to": "2026-05-31"},
        )

    assert report.status_code == status.HTTP_200_OK, report.text
    body = report.json()
    assert body["depot_id"] == depot_id
    assert body["currency"] == "EUR"

    # All three rows fall into 2026-05.
    assert len(body["rows"]) == 1
    bucket = body["rows"][0]
    assert bucket["bucket"] == "2026-05"
    assert bucket["session_count"] == 3

    # 24.044 + 11.782 (imported) + 50.0 (live) = 85.826 kWh
    assert bucket["energy_kwh"] == pytest.approx(24.044 + 11.782 + 50.0)

    # Cost: live=10.0 explicit; imported revenue=0 → cost_total=0 (explicit, not
    # estimated) → total 10.0, estimated=False.
    assert bucket["cost"]["amount"] == pytest.approx(10.0)
    assert bucket["cost"]["estimated"] is False
    assert bucket["cost"]["currency"] == "EUR"

    # Sanity: store actually contains 1 live + 2 imported rows.
    assert sum(1 for r in store.rows if r["source"] == "import") == 2
    assert sum(1 for r in store.rows if r["source"] == "live") == 1


def test_imported_only_depot_still_renders_in_monthly_report(client):
    """A depot with zero live rows still surfaces imported XLSX rows in reports.

    Pilots without OCPP-emitted sessions (HRX-style) rely on this path. The
    report query must NOT short-circuit when the depot has no chargers in
    ``charging_stations``.
    """
    depot_id = str(uuid4())
    org_id = str(uuid4())
    tz = "Europe/Vilnius"
    store = _SessionStore()

    pools, *_ = _build_shared_pool(
        store=store,
        depot_id=depot_id,
        org_id=org_id,
        timezone_name=tz,
        currency="EUR",
        charger_rows=[],  # no charging_stations rows for this depot
    )

    app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

    with patch("src.api.main.db_pools", pools), patch(
        "src.api.main.verify_depot_access", new_callable=AsyncMock
    ), patch("src.api.main._audit_identity_write", new_callable=AsyncMock):
        post_resp = client.post(
            f"/admin/depots/{depot_id}/charging-sessions/import",
            headers=AUTH_HDR,
            json={
                "import_batch_id": str(uuid4()),
                "start_time_local": "2026-05-05 12:56",
                "end_time_local": "2026-05-11 01:17",
                "energy_delivered_kwh": 24.044,
                "revenue": 5.50,
                "id_tag": "ED8503",
                "status": "Finished",
                "transaction_type": "RFID",
                "user_full_name": "HRX Transport",
                "station_owner_full_name": "Gustas Diksa",
            },
        )
        assert post_resp.status_code == status.HTTP_201_CREATED, post_resp.text

        report = client.get(
            f"/reports/depots/{depot_id}/energy/monthly",
            params={"from": "2026-05-01", "to": "2026-05-31"},
        )

    assert report.status_code == status.HTTP_200_OK, report.text
    body = report.json()
    assert len(body["rows"]) == 1
    assert body["rows"][0]["bucket"] == "2026-05"
    assert body["rows"][0]["session_count"] == 1
    assert body["rows"][0]["energy_kwh"] == pytest.approx(24.044)
    assert body["rows"][0]["cost"]["amount"] == pytest.approx(5.50)
    assert body["rows"][0]["cost"]["estimated"] is False


def test_imported_rows_outside_window_are_excluded(client):
    """Imported row in May is NOT visible in a June report. Date filter still applies."""
    depot_id = str(uuid4())
    org_id = str(uuid4())
    tz = "Europe/Vilnius"
    store = _SessionStore()

    pools, *_ = _build_shared_pool(
        store=store,
        depot_id=depot_id,
        org_id=org_id,
        timezone_name=tz,
        currency="EUR",
        charger_rows=[],
    )

    app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

    with patch("src.api.main.db_pools", pools), patch(
        "src.api.main.verify_depot_access", new_callable=AsyncMock
    ), patch("src.api.main._audit_identity_write", new_callable=AsyncMock):
        client.post(
            f"/admin/depots/{depot_id}/charging-sessions/import",
            headers=AUTH_HDR,
            json={
                "import_batch_id": str(uuid4()),
                "start_time_local": "2026-05-05 12:56",
                "end_time_local": "2026-05-05 14:00",
                "energy_delivered_kwh": 24.044,
                "revenue": 0,
                "id_tag": "ED8503",
                "status": "Finished",
                "transaction_type": "RFID",
                "user_full_name": "HRX Transport",
                "station_owner_full_name": "Gustas Diksa",
            },
        ).raise_for_status()

        report = client.get(
            f"/reports/depots/{depot_id}/energy/monthly",
            params={"from": "2026-06-01", "to": "2026-06-30"},
        )

    assert report.status_code == status.HTTP_200_OK
    assert report.json()["rows"] == []
