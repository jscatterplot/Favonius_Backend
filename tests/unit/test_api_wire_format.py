"""Regression tests for the snake_case ↔ camelCase request body contract.

The frontend's API client snake-ifies outbound bodies. This file pins the
backend's behavior:

- Request bodies are accepted in either snake_case or camelCase. snake_case
  is the canonical Python field name; camelCase is supported as a Pydantic
  alias (``populate_by_name=True`` + ``alias_generator=to_camel``).
- Response bodies are emitted in snake_case across the charger and fleet
  identity surfaces.

If a future model regresses (drops the alias config or flips a field back
to camelCase only), one of these tests will fail.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi import status as http_status

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


def _vehicle_row(depot_id: str, vehicle_id: str, *, id_tag: str | None = "VEH-1") -> dict:
    now = datetime.now(timezone.utc)
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
        "created_at": now,
        "updated_at": now,
    }


def _charger_row(depot_id: str, ocpp_id: str = "acme-001") -> dict:
    return {
        "id": str(uuid4()),
        "display_name": "Bay 1",
        "depot_id": depot_id,
        "ocpp_id": ocpp_id,
    }


class TestSnakeCaseRequestBodies:
    """Snake_case wire bodies are accepted across charger + identity writes."""

    def test_create_vehicle_accepts_snake_case_body(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        vehicle_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))
        pool, _ = mock_db_pool
        snake_body = {
            "external_id": "bus-1",
            "display_name": "Bus 1",
            "vehicle_type": "bus_large",
            "battery_kwh": 324.0,
            "max_charge_kw": 150.0,
            "id_tag": "VEH-1",
            "license_plate": "B-FB-001",
        }
        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.create_vehicle_identity",
            new_callable=AsyncMock,
            return_value=_vehicle_row(depot_id, vehicle_id),
        ) as create_mock:
            response = client.post(
                f"/admin/depots/{depot_id}/vehicles", headers=AUTH_HDR, json=snake_body
            )
        assert response.status_code == http_status.HTTP_201_CREATED, response.text
        kwargs = create_mock.await_args.kwargs
        assert kwargs["external_id"] == "bus-1"
        assert kwargs["vehicle_type"] == "bus_large"
        assert kwargs["battery_kwh"] == 324.0
        assert kwargs["max_charge_kw"] == 150.0
        assert kwargs["id_tag"] == "VEH-1"
        assert kwargs["license_plate"] == "B-FB-001"

    def test_set_primary_id_tag_accepts_snake_case(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        vehicle_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))
        pool, _ = mock_db_pool
        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.set_vehicle_primary_id_tag",
            new_callable=AsyncMock,
            return_value=_vehicle_row(depot_id, vehicle_id, id_tag="NEW-TAG"),
        ) as set_mock:
            response = client.put(
                f"/admin/depots/{depot_id}/vehicles/{vehicle_id}/primary-id-tag",
                headers=AUTH_HDR,
                json={"id_tag": "NEW-TAG"},
            )
        assert response.status_code == http_status.HTTP_200_OK, response.text
        assert set_mock.await_args.kwargs["id_tag"] == "NEW-TAG"

    def test_create_rfid_card_accepts_snake_case_assignments(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        card_id = str(uuid4())
        vehicle_ids = [str(uuid4())]
        driver_ids = [str(uuid4())]
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))
        pool, _ = mock_db_pool
        card_row = {
            "card_id": card_id,
            "depot_id": depot_id,
            "id_tag": "CARD-1",
            "label": None,
            "status": "active",
            "notes": None,
            "assigned_vehicle_ids": vehicle_ids,
            "assigned_driver_ids": driver_ids,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.create_rfid_card",
            new_callable=AsyncMock,
            return_value=card_row,
        ) as create_mock:
            response = client.post(
                f"/admin/depots/{depot_id}/rfid-cards",
                headers=AUTH_HDR,
                json={
                    "id_tag": "CARD-1",
                    "status": "active",
                    "assigned_vehicle_ids": vehicle_ids,
                    "assigned_driver_ids": driver_ids,
                },
            )
        assert response.status_code == http_status.HTTP_201_CREATED, response.text
        kwargs = create_mock.await_args.kwargs
        assert kwargs["assigned_vehicle_ids"] == vehicle_ids
        assert kwargs["assigned_driver_ids"] == driver_ids

    def test_create_charger_accepts_snake_case_body(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))
        pool, _ = mock_db_pool
        snake_body = {
            "display_name": "Bay 1",
            "vendor": "ABB",
            "model": "Terra 184",
            "serial_number": "ABB-001",
            "firmware": "1.2.3",
            "rated_kw": 150.0,
            "connector_type": "CCS",
            "connector_count": 2,
            "connector_ids": [1, 2],
            "network_notes": "Static IP reserved",
        }
        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.delete_expired_charger_onboarding_idempotency",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.acquire_charger_onboarding_idempotency_lock",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.get_charger_onboarding_idempotency",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "src.api.main.db_queries.get_depot_org_slug_context",
            new_callable=AsyncMock,
            return_value={"organization_name": "Acme", "depot_name": "Berlin Depot"},
        ), patch(
            "src.api.main.db_queries.next_charger_ocpp_id",
            new_callable=AsyncMock,
            return_value="acme-berlin-depot-001",
        ), patch(
            "src.api.main.db_queries.create_charger_with_credentials",
            new_callable=AsyncMock,
            return_value=_charger_row(depot_id, "acme-berlin-depot-001"),
        ) as create_mock, patch(
            "src.api.main.db_queries.store_charger_onboarding_idempotency",
            new_callable=AsyncMock,
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/chargers",
                headers={**AUTH_HDR, "Idempotency-Key": str(uuid4())},
                json=snake_body,
            )
        assert response.status_code == http_status.HTTP_201_CREATED, response.text
        kwargs = create_mock.await_args.kwargs
        assert kwargs["display_name"] == "Bay 1"
        assert kwargs["serial_number"] == "ABB-001"
        assert kwargs["rated_kw"] == 150.0
        assert kwargs["connector_count"] == 2
        assert kwargs["connector_ids"] == [1, 2]
        assert kwargs["network_notes"] == "Static IP reserved"


class TestSnakeCaseResponseBodies:
    """Charger onboarding responses emit snake_case keys."""

    def test_charger_onboarding_response_uses_snake_case(self, client, mock_db_pool):
        depot_id = str(uuid4())
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_user(org_id))
        pool, _ = mock_db_pool
        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.delete_expired_charger_onboarding_idempotency",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.acquire_charger_onboarding_idempotency_lock",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.get_charger_onboarding_idempotency",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "src.api.main.db_queries.get_depot_org_slug_context",
            new_callable=AsyncMock,
            return_value={"organization_name": "Acme", "depot_name": "Berlin Depot"},
        ), patch(
            "src.api.main.db_queries.next_charger_ocpp_id",
            new_callable=AsyncMock,
            return_value="acme-berlin-depot-001",
        ), patch(
            "src.api.main.db_queries.create_charger_with_credentials",
            new_callable=AsyncMock,
            return_value=_charger_row(depot_id, "acme-berlin-depot-001"),
        ), patch(
            "src.api.main.db_queries.store_charger_onboarding_idempotency",
            new_callable=AsyncMock,
        ):
            response = client.post(
                f"/admin/depots/{depot_id}/chargers",
                headers={**AUTH_HDR, "Idempotency-Key": str(uuid4())},
                json={
                    "display_name": "Bay 1",
                    "rated_kw": 150.0,
                    "connector_type": "CCS",
                    "connector_count": 2,
                    "connector_ids": [1, 2],
                },
            )
        assert response.status_code == http_status.HTTP_201_CREATED, response.text
        body = response.json()
        # snake_case keys present
        assert "display_name" in body["charger"]
        assert "depot_id" in body["charger"]
        assert "ocpp_id" in body["charger"]
        assert "shown_once" in body["credentials"]
        # camelCase keys absent
        assert "displayName" not in body["charger"]
        assert "ocppId" not in body["charger"]
        assert "shownOnce" not in body["credentials"]
