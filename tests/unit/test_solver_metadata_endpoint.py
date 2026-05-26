"""Unit tests for GET /depots/{depot_id}/optimization/solver and Issue-2 guard fixes."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.optimization import SolverMetadataResponse
from src.db.exceptions import DatabaseError, StaticDbUnavailableError
from src.security.tenant_mirror import ensure_tenant_mirrored


# ── Auth fixture ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def default_auth():
    user = {"sub": "test-admin", "app_metadata": {"favonius_role": "favonius_admin"}}
    prev = app.dependency_overrides.get(ensure_tenant_mirrored)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: user
    yield
    if prev is not None:
        app.dependency_overrides[ensure_tenant_mirrored] = prev
    else:
        app.dependency_overrides.pop(ensure_tenant_mirrored, None)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_run_row(depot_id: str) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "run_id": uuid4(),
        "depot_id": depot_id,
        "run_time": now,
        "trigger_reason": "scheduled",
        "horizon_start": now,
        "horizon_end": now,
        "solve_time_s": 12.4,
        "objective_value": 987.65,
        "peak_demand_kw": 150.0,
        "status": "optimal",
        "solver_used": "gurobi",
        "schedule_json": "{}",
    }


# ── GET /depots/{depot_id}/optimization/solver ────────────────────────────────


class TestSolverMetadataEndpoint:
    """Tests for the solver metadata endpoint."""

    def test_returns_last_run_when_optimized(self):
        depot_id = str(uuid4())
        row = _make_run_row(depot_id)

        mock_ts_conn = AsyncMock()
        mock_ts_conn.fetchrow = AsyncMock(return_value=row)
        mock_ts_pool = MagicMock()
        mock_ts_pool.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_ts_conn),
            __aexit__=AsyncMock(return_value=False),
        ))

        mock_static_pool = MagicMock()
        mock_pools = MagicMock(ts=mock_ts_pool, static=mock_static_pool)

        with (
            patch("src.api.optimization._get_db_pools", return_value=mock_pools),
            patch("src.api.optimization.verify_depot_access", new_callable=AsyncMock),
        ):
            client = TestClient(app)
            resp = client.get(f"/depots/{depot_id}/optimization/solver")

        assert resp.status_code == 200
        body = resp.json()
        assert body["depot_id"] == depot_id
        assert body["is_never_run"] is False
        assert body["solver_name"] == "gurobi"
        assert body["last_run"] is not None
        assert body["last_run"]["status"] == "optimal"
        assert body["last_run"]["solver_used"] == "gurobi"
        assert body["last_run"]["solve_time_s"] == pytest.approx(12.4)

    def test_is_never_run_when_no_rows(self):
        depot_id = str(uuid4())

        mock_ts_conn = AsyncMock()
        mock_ts_conn.fetchrow = AsyncMock(return_value=None)
        mock_ts_pool = MagicMock()
        mock_ts_pool.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_ts_conn),
            __aexit__=AsyncMock(return_value=False),
        ))

        mock_pools = MagicMock(ts=mock_ts_pool, static=MagicMock())

        with (
            patch("src.api.optimization._get_db_pools", return_value=mock_pools),
            patch("src.api.optimization.verify_depot_access", new_callable=AsyncMock),
        ):
            client = TestClient(app)
            resp = client.get(f"/depots/{depot_id}/optimization/solver")

        assert resp.status_code == 200
        body = resp.json()
        assert body["is_never_run"] is True
        assert body["solver_name"] is None
        assert body["last_run"] is None

    def test_400_on_invalid_depot_id(self):
        with (
            patch("src.api.optimization._get_db_pools"),
            patch("src.api.optimization.verify_depot_access", new_callable=AsyncMock),
        ):
            client = TestClient(app)
            resp = client.get("/depots/not-a-uuid/optimization/solver")
        assert resp.status_code == 400

    def test_503_when_db_pools_none(self):
        depot_id = str(uuid4())
        with patch(
            "src.api.optimization._get_db_pools",
            side_effect=DatabaseError("Database not available"),
        ):
            client = TestClient(app)
            resp = client.get(f"/depots/{depot_id}/optimization/solver")
        assert resp.status_code == 503
        assert resp.json()["error_code"] == "DATABASE_ERROR"

    def test_highs_solver_name_propagated(self):
        depot_id = str(uuid4())
        row = _make_run_row(depot_id)
        row["solver_used"] = "highs"

        mock_ts_conn = AsyncMock()
        mock_ts_conn.fetchrow = AsyncMock(return_value=row)
        mock_ts_pool = MagicMock()
        mock_ts_pool.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_ts_conn),
            __aexit__=AsyncMock(return_value=False),
        ))

        mock_pools = MagicMock(ts=mock_ts_pool, static=MagicMock())

        with (
            patch("src.api.optimization._get_db_pools", return_value=mock_pools),
            patch("src.api.optimization.verify_depot_access", new_callable=AsyncMock),
        ):
            client = TestClient(app)
            resp = client.get(f"/depots/{depot_id}/optimization/solver")

        assert resp.status_code == 200
        assert resp.json()["solver_name"] == "highs"


# ── StaticDbUnavailableError structured response ──────────────────────────────


class TestStaticDbUnavailableError:
    """Verify StaticDbUnavailableError produces a structured STATIC_DB_UNAVAILABLE body."""

    def test_produces_structured_503(self):
        """A customer_admin token that hits a broken static pool gets STATIC_DB_UNAVAILABLE."""
        depot_id = str(uuid4())

        # Override auth to customer_admin — favonius_admin short-circuits verify_depot_access.
        customer_admin_user = {
            "sub": "customer-user",
            "app_metadata": {
                "favonius_role": "customer_admin",
                "organization_id": str(uuid4()),
            },
        }
        app.dependency_overrides[ensure_tenant_mirrored] = lambda: customer_admin_user

        mock_pools = MagicMock()
        mock_pools.ts = MagicMock()
        mock_pools.static = MagicMock()
        try:
            with patch("src.api.main.db_pools", mock_pools):
                with patch(
                    "src.api.main.verify_depot_access",
                    new_callable=AsyncMock,
                    side_effect=StaticDbUnavailableError(),
                ):
                    client = TestClient(app)
                    resp = client.get(f"/depots/{depot_id}/state")
        finally:
            # Restore the autouse favonius_admin override for other tests.
            admin_user = {"sub": "test-admin", "app_metadata": {"favonius_role": "favonius_admin"}}
            app.dependency_overrides[ensure_tenant_mirrored] = lambda: admin_user

        assert resp.status_code == 503
        body = resp.json()
        assert body.get("error_code") == "STATIC_DB_UNAVAILABLE"
