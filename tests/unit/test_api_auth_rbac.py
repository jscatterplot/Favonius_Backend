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
from src.security.auth import verify_token


# ── Helpers ─────────────────────────────────────────────────────────────────

AUTH_HDR = {"Authorization": "Bearer test-token"}
DEPOT_ID = str(uuid4())
VEHICLE_ID = str(uuid4())


def _valid_user(role: str = "operator", depot_id: str = DEPOT_ID) -> dict:
    return {
        "sub": str(uuid4()),
        "user_metadata": {
            "favonius_role": role,
            "depot_ids": [depot_id],
        },
    }


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
    captures the `verify_token` function object in Depends() at module load
    time — module-level @patch cannot reach it.
    """

    def test_optimize_expired_token(self, client):
        app.dependency_overrides[verify_token] = _override_token_raises(401, "Token expired")
        response = client.post(
            "/optimize",
            json={"depot_id": DEPOT_ID, "horizon_hours": 24},
            headers=AUTH_HDR,
        )
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_depot_state_invalid_token(self, client):
        app.dependency_overrides[verify_token] = _override_token_raises(401, "Invalid token")
        response = client.get(f"/depots/{DEPOT_ID}/state", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_me_depots_invalid_token(self, client):
        app.dependency_overrides[verify_token] = _override_token_raises(401)
        response = client.get("/me/depots", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_commands_execute_invalid_token(self, client):
        app.dependency_overrides[verify_token] = _override_token_raises(401)
        response = client.post(
            "/commands/execute",
            json={"command": "optimization.run", "depot_id": DEPOT_ID},
            headers=AUTH_HDR,
        )
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED

    def test_openapi_invalid_token(self, client):
        app.dependency_overrides[verify_token] = _override_token_raises(401)
        response = client.get("/openapi.json", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_401_UNAUTHORIZED


# ── 403 for insufficient depot access ────────────────────────────────────────


class TestInsufficientDepotAccessReturns403:
    """Valid JWT but user lacks access to the requested depot → 403.

    Overrides _require_depot_access (the Depends factory) to raise 403,
    simulating what happens when the user's depot_ids claim doesn't include
    the requested depot.
    """

    def test_depot_state_no_access(self, client):
        app.dependency_overrides[verify_token] = _override_token(
            {"sub": str(uuid4()), "user_metadata": {}}
        )
        app.dependency_overrides[_require_depot_access] = _deny_depot_access
        response = client.get(f"/depots/{DEPOT_ID}/state", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_depot_schedule_no_access(self, client):
        app.dependency_overrides[verify_token] = _override_token(
            {"sub": str(uuid4()), "user_metadata": {}}
        )
        app.dependency_overrides[_require_depot_access] = _deny_depot_access
        response = client.get(f"/depots/{DEPOT_ID}/schedule", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_depot_alerts_no_access(self, client):
        app.dependency_overrides[verify_token] = _override_token(
            {"sub": str(uuid4()), "user_metadata": {}}
        )
        app.dependency_overrides[_require_depot_access] = _deny_depot_access
        response = client.get(f"/depots/{DEPOT_ID}/alerts", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_depot_metadata_no_access(self, client):
        app.dependency_overrides[verify_token] = _override_token(
            {"sub": str(uuid4()), "user_metadata": {}}
        )
        app.dependency_overrides[_require_depot_access] = _deny_depot_access
        response = client.get(f"/depots/{DEPOT_ID}", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN


# ── OpenAPI gating ───────────────────────────────────────────────────────────


class TestOpenApiGating:
    """GET /openapi.json requires admin role."""

    def test_operator_gets_403(self, client):
        app.dependency_overrides[verify_token] = _override_token(_valid_user(role="operator"))
        response = client.get("/openapi.json", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_viewer_gets_403(self, client):
        app.dependency_overrides[verify_token] = _override_token(_valid_user(role="viewer"))
        response = client.get("/openapi.json", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_403_FORBIDDEN

    def test_admin_gets_200(self, client):
        app.dependency_overrides[verify_token] = _override_token(_valid_user(role="admin"))
        response = client.get("/openapi.json", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_200_OK
        data = response.json()
        assert "openapi" in data
        assert "paths" in data


# ── GET /me/depots ───────────────────────────────────────────────────────────


class TestMyDepots:
    """GET /me/depots returns depot list from JWT claim or DB."""

    def test_returns_depots_from_claim(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[verify_token] = _override_token(
            _valid_user(role="operator", depot_id=DEPOT_ID)
        )
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": DEPOT_ID,
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
        assert data["depots"][0]["id"] == DEPOT_ID

    def test_returns_empty_list_when_no_depot_ids_claim(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[verify_token] = _override_token(
            {"sub": str(uuid4()), "user_metadata": {"favonius_role": "operator"}}
        )
        conn.fetch = AsyncMock(return_value=[])
        with patch("src.api.main.db_pools", pool):
            response = client.get("/me/depots", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_200_OK
        assert response.json() == {"depots": []}

    def test_admin_gets_all_depots(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        app.dependency_overrides[verify_token] = _override_token(_valid_user(role="admin"))
        conn.fetch = AsyncMock(return_value=[])
        with patch("src.api.main.db_pools", pool):
            response = client.get("/me/depots", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_200_OK


# ── POST /commands/execute — RBAC matrix ────────────────────────────────────


class TestCommandRbac:
    """RBAC permission matrix for each command type.

    Users with depot_ids=[DEPOT_ID] in their JWT pass verify_depot_access
    via the fast-path claim check without needing a DB pool.
    """

    @pytest.mark.parametrize(
        "role, command, params, expected_status",
        [
            # optimization.run — requires optimize:trigger (operator+)
            ("admin", "optimization.run", {}, 200),
            ("operator", "optimization.run", {}, 200),
            ("viewer", "optimization.run", {}, 403),
            ("auditor", "optimization.run", {}, 403),
            # fleet.charger.restart — requires depot:manage (operator+)
            # 400 = charger_id missing, which means RBAC passed
            ("admin", "fleet.charger.restart", {}, 400),
            ("operator", "fleet.charger.restart", {}, 400),
            ("viewer", "fleet.charger.restart", {}, 403),
            ("auditor", "fleet.charger.restart", {}, 403),
            # depot.config.update — requires admin:config (admin only)
            ("admin", "depot.config.update", {"max_grid_kw": 850.0}, 200),
            ("operator", "depot.config.update", {"max_grid_kw": 850.0}, 403),
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
    ):
        user = _valid_user(role=role)
        app.dependency_overrides[verify_token] = _override_token(user)

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
    def test_audit_log_written_on_dry_run(self, mock_get_audit, client):
        user_id = str(uuid4())
        user = _valid_user(role="operator")
        user["sub"] = user_id
        app.dependency_overrides[verify_token] = _override_token(user)

        mock_audit = AsyncMock()
        mock_get_audit.return_value = mock_audit

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
    def test_audit_log_written_on_real_execution(self, mock_get_audit, mock_cm, client):
        user_id = str(uuid4())
        user = {"sub": user_id, "user_metadata": {"favonius_role": "operator", "depot_ids": [DEPOT_ID]}}
        app.dependency_overrides[verify_token] = _override_token(user)

        mock_controller = AsyncMock()
        mock_cm.get_or_create_controller = AsyncMock(return_value=mock_controller)

        mock_audit = AsyncMock()
        mock_get_audit.return_value = mock_audit

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
    def test_no_error_when_audit_logger_is_none(self, mock_get_audit, client):
        """Command executes successfully even if audit logger is not initialized."""
        app.dependency_overrides[verify_token] = _override_token(_valid_user(role="operator"))
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
        app.dependency_overrides[verify_token] = _override_token(_valid_user())
        app.dependency_overrides[_require_depot_access] = _bypass_depot_access
        conn.fetchrow = AsyncMock(
            return_value={
                "id": DEPOT_ID,
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
        app.dependency_overrides[verify_token] = _override_token(_valid_user())
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
        app.dependency_overrides[verify_token] = _override_token(_valid_user())
        response = client.get("/depots/not-a-uuid", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST
