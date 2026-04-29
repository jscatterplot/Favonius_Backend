"""Unit tests for the insert-only weather snapshot pipeline (migration 021).

Covers the behavioural contract introduced by
``migrations/021_weather_insert_only_snapshots.sql``:

* Ingestion writes a fresh row on every fetch (does not overwrite).
* Duplicate fetches with the *exact* same
  ``(depot, source, fetched_at, forecast_for)`` tuple are collapsed by
  the unique constraint (``ON CONFLICT DO NOTHING``).
* The assembler pins each snapshot to the forecast bundle current at
  ``horizon_start`` via
  ``fetched_at = (SELECT MAX(fetched_at) WHERE fetched_at <= start)``.
* Surrogate training queries against historical snapshots return the
  same feature set the optimization saw at run time (replay parity).
* ``optimization_input_snapshots.weather_forecast_id`` correctly
  points to the captured bundle when (and only when) weather features
  are non-empty.

Retention is exercised in the integration suite where a real
TimescaleDB hypertable is available; here we cover the SQL contract by
asserting the migration script declares the policy.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

pytest.importorskip("openmeteo_requests")

from src.adapters.weather import (
    DEFAULT_WEATHER_SOURCE,
    WeatherData,
    get_latest_forecast_bundle,
    store_weather_forecasts,
)
from src.core.models import DepotConfig, DepotState
from src.core.state.assembler import StateAssembler
from src.core.state.readiness import (
    ReadinessReport,
    build_snapshot,
    snapshot_to_payload,
)
from src.db.pools import DatabasePools


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_pool():
    """Mock asyncpg pool with a pre-wired connection."""
    pool = MagicMock()
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=cm)
    pool._mock_conn = conn
    return pool


@pytest.fixture
def mock_pools(mock_pool):
    return DatabasePools(static=mock_pool, ts=mock_pool)


@pytest.fixture
def sample_forecasts():
    base = datetime(2026, 4, 29, 0, 0, tzinfo=timezone.utc)
    return [
        WeatherData(
            timestamp=base + timedelta(days=d),
            temperature_f=70.0 + d,
            temperature_max_f=75.0 + d,
            temperature_min_f=65.0 + d,
            precipitation_inches=0.1 * d,
            solar_radiation=500.0 + d * 10,
        )
        for d in range(3)
    ]


@pytest.fixture
def depot_config():
    return DepotConfig(
        vehicle_capacities={"bus_1": 324.0},
        vehicle_max_charge_kw={"bus_1": 80.0},
        charger_groups={80.0: 1},
        charger_vehicle_access={"charger_a": {"bus_1"}},
        charger_efficiency=0.95,
        battery_capacity=500.0,
        battery_power=100.0,
        max_site_power=800.0,
        delta_t=0.25,
    )


@pytest.fixture
def depot_state():
    return DepotState(
        vehicle_socs={"bus_1": 0.6},
        battery_soc=0.5,
        prices=[0.10] * 96,
        demand_charge_rate=20.0,
        current_month_peak=0.0,
        vehicle_availability={"bus_1": [True] * 96},
        energy_requirements={"bus_1": 200.0},
        departure_times={"bus_1": 24},
        building_power=[20.0] * 96,
    )


# ─────────────────────────────────────────────────────────────────────
# Insert-only ingestion behaviour
# ─────────────────────────────────────────────────────────────────────


class TestInsertOnlyIngestion:
    """Every fetch must INSERT new rows; the SQL must never UPDATE."""

    @pytest.mark.asyncio
    async def test_each_fetch_inserts_new_rows(self, mock_pool, sample_forecasts):
        """Two back-to-back fetches → two distinct fetched_at values."""
        depot_id = uuid4()

        await store_weather_forecasts(mock_pool, sample_forecasts, depot_id)
        first_calls = list(mock_pool._mock_conn.execute.call_args_list)
        assert len(first_calls) == len(sample_forecasts)

        # Pin a later fetched_at explicitly so the assertion is robust
        # against clock granularity on fast hosts.
        later = datetime.now(timezone.utc) + timedelta(seconds=1)
        mock_pool._mock_conn.execute.reset_mock()
        await store_weather_forecasts(
            mock_pool, sample_forecasts, depot_id, fetched_at=later
        )
        second_calls = list(mock_pool._mock_conn.execute.call_args_list)
        assert len(second_calls) == len(sample_forecasts)

        first_fetched = {call[0][3] for call in first_calls}
        second_fetched = {call[0][3] for call in second_calls}
        # Each fetch is one bundle, so each call set has one fetched_at.
        assert len(first_fetched) == 1
        assert len(second_fetched) == 1
        assert next(iter(first_fetched)).tzinfo is timezone.utc
        assert next(iter(second_fetched)).tzinfo is timezone.utc
        # The two bundles are distinct → no overwrite.
        assert first_fetched != second_fetched

    @pytest.mark.asyncio
    async def test_sql_is_insert_only_no_update_clause(
        self, mock_pool, sample_forecasts
    ):
        depot_id = uuid4()
        await store_weather_forecasts(mock_pool, [sample_forecasts[0]], depot_id)
        sql = mock_pool._mock_conn.execute.call_args[0][0]
        assert "INSERT INTO weather_forecasts" in sql
        assert "ON CONFLICT" in sql
        assert "DO NOTHING" in sql
        # Crucial regression guard: no UPDATE in the ingestion SQL.
        assert "UPDATE" not in sql

    @pytest.mark.asyncio
    async def test_all_rows_in_one_call_share_fetched_at(
        self, mock_pool, sample_forecasts
    ):
        depot_id = uuid4()
        await store_weather_forecasts(mock_pool, sample_forecasts, depot_id)
        fetched_ats = {
            call[0][3]
            for call in mock_pool._mock_conn.execute.call_args_list
        }
        assert len(fetched_ats) == 1
        assert next(iter(fetched_ats)).tzinfo is timezone.utc


# ─────────────────────────────────────────────────────────────────────
# Duplicate-fetch dedup via the UNIQUE constraint
# ─────────────────────────────────────────────────────────────────────


class TestDuplicateFetchDedup:
    """If the same (depot, source, fetched_at, forecast_for) tuple
    arrives twice, the unique constraint collapses the duplicate.
    asyncpg's status string is ``'INSERT 0 0'`` for a no-op insert."""

    @pytest.mark.asyncio
    async def test_collapse_when_pinned_fetched_at_repeats(
        self, mock_pool, sample_forecasts
    ):
        depot_id = uuid4()
        pinned = datetime(2026, 4, 29, 8, 0, tzinfo=timezone.utc)

        # First write: rows insert (asyncpg returns 'INSERT 0 1').
        mock_pool._mock_conn.execute = AsyncMock(return_value="INSERT 0 1")
        first = await store_weather_forecasts(
            mock_pool, sample_forecasts, depot_id, fetched_at=pinned
        )
        assert first == len(sample_forecasts)

        # Second identical write: ON CONFLICT DO NOTHING collapses each
        # row → 'INSERT 0 0' → no rows reported as inserted.
        mock_pool._mock_conn.execute = AsyncMock(return_value="INSERT 0 0")
        second = await store_weather_forecasts(
            mock_pool, sample_forecasts, depot_id, fetched_at=pinned
        )
        assert second == 0

    @pytest.mark.asyncio
    async def test_unique_tuple_fields_are_depot_source_fetched_forecast_for(
        self, mock_pool, sample_forecasts
    ):
        depot_id = uuid4()
        await store_weather_forecasts(mock_pool, [sample_forecasts[0]], depot_id)
        sql = mock_pool._mock_conn.execute.call_args[0][0]
        assert "ON CONFLICT (depot_id, source, fetched_at, forecast_for)" in sql


