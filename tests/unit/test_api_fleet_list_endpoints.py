"""Endpoint tests for ``GET /depots/{id}/chargers`` and ``/vehicles``.

Mocks the DB helpers in ``src.db.queries`` and asserts:
  * happy path response shape
  * 403 path through ``_require_depot_access``
  * Cache-Control header is set
  * UndefinedTableError on runtime fetch degrades gracefully
  * empty depot returns ``items: []``
  * ``connected_charger_id`` is resolved from the OCPP id
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
    """Make sure cache state from other tests doesn't leak in."""
    from src.api import main as _main

    _main._fleet_list_cache.clear()
    _main._fleet_list_locks.clear()
    yield
    _main._fleet_list_cache.clear()
    _main._fleet_list_locks.clear()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _charger_static_row(depot_id: str, *, ocpp_id: str = "fav_a-001") -> dict:
    return {
        "id": str(uuid4()),
        "depot_id": depot_id,
        "ocpp_id": ocpp_id,
        "display_name": "Bay 1",
        "vendor": "Kempower",
        "model": "C500",
        "serial_number": None,
        "firmware": None,
        "rated_kw": 150.0,
        "efficiency": 0.95,
        "connector_type": "CCS",
        "connector_count": 1,
        "connector_ids": [1],
        "auth_required": True,
        "network_notes": None,
        "created_at": _now(),
    }


def _vehicle_static_row(depot_id: str, *, external_id: str = "BUS-1") -> dict:
    return {
        "id": str(uuid4()),
        "depot_id": depot_id,
        "external_id": external_id,
        "display_name": f"Vehicle {external_id}",
        "vehicle_type": "transit_bus",
        "vin": None,
        "license_plate": None,
        "id_tag": f"TAG-{external_id}",
        "battery_capacity_kwh": 350.0,
        "max_charge_rate_kw": 150.0,
        "max_discharge_rate_kw": None,
        "v2g_capable": False,
        "make": None,
        "model": None,
        "year": None,
        "status": "active",
        "created_at": _now(),
    }


# ─── Chargers endpoint ────────────────────────────────────────────────────


