"""Tests for optimization readiness validation and input snapshot storage.

Covers:
    1. Complete readiness — all inputs present → status='ready'.
    2. Missing building load (forecast fallback) → status='degraded'.
    3. Missing schedules → status='not_ready'.
    4. Missing charger access matrix → status='not_ready'.
    5. Snapshot replay payload integrity — every required key present after
       round-trip serialisation.

The tests are pure-Python (no DB) — they exercise the readiness +
snapshot helpers directly. The controller integration is tested in
``test_controller_snapshot_integration.py``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from src.core.models import DepotConfig, DepotState, OptimizationInputSnapshot
from src.core.state.readiness import (
    BUILDING_LOAD_FORECAST,
    BUILDING_LOAD_METER,
    BUILDING_LOAD_STATIC,
    build_snapshot,
    evaluate_readiness,
    replay_payload,
    snapshot_to_payload,
)

# ── Fixtures ────────────────────────────────────────────────────────────────


def _full_config(*, with_access: bool = True) -> DepotConfig:
    vehicle_ids = ["bus_1", "bus_2"]
    access = {"charger_a": {"bus_1", "bus_2"}, "charger_b": {"bus_1"}} if with_access else {}
    return DepotConfig(
        vehicle_capacities={vid: 324.0 for vid in vehicle_ids},
        vehicle_max_charge_kw={vid: 80.0 for vid in vehicle_ids},
        charger_groups={80.0: 4},
        charger_efficiency=0.95,
        charger_vehicle_access=access,
        battery_capacity=500.0,
        battery_power=100.0,
        max_site_power=800.0,
    )


def _full_state(*, building_power_n: int = 96) -> DepotState:
    return DepotState(
        vehicle_socs={"bus_1": 0.45, "bus_2": 0.82},
        battery_soc=0.55,
        prices=[0.10, 0.15, 0.12] * 32,
        demand_charge_rate=20.0,
        current_month_peak=380.0,
        vehicle_availability={
            "bus_1": [True] * 96,
            "bus_2": [True] * 96,
        },
        energy_requirements={"bus_1": 200.0, "bus_2": 150.0},
        departure_times={"bus_1": 48, "bus_2": 60},
        building_power=[50.0] * building_power_n,
    )


def _horizon():
    start = datetime(2026, 4, 29, 12, 0, 0)
    end = start + timedelta(hours=24)
    return start, end


def _build_full_snapshot(
    *,
    config: DepotConfig | None = None,
    state: DepotState | None = None,
    schedules: list[dict] | None = None,
    building_source: str = BUILDING_LOAD_METER,
    schedules_present: bool = True,
) -> OptimizationInputSnapshot:
    config = config or _full_config()
    state = state or _full_state()
    schedules = (
        schedules
        if schedules is not None
        else [
            {
                "vehicle_id": "bus_1",
                "departure_time": datetime(2026, 4, 29, 18, 0, 0),
                "return_time": datetime(2026, 4, 30, 6, 0, 0),
                "estimated_energy_kwh": 200.0,
            }
        ]
    )
    readiness = evaluate_readiness(
        config,
        state,
        building_load_source=building_source,
        schedules_present=schedules_present,
    )
    start, end = _horizon()
    return build_snapshot(
        depot_id=uuid4(),
        organization_id=uuid4(),
        config=config,
        state=state,
        horizon_start=start,
        horizon_end=end,
        schedules=schedules,
        weather_features=[{"time": "2026-04-29T12:00:00", "temp_f": 65.0, "solar_rad": 800.0}],
        readiness=readiness,
    )


# ── 1. Complete readiness ───────────────────────────────────────────────────


class TestCompleteReadiness:
    def test_all_inputs_present_yields_ready(self):
        readiness = evaluate_readiness(
            _full_config(),
            _full_state(),
            building_load_source=BUILDING_LOAD_METER,
            schedules_present=True,
        )
        assert readiness.status == "ready"
        assert readiness.is_ready is True
        assert readiness.is_degraded is False
        assert readiness.is_blocking is False
        assert readiness.missing_inputs == []
        assert readiness.degraded_reasons == []
        assert readiness.assumptions == {}
        assert readiness.building_load_source == BUILDING_LOAD_METER

    def test_snapshot_records_organization_and_horizon(self):
        snap = _build_full_snapshot()
        assert snap.organization_id is not None
        assert snap.depot["depot_id"] == str(snap.depot_id)
        assert snap.horizon_end > snap.horizon_start
        assert snap.readiness.status == "ready"


# ── 2. Missing building load (degraded mode) ────────────────────────────────


class TestMissingBuildingLoad:
    def test_forecast_fallback_marks_run_degraded(self):
        readiness = evaluate_readiness(
            _full_config(),
            _full_state(),
            building_load_source=BUILDING_LOAD_FORECAST,
            schedules_present=True,
        )
        assert readiness.status == "degraded"
        assert readiness.is_degraded is True
        assert readiness.is_blocking is False
        assert "building_load_meter_unavailable" in readiness.degraded_reasons
        # The forecast substitution must be recorded as an explicit
        # assumption so it survives into the snapshot payload.
        assert readiness.assumptions["building_load"]["source"] == BUILDING_LOAD_FORECAST
        assert readiness.assumptions["building_load"]["note"]
        assert readiness.building_load_source == BUILDING_LOAD_FORECAST

    def test_absent_building_load_is_non_blocking(self):
        """Post-PR #119: absent building load degrades but never blocks.

        Building load is treated as forecast-fallback territory now —
        ``missing_inputs`` must never contain ``"building_load"`` and the
        run must not be marked ``not_ready`` on this basis alone.
        """
        readiness = evaluate_readiness(
            _full_config(),
            _full_state(building_power_n=0),
            building_load_source="absent",
            schedules_present=True,
        )
        assert readiness.status == "degraded"
        assert "building_load" not in readiness.missing_inputs
        assert "building_load_meter_unavailable" in readiness.degraded_reasons

    def test_snapshot_payload_records_forecast_source(self):
        snap = _build_full_snapshot(building_source=BUILDING_LOAD_FORECAST)
        payload = snapshot_to_payload(snap)
        assert payload["readiness"]["status"] == "degraded"
        assert payload["readiness"]["building_load_source"] == BUILDING_LOAD_FORECAST
        assert payload["building_load"]["source"] == BUILDING_LOAD_FORECAST
        # Building load values are still present so replay is possible.
        assert len(payload["building_load"]["values_kw"]) == 96


# ── 3. Missing schedules ────────────────────────────────────────────────────


class TestMissingSchedules:
    def test_no_schedules_blocks_optimization(self):
        readiness = evaluate_readiness(
            _full_config(),
            _full_state(),
            building_load_source=BUILDING_LOAD_METER,
            schedules_present=False,
        )
        assert readiness.status == "not_ready"
        assert readiness.is_blocking is True
        assert "schedules" in readiness.missing_inputs

    def test_blocked_snapshot_still_serialises(self):
        snap = _build_full_snapshot(schedules=[], schedules_present=False)
        payload = snapshot_to_payload(snap)
        # not_ready snapshots must still round-trip — they're the most
        # useful artefact for debugging "why didn't we run?".
        assert payload["readiness"]["status"] == "not_ready"
        assert "schedules" in payload["readiness"]["missing_inputs"]
        assert payload["schedules"] == []


# ── 4. Missing charger access matrix ────────────────────────────────────────


class TestMissingChargerAccess:
    def test_empty_access_matrix_blocks_optimization(self):
        config = _full_config(with_access=False)
        readiness = evaluate_readiness(
            config,
            _full_state(),
            building_load_source=BUILDING_LOAD_METER,
            schedules_present=True,
        )
        assert readiness.status == "not_ready"
        assert "charger_vehicle_access" in readiness.missing_inputs

    def test_all_to_all_mode_ready_with_empty_matrix(self):
        """Migration 021: depots in all_to_all mode skip the matrix
        prerequisite — readiness must NOT flag charger_vehicle_access as
        missing even when the stored matrix is empty.
        """
        config = _full_config(with_access=False)
        config.charger_vehicle_access_default = "all_to_all"
        readiness = evaluate_readiness(
            config,
            _full_state(),
            building_load_source=BUILDING_LOAD_METER,
            schedules_present=True,
        )
        assert "charger_vehicle_access" not in readiness.missing_inputs
        assert readiness.status == "ready"

    def test_explicit_matrix_with_empty_matrix_is_not_ready(self):
        """Inverse of the above: explicit_matrix mode with no rows still
        blocks (default behaviour, regression guard for migration 021).
        """
        config = _full_config(with_access=False)
        config.charger_vehicle_access_default = "explicit_matrix"
        readiness = evaluate_readiness(
            config,
            _full_state(),
            building_load_source=BUILDING_LOAD_METER,
            schedules_present=True,
        )
        assert readiness.status == "not_ready"
        assert "charger_vehicle_access" in readiness.missing_inputs

    def test_snapshot_records_access_matrix_when_present(self):
        snap = _build_full_snapshot()
        payload = snapshot_to_payload(snap)
        access = payload["charger_vehicle_access"]
        # Snapshot envelope (migration 021): {"mode", "matrix"} so the
        # mode flag is preserved through replay.
        assert access["mode"] == "explicit_matrix"
        assert access["matrix"]["charger_a"] == ["bus_1", "bus_2"]
        assert access["matrix"]["charger_b"] == ["bus_1"]

    def test_snapshot_records_all_to_all_mode_with_empty_matrix(self):
        """In all_to_all mode the snapshot persists only the mode flag —
        the matrix is synthesized at solve time so freezing the current
        roster into the snapshot would be misleading on replay.
        """
        config = _full_config()
        config.charger_vehicle_access_default = "all_to_all"
        snap = _build_full_snapshot(config=config)
        payload = snapshot_to_payload(snap)
        access = payload["charger_vehicle_access"]
        assert access["mode"] == "all_to_all"
        assert access["matrix"] == {}

    def test_no_chargers_at_all_blocks(self):
        config = _full_config()
        config.charger_groups = {}
        readiness = evaluate_readiness(
            config,
            _full_state(),
            building_load_source=BUILDING_LOAD_METER,
            schedules_present=True,
        )
        assert readiness.status == "not_ready"
        assert "chargers" in readiness.missing_inputs


# ── 5. Snapshot replay payload integrity ────────────────────────────────────


class TestSnapshotReplayPayloadIntegrity:
    def test_payload_contains_every_required_section(self):
        snap = _build_full_snapshot()
        payload = snapshot_to_payload(snap)
        # The replay helper is the contract — if it accepts the payload,
        # downstream replay tooling can rely on those keys.
        assert replay_payload(payload) is payload

    def test_payload_round_trips_through_json(self):
        snap = _build_full_snapshot()
        payload = snapshot_to_payload(snap)
        # JSONB will encode then decode this; replay must still pass.
        round_tripped = json.loads(json.dumps(payload, default=str))
        assert replay_payload(round_tripped) is round_tripped
        assert round_tripped["schema"] == "v1"
        assert round_tripped["organization_id"] == str(snap.organization_id)
        assert round_tripped["horizon"]["start"] == snap.horizon_start.isoformat()
        assert round_tripped["horizon"]["end"] == snap.horizon_end.isoformat()
        # Vehicles, prices, telemetry retained verbatim.
        vehicle_ids = {v["vehicle_id"] for v in round_tripped["vehicles"]}
        assert vehicle_ids == {"bus_1", "bus_2"}
        assert round_tripped["telemetry"] == {"bus_1": 0.45, "bus_2": 0.82}
        assert len(round_tripped["prices"]) == len(snap.prices)
        # Weather features preserved end-to-end.
        assert round_tripped["weather_features"][0]["temp_f"] == 65.0

    def test_payload_missing_section_raises(self):
        snap = _build_full_snapshot()
        payload = snapshot_to_payload(snap)
        # Drop a required section — replay must raise rather than silently
        # accept a corrupted/partial payload.
        del payload["schedules"]
        with pytest.raises(ValueError, match="missing required keys"):
            replay_payload(payload)

    def test_payload_missing_building_load_source_raises(self):
        snap = _build_full_snapshot()
        payload = snapshot_to_payload(snap)
        del payload["readiness"]["building_load_source"]
        with pytest.raises(ValueError, match="building_load_source"):
            replay_payload(payload)

    def test_charger_groups_keys_are_strings_for_jsonb(self):
        # Pyomo / DepotConfig keys charger_groups by float. JSONB requires
        # string keys, so the snapshot must coerce them.
        snap = _build_full_snapshot()
        payload = snapshot_to_payload(snap)
        json.dumps(payload)  # must not raise
        assert all(isinstance(k, str) for k in payload["chargers"]["groups"].keys())
        assert payload["chargers"]["total"] == 4


# ── 6. Static-assumption building load (HRX day-one) ────────────────────────


class TestStaticAssumptionBuildingLoad:
    def test_static_assumption_marks_degraded_with_recorded_value(self):
        config = _full_config()
        config.building_load_assumption_kw = 30.0
        readiness = evaluate_readiness(
            config,
            _full_state(),
            building_load_source=BUILDING_LOAD_STATIC,
            schedules_present=True,
        )
        assert readiness.status == "degraded"
        assert "building_load_static_assumption" in readiness.degraded_reasons
        assumption = readiness.assumptions["building_load"]
        assert assumption["source"] == BUILDING_LOAD_STATIC
        assert assumption["value_kw"] == 30.0
        assert "max_grid_kw" in assumption["note"]

    def test_static_assumption_payload_records_assumption_kw(self):
        config = _full_config()
        config.building_load_assumption_kw = 30.0
        snap = _build_full_snapshot(config=config, building_source=BUILDING_LOAD_STATIC)
        payload = snapshot_to_payload(snap)
        assert payload["readiness"]["status"] == "degraded"
        assert payload["building_load"]["source"] == BUILDING_LOAD_STATIC
        assert payload["building_load"]["assumption_kw"] == 30.0
        # Depot section also exposes the assumption for replay tools.
        assert payload["depot"]["building_load_assumption_kw"] == 30.0


# ── 7. Replay length-consistency validation (issue 9) ───────────────────────


class TestReplayLengthValidation:
    def test_payload_uses_state_timestep_count_when_horizon_varies(self):
        state = _full_state(building_power_n=48)
        state.prices = [0.10, 0.15, 0.12] * 16
        snap = _build_full_snapshot(state=state)
        payload = snapshot_to_payload(snap)
        assert payload["depot"]["n_timesteps"] == 48
        assert replay_payload(payload) is payload

    def test_prices_length_mismatch_raises(self):
        snap = _build_full_snapshot()
        payload = snapshot_to_payload(snap)
        payload["prices"] = payload["prices"][:10]  # truncate
        with pytest.raises(ValueError, match="prices"):
            replay_payload(payload)

    def test_building_load_values_kw_length_mismatch_raises(self):
        snap = _build_full_snapshot()
        payload = snapshot_to_payload(snap)
        payload["building_load"]["values_kw"] = [0.0, 1.0]
        with pytest.raises(ValueError, match="building_load"):
            replay_payload(payload)

    def test_versions_section_required(self):
        snap = _build_full_snapshot()
        payload = snapshot_to_payload(snap)
        del payload["versions"]
        with pytest.raises(ValueError, match="missing required keys"):
            replay_payload(payload)

    def test_individual_version_field_required(self):
        snap = _build_full_snapshot()
        payload = snapshot_to_payload(snap)
        del payload["versions"]["solver"]
        with pytest.raises(ValueError, match="versions.solver"):
            replay_payload(payload)


# ── 8. Version metadata + recent telemetry (migration 020) ──────────────────


class TestVersionMetadataAndTelemetry:
    def test_snapshot_carries_version_fields(self):
        snap = _build_full_snapshot()
        # code/solver may be None in the test sandbox (no git, no solver
        # installed) but surrogate is a hard-coded constant.
        assert snap.surrogate_model_version is not None
        # Code version is always at least the package version.
        # We don't assert format — only that the field is populated.

    def test_payload_versions_section(self):
        snap = _build_full_snapshot()
        payload = snapshot_to_payload(snap)
        versions = payload["versions"]
        assert "code" in versions
        assert "solver" in versions
        assert "surrogate_model" in versions
        assert versions["surrogate_model"] == snap.surrogate_model_version

    def test_recent_telemetry_round_trips(self):
        config = _full_config()
        state = _full_state()
        readiness = evaluate_readiness(
            config,
            state,
            building_load_source=BUILDING_LOAD_METER,
            schedules_present=True,
        )
        start, end = _horizon()
        recent = [
            {
                "time": "2026-04-29T11:30:00",
                "vehicle_id": "bus_1",
                "soc": 0.42,
                "charging_kw": 64.0,
                "is_plugged": True,
            }
        ]
        snap = build_snapshot(
            depot_id=uuid4(),
            organization_id=uuid4(),
            config=config,
            state=state,
            horizon_start=start,
            horizon_end=end,
            schedules=[
                {
                    "vehicle_id": "bus_1",
                    "departure_time": datetime(2026, 4, 29, 18, 0, 0),
                    "return_time": datetime(2026, 4, 30, 6, 0, 0),
                    "estimated_energy_kwh": 200.0,
                }
            ],
            recent_telemetry=recent,
            readiness=readiness,
        )
        payload = snapshot_to_payload(snap)
        round_tripped = json.loads(json.dumps(payload, default=str))
        assert replay_payload(round_tripped) is round_tripped
        assert round_tripped["recent_telemetry"][0]["vehicle_id"] == "bus_1"
        assert round_tripped["recent_telemetry"][0]["charging_kw"] == 64.0


# ── 9. TZ-aware captured_at (issue 3) ───────────────────────────────────────


class TestTimezoneAwareCapturedAt:
    def test_captured_at_is_tz_aware(self):
        snap = _build_full_snapshot()
        assert snap.captured_at.tzinfo is not None