# ─────────────────────────────────────────────────────────────────────
# Bundle pinning in the assembler
# ─────────────────────────────────────────────────────────────────────


class TestAssemblerPinsLatestBundle:
    """The assembler must filter on
    ``fetched_at = (SELECT MAX(fetched_at) WHERE fetched_at <= start)``
    so the snapshot uses the bundle current at horizon_start —
    *not* a bundle landing mid-assembly."""

    @pytest.mark.asyncio
    async def test_filter_pins_to_max_fetched_at_le_start(
        self, mock_pools, depot_config
    ):
        depot_id = str(uuid4())
        assembler = StateAssembler(mock_pools, depot_id, depot_config)

        bundle_id = uuid4()
        bundle_fetched_at = datetime(2026, 4, 29, 8, 0, tzinfo=timezone.utc)
        rows = [
            {
                "forecast_id": bundle_id,
                "forecast_for": datetime(
                    2026, 4, 29, 12, 0, tzinfo=timezone.utc
                ),
                "fetched_at": bundle_fetched_at,
                "temp_f": 70.0,
                "temp_max_f": 75.0,
                "temp_min_f": 65.0,
                "precip_in": 0.0,
                "solar_rad": 1000.0,
            }
        ]
        mock_pools.ts._mock_conn.fetch = AsyncMock(return_value=rows)

        start = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=24)
        features, forecast_id = await assembler._get_weather_features(start, end)

        assert forecast_id == bundle_id
        assert len(features) == 1

        # The query must pin the bundle by MAX(fetched_at) <= start.
        sent_sql = mock_pools.ts._mock_conn.fetch.call_args[0][0]
        assert "MAX(fetched_at)" in sent_sql
        assert "fetched_at <= $3" in sent_sql

    @pytest.mark.asyncio
    async def test_no_bundle_returns_empty_features_and_null_id(
        self, mock_pools, depot_config
    ):
        depot_id = str(uuid4())
        assembler = StateAssembler(mock_pools, depot_id, depot_config)
        mock_pools.ts._mock_conn.fetch = AsyncMock(return_value=[])

        features, forecast_id = await assembler._get_weather_features(
            datetime.utcnow(), datetime.utcnow() + timedelta(hours=24)
        )
        assert features == []
        assert forecast_id is None

    @pytest.mark.asyncio
    async def test_fetch_snapshot_extras_records_bundle_id(
        self, mock_pools, depot_config
    ):
        depot_id = str(uuid4())
        assembler = StateAssembler(mock_pools, depot_id, depot_config)

        bundle_id = uuid4()
        rows = [
            {
                "forecast_id": bundle_id,
                "forecast_for": datetime(
                    2026, 4, 29, 12, 0, tzinfo=timezone.utc
                ),
                "fetched_at": datetime(
                    2026, 4, 29, 8, 0, tzinfo=timezone.utc
                ),
                "temp_f": 70.0,
                "temp_max_f": 75.0,
                "temp_min_f": 65.0,
                "precip_in": 0.0,
                "solar_rad": 1000.0,
            }
        ]
        mock_pools.ts._mock_conn.fetch = AsyncMock(return_value=rows)
        mock_pools.static._mock_conn.fetchrow = AsyncMock(return_value=None)

        start = datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=24)
        await assembler.fetch_snapshot_extras(start, end)

        assert assembler.last_weather_features
        assert assembler.last_weather_forecast_id == bundle_id


