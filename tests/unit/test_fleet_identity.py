"""Tests for fleet vehicle, driver, and RFID identity support."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException, status as http_status
from ocpp.v16.enums import AuthorizationStatus

from src.api.main import app
from src.adapters.ocpp.server import OCPPServer
from src.db.pools import DatabasePools
from src.db import queries as db_queries
from src.security.tenant_mirror import ensure_tenant_mirrored


AUTH_HDR = {"Authorization": "Bearer test-token"}


def _user(org_id: str, role: str = "customer_admin") -> dict:
    """Build a trusted Supabase JWT payload."""
    return {
        "sub": str(uuid4()),
        "app_metadata": {"favonius_role": role, "organization_id": org_id},
    }


def _override_token(user: dict):
    """Return a dependency override that returns ``user``."""
    def override():
        return user

    return override


def _vehicle_response(depot_id: str, vehicle_id: str, id_tag: str | None = "VEH-1") -> dict:
    return {
        "vehicle_id": vehicle_id,
        "depot_id": depot_id,
        "external_id": "bus-1",
        "display_name": "Bus 1",
        "vehicle_type": "bus_large",
        "battery_kwh": 324.0,
        "max_charge_kw": 150.0,
        "id_tag": id_tag,
        "vin": None,
        "license_plate": None,
        "status": "active",
    }


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    yield
    app.dependency_overrides.clear()


class TestFleetIdentityApi:
    """Admin REST contract and tenancy checks."""

    def test_get_identity_requires_customer_admin_and_returns_tab_payload(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))
        pool, _ = mock_db_pool
        identity = {
            "vehicles": [_vehicle_response(depot_id, str(uuid4()))],
            "drivers": [],
            "rfid_cards": [],
        }

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.list_fleet_identity",
            new_callable=AsyncMock,
            return_value=identity,
        ) as list_mock:
            response = client.get(f"/admin/depots/{depot_id}/identity", headers=AUTH_HDR)

        assert response.status_code == http_status.HTTP_200_OK
        assert response.json()["vehicles"][0]["vehicle_id"] == identity["vehicles"][0]["vehicle_id"]
        assert list_mock.await_args.kwargs["organization_id"] == org_id

    def test_cross_org_identity_access_denied_before_query(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))
        pool, _ = mock_db_pool

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=403, detail="denied"),
        ), patch(
            "src.api.main.db_queries.list_fleet_identity", new_callable=AsyncMock
        ) as list_mock:
            response = client.get(f"/admin/depots/{depot_id}/identity", headers=AUTH_HDR)

        assert response.status_code == http_status.HTTP_403_FORBIDDEN
        list_mock.assert_not_awaited()

    def test_create_vehicle_duplicate_id_tag_returns_409(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))
        pool, _ = mock_db_pool
        payload = {
            "externalId": "bus-1",
            "displayName": "Bus 1",
            "vehicleType": "bus_large",
            "batteryKwh": 324.0,
            "maxChargeKw": 150.0,
            "idTag": "DUPLICATE",
        }
        err = asyncpg.UniqueViolationError("duplicate")
        err.constraint_name = "vehicles_id_tag_unique_idx"

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.create_vehicle_identity",
            new_callable=AsyncMock,
            side_effect=err,
        ):
            response = client.post(f"/admin/depots/{depot_id}/vehicles", headers=AUTH_HDR, json=payload)

        assert response.status_code == http_status.HTTP_409_CONFLICT
        assert "idTag" in response.json()["detail"]

    def test_create_rfid_card_with_many_assignments(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        card_id = str(uuid4())
        vehicle_ids = [str(uuid4()), str(uuid4())]
        driver_ids = [str(uuid4()), str(uuid4())]
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))
        pool, conn = mock_db_pool
        conn.transaction = MagicMock()
        conn.transaction.return_value.__aenter__.return_value = None
        conn.transaction.return_value.__aexit__.return_value = None
        payload = {
            "idTag": "CARD-1",
            "label": "Shared depot card",
            "status": "active",
            "assignedVehicleIds": vehicle_ids,
            "assignedDriverIds": driver_ids,
        }
        card = {
            "card_id": card_id,
            "depot_id": depot_id,
            "id_tag": "CARD-1",
            "label": "Shared depot card",
            "status": "active",
            "notes": None,
            "assigned_vehicle_ids": vehicle_ids,
            "assigned_driver_ids": driver_ids,
        }

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.create_rfid_card",
            new_callable=AsyncMock,
            return_value=card,
        ) as create_mock:
            response = client.post(
                f"/admin/depots/{depot_id}/rfid-cards",
                headers=AUTH_HDR,
                json=payload,
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        assert response.json()["assigned_vehicle_ids"] == vehicle_ids
        assert response.json()["assigned_driver_ids"] == driver_ids
        assert create_mock.await_args.kwargs["assigned_vehicle_ids"] == vehicle_ids


class TestFleetIdentityQueries:
    """Pure query helper behavior with mocked asyncpg connections."""

    @pytest.mark.asyncio
    async def test_assignment_replacement_rejects_cross_depot_vehicle(self):
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=0)

        with pytest.raises(ValueError, match="assigned_vehicle_ids"):
            await db_queries.replace_rfid_card_assignments(
                conn,
                depot_id=str(uuid4()),
                card_id=str(uuid4()),
                assigned_vehicle_ids=[str(uuid4())],
                assigned_driver_ids=None,
            )

    @pytest.mark.asyncio
    async def test_known_vehicle_id_tag_resolves_vehicle_identity(self):
        conn = AsyncMock()
        vehicle_id = str(uuid4())
        depot_id = str(uuid4())
        conn.fetch = AsyncMock(
            side_effect=[
                [
                    {
                        "vehicle_id": vehicle_id,
                        "depot_id": depot_id,
                        "driver_id": None,
                        "card_id": None,
                        "source": "vehicle",
                    }
                ],
            ]
        )

        result = await db_queries.resolve_id_tag_identity(conn, "VEH-1", station_id="charger-1")

        assert result["vehicle_id"] == vehicle_id
        assert result["card_id"] is None

    @pytest.mark.asyncio
    async def test_known_driver_card_resolves_driver_card_identity(self):
        conn = AsyncMock()
        vehicle_id = str(uuid4())
        driver_id = str(uuid4())
        card_id = str(uuid4())
        conn.fetch = AsyncMock(
            side_effect=[
                [],
                [
                    {
                        "vehicle_id": vehicle_id,
                        "depot_id": str(uuid4()),
                        "driver_id": driver_id,
                        "card_id": card_id,
                        "source": "rfid_card",
                    }
                ],
            ]
        )

        result = await db_queries.resolve_id_tag_identity(conn, "CARD-1", station_id="charger-1")

        assert result["vehicle_id"] == vehicle_id
        assert result["driver_id"] == driver_id
        assert result["card_id"] == card_id

    @pytest.mark.asyncio
    async def test_unknown_card_returns_none(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(side_effect=[[], []])

        result = await db_queries.resolve_id_tag_identity(conn, "UNKNOWN", station_id="charger-1")

        assert result is None


class TestOCPPServerIdentityCallbacks:
    """New OCPP server callback behavior."""

    @pytest.mark.asyncio
    async def test_known_driver_card_authorizes_and_preserves_identity(self):
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pools = DatabasePools(static=pool, ts=pool)
        server = OCPPServer(pools=pools)
        identity = {
            "vehicle_id": str(uuid4()),
            "depot_id": str(uuid4()),
            "driver_id": str(uuid4()),
            "card_id": str(uuid4()),
            "source": "rfid_card",
        }

        with patch(
            "src.adapters.ocpp.server.db_queries.resolve_id_tag_identity",
            new_callable=AsyncMock,
            return_value=identity,
        ):
            status = await server._handle_transaction_start(
                "charger-1",
                1,
                "CARD-1",
                0,
                "2026-01-01T00:00:00Z",
            )

        assert status == AuthorizationStatus.accepted
        assert server._pending_starts["charger-1"]["driver_id"] == identity["driver_id"]
        assert server._connector_identity_cache[("charger-1", 1)]["card_id"] == identity["card_id"]

    @pytest.mark.asyncio
    async def test_unknown_card_is_rejected(self):
        pool = MagicMock()
        conn = AsyncMock()
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pools = DatabasePools(static=pool, ts=pool)
        server = OCPPServer(pools=pools)

        with patch(
            "src.adapters.ocpp.server.db_queries.resolve_id_tag_identity",
            new_callable=AsyncMock,
            return_value=None,
        ):
            status = await server._handle_authorize("charger-1", "UNKNOWN")

        assert status == AuthorizationStatus.invalid
