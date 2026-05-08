"""Endpoint tests for the live-session / realtime-state endpoints.

Covers:
  * ``GET /depots/{id}/sessions/active``
  * ``GET /depots/{id}/sessions``
  * ``GET /depots/{id}/vehicles/state``

These three endpoints replace the Supabase mirror tables
(``charging_sessions_active``, ``charging_sessions_summary``,
``vehicle_realtime_state``) — the canonical source is TimescaleDB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
import pytest

from src.api.main import app
from src.security.tenant_mirror import ensure_tenant_mirrored

AUTH_HDR = {"Authorization": "Bearer test-token"}


def _user(org_id: str, role: str = "customer_admin") -> dict:
    return {
        "sub": str(uuid4()),
        "app_metadata": {"favonius_role": role, "organization_id": org_id},
    }


def _override_token(user: dict):
    def override():
        return user

    return override


@pytest.fixture(autouse=True)
def _clear_fleet_cache():
    """Cache state from earlier tests must not leak into these."""
    from src.api import main as _main

    _main._fleet_list_cache.clear()
    _main._fleet_list_locks.clear()
    yield
    _main._fleet_list_cache.clear()
    _main._fleet_list_locks.clear()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ─── /depots/{id}/sessions/active ─────────────────────────────────────────


class TestGetDepotActiveSessions:
    def test_happy_path_returns_open_sessions(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        ocpp_id = "hrx-uab_hrx-vilnius-001"
        ocpp_id_map = {ocpp_id: str(uuid4())}
        active_rows = [
            {
                "session_id": str(uuid4()),
                "ocpp_id": ocpp_id,
                "connector_id": 1,
                "vehicle_id": str(uuid4()),
                "started_at": _now() - timedelta(minutes=12),
                "current_power_kw": 75.4,
                "current_soc": 0.62,
                "target_soc": 0.99,
                "estimated_end_at": _now() + timedelta(minutes=20),
                "last_sample_at": _now() - timedelta(seconds=5),
            }
        ]

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value=ocpp_id_map,
            ),
            patch(
                "src.api.main.db_queries.list_active_sessions_for_depot",
                new_callable=AsyncMock,
                return_value=active_rows,
            ),
        ):
            response = client.get(
                f"/depots/{depot_id}/sessions/active", headers=AUTH_HDR
            )

        assert response.status_code == 200
        body = response.json()
        assert "fetched_at" in body
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["ocpp_id"] == ocpp_id
        assert item["current_power_kw"] == pytest.approx(75.4)
        assert item["current_soc"] == pytest.approx(0.62)
        # Cache header so polling browsers / BFF can dedupe.
        assert "max-age=5" in response.headers.get("cache-control", "")

        app.dependency_overrides.clear()

    def test_empty_depot_returns_empty_items(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "src.api.main.db_queries.list_active_sessions_for_depot",
                new_callable=AsyncMock,
                return_value=[],
            ) as list_active,
        ):
            response = client.get(
                f"/depots/{depot_id}/sessions/active", headers=AUTH_HDR
            )

        assert response.status_code == 200
        assert response.json()["items"] == []
        # No chargers in the depot → we should not pay for a Timescale round-trip.
        list_active.assert_not_called()

        app.dependency_overrides.clear()

    def test_missing_charging_sessions_table_degrades(self, client, mock_db_pool):
        """If `charging_sessions` is missing, return empty items rather than 500."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={"hrx-uab_hrx-vilnius-001": str(uuid4())},
            ),
            patch(
                "src.api.main.db_queries.list_active_sessions_for_depot",
                new_callable=AsyncMock,
                side_effect=asyncpg.UndefinedTableError("charging_sessions missing"),
            ),
        ):
            response = client.get(
                f"/depots/{depot_id}/sessions/active", headers=AUTH_HDR
            )

        assert response.status_code == 200
        assert response.json()["items"] == []

        app.dependency_overrides.clear()

    def test_cache_returns_same_payload_within_ttl(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        list_mock = AsyncMock(return_value=[])
        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={"hrx-001": str(uuid4())},
            ) as map_mock,
            patch(
                "src.api.main.db_queries.list_active_sessions_for_depot", list_mock
            ),
        ):
            r1 = client.get(f"/depots/{depot_id}/sessions/active", headers=AUTH_HDR)
            r2 = client.get(f"/depots/{depot_id}/sessions/active", headers=AUTH_HDR)

        assert r1.status_code == 200
        assert r2.status_code == 200
        # Within the 2 s TTL the second call must hit the cache and skip both DBs.
        assert map_mock.call_count == 1
        assert list_mock.call_count == 1

        app.dependency_overrides.clear()