# ─────────────────────────────────────────────────────────────────────
# Snapshot ↔ forecast FK linkage
# ─────────────────────────────────────────────────────────────────────


class TestSnapshotWeatherForecastIdLinkage:
    """``optimization_input_snapshots.weather_forecast_id`` must be
    populated when (and only when) the assembler captured weather
    features. This gives the surrogate model a single FK to load
    from when replaying a historical run."""

    def test_set_when_features_present(self, depot_config, depot_state):
        forecast_id = uuid4()
        readiness = ReadinessReport(status="ready", building_load_source="meter")
        snap = build_snapshot(
            depot_id=uuid4(),
            organization_id=uuid4(),
            config=depot_config,
            state=depot_state,
            horizon_start=datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
            horizon_end=datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
            schedules=[
                {
                    "vehicle_id": "bus_1",
                    "departure_time": "2026-04-29T18:00:00+00:00",
                    "return_time": "2026-04-30T08:00:00+00:00",
                }
            ],
            weather_features=[{"time": "2026-04-29T12:00:00", "temp_f": 70.0}],
            weather_forecast_id=forecast_id,
            readiness=readiness,
        )
        assert snap.weather_forecast_id == forecast_id

    def test_null_when_features_empty(self, depot_config, depot_state):
        """No features → no FK target. ON DELETE SET NULL doesn't help
        here; the column should never have been pointed at a bundle
        the snapshot didn't actually use."""
        forecast_id = uuid4()
        readiness = ReadinessReport(status="ready", building_load_source="meter")
        snap = build_snapshot(
            depot_id=uuid4(),
            organization_id=uuid4(),
            config=depot_config,
            state=depot_state,
            horizon_start=datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
            horizon_end=datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
            schedules=[
                {
                    "vehicle_id": "bus_1",
                    "departure_time": "2026-04-29T18:00:00+00:00",
                    "return_time": "2026-04-30T08:00:00+00:00",
                }
            ],
            weather_features=[],  # empty
            weather_forecast_id=forecast_id,  # caller passed something
            readiness=readiness,
        )
        # build_snapshot must drop the FK in this case.
        assert snap.weather_forecast_id is None

    def test_payload_serialises_forecast_id_as_string(self, depot_config, depot_state):
        forecast_id = uuid4()
        readiness = ReadinessReport(status="ready", building_load_source="meter")
        snap = build_snapshot(
            depot_id=uuid4(),
            organization_id=uuid4(),
            config=depot_config,
            state=depot_state,
            horizon_start=datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
            horizon_end=datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
            schedules=[
                {
                    "vehicle_id": "bus_1",
                    "departure_time": "2026-04-29T18:00:00+00:00",
                    "return_time": "2026-04-30T08:00:00+00:00",
                }
            ],
            weather_features=[{"time": "2026-04-29T12:00:00", "temp_f": 70.0}],
            weather_forecast_id=forecast_id,
            readiness=readiness,
        )
        payload = snapshot_to_payload(snap)
        assert payload["weather_forecast_id"] == str(forecast_id)

    def test_payload_forecast_id_is_null_when_unset(self, depot_config, depot_state):
        readiness = ReadinessReport(status="ready", building_load_source="meter")
        snap = build_snapshot(
            depot_id=uuid4(),
            organization_id=uuid4(),
            config=depot_config,
            state=depot_state,
            horizon_start=datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
            horizon_end=datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
            schedules=[
                {
                    "vehicle_id": "bus_1",
                    "departure_time": "2026-04-29T18:00:00+00:00",
                    "return_time": "2026-04-30T08:00:00+00:00",
                }
            ],
            weather_features=[],
            readiness=readiness,
        )
        payload = snapshot_to_payload(snap)
        assert payload["weather_forecast_id"] is None