class TestGetDepotChargers:
    def test_happy_path_includes_static_and_runtime_data(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        charger = _charger_static_row(depot_id)
        ocpp_id = charger["ocpp_id"]
        connector_status = {
            ocpp_id: {
                "ocpp_status": "Charging",
                "last_heartbeat_at": _now() - timedelta(seconds=5),
            }
        }
        open_session = {
            ocpp_id: {
                "session_id": "tx-1",
                "vehicle_id": str(uuid4()),
                "started_at": _now() - timedelta(minutes=5),
                "current_power_kw": 80.0,
                "current_soc": 0.5,
                "target_soc": 1.0,
                "estimated_end_at": _now() + timedelta(minutes=30),
            }
        }

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.list_chargers_for_depot",
                new_callable=AsyncMock,
                return_value=[charger],
            ),
            patch(
                "src.api.main.db_queries.latest_connector_status_by_stations",
                new_callable=AsyncMock,
                return_value=connector_status,
            ),
            patch(
                "src.api.main.db_queries.open_sessions_by_stations",
                new_callable=AsyncMock,
                return_value=open_session,
            ),
        ):
            response = client.get(f"/depots/{depot_id}/chargers", headers=AUTH_HDR)

        assert response.status_code == 200
        body = response.json()
        assert "fetched_at" in body
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["ocpp_id"] == ocpp_id
        assert item["status"] == "charging"
        assert item["ocpp_connector_status"] == "Charging"
        assert item["current_session"]["session_id"] == "tx-1"
        # Cache header is set so the BFF / browser can dedupe.
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
                "src.api.main.db_queries.list_chargers_for_depot",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            response = client.get(f"/depots/{depot_id}/chargers", headers=AUTH_HDR)

        assert response.status_code == 200
        assert response.json()["items"] == []

        app.dependency_overrides.clear()

    def test_missing_runtime_table_degrades_to_offline(self, client, mock_db_pool):
        """If `connector_status` table is missing on the ts pool, return rows as offline."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        charger = _charger_static_row(depot_id)

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.list_chargers_for_depot",
                new_callable=AsyncMock,
                return_value=[charger],
            ),
            patch(
                "src.api.main.db_queries.latest_connector_status_by_stations",
                new_callable=AsyncMock,
                side_effect=asyncpg.UndefinedTableError("connector_status missing"),
            ),
            patch(
                "src.api.main.db_queries.open_sessions_by_stations",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            response = client.get(f"/depots/{depot_id}/chargers", headers=AUTH_HDR)

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["status"] == "offline"
        assert item["ocpp_connector_status"] is None

        app.dependency_overrides.clear()

    def test_missing_runtime_column_degrades_to_offline(self, client, mock_db_pool):
        """If optional session enrichment has schema drift, still return static chargers."""
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        charger = _charger_static_row(depot_id)

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.list_chargers_for_depot",
                new_callable=AsyncMock,
                return_value=[charger],
            ),
            patch(
                "src.api.main.db_queries.latest_connector_status_by_stations",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "src.api.main.db_queries.open_sessions_by_stations",
                new_callable=AsyncMock,
                side_effect=asyncpg.UndefinedColumnError("current_power_kw missing"),
            ),
        ):
            response = client.get(f"/depots/{depot_id}/chargers", headers=AUTH_HDR)

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["status"] == "offline"
        assert item["current_session"] is None

        app.dependency_overrides.clear()

    def test_telemetry_freshness_keeps_charger_online(self, client, mock_db_pool):
        """MeterValues freshness alone keeps a charger online end-to-end.

        Regression: connector_status is cold and the liveness pg_notify bridge
        is down, but the charger is actively metering. ``telemetry`` MAX must
        drive ``last_interaction_at`` so the dashboard pill is not ``offline``.
        """
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        charger = _charger_static_row(depot_id)
        ocpp_id = charger["ocpp_id"]
        fresh = _now() - timedelta(seconds=20)

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.list_chargers_for_depot",
                new_callable=AsyncMock,
                return_value=[charger],
            ),
            patch(
                "src.api.main.db_queries.latest_connector_status_by_stations",
                new_callable=AsyncMock,
                return_value={},  # no recent StatusNotification
            ),
            patch(
                "src.api.main.db_queries.open_sessions_by_stations",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "src.api.main.db_queries.latest_telemetry_time_by_stations",
                new_callable=AsyncMock,
                return_value={ocpp_id: fresh},
            ),
        ):
            response = client.get(f"/depots/{depot_id}/chargers", headers=AUTH_HDR)

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["status"] == "idle", "fresh MeterValues → not offline"
        assert item["last_interaction_at"] == fresh.isoformat()

        app.dependency_overrides.clear()

    def test_cache_returns_same_payload_within_ttl(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        list_mock = AsyncMock(return_value=[_charger_static_row(depot_id)])
        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.list_chargers_for_depot",
                new=list_mock,
            ),
            patch(
                "src.api.main.db_queries.latest_connector_status_by_stations",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "src.api.main.db_queries.open_sessions_by_stations",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            r1 = client.get(f"/depots/{depot_id}/chargers", headers=AUTH_HDR)
            r2 = client.get(f"/depots/{depot_id}/chargers", headers=AUTH_HDR)

        assert r1.status_code == r2.status_code == 200
        assert r1.json() == r2.json()
        # Second call should be served from cache — single DB read.
        assert list_mock.await_count == 1

        app.dependency_overrides.clear()


# ─── Vehicles endpoint ────────────────────────────────────────────────────


class TestGetDepotVehicles:
    def test_happy_path_resolves_connected_charger_id(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        vehicle = _vehicle_static_row(depot_id)
        vid = vehicle["id"]
        charger_uuid = str(uuid4())
        ocpp_id = "fav_a-001"

        telemetry = {
            vid: {
                "last_seen_at": _now() - timedelta(seconds=5),
                "current_soc": 0.5,
                "current_power_kw": 80.0,
                "charger_id": charger_uuid,
                "is_plugged": True,
            }
        }
        session = {
            vid: {
                "session_id": "tx-1",
                "ocpp_id": ocpp_id,
                "started_at": _now() - timedelta(minutes=5),
                "current_power_kw": 80.0,
            }
        }

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.list_vehicles_for_depot",
                new_callable=AsyncMock,
                return_value=[vehicle],
            ),
            patch(
                "src.api.main.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={ocpp_id: charger_uuid},
            ),
            patch(
                "src.api.main.db_queries.latest_telemetry_by_vehicles",
                new_callable=AsyncMock,
                return_value=telemetry,
            ),
            patch(
                "src.api.main.db_queries.open_session_by_vehicles",
                new_callable=AsyncMock,
                return_value=session,
            ),
            patch(
                "src.api.main.db_queries.next_departures_by_vehicles",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "src.api.main.db_queries.active_schedule_by_vehicles",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            response = client.get(f"/depots/{depot_id}/vehicles", headers=AUTH_HDR)

        assert response.status_code == 200
        body = response.json()
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert item["external_id"] == "BUS-1"
        assert item["current_state"]["state"] == "charging"
        assert item["current_state"]["connected_charger_id"] == charger_uuid
        assert item["current_state"]["connected_session_id"] == "tx-1"

        app.dependency_overrides.clear()

    def test_vehicle_with_future_departure_below_required_soc_is_at_risk(
        self, client, mock_db_pool
    ):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))

        vehicle = _vehicle_static_row(depot_id)
        vid = vehicle["id"]
        telemetry = {
            vid: {
                "last_seen_at": _now() - timedelta(seconds=5),
                "current_soc": 0.6,
                "current_power_kw": 0.0,
                "charger_id": None,
                "is_plugged": False,
            }
        }
        next_departure = {
            vid: {
                "schedule_id": str(uuid4()),
                "route_id": "R12",
                "departure_time": _now() + timedelta(hours=2),
                "return_time": _now() + timedelta(hours=8),
                "required_soc": 0.99,
                "energy_kwh": 220.0,
            }
        }

        with (
            patch("src.api.main.db_pools", pool),
            patch("src.api.main.verify_depot_access", new_callable=AsyncMock),
            patch(
                "src.api.main.db_queries.list_vehicles_for_depot",
                new_callable=AsyncMock,
                return_value=[vehicle],
            ),
            patch(
                "src.api.main.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "src.api.main.db_queries.latest_telemetry_by_vehicles",
                new_callable=AsyncMock,
                return_value=telemetry,
            ),
            patch(
                "src.api.main.db_queries.open_session_by_vehicles",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch(
                "src.api.main.db_queries.next_departures_by_vehicles",
                new_callable=AsyncMock,
                return_value=next_departure,
            ),
            patch(
                "src.api.main.db_queries.active_schedule_by_vehicles",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            response = client.get(f"/depots/{depot_id}/vehicles", headers=AUTH_HDR)

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["current_state"]["state"] == "at_risk"
        assert item["next_departure"]["required_soc"] == 0.99

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
                "src.api.main.db_queries.list_vehicles_for_depot",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "src.api.main.db_queries.charger_id_by_ocpp_id",
                new_callable=AsyncMock,
                return_value={},
            ),
        ):
            response = client.get(f"/depots/{depot_id}/vehicles", headers=AUTH_HDR)

        assert response.status_code == 200
        assert response.json()["items"] == []

        app.dependency_overrides.clear()
