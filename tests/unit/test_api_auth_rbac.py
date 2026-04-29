"""Tests for authentication, authorization, and RBAC across all endpoints.

Covers:
- 401 responses for missing/invalid JWT (all endpoints)
- 403 responses for insufficient depot access (depot-scoped endpoints)
- RBAC permission matrix for POST /commands/execute
- Audit trail written on command execution
- OpenAPI schema gating behind admin role

Reference: PRD.md#10-4-rate-limiting, PRD.md#11-2-unit-test-requirements
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import asyncpg
import pytest
from fastapi import HTTPException, status as http_status

from src.api.main import _require_depot_access, app
from src.db import queries as db_queries
from src.security.audit_log import AuditEvent
from src.security.auth import verify_depot_access, verify_token
from src.security.tenant_mirror import ensure_tenant_mirrored


# ── Helpers ─────────────────────────────────────────────────────────────────

AUTH_HDR = {"Authorization": "Bearer test-token"}
DEPOT_ID = str(uuid4())
VEHICLE_ID = str(uuid4())
DEFAULT_ORG_ID = str(uuid4())


def _valid_user(
    role: str = "customer_operator",
    organization_id: str | None = None,
    *,
    omit_organization_id: bool = False,
) -> dict:
    """Build JWT payload using Supabase ``app_metadata`` (trusted claims)."""
    meta: dict = {"favonius_role": role}
    if role != "favonius_admin" and not omit_organization_id:
        meta["organization_id"] = organization_id or DEFAULT_ORG_ID
    return {"sub": str(uuid4()), "app_metadata": meta}


def _override_token(user: dict):
    """Return a dependency override that returns the given user dict."""
    def override():
        return user
    return override


def _override_token_raises(status_code: int, detail: str = ""):
    """Return a dependency override that raises HTTPException."""
    def override():
        raise HTTPException(status_code=status_code, detail=detail)
    return override


def _bypass_depot_access(depot_id: str):
    """FastAPI dependency override: always grant depot access."""
    return depot_id


def _deny_depot_access(depot_id: str):
    """FastAPI dependency override: always deny depot access."""
    raise HTTPException(status_code=403, detail="Access denied: you do not have permission for this depot")


def _first_depot_payload(max_grid_kw: float = 1200.0) -> dict:
    """Valid first depot setup payload."""
    return {
        "depot": {
            "name": "TOKS Vilnius Depot",
            "address": {
                "line1": "Main street 1",
                "line2": "optional",
                "city": "Vilnius",
                "postal_code": "optional",
                "country": "Lithuania",
                "latitude": 54.6872,
                "longitude": 25.2797,
            },
            "timezone": "Europe/Vilnius",
            "currency": "EUR",
            "utility_id": "eso-main",
            "max_grid_kw": max_grid_kw,
            "demand_charge": {"rate_eur_per_kw": 8.5, "billing_period": "monthly"},
            "billing": {
                "account_number": "optional",
                "tariff_name": "optional",
                "meter_id": "optional",
                "billing_cycle_day": 15,
                "notes": "optional",
            },
            "building_load_source": {
                "type": "meter",
                "provider": "optional",
                "identifier": "optional",
                "interval_minutes": 15,
                "notes": "optional",
            },
            "stationary_battery": {
                "present": True,
                "name": "optional",
                "capacity_kwh": 500,
                "max_charge_kw": 250,
                "max_discharge_kw": 250,
                "min_soc_pct": 10,
                "max_soc_pct": 90,
            },
        }
    }


def _charger_payload() -> dict:
    """Valid charger onboarding payload."""
    return {
        "displayName": "ABB charger by gate 1",
        "vendor": "ABB",
        "model": "Terra 184",
        "serialNumber": "ABB-001",
        "firmware": "1.2.3",
        "ratedKw": 150.0,
        "connectorType": "CCS",
        "connectorCount": 2,
        "connectorIds": [1, 2],
        "networkNotes": "Static IP reserved",
    }


def _manual_schedule_payload(
    *,
    vehicle_id: str = VEHICLE_ID,
    required_soc: float | None = None,
) -> dict:
    """Valid manual schedule payload."""
    entry = {
        "vehicle_id": vehicle_id,
        "route_id": "route-12",
        "departure_time": "2026-04-29T08:00:00+00:00",
        "return_time": "2026-04-29T17:00:00+00:00",
        "energy_kwh": 120.5,
    }
    if required_soc is not None:
        entry["required_soc"] = required_soc
    return {"entries": [entry]}


# ── Cleanup fixture ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    """Reset FastAPI dependency overrides after each test."""
    yield
    app.dependency_overrides.clear()


# ── 401 for missing JWT ──────────────────────────────────────────────────────


class TestMissingJwtReturns401:
    """HTTPBearer returns 401 when the Authorization header is absent.

    In Starlette 1.0.0, HTTPBearer with auto_error=True raises 401 (not 403)
    when no Authorization header is present.
    """

    @pytest.fixture(autouse=True)
    def _mock_db_available(self, mock_db_pool):
        """Keep DB checks from masking missing-token 401 responses."""
        pool, _ = mock_db_pool
        with patch("src.api.main.db_pools", pool):
            yield

    @pytest.fixture(autouse=True)
    def _remove_global_auth_override(self):
        """Disable global test auth override for missing-token assertions."""
        app.dependency_overrides.pop(ensure_tenant_mirrored, None)
        yield

    def test_optimize_no_token(self, client):
        response = client.post("/optimize", json={"depot_id": DEPOT_ID, "horizon_hours": 24})
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_depot_state_no_token(self, client):
        response = client.get(f"/depots/{DEPOT_ID}/state")
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_depot_schedule_no_token(self, client):
        response = client.get(f"/depots/{DEPOT_ID}/schedule")
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_depot_alerts_no_token(self, client):
        response = client.get(f"/depots/{DEPOT_ID}/alerts")
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_send_handoff_no_token(self, client):
        response = client.post(
            f"/depots/{DEPOT_ID}/vehicles/{VEHICLE_ID}/handoff",
            json={
                "dest_depot_id": str(uuid4()),
                "expected_soc": 0.5,
                "arrival_time": (datetime.utcnow() + timedelta(hours=2)).isoformat(),
                "battery_kwh": 100.0,
                "max_charge_kw": 80.0,
            },
        )
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_me_depots_no_token(self, client):
        response = client.get("/me/depots")
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_depot_metadata_no_token(self, client):
        response = client.get(f"/depots/{DEPOT_ID}")
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_commands_execute_no_token(self, client):
        response = client.post(
            "/commands/execute",
            json={"command": "optimization.run", "depot_id": DEPOT_ID},
        )
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_admin_controllers_no_token(self, client):
        response = client.get("/admin/controllers")
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_openapi_no_token(self, client):
        response = client.get("/openapi.json")
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED


# ── 401 for invalid JWT ──────────────────────────────────────────────────────


class TestInvalidJwtReturns401:
    """Expired or malformed JWT raises 401.

    Uses app.dependency_overrides to inject the exception, because FastAPI
    captures the ``ensure_tenant_mirrored`` function object in Depends() at
    module load time — module-level @patch cannot reach it.
    """

    def test_optimize_expired_token(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token_raises(401, "Token expired")
        response = client.post(
            "/optimize",
            json={"depot_id": DEPOT_ID, "horizon_hours": 24},
            headers=AUTH_HDR,
        )
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_depot_state_invalid_token(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token_raises(401, "Invalid token")
        response = client.get(f"/depots/{DEPOT_ID}/state", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_me_depots_invalid_token(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token_raises(401)
        response = client.get("/me/depots", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_commands_execute_invalid_token(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token_raises(401)
        response = client.post(
            "/commands/execute",
            json={"command": "optimization.run", "depot_id": DEPOT_ID},
            headers=AUTH_HDR,
        )
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_openapi_invalid_token(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token_raises(401)
        response = client.get("/openapi.json", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED


# ── 403 for insufficient depot access ────────────────────────────────────────


class TestInsufficientDepotAccessReturns403:
    """Valid JWT but user lacks access to the requested depot → 403.

    Overrides _require_depot_access (the Depends factory) to raise 403,
    simulating denied depot access after tenancy checks.
    """

    def test_depot_state_no_access(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token({"sub": str(uuid4()), "app_metadata": {}})
        app.dependency_overrides[_require_depot_access] = _deny_depot_access
        response = client.get(f"/depots/{DEPOT_ID}/state", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_depot_schedule_no_access(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token({"sub": str(uuid4()), "app_metadata": {}})
        app.dependency_overrides[_require_depot_access] = _deny_depot_access
        response = client.get(f"/depots/{DEPOT_ID}/schedule", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_depot_alerts_no_access(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token({"sub": str(uuid4()), "app_metadata": {}})
        app.dependency_overrides[_require_depot_access] = _deny_depot_access
        response = client.get(f"/depots/{DEPOT_ID}/alerts", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_depot_metadata_no_access(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token({"sub": str(uuid4()), "app_metadata": {}})
        app.dependency_overrides[_require_depot_access] = _deny_depot_access
        response = client.get(f"/depots/{DEPOT_ID}", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN


# ── OpenAPI gating ───────────────────────────────────────────────────────────


class TestOpenApiGating:
    """GET /openapi.json requires favonius_admin role."""

    def test_customer_operator_gets_403(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="customer_operator"))
        response = client.get("/openapi.json", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_viewer_gets_403(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="viewer"))
        response = client.get("/openapi.json", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_favonius_admin_gets_200(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="favonius_admin"))
        response = client.get("/openapi.json", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_200_OK
        data = response.json()
        assert "openapi" in data
        assert "paths" in data


# ── GET /me/depots ───────────────────────────────────────────────────────────


class TestMyDepots:
    """GET /me/depots returns depots for the caller organization (or all for platform admin)."""

    def test_returns_depots_for_organization(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        org_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_operator", organization_id=org_id)
        )
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "depot_id": DEPOT_ID,
                    "organization_id": org_id,
                    "name": "Test Depot",
                    "timezone": "Europe/Vilnius",
                    "currency": "EUR",
                    "max_grid_kw": 800.0,
                }
            ]
        )
        with patch("src.api.main.db_pools", pool):
            response = client.get("/me/depots", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_200_OK
        data = response.json()
        assert "depots" in data
        assert len(data["depots"]) == 1
        assert data["depots"][0]["depot_id"] == DEPOT_ID
        assert data["depots"][0]["organization_id"] == org_id

    def test_returns_empty_when_no_organization_id(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_operator", omit_organization_id=True)
        )
        conn.fetch = AsyncMock(return_value=[])
        with patch("src.api.main.db_pools", pool):
            response = client.get("/me/depots", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_200_OK
        assert response.json() == {"depots": []}

    def test_favonius_admin_gets_all_depots(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="favonius_admin"))
        conn.fetch = AsyncMock(return_value=[])
        with patch("src.api.main.db_pools", pool):
            response = client.get("/me/depots", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_200_OK


class TestTenantMirrorOnAuthenticatedRequest:
    """JIT mirror runs on real auth path (verify_token override, not ensure override)."""

    def test_authenticated_endpoint_triggers_mirror_once(self, client, mock_db_pool):
        app.dependency_overrides.pop(ensure_tenant_mirrored, None)
        app.dependency_overrides[verify_token] = _override_token(
            _valid_user(role="customer_operator", organization_id=DEFAULT_ORG_ID)
        )
        pool, conn = mock_db_pool
        conn.fetch = AsyncMock(return_value=[])
        with patch("src.security.tenant_mirror.mirror_user_tenant", new_callable=AsyncMock) as mock_mirror:
            with patch("src.api.main.db_pools", pool):
                client.get("/me/depots", headers=AUTH_HDR)
        mock_mirror.assert_awaited_once()

    def test_cross_org_denial_unaffected_by_mirror_failure(self, client, mock_db_pool):
        """403 from verify_depot_access must not depend on tenant mirror succeeding."""
        app.dependency_overrides.pop(ensure_tenant_mirrored, None)
        app.dependency_overrides[verify_token] = _override_token(_valid_user(role="customer_operator"))
        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=False)
        with patch(
            "src.security.tenant_mirror.mirror_user_tenant",
            new_callable=AsyncMock,
            side_effect=RuntimeError("mirror boom"),
        ):
            with patch("src.api.main.db_pools", pool):
                response = client.get(f"/depots/{DEPOT_ID}", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN


class TestDepotSetupWrites:
    """POST/PATCH depot setup auth, validation, and readiness behavior."""

    def test_authorized_creation_with_tenant_mirror(self, client, mock_db_pool):
        app.dependency_overrides.pop(ensure_tenant_mirrored, None)
        app.dependency_overrides[verify_token] = _override_token(
            _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        )
        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=True)
        payload = _first_depot_payload()

        created_row = {
            "depot_id": DEPOT_ID,
            "organization_id": DEFAULT_ORG_ID,
            "name": payload["depot"]["name"],
            "timezone": payload["depot"]["timezone"],
            "currency": payload["depot"]["currency"],
            "max_grid_kw": payload["depot"]["max_grid_kw"],
        }
        readiness = [{"id": "tariff", "label": "Tariff configured", "status": "ready", "detail": "ok"}]

        with patch("src.api.main.db_pools", pool), patch(
            "src.security.tenant_mirror.mirror_user_tenant", new_callable=AsyncMock
        ) as mirror_mock, patch(
            "src.api.main.db_queries.create_depot_setup", new_callable=AsyncMock, return_value=created_row
        ) as create_mock, patch(
            "src.api.main.db_queries.upsert_battery_storage", new_callable=AsyncMock
        ), patch(
            "src.api.main._build_readiness_checklist", new_callable=AsyncMock, return_value=readiness
        ):
            response = client.post("/admin/first-depot-setup", headers=AUTH_HDR, json=payload)

        assert response.status_code == http_status.HTTP_200_OK
        assert response.json()["depot"]["id"] == DEPOT_ID
        assert response.json()["readiness_checklist"] == readiness
        mirror_mock.assert_awaited_once()
        assert create_mock.await_args.kwargs["organization_id"] == DEFAULT_ORG_ID

    def test_cross_org_update_denied(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="customer_admin"))
        conn.fetchval = AsyncMock(return_value=False)

        with patch("src.api.main.db_pools", pool):
            response = client.patch(
                f"/admin/depots/{DEPOT_ID}",
                headers=AUTH_HDR,
                json=_first_depot_payload(),
            )

        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_missing_organization_id_denied(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin", omit_organization_id=True)
        )
        response = client.post("/admin/first-depot-setup", headers=AUTH_HDR, json=_first_depot_payload())
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_invalid_depot_data_returns_structured_validation(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="customer_admin"))
        payload = _first_depot_payload(max_grid_kw=-1.0)
        response = client.post("/admin/first-depot-setup", headers=AUTH_HDR, json=payload)
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST
        data = response.json()
        assert data["error_code"] == "VALIDATION_ERROR"
        assert "field_errors" in data
        assert "validation_errors" in data

    def test_non_static_building_source_rejects_assumption_kw(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="customer_admin"))
        payload = _first_depot_payload()
        payload["depot"]["building_load_source"]["assumption_kw"] = 50.0
        response = client.post("/admin/first-depot-setup", headers=AUTH_HDR, json=payload)
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    def test_favonius_admin_write_forbidden_visibility_unchanged(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="favonius_admin"))
        response = client.post("/admin/first-depot-setup", headers=AUTH_HDR, json=_first_depot_payload())
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_readiness_checklist_contains_exact_missing_inputs(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="customer_admin"))
        # Order matches _build_depot_readiness_checklist:
        # has_vehicles, has_chargers, access_default, has_access,
        # has_schedules, has_battery, has_prices, has_building_load.
        conn.fetchval = AsyncMock(
            side_effect=[False, False, "explicit_matrix", False, False, False, False, False]
        )
        payload = _first_depot_payload()
        created_row = {
            "depot_id": DEPOT_ID,
            "organization_id": DEFAULT_ORG_ID,
            "name": payload["depot"]["name"],
            "timezone": payload["depot"]["timezone"],
            "currency": payload["depot"]["currency"],
            "max_grid_kw": payload["depot"]["max_grid_kw"],
        }

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.db_queries.create_depot_setup", new_callable=AsyncMock, return_value=created_row
        ), patch(
            "src.api.main.db_queries.upsert_battery_storage", new_callable=AsyncMock
        ):
            response = client.post("/admin/first-depot-setup", headers=AUTH_HDR, json=payload)

        assert response.status_code == http_status.HTTP_200_OK
        checklist = {item["id"]: item for item in response.json()["readiness_checklist"]}
        assert checklist["vehicles"]["status"] == "blocked"
        assert checklist["chargers"]["status"] == "blocked"
        assert checklist["charger_access"]["status"] == "blocked"
        assert checklist["schedules"]["status"] == "blocked"
        assert checklist["prices"]["status"] == "blocked"
        assert checklist["building_load"]["status"] == "blocked"
        assert checklist["battery"]["status"] == "blocked"

    # ── Migration 021: charger_vehicle_access_default + energy_cap tariff ──

    def test_first_depot_setup_accepts_all_to_all_mode(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin")
        )
        payload = _first_depot_payload()
        payload["depot"]["charger_vehicle_access_default"] = "all_to_all"
        created_row = {
            "depot_id": DEPOT_ID,
            "organization_id": DEFAULT_ORG_ID,
            "name": payload["depot"]["name"],
            "timezone": payload["depot"]["timezone"],
            "currency": payload["depot"]["currency"],
            "max_grid_kw": payload["depot"]["max_grid_kw"],
        }
        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.db_queries.create_depot_setup",
            new_callable=AsyncMock,
            return_value=created_row,
        ) as create_mock, patch(
            "src.api.main.db_queries.upsert_battery_storage", new_callable=AsyncMock
        ), patch(
            "src.api.main._build_readiness_checklist",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = client.post(
                "/admin/first-depot-setup", headers=AUTH_HDR, json=payload
            )
        assert response.status_code == http_status.HTTP_200_OK
        assert (
            create_mock.await_args.kwargs["charger_vehicle_access_default"] == "all_to_all"
        )
        assert create_mock.await_args.kwargs["tariff_type"] == "simple_demand"

    def test_first_depot_setup_accepts_energy_cap_tariff(self, client, mock_db_pool):
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin")
        )
        payload = _first_depot_payload()
        payload["depot"]["demand_charge"] = {
            "tariff_type": "energy_cap",
            "energy_cap_kwh": 400.0,
            "under_cap_rate_per_kwh": 0.10,
            "over_cap_penalty_per_kwh": 2.00,
            "cap_billing_period": "monthly",
        }
        created_row = {
            "depot_id": DEPOT_ID,
            "organization_id": DEFAULT_ORG_ID,
            "name": payload["depot"]["name"],
            "timezone": payload["depot"]["timezone"],
            "currency": payload["depot"]["currency"],
            "max_grid_kw": payload["depot"]["max_grid_kw"],
        }
        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.db_queries.create_depot_setup",
            new_callable=AsyncMock,
            return_value=created_row,
        ) as create_mock, patch(
            "src.api.main.db_queries.upsert_battery_storage", new_callable=AsyncMock
        ), patch(
            "src.api.main._build_readiness_checklist",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = client.post(
                "/admin/first-depot-setup", headers=AUTH_HDR, json=payload
            )
        assert response.status_code == http_status.HTTP_200_OK
        kwargs = create_mock.await_args.kwargs
        assert kwargs["tariff_type"] == "energy_cap"
        assert kwargs["energy_cap_kwh"] == 400.0
        assert kwargs["under_cap_rate_per_kwh"] == 0.10
        assert kwargs["over_cap_penalty_per_kwh"] == 2.00

    def test_energy_cap_rejects_rate_eur_per_kw(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin")
        )
        payload = _first_depot_payload()
        # rate_eur_per_kw is forbidden by EnergyCapTariffPayload (extra='forbid').
        payload["depot"]["demand_charge"] = {
            "tariff_type": "energy_cap",
            "rate_eur_per_kw": 8.5,
            "energy_cap_kwh": 400.0,
            "under_cap_rate_per_kwh": 0.10,
            "over_cap_penalty_per_kwh": 2.00,
        }
        response = client.post(
            "/admin/first-depot-setup", headers=AUTH_HDR, json=payload
        )
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    def test_energy_cap_rejects_penalty_below_under_rate(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin")
        )
        payload = _first_depot_payload()
        payload["depot"]["demand_charge"] = {
            "tariff_type": "energy_cap",
            "energy_cap_kwh": 400.0,
            "under_cap_rate_per_kwh": 1.00,
            "over_cap_penalty_per_kwh": 0.50,  # ← below under_cap_rate
        }
        response = client.post(
            "/admin/first-depot-setup", headers=AUTH_HDR, json=payload
        )
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST
        assert response.json()["error_code"] == "VALIDATION_ERROR"

    def test_patch_depot_setup_invalidates_config_cache(self, client, mock_db_pool):
        from src.api import main as api_main

        pool, conn = mock_db_pool
        # Pre-populate the cache so we can verify eviction.
        api_main._depot_config_cache[DEPOT_ID] = (object(), 0.0)
        assert DEPOT_ID in api_main._depot_config_cache

        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin")
        )
        # verify_depot_access reads fetchval → True for org match.
        conn.fetchval = AsyncMock(return_value=True)
        updated_row = {
            "depot_id": DEPOT_ID,
            "name": "Renamed",
            "timezone": "Europe/Vilnius",
            "currency": "EUR",
            "max_grid_kw": 1500.0,
        }
        payload = _first_depot_payload()
        payload["depot"]["charger_vehicle_access_default"] = "all_to_all"
        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.db_queries.update_depot_setup",
            new_callable=AsyncMock,
            return_value=updated_row,
        ), patch(
            "src.api.main.db_queries.upsert_battery_storage", new_callable=AsyncMock
        ), patch(
            "src.api.main._build_readiness_checklist",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = client.patch(
                f"/admin/depots/{DEPOT_ID}", headers=AUTH_HDR, json=payload
            )
        assert response.status_code == http_status.HTTP_200_OK
        # Cache must be invalidated so the next solve picks up the new mode.
        assert DEPOT_ID not in api_main._depot_config_cache


class TestChargerVehicleAccessEndpoint:
    """POST /admin/depots/{id}/charger-vehicle-access (migration 021)."""

    def _payload(self, *, accessible: bool = True) -> dict:
        return {
            "entries": [
                {
                    "charger_id": str(uuid4()),
                    "vehicle_id": str(uuid4()),
                    "is_accessible": accessible,
                }
            ]
        }

    def test_rejects_all_to_all_mode_with_409(self, client, mock_db_pool):
        from src.api import main as api_main

        pool, conn = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin")
        )
        # First fetchval = verify_depot_access (org match) = True
        # Second fetchval = charger_vehicle_access_default = 'all_to_all'
        conn.fetchval = AsyncMock(side_effect=[True, "all_to_all"])
        with patch("src.api.main.db_pools", pool):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/charger-vehicle-access",
                headers=AUTH_HDR,
                json=self._payload(),
            )
        assert response.status_code == http_status.HTTP_409_CONFLICT
        assert "all_to_all" in response.json()["detail"]
        # Cache must NOT be evicted on a rejected request.
        assert DEPOT_ID not in api_main._depot_config_cache

    def test_explicit_matrix_upserts_and_invalidates_cache(self, client, mock_db_pool):
        from src.api import main as api_main

        pool, conn = mock_db_pool
        api_main._depot_config_cache[DEPOT_ID] = (object(), 0.0)
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin")
        )
        conn.fetchval = AsyncMock(side_effect=[True, "explicit_matrix"])
        depot_row = {
            "name": "TOKS Vilnius Depot",
            "timezone": "Europe/Vilnius",
            "currency": "EUR",
            "max_grid_kw": 1200.0,
        }
        conn.fetchrow = AsyncMock(return_value=depot_row)
        upsert_result = {"invalid_chargers": [], "invalid_vehicles": []}
        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.db_queries.upsert_charger_vehicle_access",
            new_callable=AsyncMock,
            return_value=upsert_result,
        ) as upsert_mock, patch(
            "src.api.main._build_depot_readiness_checklist",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/charger-vehicle-access",
                headers=AUTH_HDR,
                json=self._payload(),
            )
        assert response.status_code == http_status.HTTP_200_OK
        assert response.json()["depot"]["id"] == DEPOT_ID
        upsert_mock.assert_awaited_once()
        # Cache evicted so the next solve sees the new rows.
        assert DEPOT_ID not in api_main._depot_config_cache

    def test_invalid_membership_returns_400(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin")
        )
        conn.fetchval = AsyncMock(side_effect=[True, "explicit_matrix"])
        bad_charger = str(uuid4())
        bad_vehicle = str(uuid4())
        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.db_queries.upsert_charger_vehicle_access",
            new_callable=AsyncMock,
            return_value={
                "invalid_chargers": [bad_charger],
                "invalid_vehicles": [bad_vehicle],
            },
        ):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/charger-vehicle-access",
                headers=AUTH_HDR,
                json=self._payload(),
            )
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST
        body = response.json()
        assert body["error_code"] == "INVALID_DEPOT_MEMBERSHIP"
        assert bad_charger in body["invalid_chargers"]
        assert bad_vehicle in body["invalid_vehicles"]

    def test_cross_org_request_denied(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin")
        )
        # verify_depot_access fetchval returns False -> 403
        conn.fetchval = AsyncMock(return_value=False)
        with patch("src.api.main.db_pools", pool):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/charger-vehicle-access",
                headers=AUTH_HDR,
                json=self._payload(),
            )
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_invalid_uuid_in_entry_returns_400(self, client, mock_db_pool):
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin")
        )
        with patch("src.api.main.db_pools", pool):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/charger-vehicle-access",
                headers=AUTH_HDR,
                json={
                    "entries": [
                        {
                            "charger_id": "not-a-uuid",
                            "vehicle_id": str(uuid4()),
                            "is_accessible": True,
                        }
                    ]
                },
            )
        # Project-wide Pydantic ValidationError handler converts to 400.
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST


class TestChargerOnboarding:
    """POST /admin/depots/{depot_id}/chargers provisioning behavior."""

    def test_creates_charger_and_one_time_basic_auth_credentials(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        conn.transaction = MagicMock()
        conn.transaction.return_value.__aenter__.return_value = None
        conn.transaction.return_value.__aexit__.return_value = None
        user = _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(user)
        context = {
            "depot_id": DEPOT_ID,
            "depot_name": "Berlin Depot",
            "organization_id": DEFAULT_ORG_ID,
            "organization_name": "Acme Transit",
        }
        charger = {
            "id": str(uuid4()),
            "depot_id": DEPOT_ID,
            "ocpp_id": "acme-transit-berlin-depot-001",
            "display_name": "ABB charger by gate 1",
        }

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ) as access_mock, patch(
            "src.api.main.db_queries.delete_expired_charger_onboarding_idempotency",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.get_charger_onboarding_idempotency",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "src.api.main.db_queries.get_depot_org_slug_context",
            new_callable=AsyncMock,
            return_value=context,
        ), patch(
            "src.api.main.db_queries.next_charger_ocpp_id",
            new_callable=AsyncMock,
            return_value="acme-transit-berlin-depot-001",
        ), patch(
            "src.api.main.db_queries.create_charger_with_credentials",
            new_callable=AsyncMock,
            return_value=charger,
        ) as create_mock, patch(
            "src.api.main.db_queries.store_charger_onboarding_idempotency",
            new_callable=AsyncMock,
        ) as idem_store:
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/chargers",
                headers={**AUTH_HDR, "Idempotency-Key": str(uuid4())},
                json=_charger_payload(),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        data = response.json()
        assert data["charger"]["ocppId"] == "acme-transit-berlin-depot-001"
        assert data["credentials"]["username"] == "acme-transit-berlin-depot-001"
        assert data["credentials"]["password"]
        assert data["credentials"]["shownOnce"] is True
        assert create_mock.await_args.kwargs["connector_ids"] == [1, 2]
        access_mock.assert_awaited_once()
        password_hash = create_mock.await_args.kwargs["password_hash"]
        assert data["credentials"]["password"] not in password_hash
        assert password_hash.startswith("$2")
        replay_payload = idem_store.await_args.kwargs["response_json"]
        assert replay_payload["credentials"]["password"] == data["credentials"]["password"]
        assert idem_store.await_args.kwargs["ttl_minutes"] == 30

    def test_null_sub_claim_is_rejected_before_idempotency_write(self, client):
        user = _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        user["sub"] = None
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(user)

        response = client.post(
            f"/admin/depots/{DEPOT_ID}/chargers",
            headers={**AUTH_HDR, "Idempotency-Key": str(uuid4())},
            json=_charger_payload(),
        )

        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED
        assert response.json()["detail"] == "Token missing 'sub' claim"

    @pytest.mark.asyncio
    async def test_idempotency_ttl_uses_integer_interval_parameter(self):
        db = AsyncMock()

        await db_queries.store_charger_onboarding_idempotency(
            db,
            organization_id=DEFAULT_ORG_ID,
            user_id=str(uuid4()),
            endpoint=f"POST /admin/depots/{DEPOT_ID}/chargers",
            idempotency_key="retry-key",
            request_hash="hash",
            response_json={"ok": True},
            status_code=http_status.HTTP_201_CREATED,
            ttl_minutes=30,
        )

        query = db.execute.await_args.args[0]
        assert "$8::int * INTERVAL '1 minute'" in query
        assert "::text || ' minutes'" not in query
        assert db.execute.await_args.args[-1] == 30

    def test_idempotency_replays_same_response(self, client, mock_db_pool):
        pool, _ = mock_db_pool
        user = _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(user)
        payload = _charger_payload()
        replay = {
            "charger": {
                "id": str(uuid4()),
                "displayName": payload["displayName"],
                "depotId": DEPOT_ID,
                "ocppId": "acme-berlin-001",
            },
            "credentials": {
                "username": "acme-berlin-001",
                "password": "plaintext-replay",
                "scheme": "basic",
                "shownOnce": True,
            },
        }

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.delete_expired_charger_onboarding_idempotency",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.get_charger_onboarding_idempotency",
            new_callable=AsyncMock,
            return_value={
                "request_hash": "unused",
                "response_json": replay,
                "status_code": http_status.HTTP_201_CREATED,
            },
        ), patch("src.api.main._canonical_request_hash", return_value="unused"):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/chargers",
                headers={**AUTH_HDR, "Idempotency-Key": "retry-key"},
                json=payload,
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        assert response.json() == replay

    def test_cross_org_creation_denied(self, client, mock_db_pool):
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        )

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=403, detail="Access denied"),
        ):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/chargers",
                headers={**AUTH_HDR, "Idempotency-Key": str(uuid4())},
                json=_charger_payload(),
            )

        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_duplicate_generated_ocpp_id_returns_conflict(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        conn.transaction = MagicMock()
        conn.transaction.return_value.__aenter__.return_value = None
        conn.transaction.return_value.__aexit__.return_value = None
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        )
        context = {
            "depot_id": DEPOT_ID,
            "depot_name": "Berlin Depot",
            "organization_id": DEFAULT_ORG_ID,
            "organization_name": "Acme Transit",
        }

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.delete_expired_charger_onboarding_idempotency",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.get_charger_onboarding_idempotency",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "src.api.main.db_queries.get_depot_org_slug_context",
            new_callable=AsyncMock,
            return_value=context,
        ), patch(
            "src.api.main.db_queries.next_charger_ocpp_id",
            new_callable=AsyncMock,
            return_value="acme-transit-berlin-depot-001",
        ), patch(
            "src.api.main.db_queries.create_charger_with_credentials",
            new_callable=AsyncMock,
            side_effect=asyncpg.UniqueViolationError("duplicate key"),
        ):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/chargers",
                headers={**AUTH_HDR, "Idempotency-Key": str(uuid4())},
                json=_charger_payload(),
            )

        assert response.status_code == http_status.HTTP_409_CONFLICT

    def test_idempotency_key_with_different_body_returns_conflict(self, client, mock_db_pool):
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        )

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.delete_expired_charger_onboarding_idempotency",
            new_callable=AsyncMock,
        ), patch(
            "src.api.main.db_queries.get_charger_onboarding_idempotency",
            new_callable=AsyncMock,
            return_value={
                "request_hash": "old-hash",
                "response_json": {},
                "status_code": http_status.HTTP_201_CREATED,
            },
        ), patch("src.api.main._canonical_request_hash", return_value="new-hash"):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/chargers",
                headers={**AUTH_HDR, "Idempotency-Key": "retry-key"},
                json=_charger_payload(),
            )

        assert response.status_code == http_status.HTTP_409_CONFLICT

    def test_plaintext_not_retrievable_from_charger_metadata(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        )
        conn.fetchrow = AsyncMock(
            return_value={
                "depot_id": DEPOT_ID,
                "organization_id": DEFAULT_ORG_ID,
                "name": "Berlin Depot",
                "timezone": "Europe/Berlin",
                "currency": "EUR",
                "max_grid_kw": 800.0,
            }
        )

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.get(f"/depots/{DEPOT_ID}", headers=AUTH_HDR)

        assert response.status_code == http_status.HTTP_200_OK
        serialized = response.text.lower()
        assert "password" not in serialized
        assert "credential" not in serialized


class TestManualScheduleAdmin:
    """Admin manual schedule setup endpoints."""

    def test_creates_manual_schedule_and_defaults_required_soc(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        schedule_id = str(uuid4())
        created_row = {
            "schedule_id": schedule_id,
            "vehicle_id": VEHICLE_ID,
            "route_id": "route-12",
            "departure_time": datetime.fromisoformat("2026-04-29T08:00:00+00:00"),
            "return_time": datetime.fromisoformat("2026-04-29T17:00:00+00:00"),
            "required_soc": 1.0,
            "energy_kwh": 120.5,
        }
        conn.transaction = MagicMock()
        conn.transaction.return_value.__aenter__.return_value = None
        conn.transaction.return_value.__aexit__.return_value = None

        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        )
        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.get_vehicle_ids_for_depot",
            new_callable=AsyncMock,
            return_value={VEHICLE_ID},
        ), patch(
            "src.api.main.db_queries.create_manual_schedule",
            new_callable=AsyncMock,
            return_value=created_row,
        ) as create_mock, patch(
            "src.api.main._build_depot_readiness_checklist",
            new_callable=AsyncMock,
            return_value=[
                {
                    "id": "schedules",
                    "label": "Schedules available",
                    "status": "ready",
                    "detail": "Upcoming schedules found",
                }
            ],
        ):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/schedule/manual",
                headers=AUTH_HDR,
                json=_manual_schedule_payload(),
            )

        assert response.status_code == http_status.HTTP_201_CREATED
        data = response.json()
        assert data["created"][0]["schedule_id"] == schedule_id
        assert data["created"][0]["required_soc"] == 1.0
        assert data["readiness"]["ready"] is True
        assert create_mock.await_args.kwargs["required_soc"] == 1.0

    def test_invalid_vehicle_depot_mismatch_rejected(self, client, mock_db_pool):
        pool, _ = mock_db_pool
        other_vehicle_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        )

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.get_vehicle_ids_for_depot",
            new_callable=AsyncMock,
            return_value=set(),
        ), patch(
            "src.api.main.db_queries.create_manual_schedule",
            new_callable=AsyncMock,
        ) as create_mock:
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/schedule/manual",
                headers=AUTH_HDR,
                json=_manual_schedule_payload(vehicle_id=other_vehicle_id),
            )

        assert response.status_code == http_status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.json()["detail"]["error_code"] == "VEHICLE_DEPOT_MISMATCH"
        create_mock.assert_not_awaited()

    def test_cross_org_schedule_creation_denied(self, client, mock_db_pool):
        pool, _ = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        )

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=403, detail="Access denied"),
        ):
            response = client.post(
                f"/admin/depots/{DEPOT_ID}/schedule/manual",
                headers=AUTH_HDR,
                json=_manual_schedule_payload(),
            )

        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_readiness_integration_reports_schedule_gap_then_ready(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        )
        # Order per call (migration 021 inserts charger_vehicle_access_default
        # between has_chargers and has_access):
        #   has_vehicles, has_chargers, access_default, has_access,
        #   has_schedules, has_battery, has_prices, has_building_load.
        # First call simulates a "schedule gap" (has_schedules=False); second
        # call simulates a fully ready depot.
        conn.fetchval = AsyncMock(
            side_effect=[
                True,                # has_vehicles (call 1)
                True,                # has_chargers
                "explicit_matrix",   # access_default
                True,                # has_access
                False,               # has_schedules ← the "gap"
                True,                # has_battery
                True,                # has_prices
                True,                # has_building_load
                True,                # has_vehicles (call 2)
                True,                # has_chargers
                "explicit_matrix",   # access_default
                True,                # has_access
                True,                # has_schedules (now ready)
                True,                # has_battery
                True,                # has_prices
                True,                # has_building_load
            ]
        )

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            blocked = client.get(
                f"/admin/depots/{DEPOT_ID}/schedule/readiness",
                headers=AUTH_HDR,
            )
            ready = client.get(
                f"/admin/depots/{DEPOT_ID}/schedule/readiness",
                headers=AUTH_HDR,
            )

        assert blocked.status_code == http_status.HTTP_200_OK
        blocked_checks = {item["id"]: item for item in blocked.json()["checks"]}
        assert blocked_checks["schedules"]["status"] == "blocked"
        assert ready.status_code == http_status.HTTP_200_OK
        ready_checks = {item["id"]: item for item in ready.json()["checks"]}
        assert ready_checks["schedules"]["status"] == "ready"

    def test_patch_manual_schedule_updates_scoped_row(self, client, mock_db_pool):
        pool, _ = mock_db_pool
        schedule_id = str(uuid4())
        existing = {
            "schedule_id": schedule_id,
            "vehicle_id": VEHICLE_ID,
            "route_id": "route-12",
            "departure_time": datetime.fromisoformat("2026-04-29T08:00:00+00:00"),
            "return_time": datetime.fromisoformat("2026-04-29T17:00:00+00:00"),
            "required_soc": 1.0,
            "energy_kwh": 120.5,
        }
        updated = {
            **existing,
            "required_soc": 0.99,
            "energy_kwh": 110.0,
        }
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        )

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ), patch(
            "src.api.main.db_queries.get_schedule_for_depot",
            new_callable=AsyncMock,
            return_value=existing,
        ), patch(
            "src.api.main.db_queries.get_vehicle_ids_for_depot",
            new_callable=AsyncMock,
            return_value={VEHICLE_ID},
        ), patch(
            "src.api.main.db_queries.update_manual_schedule",
            new_callable=AsyncMock,
            return_value=updated,
        ) as update_mock, patch(
            "src.api.main._build_depot_readiness_checklist",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = client.patch(
                f"/admin/depots/{DEPOT_ID}/schedule/manual/{schedule_id}",
                headers=AUTH_HDR,
                json={"required_soc": 0.99, "energy_kwh": 110.0},
            )

        assert response.status_code == http_status.HTTP_200_OK
        payload = response.json()
        assert payload["updated"]["required_soc"] == 0.99
        assert payload["readiness"]["depot_id"] == DEPOT_ID
        assert update_mock.await_args.kwargs["required_soc"] == 0.99

    @pytest.mark.parametrize(
        "field_name",
        ["vehicle_id", "route_id", "departure_time", "return_time", "required_soc"],
    )
    def test_patch_manual_schedule_rejects_null_non_nullable_fields(
        self,
        client,
        mock_db_pool,
        field_name: str,
    ):
        pool, _ = mock_db_pool
        schedule_id = str(uuid4())
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
            _valid_user(role="customer_admin", organization_id=DEFAULT_ORG_ID)
        )

        with patch("src.api.main.db_pools", pool), patch(
            "src.api.main.verify_depot_access", new_callable=AsyncMock
        ):
            response = client.patch(
                f"/admin/depots/{DEPOT_ID}/schedule/manual/{schedule_id}",
                headers=AUTH_HDR,
                json={field_name: None},
            )

        assert response.status_code == http_status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "cannot be null" in response.json()["detail"]


class TestAdminControllersRbac:
    """GET /admin/controllers is restricted to favonius_admin."""

    def test_customer_operator_forbidden(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="customer_operator"))
        response = client.get("/admin/controllers", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_favonius_admin_ok(self, client):
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="favonius_admin"))
        response = client.get("/admin/controllers", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_200_OK


# ── POST /commands/execute — RBAC matrix ────────────────────────────────────


class TestCommandRbac:
    """RBAC permission matrix for each command type.

    Tenant access uses ``verify_depot_access`` with static DB pool (mocked).
    """

    @pytest.mark.parametrize(
        "role, command, params, expected_status",
        [
            # optimization.run — requires optimize:trigger (operator+)
            ("favonius_admin", "optimization.run", {}, 200),
            ("customer_operator", "optimization.run", {}, 200),
            ("viewer", "optimization.run", {}, 403),
            ("auditor", "optimization.run", {}, 403),
            # fleet.charger.restart — requires depot:manage (operator+)
            # 400 = charger_id missing, which means RBAC passed
            ("favonius_admin", "fleet.charger.restart", {}, 400),
            ("customer_operator", "fleet.charger.restart", {}, 400),
            ("viewer", "fleet.charger.restart", {}, 403),
            ("auditor", "fleet.charger.restart", {}, 403),
            # depot.config.update — requires admin:config (customer_admin+)
            ("favonius_admin", "depot.config.update", {"max_grid_kw": 850.0}, 200),
            ("customer_admin", "depot.config.update", {"max_grid_kw": 850.0}, 200),
            ("customer_operator", "depot.config.update", {"max_grid_kw": 850.0}, 403),
            ("viewer", "depot.config.update", {"max_grid_kw": 850.0}, 403),
        ],
    )
    @patch("src.api.main.get_audit_logger", return_value=None)
    def test_command_role_matrix(
        self,
        mock_audit,
        role,
        command,
        params,
        expected_status,
        client,
        mock_db_pool,
    ):
        user = _valid_user(role=role)
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(user)

        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=True)
        with patch("src.api.main.db_pools", pool):
            response = client.post(
                "/commands/execute",
                json={
                    "command": command,
                    "depot_id": DEPOT_ID,
                    "params": params,
                    "dry_run": True,
                },
                headers=AUTH_HDR,
            )

        assert response.status_code == expected_status, (
            f"role={role} command={command}: "
            f"expected {expected_status}, got {response.status_code}. "
            f"Body: {response.text}"
        )


# ── POST /commands/execute — audit trail ────────────────────────────────────


class TestCommandAuditTrail:
    """Every command execution (real or dry-run) writes to the audit log."""

    @patch("src.api.main.get_audit_logger")
    def test_audit_log_written_on_dry_run(self, mock_get_audit, client, mock_db_pool):
        user_id = str(uuid4())
        user = _valid_user(role="customer_operator")
        user["sub"] = user_id
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(user)

        mock_audit = AsyncMock()
        mock_get_audit.return_value = mock_audit

        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=True)
        with patch("src.api.main.db_pools", pool):
            response = client.post(
                "/commands/execute",
                json={
                    "command": "optimization.run",
                    "depot_id": DEPOT_ID,
                    "params": {"horizon_hours": 4},
                    "dry_run": True,
                },
                headers=AUTH_HDR,
            )

        assert response.status_code == http_status.HTTP_200_OK
        assert response.json()["status"] == "dry_run"

        mock_audit.log.assert_called_once()
        event: AuditEvent = mock_audit.log.call_args[0][0]
        assert event.event_type == "COMMAND_EXECUTED"
        assert event.user_id == user_id
        assert event.resource == "/commands/optimization.run"
        assert event.details["dry_run"] is True

    @patch("src.api.main.controller_manager")
    @patch("src.api.main.get_audit_logger")
    def test_audit_log_written_on_real_execution(self, mock_get_audit, mock_cm, client, mock_db_pool):
        user_id = str(uuid4())
        user = {
            "sub": user_id,
            "app_metadata": {
                "favonius_role": "customer_operator",
                "organization_id": DEFAULT_ORG_ID,
            },
        }
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(user)

        mock_controller = AsyncMock()
        mock_cm.get_or_create_controller = AsyncMock(return_value=mock_controller)

        mock_audit = AsyncMock()
        mock_get_audit.return_value = mock_audit

        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=True)
        with patch("src.api.main.db_pools", pool):
            response = client.post(
                "/commands/execute",
                json={
                    "command": "optimization.run",
                    "depot_id": DEPOT_ID,
                    "params": {"horizon_hours": 24},
                    "dry_run": False,
                },
                headers=AUTH_HDR,
            )

        assert response.status_code == http_status.HTTP_200_OK
        assert response.json()["status"] == "ok"

        mock_audit.log.assert_called_once()
        event: AuditEvent = mock_audit.log.call_args[0][0]
        assert event.event_type == "COMMAND_EXECUTED"
        assert event.user_id == user_id
        assert event.details["dry_run"] is False

    @patch("src.api.main.get_audit_logger", return_value=None)
    def test_no_error_when_audit_logger_is_none(self, mock_get_audit, client, mock_db_pool):
        """Command executes successfully even if audit logger is not initialized."""
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="customer_operator"))
        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=True)
        with patch("src.api.main.db_pools", pool):
            response = client.post(
                "/commands/execute",
                json={
                    "command": "optimization.run",
                    "depot_id": DEPOT_ID,
                    "dry_run": True,
                },
                headers=AUTH_HDR,
            )
        assert response.status_code == http_status.HTTP_200_OK


# ── GET /depots/{id} metadata ─────────────────────────────────────────────────


class TestDepotMetadata:
    """GET /depots/{depot_id} — metadata endpoint."""

    def test_returns_depot_fields(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user())
        app.dependency_overrides[_require_depot_access] = _bypass_depot_access
        conn.fetchrow = AsyncMock(
            return_value={
                "depot_id": DEPOT_ID,
                "organization_id": DEFAULT_ORG_ID,
                "name": "TOKS Vilnius",
                "timezone": "Europe/Vilnius",
                "currency": "EUR",
                "max_grid_kw": 800.0,
                "demand_charge_rate_kw": 12.5,
            }
        )
        with patch("src.api.main.db_pools", pool):
            response = client.get(f"/depots/{DEPOT_ID}", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_200_OK
        data = response.json()
        assert data["currency"] == "EUR"
        assert data["timezone"] == "Europe/Vilnius"
        assert data["max_grid_kw"] == 800.0

    def test_returns_404_when_not_found(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user())
        app.dependency_overrides[_require_depot_access] = _bypass_depot_access
        conn.fetchrow = AsyncMock(return_value=None)
        with patch("src.api.main.db_pools", pool):
            response = client.get(f"/depots/{DEPOT_ID}", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_404_NOT_FOUND

    def test_invalid_uuid_returns_400(self, client):
        # No auth header → 401 before UUID validation even runs
        response = client.get("/depots/not-a-uuid", headers=AUTH_HDR)
        # Without auth override: JWT fails → 500 (no JWT_SECRET_KEY in test env)
        # With auth override: UUID validation fires → 400
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user())
        response = client.get("/depots/not-a-uuid", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST


# ── Cross-organization access (tenant isolation) ────────────────────────────


class TestCrossOrganizationDepotAccessDenied:
    """Depot access must follow app_metadata.organization_id + static DB row."""

    @pytest.mark.asyncio
    async def test_verify_depot_access_denied_when_depot_not_in_org(self, mock_db_pool):
        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=False)
        user = _valid_user(role="customer_operator")
        with pytest.raises(HTTPException) as exc:
            await verify_depot_access(DEPOT_ID, user, pool=pool)
        assert exc.value.status_code == http_status.HTTP_403_FORBIDDEN

    def test_get_depot_metadata_403_when_org_mismatch(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=False)
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="customer_operator"))
        with patch("src.api.main.db_pools", pool):
            response = client.get(f"/depots/{DEPOT_ID}", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_user_metadata_favonius_role_ignored_for_depot_access(self, client, mock_db_pool):
        """user_metadata must not elevate role; app_metadata governs."""
        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=False)
        user = {
            "sub": str(uuid4()),
            "user_metadata": {"favonius_role": "favonius_admin"},
            "app_metadata": {
                "favonius_role": "customer_operator",
                "organization_id": DEFAULT_ORG_ID,
            },
        }
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(user)
        with patch("src.api.main.db_pools", pool):
            response = client.get(f"/depots/{DEPOT_ID}", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_user_metadata_only_legacy_admin_no_app_role_denied(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        user = {"sub": str(uuid4()), "user_metadata": {"favonius_role": "admin"}, "app_metadata": {}}
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(user)
        with patch("src.api.main.db_pools", pool):
            response = client.get(f"/depots/{DEPOT_ID}", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    @patch("src.api.main.httpx.AsyncClient")
    def test_send_handoff_allows_cross_org_destination(
        self,
        mock_httpx_client,
        client,
        mock_db_pool,
    ):
        """Sender should only be authorized for source depot, not destination depot."""
        pool, conn = mock_db_pool
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(_valid_user(role="customer_operator"))
        app.dependency_overrides[_require_depot_access] = _bypass_depot_access

        conn.fetchrow = AsyncMock(
            return_value={
                "external_id": "bus-42",
                "battery_kwh": 120.0,
                "max_charge_kw": 80.0,
            }
        )
        conn.execute = AsyncMock()

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"acknowledged_at": datetime.utcnow().isoformat() + "Z"}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_httpx_client.return_value.__aenter__.return_value = mock_client
        mock_httpx_client.return_value.__aexit__.return_value = AsyncMock(return_value=None)

        dest_depot_id = str(uuid4())
        request = {
            "dest_depot_id": dest_depot_id,
            "expected_soc": 0.5,
            "arrival_time": (datetime.utcnow() + timedelta(hours=2)).isoformat(),
            "battery_kwh": 120.0,
            "max_charge_kw": 80.0,
        }

        with patch("src.api.main.db_pools", pool):
            response = client.post(
                f"/depots/{DEPOT_ID}/vehicles/{VEHICLE_ID}/handoff",
                json=request,
                headers=AUTH_HDR,
            )

        assert response.status_code == http_status.HTTP_200_OK