# ─────────────────────────────────────────────────────────────────────
# Replay parity: training reads the same bundle the optimization saw
# ─────────────────────────────────────────────────────────────────────


class TestSurrogateReplayParity:
    """The surrogate training query must filter to MAX(fetched_at) <=
    departure_time, matching the assembler. If the training query
    drifts away from that contract, training will see a different
    feature set than the optimization saw — silently breaking model
    accuracy."""

    def test_training_query_pins_to_same_bundle_as_assembler(self):
        # Read the training module's source directly rather than
        # importing it — the surrogate package pulls in scikit-learn,
        # which we don't want as a hard requirement for this unit test.
        source = Path("src/core/surrogate/training.py").read_text()

        # Both clauses must be present, on the SAME query, otherwise
        # training and the assembler can disagree.
        assert "weather_forecasts" in source
        assert "MAX(fetched_at)" in source
        # The pin must be ``fetched_at <= s.departure_time`` — pinning
        # to NOW() would re-introduce the drift this migration fixes.
        assert re.search(
            r"fetched_at\s*<=\s*s\.departure_time", source
        ), "training must pin bundle to the schedule's departure_time"

    @pytest.mark.asyncio
    async def test_storage_helper_returns_same_bundle_view(
        self, mock_pool
    ):
        """The shared helper that both layers go through must always
        select rows with a single fetched_at value — that's the
        invariant 'one bundle = one snapshot' both layers depend on."""
        depot_id = uuid4()
        bundle_fetched_at = datetime(2026, 4, 29, 8, 0, tzinfo=timezone.utc)
        bundle_rows = [
            {
                "forecast_id": uuid4(),
                "forecast_for": bundle_fetched_at + timedelta(days=d),
                "fetched_at": bundle_fetched_at,
                "temp_f": 70.0 + d,
                "temp_max_f": 75.0 + d,
                "temp_min_f": 65.0 + d,
                "precip_in": 0.0,
                "solar_rad": 1000.0,
            }
            for d in range(3)
        ]
        mock_pool._mock_conn.fetchrow = AsyncMock(
            return_value={"max_fetched_at": bundle_fetched_at}
        )
        mock_pool._mock_conn.fetch = AsyncMock(return_value=bundle_rows)

        fetched_at, rows = await get_latest_forecast_bundle(mock_pool, depot_id)
        assert fetched_at == bundle_fetched_at
        assert {r["fetched_at"] for r in rows} == {bundle_fetched_at}


# ─────────────────────────────────────────────────────────────────────
# Migration script declares a 24-month retention policy
# ─────────────────────────────────────────────────────────────────────


class TestMigrationDeclaresRetention:
    """The integration suite exercises retention on a real Timescale
    instance. Here we keep a cheap unit-level guard so the policy
    doesn't silently disappear from the migration script."""

    def test_migration_021_declares_retention_and_chunk_interval(self):
        sql = Path("migrations/021_weather_insert_only_snapshots.sql").read_text()
        assert "create_hypertable" in sql
        assert "fetched_at" in sql
        assert "30 days" in sql
        assert "add_retention_policy" in sql
        assert "24 months" in sql

    def test_migration_021_declares_unique_tuple_constraint(self):
        sql = Path("migrations/021_weather_insert_only_snapshots.sql").read_text()
        # The natural-dedup key.
        assert (
            "UNIQUE (depot_id, source, fetched_at, forecast_for)" in sql
        )

    def test_migration_021_adds_weather_forecast_id_fk_to_snapshots(self):
        sql = Path("migrations/021_weather_insert_only_snapshots.sql").read_text()
        assert "weather_forecast_id" in sql
        assert (
            "REFERENCES weather_forecasts (forecast_id)" in sql
            or "REFERENCES weather_forecasts(forecast_id)" in sql
        )
        assert "ON DELETE SET NULL" in sql

    def test_migration_021_defines_required_columns(self):
        sql = Path("migrations/021_weather_insert_only_snapshots.sql").read_text()
        # Required columns from the spec.
        assert "forecast_id" in sql
        assert "forecast_for" in sql
        assert "source" in sql
        assert "fetched_at" in sql
        # Default source matches the storage helper.
        assert f"'{DEFAULT_WEATHER_SOURCE}'" in sql
