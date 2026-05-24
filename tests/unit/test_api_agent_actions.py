"""Tests for agent actions, autonomy settings, and agent commands.

Covers:
- GET /depots/{id}/agent-actions — camelCase serialization, DB-backed rows
- GET /depots/{id}/autonomy-settings — matrix shape, defaults, persisted rows
- POST /commands/execute agents.autonomy.set — upsert, validation, RBAC
- POST /commands/execute agents.action.{approve,reject,rollback} — smoke
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import status as http_status

from src.api.main import _require_depot_access, app
from src.security.tenant_mirror import ensure_tenant_mirrored


AUTH_HDR = {"Authorization": "Bearer test-token"}
DEPOT_ID = str(uuid4())
DEFAULT_ORG_ID = str(uuid4())


def _valid_user(role: str = "customer_operator", organization_id: str | None = None) -> dict:
    meta: dict = {"favonius_role": role}
    if role != "favonius_admin":
        meta["organization_id"] = organization_id or DEFAULT_ORG_ID
    return {"sub": str(uuid4()), "app_metadata": meta}


def _override_token(user: dict):
    def override() -> dict:
        return user

    return override


def _bypass_depot_access(depot_id: str) -> str:
    return depot_id


@pytest.fixture(autouse=True)
def _bypass_depot_access_dep():
    """Bypass _require_depot_access so we test endpoint logic, not auth plumbing."""
    app.dependency_overrides[_require_depot_access] = _bypass_depot_access
    yield
    app.dependency_overrides.pop(_require_depot_access, None)


@pytest.fixture(autouse=True)
def _default_user():
    """Inject a customer_operator user by default for all tests in this file."""
    app.dependency_overrides[ensure_tenant_mirrored] = _override_token(
        _valid_user(role="customer_operator")
    )
    yield
    app.dependency_overrides.pop(ensure_tenant_mirrored, None)


# ── GET /depots/{id}/agent-actions ────────────────────────────────────────────


class TestListAgentActionsSerialization:
    """The endpoint must emit camelCase to match the frontend zod schema."""

    def test_returns_camel_case_keys(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        created_at = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "id": uuid4(),
                    "depot_id": DEPOT_ID,
                    "agent_type": "reporting",
                    "action_class": "report_draft",
                    "mode": "proposed",
                    "status": "pending",
                    "summary": "Generate April 2026 consumption report",
                    "entity_type": None,
                    "entity_id": None,
                    "created_at": created_at,
                    "resolved_at": None,
                    "payload": {
                        "kind": "monthly_consumption",
                        "periodStart": "2026-04-01",
                        "periodEnd": "2026-04-30",
                    },
                }
            ]
        )
        with patch("src.api.main.db_pools", pool):
            response = client.get(f"/depots/{DEPOT_ID}/agent-actions", headers=AUTH_HDR)

        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert isinstance(body, list) and len(body) == 1
        row = body[0]
        # camelCase wire keys
        assert row["agentType"] == "reporting"
        assert row["actionClass"] == "report_draft"
        assert row["createdAt"].startswith("2026-05-01T12:00")
        assert "resolvedAt" in row
        # payload is JSONB and passes through untouched
        assert row["payload"]["kind"] == "monthly_consumption"
        assert row["payload"]["periodStart"] == "2026-04-01"
        # snake_case must NOT appear on the wire
        assert "agent_type" not in row
        assert "action_class" not in row
        assert "created_at" not in row

    def test_empty_returns_empty_list(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        conn.fetch = AsyncMock(return_value=[])
        with patch("src.api.main.db_pools", pool):
            response = client.get(f"/depots/{DEPOT_ID}/agent-actions", headers=AUTH_HDR)
        assert response.status_code == http_status.HTTP_200_OK
        assert response.json() == []


# ── GET /depots/{id}/autonomy-settings ────────────────────────────────────────


class TestAutonomySettingsShape:
    """Response must match {rows: [{actionClass, level}], asOf}."""

    def test_defaults_when_db_empty(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        conn.fetch = AsyncMock(return_value=[])
        with patch("src.api.main.db_pools", pool):
            response = client.get(f"/depots/{DEPOT_ID}/autonomy-settings", headers=AUTH_HDR)

        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert "rows" in body and "asOf" in body
        # Defaults: all 5 known action classes at level 'proposed'.
        classes = {r["actionClass"] for r in body["rows"]}
        assert classes == {
            "charger_restart",
            "session_reassign",
            "price_reoptimize",
            "soc_guardrail",
            "report_draft",
        }
        assert all(r["level"] == "proposed" for r in body["rows"])
        # asOf is an ISO-8601 timestamp
        assert isinstance(body["asOf"], str) and "T" in body["asOf"]
        # Sorted alphabetically by actionClass for stable rendering
        action_classes = [r["actionClass"] for r in body["rows"]]
        assert action_classes == sorted(action_classes)

    def test_db_rows_override_defaults(self, client, mock_db_pool):
        pool, conn = mock_db_pool
        as_of = datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc)
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "action_class": "report_draft",
                    "level": "auto_silent",
                    "updated_at": as_of,
                },
                {
                    "action_class": "charger_restart",
                    "level": "shadow",
                    "updated_at": as_of,
                },
            ]
        )
        with patch("src.api.main.db_pools", pool):
            response = client.get(f"/depots/{DEPOT_ID}/autonomy-settings", headers=AUTH_HDR)

        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        by_class = {r["actionClass"]: r["level"] for r in body["rows"]}
        assert by_class["report_draft"] == "auto_silent"
        assert by_class["charger_restart"] == "shadow"
        # Unset classes still surface their default
        assert by_class["session_reassign"] == "proposed"
        assert body["asOf"].startswith("2026-05-20T09:30")

    def test_extra_action_class_from_db_is_included(self, client, mock_db_pool):
        """Frontend renders any class string; new rows must not be dropped."""
        pool, conn = mock_db_pool
        conn.fetch = AsyncMock(
            return_value=[
                {
                    "action_class": "experimental_new_class",
                    "level": "proposed",
                    "updated_at": datetime.now(timezone.utc),
                },
            ]
        )
        with patch("src.api.main.db_pools", pool):
            response = client.get(f"/depots/{DEPOT_ID}/autonomy-settings", headers=AUTH_HDR)

        body = response.json()
        classes = [r["actionClass"] for r in body["rows"]]
        assert "experimental_new_class" in classes
        # Defaults still present alongside the new class
        assert "report_draft" in classes


# ── POST /commands/execute — agents.autonomy.set ──────────────────────────────


def _autonomy_set_body(action_class: str = "charger_restart", level: str = "proposed") -> dict:
    return {
        "command": "agents.autonomy.set",
        "depot_id": DEPOT_ID,
        "params": {"actionClass": action_class, "level": level},
        "dry_run": False,
    }


class TestAgentsAutonomySet:
    @patch("src.api.main.get_audit_logger", return_value=None)
    def test_happy_path_upserts_row(self, _audit, client, mock_db_pool):
        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=True)
        updated_at = datetime(2026, 5, 22, 10, 0, tzinfo=timezone.utc)
        conn.fetchrow = AsyncMock(
            return_value={
                "action_class": "charger_restart",
                "level": "auto_notify",
                "updated_at": updated_at,
            }
        )
        with patch("src.api.main.db_pools", pool):
            response = client.post(
                "/commands/execute",
                json=_autonomy_set_body("charger_restart", "auto_notify"),
                headers=AUTH_HDR,
            )

        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["status"] == "ok"
        assert body["command"] == "agents.autonomy.set"
        result = body["result"]
        assert result["actionClass"] == "charger_restart"
        assert result["level"] == "auto_notify"
        assert result["updatedAt"].startswith("2026-05-22T10:00")

        # Confirm we wrote with the right UPSERT shape.
        write_call = conn.fetchrow.await_args
        assert write_call is not None
        sql = write_call.args[0]
        assert "INSERT INTO agent_autonomy_settings" in sql
        assert "ON CONFLICT" in sql

    @patch("src.api.main.get_audit_logger", return_value=None)
    def test_dry_run_skips_db(self, _audit, client, mock_db_pool):
        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=True)
        with patch("src.api.main.db_pools", pool):
            body = _autonomy_set_body("report_draft", "shadow")
            body["dry_run"] = True
            response = client.post("/commands/execute", json=body, headers=AUTH_HDR)

        assert response.status_code == http_status.HTTP_200_OK
        out = response.json()
        assert out["status"] == "dry_run"
        assert out["result"]["actionClass"] == "report_draft"
        assert out["result"]["level"] == "shadow"
        assert out["result"]["simulated"] is True
        # No DB write attempted
        conn.fetchrow.assert_not_called()

    @pytest.mark.parametrize(
        "params,expected",
        [
            ({"actionClass": "", "level": "proposed"}, 400),
            ({"actionClass": "   ", "level": "proposed"}, 400),  # whitespace-only
            ({"actionClass": "\t\n", "level": "proposed"}, 400),  # other whitespace
            ({"actionClass": 5, "level": "proposed"}, 400),  # non-string
            ({"level": "proposed"}, 400),
            ({"actionClass": "charger_restart", "level": "nonsense"}, 422),
            ({"actionClass": "charger_restart"}, 422),
        ],
    )
    @patch("src.api.main.get_audit_logger", return_value=None)
    def test_validation_errors(
        self, _audit, params, expected, client, mock_db_pool
    ):
        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=True)
        with patch("src.api.main.db_pools", pool):
            response = client.post(
                "/commands/execute",
                json={
                    "command": "agents.autonomy.set",
                    "depot_id": DEPOT_ID,
                    "params": params,
                    "dry_run": False,
                },
                headers=AUTH_HDR,
            )
        assert response.status_code == expected
        # No DB write on validation failure
        conn.fetchrow.assert_not_called()

    @pytest.mark.parametrize(
        "role,expected_status",
        [
            ("favonius_admin", 200),
            ("customer_admin", 200),
            ("customer_operator", 200),
            ("viewer", 403),
            ("auditor", 403),
        ],
    )
    @patch("src.api.main.get_audit_logger", return_value=None)
    def test_rbac_matrix(
        self, _audit, role, expected_status, client, mock_db_pool
    ):
        user = _valid_user(role=role)
        app.dependency_overrides[ensure_tenant_mirrored] = _override_token(user)

        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=True)
        conn.fetchrow = AsyncMock(
            return_value={
                "action_class": "charger_restart",
                "level": "proposed",
                "updated_at": datetime.now(timezone.utc),
            }
        )
        with patch("src.api.main.db_pools", pool):
            response = client.post(
                "/commands/execute",
                json=_autonomy_set_body(),
                headers=AUTH_HDR,
            )
        assert response.status_code == expected_status, (
            f"role={role}: expected {expected_status}, got {response.status_code} "
            f"body={response.text}"
        )

    @patch("src.api.main.get_audit_logger", return_value=None)
    def test_action_class_trimmed_before_write(self, _audit, client, mock_db_pool):
        """Surrounding whitespace is stripped before validation and persistence."""
        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=True)
        conn.fetchrow = AsyncMock(
            return_value={
                "action_class": "charger_restart",
                "level": "proposed",
                "updated_at": datetime.now(timezone.utc),
            }
        )
        with patch("src.api.main.db_pools", pool):
            response = client.post(
                "/commands/execute",
                json=_autonomy_set_body("  charger_restart  ", "proposed"),
                headers=AUTH_HDR,
            )
        assert response.status_code == http_status.HTTP_200_OK
        # The trimmed value is what gets written to the DB.
        write_call = conn.fetchrow.await_args
        assert write_call.args[2] == "charger_restart"

    @patch("src.api.main.get_audit_logger", return_value=None)
    def test_all_four_levels_accepted(self, _audit, client, mock_db_pool):
        """Confirm every documented level value validates and writes."""
        pool, conn = mock_db_pool
        conn.fetchval = AsyncMock(return_value=True)

        for level in ("shadow", "proposed", "auto_notify", "auto_silent"):
            conn.fetchrow = AsyncMock(
                return_value={
                    "action_class": "charger_restart",
                    "level": level,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            with patch("src.api.main.db_pools", pool):
                response = client.post(
                    "/commands/execute",
                    json=_autonomy_set_body("charger_restart", level),
                    headers=AUTH_HDR,
                )
            assert response.status_code == http_status.HTTP_200_OK, (
                f"level={level}: {response.text}"
            )
            assert response.json()["result"]["level"] == level


# ── POST /commands/execute — agents.action.{approve,reject,rollback} ─────────


class TestAgentActionCommands:
    """Smoke tests covering the wire shape for the three lifecycle commands."""

    @patch("src.api.main.get_audit_logger", return_value=None)
    def test_reject_executes(self, _audit, client, mock_db_pool):
        pool, conn = mock_db_pool
        action_id = str(uuid4())
        conn.fetchval = AsyncMock(return_value=True)
        conn.fetchrow = AsyncMock(return_value={"id": action_id})

        with patch("src.api.main.db_pools", pool):
            response = client.post(
                "/commands/execute",
                json={
                    "command": "agents.action.reject",
                    "depot_id": DEPOT_ID,
                    "params": {"actionId": action_id},
                    "dry_run": False,
                },
                headers=AUTH_HDR,
            )
        assert response.status_code == http_status.HTTP_200_OK
        result = response.json()["result"]
        assert result["actionId"] == action_id
        assert result["actionStatus"] == "rejected"

    @patch("src.api.main.get_audit_logger", return_value=None)
    def test_rollback_executes(self, _audit, client, mock_db_pool):
        pool, conn = mock_db_pool
        action_id = str(uuid4())
        conn.fetchval = AsyncMock(return_value=True)
        conn.fetchrow = AsyncMock(return_value={"id": action_id})

        with patch("src.api.main.db_pools", pool):
            response = client.post(
                "/commands/execute",
                json={
                    "command": "agents.action.rollback",
                    "depot_id": DEPOT_ID,
                    "params": {"actionId": action_id},
                    "dry_run": False,
                },
                headers=AUTH_HDR,
            )
        assert response.status_code == http_status.HTTP_200_OK
        result = response.json()["result"]
        assert result["actionId"] == action_id
        assert result["actionStatus"] == "rolled_back"

    @patch("src.api.main.get_audit_logger", return_value=None)
    def test_approve_dry_run_returns_status_without_writes(
        self, _audit, client, mock_db_pool
    ):
        pool, conn = mock_db_pool
        action_id = str(uuid4())
        conn.fetchval = AsyncMock(return_value=True)
        # action_class != 'report_draft' so no nested report call required
        conn.fetchrow = AsyncMock(
            return_value={
                "action_class": "charger_restart",
                "status": "pending",
                "payload": None,
            }
        )

        with patch("src.api.main.db_pools", pool):
            response = client.post(
                "/commands/execute",
                json={
                    "command": "agents.action.approve",
                    "depot_id": DEPOT_ID,
                    "params": {"actionId": action_id},
                    "dry_run": True,
                },
                headers=AUTH_HDR,
            )
        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["status"] == "dry_run"
        assert body["result"]["actionId"] == action_id
        assert body["result"]["actionStatus"] == "executed"
