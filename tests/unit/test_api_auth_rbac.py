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

import pytest
from fastapi import HTTPException, status as http_status

from src.api.main import CommandRequest, _require_depot_access, app
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
