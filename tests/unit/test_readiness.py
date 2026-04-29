"""Unit tests for the optimization readiness service and endpoint.

Covers ``src/core/state/readiness.py`` and the ``GET /depots/{depot_id}/
optimization/readiness`` endpoint, plus the ``not_ready`` gate that ``POST
/optimize`` now enforces.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import status as http_status

from src.api.main import (
    OptimizationRequest,
    ReadinessResponse,
    _readiness_to_response,
    app,
)
from src.core.state.readiness import (
    DEFAULT_SOC,
    ReadinessResult,
    _resolve_building_load_source,
    evaluate_readiness,
)
from src.security.tenant_mirror import ensure_tenant_mirrored

# --- Fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def admin_auth():
    """Inject admin auth so depot-access guards bypass DB checks."""
    user = {"sub": "test-admin", "app_metadata": {"favonius_role": "favonius_admin"}}
    prev = app.dependency_overrides.get(ensure_tenant_mirrored)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: user
    yield
    if prev is not None:
        app.dependency_overrides[ensure_tenant_mirrored] = prev
    else:
        app.dependency_overrides.pop(ensure_tenant_mirrored, None)


def _make_pools(
    *,
    fetchval_sequence: list,
    fetch_sequence: list[list],
):
    """Build a DatabasePools-shaped MagicMock with scripted query results.

    ``fetchval_sequence`` is consumed in call order across both pools (same
    mock conn underlies static and ts). Same for ``fetch_sequence``.
    """
    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=fetchval_sequence)
    conn.fetch = AsyncMock(side_effect=fetch_sequence)

    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None

    pools = MagicMock()
    pools.static = pool
    pools.ts = pool
    return pools, conn


# --- Pure-function tests ---------------------------------------------------


class TestResolveBuildingLoadSource:
    """Closed-set mapping from (meter availability, configured type) to enum."""

    def test_meter_available_returns_meter(self):
        assert (
            _resolve_building_load_source(has_meter_data=True, configured_type="meter") == "meter"
        )

    def test_no_meter_with_configured_returns_forecast_fallback(self):
        assert (
            _resolve_building_load_source(has_meter_data=False, configured_type="api")
            == "forecast_fallback"
        )

    def test_no_meter_with_none_type_returns_absent(self):
        assert (
            _resolve_building_load_source(has_meter_data=False, configured_type="none") == "absent"
        )

    def test_no_meter_with_no_config_returns_absent(self):
        assert _resolve_building_load_source(has_meter_data=False, configured_type=None) == "absent"


# --- evaluate_readiness behavior -------------------------------------------


class TestEvaluateReadiness:
    """End-to-end behavior of the readiness service against mocked pools."""

    @pytest.mark.asyncio
    async def test_ready_when_all_inputs_present_with_meter(self):
        depot_id = str(uuid4())
        # _check_inputs: vehicles, chargers, access, schedules, building_load_source,
        #                prices, building_load_meter
        # _check_telemetry: (no fetchval; fetch vehicle list, fetch telemetry rows)
        fetchvals = [
            True,  # has_vehicles
            True,  # has_chargers
            True,  # has_charger_vehicle_access
            True,  # has_schedules
            {"type": "meter"},  # depots.building_load_source
            True,  # has_prices
            True,  # has_building_load_meter
        ]
        fetches = [
            [{"vehicle_id": "veh-1"}],  # vehicles list (static)
            [{"vehicle_id": "veh-1"}],  # fresh telemetry rows (ts)
        ]
        pools, _ = _make_pools(fetchval_sequence=fetchvals, fetch_sequence=fetches)

        result = await evaluate_readiness(pools, depot_id, horizon_hours=24)

        assert result.status == "ready"
        assert result.missing_inputs == []
        assert result.degraded_reasons == []
        assert result.building_load_source == "meter"
        assert result.snapshot_id is None
        assert result.assumptions == {}

    @pytest.mark.asyncio
    async def test_degraded_when_no_meter_but_fallback_configured(self):
        depot_id = str(uuid4())
        fetchvals = [
            True,  # has_vehicles
            True,  # has_chargers
            True,  # has_charger_vehicle_access
            True,  # has_schedules
            {"type": "api"},  # building_load source configured but not "meter"
            True,  # has_prices
            False,  # has_building_load_meter (no live data)
        ]
        fetches = [
            [{"vehicle_id": "veh-1"}],
            [{"vehicle_id": "veh-1"}],
        ]
        pools, _ = _make_pools(fetchval_sequence=fetchvals, fetch_sequence=fetches)

        result = await evaluate_readiness(pools, depot_id, horizon_hours=24)

        assert result.status == "degraded"
        assert result.missing_inputs == []
        assert "building_load_meter_unavailable" in result.degraded_reasons
        assert result.building_load_source == "forecast_fallback"
        assert "building_load" in result.assumptions
        assert result.assumptions["building_load"]["source"] == "forecast_fallback"

    @pytest.mark.asyncio
    async def test_degraded_when_telemetry_all_defaulted(self):
        depot_id = str(uuid4())
        fetchvals = [
            True,  # has_vehicles
            True,  # has_chargers
            True,  # has_charger_vehicle_access
            True,  # has_schedules
            {"type": "meter"},
            True,  # has_prices
            True,  # has_building_load_meter
        ]
        # Two configured vehicles, none with fresh telemetry
        fetches = [
            [{"vehicle_id": "veh-1"}, {"vehicle_id": "veh-2"}],
            [],  # no fresh rows
        ]
        pools, _ = _make_pools(fetchval_sequence=fetchvals, fetch_sequence=fetches)

        result = await evaluate_readiness(pools, depot_id, horizon_hours=24)

        assert result.status == "degraded"
        assert "telemetry_all_defaulted" in result.degraded_reasons
        tel = result.assumptions["telemetry"]
        assert tel["source"] == "default_soc"
        assert tel["default_value"] == DEFAULT_SOC
        assert set(tel["vehicles"]) == {"veh-1", "veh-2"}

    @pytest.mark.asyncio
    async def test_not_ready_when_inputs_missing(self):
        depot_id = str(uuid4())
        fetchvals = [
            False,  # has_vehicles
            True,  # has_chargers
            False,  # has_charger_vehicle_access
            False,  # has_schedules
            None,  # building_load_source (not configured)
            False,  # has_prices
            False,  # has_building_load_meter
        ]
        fetches: list[list] = [
            # _check_telemetry short-circuits (no vehicles), so only the
            # vehicle-list fetch runs.
            [],
        ]
        pools, _ = _make_pools(fetchval_sequence=fetchvals, fetch_sequence=fetches)

        result = await evaluate_readiness(pools, depot_id, horizon_hours=24)

        assert result.status == "not_ready"
        # All five hard-blockers + building_load (because source=absent)
        assert "vehicles" in result.missing_inputs
        assert "charger_vehicle_access" in result.missing_inputs
        assert "schedules" in result.missing_inputs
        assert "prices" in result.missing_inputs
        assert "building_load" in result.missing_inputs
        # chargers were present, so not flagged
        assert "chargers" not in result.missing_inputs
        assert result.building_load_source == "absent"

    @pytest.mark.asyncio
    async def test_telemetry_degraded_suppressed_when_no_vehicles(self):
        """If vehicles are missing, telemetry_all_defaulted is not also surfaced."""
        depot_id = str(uuid4())
        fetchvals = [
            False,  # has_vehicles
            True,
            True,
            True,
            {"type": "meter"},
            True,
            True,
        ]
        fetches: list[list] = [[]]
        pools, _ = _make_pools(fetchval_sequence=fetchvals, fetch_sequence=fetches)

        result = await evaluate_readiness(pools, depot_id, horizon_hours=24)
        assert "telemetry_all_defaulted" not in result.degraded_reasons

    @pytest.mark.asyncio
    async def test_persist_true_writes_snapshot_and_returns_id(self):
        depot_id = str(uuid4())
        snapshot_uuid = uuid4()
        fetchvals = [
            True,
            True,
            True,
            True,
            {"type": "meter"},
            True,
            True,
            snapshot_uuid,  # INSERT ... RETURNING snapshot_id
        ]
        fetches = [
            [{"vehicle_id": "veh-1"}],
            [{"vehicle_id": "veh-1"}],
        ]
        pools, _ = _make_pools(fetchval_sequence=fetchvals, fetch_sequence=fetches)

        result = await evaluate_readiness(pools, depot_id, horizon_hours=12, persist=True)
        assert result.snapshot_id == str(snapshot_uuid)
        assert result.horizon_hours == 12

    @pytest.mark.asyncio
    async def test_persist_false_does_not_query_snapshot_insert(self):
        depot_id = str(uuid4())
        fetchvals = [
            True,
            True,
            True,
            True,
            {"type": "meter"},
            True,
            True,
        ]
        fetches = [
            [{"vehicle_id": "veh-1"}],
            [{"vehicle_id": "veh-1"}],
        ]
        pools, conn = _make_pools(fetchval_sequence=fetchvals, fetch_sequence=fetches)

        result = await evaluate_readiness(pools, depot_id, horizon_hours=24)
        # 7 fetchvals consumed, no 8th INSERT call
        assert conn.fetchval.await_count == 7
        assert result.snapshot_id is None


# --- Response serialization ------------------------------------------------


class TestReadinessSerializer:
    """``_readiness_to_response`` should mirror the Pydantic contract exactly."""

    def test_round_trip_ready(self):
        captured = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
        result = ReadinessResult(
            depot_id="dep-1",
            status="ready",
            missing_inputs=[],
            degraded_reasons=[],
            building_load_source="meter",
            horizon_hours=24,
            captured_at=captured,
            assumptions={},
        )
        response = _readiness_to_response(result)
        assert isinstance(response, ReadinessResponse)
        assert response.captured_at == captured.isoformat()
        assert response.assumptions.building_load is None
        assert response.assumptions.telemetry is None

    def test_round_trip_degraded_with_assumptions(self):
        captured = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
        result = ReadinessResult(
            depot_id="dep-1",
            status="degraded",
            missing_inputs=[],
            degraded_reasons=[
                "building_load_meter_unavailable",
                "telemetry_all_defaulted",
            ],
            building_load_source="forecast_fallback",
            horizon_hours=24,
            captured_at=captured,
            assumptions={
                "building_load": {
                    "source": "forecast_fallback",
                    "note": "Meter unavailable",
                },
                "telemetry": {
                    "source": "default_soc",
                    "default_value": 0.5,
                    "vehicles": ["v1", "v2"],
                },
            },
        )
        response = _readiness_to_response(result)
        assert response.assumptions.building_load.source == "forecast_fallback"
        assert response.assumptions.telemetry.default_value == 0.5
        assert response.assumptions.telemetry.vehicles == ["v1", "v2"]


# --- Endpoint behavior -----------------------------------------------------


class TestReadinessEndpoint:
    """``GET /depots/{depot_id}/optimization/readiness`` integration."""

    def _stub_result(self, depot_id: str, status_value: str) -> ReadinessResult:
        return ReadinessResult(
            depot_id=depot_id,
            status=status_value,  # type: ignore[arg-type]
            missing_inputs=[] if status_value != "not_ready" else ["vehicles"],
            degraded_reasons=[],
            building_load_source="meter",
            horizon_hours=24,
            captured_at=datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
            assumptions={},
        )

    def test_invalid_uuid_returns_400(self, client):
        response = client.get("/depots/not-a-uuid/optimization/readiness")
        assert response.status_code in (
            http_status.HTTP_400_BAD_REQUEST,
            http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    def test_invalid_horizon_returns_4xx(self, client):
        depot_id = str(uuid4())
        with patch("src.api.main.db_pools", MagicMock()):
            response = client.get(f"/depots/{depot_id}/optimization/readiness?horizon_hours=99")
        # The custom validation handler in main.py reshapes 422 → 400.
        assert response.status_code in (
            http_status.HTTP_400_BAD_REQUEST,
            http_status.HTTP_422_UNPROCESSABLE_ENTITY,
        )

    def test_missing_db_returns_503(self, client):
        depot_id = str(uuid4())
        with patch("src.api.main.db_pools", None):
            response = client.get(f"/depots/{depot_id}/optimization/readiness")
        assert response.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE

    def test_unknown_depot_returns_404(self, client):
        depot_id = str(uuid4())
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=False)  # depot does not exist
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pools = MagicMock(static=pool, ts=pool)

        with patch("src.api.main.db_pools", pools):
            response = client.get(f"/depots/{depot_id}/optimization/readiness")
        assert response.status_code == http_status.HTTP_404_NOT_FOUND

    def test_ready_response_shape(self, client):
        depot_id = str(uuid4())
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=True)  # depot exists
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pools = MagicMock(static=pool, ts=pool)

        async def fake_evaluate(*args, **kwargs):
            return self._stub_result(depot_id, "ready")

        with (
            patch("src.api.main.db_pools", pools),
            patch("src.api.main.evaluate_readiness", side_effect=fake_evaluate),
        ):
            response = client.get(f"/depots/{depot_id}/optimization/readiness")
        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["status"] == "ready"
        assert body["depot_id"] == depot_id
        assert body["building_load_source"] == "meter"
        assert body["snapshot_id"] is None
        # captured_at must be ISO 8601
        assert "T" in body["captured_at"]

    def test_persist_true_propagated(self, client):
        depot_id = str(uuid4())
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=True)
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pools = MagicMock(static=pool, ts=pool)

        captured_kwargs: dict = {}

        async def fake_evaluate(_pools, _depot, horizon_hours, *, persist):
            captured_kwargs["persist"] = persist
            captured_kwargs["horizon_hours"] = horizon_hours
            res = self._stub_result(_depot, "ready")
            res.snapshot_id = "00000000-0000-0000-0000-000000000123"
            return res

        with (
            patch("src.api.main.db_pools", pools),
            patch("src.api.main.evaluate_readiness", side_effect=fake_evaluate),
        ):
            response = client.get(
                f"/depots/{depot_id}/optimization/readiness" "?horizon_hours=12&persist=true"
            )
        assert response.status_code == http_status.HTTP_200_OK
        assert captured_kwargs == {"persist": True, "horizon_hours": 12}
        assert response.json()["snapshot_id"] == "00000000-0000-0000-0000-000000000123"


# --- /optimize gate --------------------------------------------------------


class TestOptimizeReadinessGate:
    """``POST /optimize`` must refuse runs when readiness is not_ready."""

    def test_optimize_blocked_when_not_ready(self, client, sample_depot_config):
        depot_id = str(uuid4())

        async def fake_evaluate(_pools, _depot, _horizon, *, persist):
            return ReadinessResult(
                depot_id=_depot,
                status="not_ready",
                missing_inputs=["prices", "schedules"],
                degraded_reasons=[],
                building_load_source="meter",
                horizon_hours=_horizon,
                captured_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
                assumptions={},
            )

        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=True)  # depot exists
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pools = MagicMock(static=pool, ts=pool)

        controller_manager_mock = MagicMock()
        controller_manager_mock.get_or_create_controller = AsyncMock(return_value=AsyncMock())

        with (
            patch("src.api.main.db_pools", pools),
            patch(
                "src.api.main._get_depot_config",
                AsyncMock(return_value=sample_depot_config),
            ),
            patch("src.api.main.evaluate_readiness", side_effect=fake_evaluate),
            patch("src.api.main.controller_manager", controller_manager_mock),
        ):
            payload = OptimizationRequest(depot_id=depot_id, horizon_hours=24, force=False)
            response = client.post("/optimize", json=payload.model_dump())

        assert response.status_code == http_status.HTTP_422_UNPROCESSABLE_ENTITY
        body = response.json()
        # FastAPI wraps 4xx with detail; the structured detail is preserved.
        detail = body.get("detail")
        # Some middleware reshapes detail into the ErrorResponse model
        if isinstance(detail, dict):
            assert detail.get("error_code") == "OPTIMIZATION_NOT_READY"
            assert "prices" in detail.get("missing_inputs", [])
        else:
            # fallback: at least our error_code string is somewhere in the body
            assert "OPTIMIZATION_NOT_READY" in response.text
        # Controller must not have been invoked
        controller_manager_mock.get_or_create_controller.assert_not_called()

    def test_optimize_allowed_when_degraded(
        self, client, sample_depot_config, sample_optimization_result
    ):
        depot_id = str(uuid4())

        async def fake_evaluate(_pools, _depot, _horizon, *, persist):
            return ReadinessResult(
                depot_id=_depot,
                status="degraded",
                missing_inputs=[],
                degraded_reasons=["building_load_meter_unavailable"],
                building_load_source="forecast_fallback",
                horizon_hours=_horizon,
                captured_at=datetime(2026, 4, 29, tzinfo=timezone.utc),
                assumptions={
                    "building_load": {
                        "source": "forecast_fallback",
                        "note": "Meter unavailable",
                    }
                },
            )

        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchval = AsyncMock(return_value=True)
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pools = MagicMock(static=pool, ts=pool)

        controller_mock = AsyncMock()
        controller_mock.run_optimization = AsyncMock(return_value=sample_optimization_result)
        controller_manager_mock = MagicMock()
        controller_manager_mock.get_or_create_controller = AsyncMock(return_value=controller_mock)

        with (
            patch("src.api.main.db_pools", pools),
            patch(
                "src.api.main._get_depot_config",
                AsyncMock(return_value=sample_depot_config),
            ),
            patch("src.api.main.evaluate_readiness", side_effect=fake_evaluate),
            patch("src.api.main.controller_manager", controller_manager_mock),
        ):
            payload = OptimizationRequest(depot_id=depot_id, horizon_hours=24, force=False)
            response = client.post("/optimize", json=payload.model_dump())

        assert response.status_code == http_status.HTTP_200_OK
        controller_manager_mock.get_or_create_controller.assert_awaited_once_with(depot_id)