# ─── /depots/{id}/sessions ────────────────────────────────────────────────


class TestGetDepotSessions:
    def test_happy_path_paginates_completed_sessions(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        end_time = _now() - timedelta(hours=1)
        sid_a = str(uuid4())
        sid_b = str(uuid4())
        rows = [
            {
                "session_id": sid_a,
                "ocpp_id": "hrx-001",
                "connector_id": 1,
                "vehicle_id": str(uuid4()),
                "driver_id": None,
                "started_at": end_time - timedelta(minutes=45),
                "ended_at": end_time,
                "energy_delivered_kwh": 31.5,
                "energy_received_kwh": None,
                "cost_total": 5.12,
                "start_soc_percent": 30.0,
                "end_soc_percent": 95.0,
                "source": "live",
            },
            {
                "session_id": sid_b,
                "ocpp_id": None,
                "connector_id": None,
                "vehicle_id": None,
                "driver_id": None,
                "started_at": end_time - timedelta(hours=2),
                "ended_at": end_time - timedelta(minutes=30),
                "energy_delivered_kwh": 22.0,
                "energy_received_kwh": None,
                "cost_total": 3.50,
                "start_soc_percent": 40.0,
                "end_soc_percent": 80.0,
                "source": "import",
            },
        ]

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={"hrx-001": str(uuid4())},
            ),
            patch(
                "src.api.main.db_queries.list_completed_sessions_for_depot",
                new_callable=AsyncMock,
                return_value=rows,
            ),
        ):
            response = client.get(
                f"/depots/{depot_id}/sessions?limit=2", headers=AUTH_HDR
            )

        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 2
        # Page is full at the requested limit → cursor must be returned.
        assert body["next_cursor"] is not None
        assert body["items"][1]["source"] == "import"
        assert body["items"][1]["ocpp_id"] is None  # imported row, no live charger

        app.dependency_overrides.clear()

    def test_partial_page_returns_no_cursor(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        rows = [
            {
                "session_id": str(uuid4()),
                "ocpp_id": "hrx-001",
                "connector_id": 1,
                "vehicle_id": None,
                "driver_id": None,
                "started_at": _now() - timedelta(hours=2),
                "ended_at": _now() - timedelta(hours=1),
                "energy_delivered_kwh": None,
                "energy_received_kwh": None,
                "cost_total": None,
                "start_soc_percent": None,
                "end_soc_percent": None,
                "source": "live",
            }
        ]

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={"hrx-001": str(uuid4())},
            ),
            patch(
                "src.api.main.db_queries.list_completed_sessions_for_depot",
                new_callable=AsyncMock,
                return_value=rows,
            ),
        ):
            response = client.get(
                f"/depots/{depot_id}/sessions?limit=50", headers=AUTH_HDR
            )

        body = response.json()
        assert len(body["items"]) == 1
        assert body["next_cursor"] is None

        app.dependency_overrides.clear()

    def test_invalid_cursor_returns_400(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
        ):
            response = client.get(
                f"/depots/{depot_id}/sessions?cursor=not-base64", headers=AUTH_HDR
            )

        assert response.status_code == 400

        app.dependency_overrides.clear()

    def test_cursor_round_trip(self, client, mock_db_pool):
        """Cursor returned in page 1 must decode and be passed through to the helper."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        end_time = _now() - timedelta(hours=1)
        last_session_id = str(uuid4())
        page1_rows = [
            {
                "session_id": last_session_id,
                "ocpp_id": "hrx-001",
                "connector_id": 1,
                "vehicle_id": None,
                "driver_id": None,
                "started_at": end_time - timedelta(minutes=10),
                "ended_at": end_time,
                "energy_delivered_kwh": None,
                "energy_received_kwh": None,
                "cost_total": None,
                "start_soc_percent": None,
                "end_soc_percent": None,
                "source": "live",
            }
        ]

        list_mock = AsyncMock(return_value=page1_rows)
        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={"hrx-001": str(uuid4())},
            ),
            patch(
                "src.api.main.db_queries.list_completed_sessions_for_depot", list_mock
            ),
        ):
            r1 = client.get(
                f"/depots/{depot_id}/sessions?limit=1", headers=AUTH_HDR
            )
            cursor = r1.json()["next_cursor"]
            assert cursor

            list_mock.return_value = []
            r2 = client.get(
                f"/depots/{depot_id}/sessions?limit=1&cursor={cursor}",
                headers=AUTH_HDR,
            )

        assert r2.status_code == 200
        # Helper got the decoded tuple, not the raw cursor string.
        kwargs = list_mock.await_args_list[-1].kwargs
        assert kwargs["cursor"] is not None
        ts, sid = kwargs["cursor"]
        assert sid == last_session_id
        assert isinstance(ts, datetime)

        app.dependency_overrides.clear()

    def test_inverted_date_range_returns_400(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
        ):
            response = client.get(
                f"/depots/{depot_id}/sessions"
                "?from=2026-05-08T00:00:00Z&to=2026-05-07T00:00:00Z",
                headers=AUTH_HDR,
            )

        assert response.status_code == 400

        app.dependency_overrides.clear()


# ─── /depots/{id}/vehicles/state ──────────────────────────────────────────


class TestGetDepotVehiclesState:
    def test_happy_path_returns_latest_telemetry(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        veh_id = str(uuid4())
        static_rows = [
            {
                "id": veh_id,
                "depot_id": depot_id,
                "external_id": "BUS-1",
                "display_name": "Bus 1",
                "vehicle_type": "transit_bus",
                "vin": None,
                "license_plate": None,
                "id_tag": "TAG-1",
                "battery_capacity_kwh": 350.0,
                "max_charge_rate_kw": 150.0,
                "max_discharge_rate_kw": None,
                "v2g_capable": False,
                "status": "active",
                "created_at": _now(),
            }
        ]
        telemetry_rows = [
            {
                "vehicle_id": veh_id,
                "charger_id": str(uuid4()),
                "last_seen_at": _now() - timedelta(seconds=10),
                "soc": 0.61,
                "power_kw": 72.0,
                "is_plugged": True,
            }
        ]

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.list_vehicles_for_depot",
                new_callable=AsyncMock,
                return_value=static_rows,
            ),
            patch(
                "src.api.main.db_queries.latest_telemetry_for_depot_vehicles",
                new_callable=AsyncMock,
                return_value=telemetry_rows,
            ),
        ):
            response = client.get(
                f"/depots/{depot_id}/vehicles/state", headers=AUTH_HDR
            )

        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["vehicle_id"] == veh_id
        assert item["soc"] == pytest.approx(0.61)
        assert item["power_kw"] == pytest.approx(72.0)
        assert item["is_plugged"] is True
        assert "max-age=5" in response.headers.get("cache-control", "")

        app.dependency_overrides.clear()

    def test_no_vehicles_returns_empty(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.list_vehicles_for_depot",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "src.api.main.db_queries.latest_telemetry_for_depot_vehicles",
                new_callable=AsyncMock,
                return_value=[],
            ) as telemetry_mock,
        ):
            response = client.get(
                f"/depots/{depot_id}/vehicles/state", headers=AUTH_HDR
            )

        assert response.status_code == 200
        assert response.json()["items"] == []
        telemetry_mock.assert_not_called()

        app.dependency_overrides.clear()

    def test_missing_telemetry_table_degrades(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        veh_id = str(uuid4())
        static_rows = [
            {
                "id": veh_id,
                "depot_id": depot_id,
                "external_id": "BUS-1",
                "display_name": "Bus 1",
                "vehicle_type": "transit_bus",
                "vin": None,
                "license_plate": None,
                "id_tag": "TAG-1",
                "battery_capacity_kwh": 350.0,
                "max_charge_rate_kw": 150.0,
                "max_discharge_rate_kw": None,
                "v2g_capable": False,
                "status": "active",
                "created_at": _now(),
            }
        ]

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.list_vehicles_for_depot",
                new_callable=AsyncMock,
                return_value=static_rows,
            ),
            patch(
                "src.api.main.db_queries.latest_telemetry_for_depot_vehicles",
                new_callable=AsyncMock,
                side_effect=asyncpg.UndefinedTableError("telemetry missing"),
            ),
        ):
            response = client.get(
                f"/depots/{depot_id}/vehicles/state", headers=AUTH_HDR
            )

        assert response.status_code == 200
        # Static vehicles exist but no telemetry → the endpoint returns an
        # empty items list rather than 500.
        assert response.json()["items"] == []

        app.dependency_overrides.clear()
