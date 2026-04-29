"""Tests for GET /depots/{id}/optimization/readiness endpoint.

Covers happy path, degraded mode (forecast fallback), missing inputs
(not_ready), invalid depot_id, missing depot, persist=true path, and
the invariant that the response uses the literal status / missing-input
strings the frontend depends on.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import status as http_status

from src.api.main import app
from src.core.models import DepotConfig, DepotState
from src.security.tenant_mirror import ensure_tenant_mirrored


@pytest.fixture(autouse=True)
def admin_auth():
    """Override auth so depot access checks pass for arbitrary UUIDs."""
    user = {"sub": "test-admin", "app_metadata": {"favonius_role": "favonius_admin"}}
    prev = app.dependency_overrides.get(ensure_tenant_mirrored)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: user
    yield
    if prev is not None:
        app.dependency_overrides[ensure_tenant_mirrored] = prev
    else:
        app.dependency_overrides.pop(ensure_tenant_mirrored, None)


def _full_config() -> DepotConfig:
    return DepotConfig(
        vehicle_capacities={"bus_1": 324.0, "bus_2": 324.0},
        vehicle_max_charge_kw={"bus_1": 80.0, "bus_2": 80.0},
        charger_groups={80.0: 4},
        charger_efficiency=0.95,
        charger_vehicle_access={
            "charger_a": {"bus_1", "bus_2"},
            "charger_b": {"bus_1"},
        },
        battery_capacity=500.0,
        battery_power=100.0,
        max_site_power=800.0,
    )


def _full_state() -> DepotState:
    n = 96
    return DepotState(
        vehicle_socs={"bus_1": 0.45, "bus_2": 0.82},
        battery_soc=0.55,
        prices=[0.10] * n,
        demand_charge_rate=20.0,
        current_month_peak=380.0,
        vehicle_availability={"bus_1": [True] * n, "bus_2": [True] * n},
        energy_requirements={"bus_1": 200.0, "bus_2": 150.0},
        departure_times={"bus_1": 48, "bus_2": 60},
        building_power=[50.0] * n,
    )


def _stub_assembler(*, building_source: str, schedules_present: bool):
    """Build a mock StateAssembler with the metadata properties the
    endpoint reads after calling get_current_state.
    """
    assembler = AsyncMock()
    assembler.get_current_state = AsyncMock(return_value=_full_state())
    assembler.fetch_snapshot_extras = AsyncMock(return_value=None)
    now = datetime(2026, 4, 29, 12, 0, 0)
    horizon_end = now + timedelta(hours=24)
    # Properties — set as plain attributes on the AsyncMock spec.
    assembler.last_horizon = (now, horizon_end)
    assembler.last_building_load_source = building_source
    assembler.last_schedules_present = schedules_present
    assembler.last_schedules = (
        [
            {
                "vehicle_id": "bus_1",
                "departure_time": now + timedelta(hours=6),
                "return_time": now + timedelta(hours=18),
                "estimated_energy_kwh": 200.0,
            }
        ]
        if schedules_present
        else []
    )
    assembler.last_weather_features = []
    assembler.last_organization_id = str(uuid4())
    return assembler


class TestReadinessEndpoint:
    @patch("src.api.main.StateAssembler")
    @patch("src.api.main._get_depot_config")
    @patch("src.api.main.db_pools")
    def test_ready_response(
        self, mock_pool, mock_get_config, mock_assembler_cls, client
    ):
        mock_pool_value = MagicMock()
        mock_get_config.return_value = _full_config()
        mock_assembler_cls.return_value = _stub_assembler(
            building_source="meter", schedules_present=True
        )

        depot_id = str(uuid4())
        with patch("src.api.main.db_pools", mock_pool_value):
            response = client.get(f"/depots/{depot_id}/optimization/readiness")

        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["depot_id"] == depot_id
        assert body["status"] == "ready"
        assert body["missing_inputs"] == []
        assert body["degraded_reasons"] == []
        assert body["assumptions"] == {}
        assert body["building_load_source"] == "meter"
        assert body["horizon_hours"] == 24
        assert body["snapshot_id"] is None  # persist=false by default
        assert body["captured_at"]  # ISO8601

    @patch("src.api.main.StateAssembler")
    @patch("src.api.main._get_depot_config")
    @patch("src.api.main.db_pools")
    def test_degraded_response_for_forecast_fallback(
        self, mock_pool, mock_get_config, mock_assembler_cls, client
    ):
        mock_pool_value = MagicMock()
        mock_get_config.return_value = _full_config()
        mock_assembler_cls.return_value = _stub_assembler(
            building_source="forecast_fallback", schedules_present=True
        )

        depot_id = str(uuid4())
        with patch("src.api.main.db_pools", mock_pool_value):
            response = client.get(f"/depots/{depot_id}/optimization/readiness")

        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["status"] == "degraded"
        assert "building_load_meter_unavailable" in body["degraded_reasons"]
        assert body["building_load_source"] == "forecast_fallback"
        # Frontend renders this — assert the contract.
        assert body["assumptions"]["building_load"]["source"] == "forecast_fallback"
        assert body["assumptions"]["building_load"]["note"]

    @patch("src.api.main.StateAssembler")
    @patch("src.api.main._get_depot_config")
    @patch("src.api.main.db_pools")
    def test_not_ready_when_schedules_missing(
        self, mock_pool, mock_get_config, mock_assembler_cls, client
    ):
        mock_pool_value = MagicMock()
        mock_get_config.return_value = _full_config()
        mock_assembler_cls.return_value = _stub_assembler(
            building_source="meter", schedules_present=False
        )

        depot_id = str(uuid4())
        with patch("src.api.main.db_pools", mock_pool_value):
            response = client.get(f"/depots/{depot_id}/optimization/readiness")

        # 200 — not_ready is a valid readiness verdict, not an HTTP error.
        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["status"] == "not_ready"
        assert "schedules" in body["missing_inputs"]

    @patch("src.api.main.StateAssembler")
    @patch("src.api.main._get_depot_config")
    @patch("src.api.main.db_pools")
    def test_not_ready_when_charger_access_empty(
        self, mock_pool, mock_get_config, mock_assembler_cls, client
    ):
        config = _full_config()
        config.charger_vehicle_access = {}
        mock_pool_value = MagicMock()
        mock_get_config.return_value = config
        mock_assembler_cls.return_value = _stub_assembler(
            building_source="meter", schedules_present=True
        )

        depot_id = str(uuid4())
        with patch("src.api.main.db_pools", mock_pool_value):
            response = client.get(f"/depots/{depot_id}/optimization/readiness")

        body = response.json()
        assert body["status"] == "not_ready"
        assert "charger_vehicle_access" in body["missing_inputs"]

    def test_invalid_depot_uuid_returns_400(self, client):
        response = client.get("/depots/not-a-uuid/optimization/readiness")
        assert response.status_code == http_status.HTTP_400_BAD_REQUEST

    @patch("src.api.main.StateAssembler")
    @patch("src.api.main._get_depot_config")
    @patch("src.api.main.db_pools")
    def test_horizon_hours_query_param_propagates(
        self, mock_pool, mock_get_config, mock_assembler_cls, client
    ):
        mock_pool_value = MagicMock()
        mock_get_config.return_value = _full_config()
        assembler = _stub_assembler(building_source="meter", schedules_present=True)
        mock_assembler_cls.return_value = assembler

        depot_id = str(uuid4())
        with patch("src.api.main.db_pools", mock_pool_value):
            response = client.get(
                f"/depots/{depot_id}/optimization/readiness?horizon_hours=12"
            )

        assert response.status_code == http_status.HTTP_200_OK
        assert response.json()["horizon_hours"] == 12
        assembler.get_current_state.assert_awaited_once_with(12)

    @patch("src.api.main.persist_snapshot")
    @patch("src.api.main.StateAssembler")
    @patch("src.api.main._get_depot_config")
    @patch("src.api.main.db_pools")
    def test_persist_true_writes_snapshot_and_returns_id(
        self,
        mock_pool,
        mock_get_config,
        mock_assembler_cls,
        mock_persist,
        client,
    ):
        mock_pool_value = MagicMock()
        mock_get_config.return_value = _full_config()
        mock_assembler_cls.return_value = _stub_assembler(
            building_source="meter", schedules_present=True
        )
        mock_persist.return_value = uuid4()

        depot_id = str(uuid4())
        with patch("src.api.main.db_pools", mock_pool_value):
            response = client.get(
                f"/depots/{depot_id}/optimization/readiness?persist=true"
            )

        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        assert body["snapshot_id"] is not None
        mock_persist.assert_awaited_once()

    @patch("src.api.main.persist_snapshot")
    @patch("src.api.main.StateAssembler")
    @patch("src.api.main._get_depot_config")
    @patch("src.api.main.db_pools")
    def test_persist_true_does_not_run_dead_payload_validation(
        self,
        mock_pool,
        mock_get_config,
        mock_assembler_cls,
        mock_persist,
        client,
    ):
        mock_pool_value = MagicMock()
        mock_get_config.return_value = _full_config()
        mock_assembler_cls.return_value = _stub_assembler(
            building_source="meter", schedules_present=True
        )
        mock_persist.return_value = uuid4()
        payload_validator = MagicMock(side_effect=RuntimeError("payload failed"))

        depot_id = str(uuid4())
        with patch("src.api.main.db_pools", mock_pool_value), patch(
            "src.api.main.snapshot_to_payload", payload_validator, create=True
        ):
            response = client.get(
                f"/depots/{depot_id}/optimization/readiness?persist=true"
            )

        assert response.status_code == http_status.HTTP_200_OK
        assert response.json()["status"] == "ready"
        payload_validator.assert_not_called()

    @patch("src.api.main.persist_snapshot")
    @patch("src.api.main.StateAssembler")
    @patch("src.api.main._get_depot_config")
    @patch("src.api.main.db_pools")
    def test_persist_failure_does_not_break_response(
        self,
        mock_pool,
        mock_get_config,
        mock_assembler_cls,
        mock_persist,
        client,
    ):
        mock_pool_value = MagicMock()
        mock_get_config.return_value = _full_config()
        mock_assembler_cls.return_value = _stub_assembler(
            building_source="meter", schedules_present=True
        )
        mock_persist.side_effect = RuntimeError("DB down")

        depot_id = str(uuid4())
        with patch("src.api.main.db_pools", mock_pool_value):
            response = client.get(
                f"/depots/{depot_id}/optimization/readiness?persist=true"
            )

        assert response.status_code == http_status.HTTP_200_OK
        body = response.json()
        # Pre-flight persistence is best-effort; readiness still returned.
        assert body["status"] == "ready"
        assert body["snapshot_id"] is None
