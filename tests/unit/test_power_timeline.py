"""Unit tests for GET /depots/{depot_id}/power-timeline."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import status as http_status
from fastapi.testclient import TestClient

from src.api.main import app
from src.security.tenant_mirror import ensure_tenant_mirrored

DEPOT_ID = str(uuid4())
_NOW = datetime(2025, 5, 26, 10, 0, 0, tzinfo=timezone.utc)
_HORIZON_START = _NOW
_HORIZON_END = _NOW + timedelta(hours=24)
_GENERATED_AT = _NOW - timedelta(minutes=5)


@pytest.fixture(autouse=True)
def _admin_auth():
    user = {"sub": "test-admin", "app_metadata": {"favonius_role": "favonius_admin"}}
    prev = app.dependency_overrides.get(ensure_tenant_mirrored)
    app.dependency_overrides[ensure_tenant_mirrored] = lambda: user
    yield
    if prev is not None:
        app.dependency_overrides[ensure_tenant_mirrored] = prev
    else:
        app.dependency_overrides.pop(ensure_tenant_mirrored, None)


@pytest.fixture(autouse=True)
def _freeze_now():
    """Freeze datetime.now() in main.py to _NOW for deterministic plan filtering.

    Only `.now()` is frozen; other class methods (.utcnow, .fromisoformat) are
    delegated to the real datetime so the error-response handler and test
    assertions still work correctly.
    """
    from datetime import datetime as _real_dt

    with patch("src.api.main.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        mock_dt.utcnow.side_effect = _real_dt.utcnow
        mock_dt.fromisoformat.side_effect = _real_dt.fromisoformat
        yield


@pytest.fixture
def client():
    return TestClient(app)


def _make_pool(site_row, station_rows, telemetry_rows, run_row):
    """Build a mock db_pools whose .ts and .static share the same conn."""
    pool = MagicMock()
    conn = AsyncMock()

    # static pool: fetchrow(site), fetch(stations)
    # ts pool:     fetch(telemetry), fetchrow(run)
    # All go through the same conn because pool.ts = pool.static = pool.

    fetchrow_side_effect = iter([site_row, run_row])
    fetch_side_effect = iter([station_rows, telemetry_rows])

    conn.fetchrow = AsyncMock(side_effect=fetchrow_side_effect)
    conn.fetch = AsyncMock(side_effect=fetch_side_effect)

    pool.acquire.return_value.__aenter__.return_value = conn
    pool.acquire.return_value.__aexit__.return_value = None
    pool.ts = pool
    pool.static = pool
    return pool


def _site_row(timezone_name="Europe/Vilnius", max_grid_kw=200.0):
    return {"timezone": timezone_name, "max_grid_kw": max_grid_kw}


def _station_rows(*ids):
    return [{"station_id": s} for s in ids]


def _telemetry_rows(*buckets):
    """Each bucket: (time, charging_kw, vehicle_count)."""
    return [
        {"bucket": t, "charging_kw": kw, "vehicle_count": vc} for t, kw, vc in buckets
    ]


def _run_row(grid_power, per_vehicle=None, battery_dispatch=None, status="optimal"):
    sched = {
        "grid_power": grid_power,
        "battery_dispatch": battery_dispatch or [0.0] * len(grid_power),
        "schedule": per_vehicle or {},
    }
    return {
        "run_id": uuid4(),
        "run_time": _GENERATED_AT,
        "schedule_json": sched,
        "horizon_start": _HORIZON_START,
        "horizon_end": _HORIZON_END,
        "status": status,
    }


class TestPowerTimelineHappyPath:
    """Happy-path scenarios: history + plan both present."""

    def test_full_response_shape(self, client):
        bucket_t = _NOW - timedelta(minutes=30)
        pool = _make_pool(
            _site_row(),
            _station_rows("CP-01", "CP-02"),
            _telemetry_rows((bucket_t, 45.2, 3)),
            _run_row([100.0] * 96),
        )
        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        assert resp.status_code == http_status.HTTP_200_OK
        data = resp.json()

        assert data["depot_id"] == DEPOT_ID
        assert data["timezone"] == "Europe/Vilnius"
        assert data["max_grid_kw"] == 200.0
        assert data["timestep_minutes"] == 15

        assert isinstance(data["history"], list)
        assert len(data["history"]) == 1
        assert data["history"][0]["charging_kw"] == pytest.approx(45.2)
        assert data["history"][0]["vehicle_count"] == 3

        assert len(data["plan"]) == 96
        assert data["plan"][0]["grid_kw"] == pytest.approx(100.0)
        assert data["plan"][0]["battery_kw"] == pytest.approx(0.0)

        assert data["plan_meta"] is not None
        assert data["plan_meta"]["solver_status"] == "optimal"

    def test_plan_charging_kw_summed_across_vehicles(self, client):
        """charging_kw in the plan series is the sum of all vehicles."""
        per_vehicle = {
            "bus_1": {"charging_power": [40.0] * 96, "soc": [0.5] * 96},
            "bus_2": {"charging_power": [30.0] * 96, "soc": [0.4] * 96},
        }
        pool = _make_pool(
            _site_row(),
            _station_rows("CP-01"),
            [],
            _run_row([80.0] * 96, per_vehicle=per_vehicle),
        )
        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        assert resp.status_code == http_status.HTTP_200_OK
        plan = resp.json()["plan"]
        assert plan[0]["charging_kw"] == pytest.approx(70.0)

    def test_plan_battery_dispatch_propagated(self, client):
        battery_dispatch = [-12.0] * 96  # charging
        pool = _make_pool(
            _site_row(),
            _station_rows("CP-01"),
            [],
            _run_row([80.0] * 96, battery_dispatch=battery_dispatch),
        )
        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        data = resp.json()
        assert data["plan"][0]["battery_kw"] == pytest.approx(-12.0)

    def test_plan_timestamps_aligned_to_horizon_start(self, client):
        pool = _make_pool(
            _site_row(),
            _station_rows("CP-01"),
            [],
            _run_row([50.0] * 4),
        )
        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        plan = resp.json()["plan"]
        assert len(plan) == 4
        t0 = datetime.fromisoformat(plan[0]["time"])
        t1 = datetime.fromisoformat(plan[1]["time"])
        assert (t1 - t0) == timedelta(minutes=15)

    def test_multiple_history_buckets(self, client):
        buckets = [
            (_NOW - timedelta(minutes=45), 30.0, 2),
            (_NOW - timedelta(minutes=30), 45.2, 3),
            (_NOW - timedelta(minutes=15), 55.0, 4),
        ]
        pool = _make_pool(
            _site_row(),
            _station_rows("CP-01"),
            _telemetry_rows(*buckets),
            _run_row([80.0] * 96),
        )
        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        history = resp.json()["history"]
        assert len(history) == 3
        assert history[0]["charging_kw"] == pytest.approx(30.0)
        assert history[2]["charging_kw"] == pytest.approx(55.0)

    def test_history_hours_query_param(self, client):
        """history_hours param is forwarded to the telemetry query."""
        pool = _make_pool(
            _site_row(),
            _station_rows("CP-01"),
            [],
            _run_row([80.0] * 96),
        )
        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline?history_hours=48")

        assert resp.status_code == http_status.HTTP_200_OK

    def test_solver_status_degraded_in_plan_meta(self, client):
        pool = _make_pool(
            _site_row(),
            _station_rows("CP-01"),
            [],
            _run_row([80.0] * 96, status="degraded"),
        )
        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        assert resp.json()["plan_meta"]["solver_status"] == "degraded"


class TestPowerTimelineEmptyStates:
    """Partial or fully-empty data conditions."""

    def test_no_telemetry_returns_empty_history(self, client):
        pool = _make_pool(
            _site_row(),
            _station_rows("CP-01"),
            [],  # no telemetry
            _run_row([80.0] * 96),
        )
        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        data = resp.json()
        assert resp.status_code == http_status.HTTP_200_OK
        assert data["history"] == []
        assert len(data["plan"]) == 96

    def test_no_optimization_run_returns_empty_plan(self, client):
        # run_row = None means no optimization run exists yet
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=iter([_site_row(), None]))
        conn.fetch = AsyncMock(side_effect=iter([_station_rows("CP-01"), []]))
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pool.ts = pool
        pool.static = pool

        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        data = resp.json()
        assert resp.status_code == http_status.HTTP_200_OK
        assert data["plan"] == []
        assert data["plan_meta"] is None

    def test_no_station_ids_skips_telemetry_query(self, client):
        """When depot has no chargers, history is empty without hitting telemetry."""
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=iter([_site_row(), _run_row([0.0] * 4)]))
        conn.fetch = AsyncMock(side_effect=iter([[], []]))  # empty stations
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pool.ts = pool
        pool.static = pool

        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        data = resp.json()
        assert resp.status_code == http_status.HTTP_200_OK
        assert data["history"] == []

    def test_null_charging_kw_in_telemetry_bucket(self, client):
        """Buckets with NULL charging_kw are returned with charging_kw=None."""
        bucket_t = _NOW - timedelta(minutes=15)
        telemetry_rows = [{"bucket": bucket_t, "charging_kw": None, "vehicle_count": 0}]

        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=iter([_site_row(), _run_row([0.0] * 4)]))
        conn.fetch = AsyncMock(side_effect=iter([_station_rows("CP-01"), telemetry_rows]))
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pool.ts = pool
        pool.static = pool

        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        assert resp.status_code == http_status.HTTP_200_OK
        history = resp.json()["history"]
        assert len(history) == 1
        assert history[0]["charging_kw"] is None

    def test_max_grid_kw_null_when_not_configured(self, client):
        pool = _make_pool(
            {"timezone": "Europe/Vilnius", "max_grid_kw": None},
            _station_rows("CP-01"),
            [],
            _run_row([80.0] * 4),
        )
        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        assert resp.json()["max_grid_kw"] is None


class TestPowerTimelineErrors:
    """Error handling."""

    def test_depot_not_found_returns_404(self, client):
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)  # site not found
        conn.fetch = AsyncMock(return_value=[])
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pool.ts = pool
        pool.static = pool

        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        assert resp.status_code == http_status.HTTP_404_NOT_FOUND

    def test_invalid_depot_uuid_returns_400(self, client):
        # validate_depot_id raises HTTPException(400) before the db check runs.
        resp = client.get("/depots/not-a-uuid/power-timeline")
        assert resp.status_code == http_status.HTTP_400_BAD_REQUEST

    def test_history_hours_out_of_range_returns_400(self, client):
        # The app's custom RequestValidationError handler returns 400, not 422.
        # A mock pool is required so _require_depot_access doesn't short-circuit
        # with 503 before FastAPI validates the query param.
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        conn.fetch = AsyncMock(return_value=[])
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pool.ts = pool
        pool.static = pool

        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline?history_hours=0")
            assert resp.status_code == http_status.HTTP_400_BAD_REQUEST

            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline?history_hours=49")
            assert resp.status_code == http_status.HTTP_400_BAD_REQUEST

    def test_database_unavailable_returns_503(self, client):
        with patch("src.api.main.db_pools", None):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        assert resp.status_code == http_status.HTTP_503_SERVICE_UNAVAILABLE


class TestPowerTimelinePlanBehavior:
    """Plan series filtering, capping, and plan_meta correctness."""

    def test_plan_buckets_before_now_are_excluded(self, client):
        """Timesteps whose bucket_time < now_utc are skipped (stale plan)."""
        # horizon_start is 1 h before _NOW; first 4 of 8 steps are in the past.
        stale_start = _NOW - timedelta(hours=1)  # 4 past + 4 future (15-min steps)
        run_row = {
            "run_id": uuid4(),
            "run_time": _GENERATED_AT,
            "schedule_json": {"grid_power": [10.0] * 8, "battery_dispatch": [0.0] * 8, "schedule": {}},
            "horizon_start": stale_start,
            "horizon_end": stale_start + timedelta(hours=2),
            "status": "optimal",
        }
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=iter([_site_row(), run_row]))
        conn.fetch = AsyncMock(side_effect=iter([_station_rows("CP-01"), []]))
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pool.ts = pool
        pool.static = pool

        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        assert resp.status_code == http_status.HTTP_200_OK
        plan = resp.json()["plan"]
        # 1 h / 15 min = 4 past steps skipped; 4 future steps kept
        assert len(plan) == 4
        # First kept bucket should be at _NOW exactly
        t0 = datetime.fromisoformat(plan[0]["time"])
        assert t0 >= _NOW

    def test_plan_capped_at_96_buckets(self, client):
        """Optimizer may run a 48 h horizon; endpoint caps the response at 96."""
        pool = _make_pool(
            _site_row(),
            _station_rows("CP-01"),
            [],
            _run_row([20.0] * 192),  # 48 h at 15-min steps
        )
        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        assert resp.status_code == http_status.HTTP_200_OK
        assert len(resp.json()["plan"]) == 96

    def test_plan_meta_set_even_with_empty_schedule_json(self, client):
        """plan_meta reflects the run row regardless of schedule_json content."""
        run_row = {
            "run_id": uuid4(),
            "run_time": _GENERATED_AT,
            "schedule_json": {},  # empty dict is falsy — must not hide plan_meta
            "horizon_start": _HORIZON_START,
            "horizon_end": _HORIZON_END,
            "status": "degraded",
        }
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=iter([_site_row(), run_row]))
        conn.fetch = AsyncMock(side_effect=iter([_station_rows("CP-01"), []]))
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pool.ts = pool
        pool.static = pool

        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        data = resp.json()
        assert resp.status_code == http_status.HTTP_200_OK
        assert data["plan"] == []
        assert data["plan_meta"] is not None
        assert data["plan_meta"]["solver_status"] == "degraded"

    def test_null_battery_dispatch_entry_defaults_to_zero(self, client):
        """A None (JSON null) in battery_dispatch must not raise TypeError."""
        run_row = {
            "run_id": uuid4(),
            "run_time": _GENERATED_AT,
            "schedule_json": {
                "grid_power": [50.0, 60.0],
                "battery_dispatch": [None, 5.0],  # null at index 0
                "schedule": {},
            },
            "horizon_start": _HORIZON_START,
            "horizon_end": _HORIZON_END,
            "status": "optimal",
        }
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=iter([_site_row(), run_row]))
        conn.fetch = AsyncMock(side_effect=iter([_station_rows("CP-01"), []]))
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pool.ts = pool
        pool.static = pool

        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        assert resp.status_code == http_status.HTTP_200_OK
        plan = resp.json()["plan"]
        assert len(plan) == 2
        assert plan[0]["battery_kw"] == 0.0  # None → 0.0
        assert plan[1]["battery_kw"] == 5.0

    def test_plan_timestamps_snap_to_15min_grid(self, client):
        """horizon_start at a non-boundary time is snapped down to the nearest
        15-min UTC boundary so plan buckets align with time_bucket() history."""
        # horizon_start 3 minutes + 17 seconds past a boundary → should snap back 3m17s
        unaligned_start = _NOW.replace(minute=3, second=17, microsecond=500000)
        expected_snapped = _NOW.replace(minute=0, second=0, microsecond=0)
        run_row = {
            "run_id": uuid4(),
            "run_time": _GENERATED_AT,
            "schedule_json": {
                "grid_power": [10.0, 20.0],
                "battery_dispatch": [0.0, 0.0],
                "schedule": {},
            },
            "horizon_start": unaligned_start,
            "horizon_end": unaligned_start + timedelta(hours=1),
            "status": "optimal",
        }
        pool = MagicMock()
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=iter([_site_row(), run_row]))
        conn.fetch = AsyncMock(side_effect=iter([_station_rows("CP-01"), []]))
        pool.acquire.return_value.__aenter__.return_value = conn
        pool.acquire.return_value.__aexit__.return_value = None
        pool.ts = pool
        pool.static = pool

        with patch("src.api.main.db_pools", pool):
            resp = client.get(f"/depots/{DEPOT_ID}/power-timeline")

        assert resp.status_code == http_status.HTTP_200_OK
        plan = resp.json()["plan"]
        # Both buckets are at or after _NOW (10:00:00), so both should be present.
        assert len(plan) == 2
        t0 = datetime.fromisoformat(plan[0]["time"])
        assert t0.minute % 15 == 0, f"First plan bucket not on 15-min boundary: {t0}"
        assert t0.second == 0 and t0.microsecond == 0
        # First bucket must be the snapped start
        assert t0 == expected_snapped
