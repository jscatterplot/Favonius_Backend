"""Integration tests for ``scripts/onboard_depot_from_kempower.py``.

In-memory fakes for the static pool, the timescaledb pool, and the
``KempowerClient``. Drives the CLI's stage functions end-to-end and
asserts on the resulting state — same shape as
``tests/integration/test_charging_session_import_flow.py``.

What we cover here:

* Happy path: chargers / vehicles / access matrix / sessions all land.
* Cross-day re-run dedup: the second invocation adds zero new rows.
* Non-CCS connector → per-station skip + count surfaced.
* ``--apply-site-suggestions``: confirmed fields PATCH the depot row.
* Wrong-depot guard: ``_load_depot`` aborts on missing UUID.

A real-Postgres integration test would also catch SQL syntax errors in
the import-session INSERT — but the helper-level SQL we lean on
(``create_charger_with_credentials``, ``create_vehicle_identity``,
``upsert_charger_vehicle_access``) is exercised by the existing admin
endpoint tests, so this layer focuses on the orchestration we own.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

# Stub pyomo before importing the CLI (mirrors other integration tests).
if "pyomo" not in sys.modules:
    _stub = MagicMock()
    sys.modules["pyomo"] = _stub
    sys.modules["pyomo.environ"] = _stub
    sys.modules["pyomo.core"] = _stub
    sys.modules["pyomo.opt"] = _stub

# CLI orchestrator under test — imported as a module so we can drive
# its stage functions without going through argparse.
import importlib

cli = importlib.import_module("scripts.onboard_depot_from_kempower")

from src.adapters.kempower.defaults import compute_import_row_hash


# --------------------------------------------------------------------------- #
# In-memory fakes
# --------------------------------------------------------------------------- #


class _Store:
    """Shared row store both fake pools read/write."""

    def __init__(self) -> None:
        self.sites: dict[str, dict[str, Any]] = {}
        self.charging_stations: list[dict[str, Any]] = []
        self.vehicles: list[dict[str, Any]] = []
        self.charger_vehicle_access: list[dict[str, Any]] = []
        self.station_credentials: list[dict[str, Any]] = []
        self.charging_sessions: list[dict[str, Any]] = []


class _FakeTxn:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _FakeConn:
    """Quacks like asyncpg.Connection for the SQL we issue."""

    def __init__(self, store: _Store, *, kind: str) -> None:
        self._store = store
        self._kind = kind  # "static" or "ts"

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()

    # --- routers ----------------------------------------------------------

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        sql_lc = " ".join(sql.lower().split())
        if "from sites where id = $1::uuid" in sql_lc and "select id::text" in sql_lc:
            row = self._store.sites.get(args[0])
            return dict(row) if row else None
        if (
            "from charging_stations where site_id = $1::uuid and station_id = $2"
            in sql_lc
        ):
            for station in self._store.charging_stations:
                if station["site_id"] == args[0] and station["station_id"] == args[1]:
                    return {"id": station["id"]}
            return None
        if "from vehicles where site_id = $1::uuid and external_id = $2" in sql_lc:
            for vehicle in self._store.vehicles:
                if vehicle["site_id"] == args[0] and vehicle["external_id"] == args[1]:
                    return {"id": vehicle["id"]}
            return None
        if "insert into charging_stations" in sql_lc:
            return self._insert_charging_station(args)
        if "insert into vehicles" in sql_lc:
            return self._insert_vehicle(args)
        if "insert into charging_sessions" in sql_lc:
            return self._insert_session(args)
        if "update sites" in sql_lc:
            return self._update_site(args)
        raise NotImplementedError(f"unmocked fetchrow: {sql_lc[:120]}")

    async def execute(self, sql: str, *args: Any) -> str:
        sql_lc = " ".join(sql.lower().split())
        if "insert into station_credentials" in sql_lc:
            self._store.station_credentials.append(
                {
                    "station_id": args[0],
                    "password_hash": args[1],
                }
            )
            return "INSERT 0 1"
        if "insert into charger_vehicle_access" in sql_lc:
            self._upsert_access(args)
            return "INSERT 0 1"
        raise NotImplementedError(f"unmocked execute: {sql_lc[:120]}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        sql_lc = " ".join(sql.lower().split())
        if (
            "from charging_stations where site_id = $1::uuid and id = any($2::uuid[])"
            in sql_lc
        ):
            wanted = set(args[1])
            return [
                {"charger_id": st["id"]}
                for st in self._store.charging_stations
                if st["site_id"] == args[0] and st["id"] in wanted
            ]
        if "from vehicles where site_id = $1::uuid and id = any($2::uuid[])" in sql_lc:
            wanted = set(args[1])
            return [
                {"vehicle_id": v["id"]}
                for v in self._store.vehicles
                if v["site_id"] == args[0] and v["id"] in wanted
            ]
        raise NotImplementedError(f"unmocked fetch: {sql_lc[:120]}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        raise NotImplementedError(f"unmocked fetchval: {sql[:80]}")

    # --- private inserts ----------------------------------------------------

    def _insert_charging_station(self, args: tuple) -> dict[str, Any]:
        (
            depot_id,
            ocpp_id,
            rated_kw,
            connector_type,
            display_name,
            vendor,
            model,
            serial_number,
            firmware,
            connector_count,
            connector_ids_json,
            network_notes,
        ) = args
        row_id = str(uuid4())
        row = {
            "id": row_id,
            "site_id": depot_id,
            "station_id": ocpp_id,
            "max_power_kw": rated_kw,
            "rated_kw": rated_kw,
            "connector_type": connector_type,
            "display_name": display_name,
            "vendor": vendor,
            "model": model,
            "serial_number": serial_number,
            "firmware_version": firmware,
            "connector_count": connector_count,
            "connector_ids": connector_ids_json,
            "network_notes": network_notes,
            "status": "active",
            "auth_required": True,
            "created_at": datetime.now(timezone.utc),
            "efficiency": 1.0,
            "depot_id": depot_id,
            "ocpp_id": ocpp_id,
            "firmware": firmware,
        }
        self._store.charging_stations.append(row)
        return row

    def _insert_vehicle(self, args: tuple) -> dict[str, Any]:
        (
            depot_id,
            organization_id,
            external_id,
            vehicle_type,
            battery_kwh,
            max_charge_kw,
            display_name,
            id_tag,
            vin,
            license_plate,
            vehicle_status,
        ) = args
        site = self._store.sites.get(depot_id)
        if not site or site["organization_id"] != organization_id:
            return None  # type: ignore[return-value]
        row_id = str(uuid4())
        row = {
            "id": row_id,
            "vehicle_id": row_id,
            "site_id": depot_id,
            "depot_id": depot_id,
            "organization_id": organization_id,
            "external_id": external_id,
            "vehicle_type": vehicle_type,
            "battery_kwh": battery_kwh,
            "battery_capacity_kwh": battery_kwh,
            "max_charge_kw": max_charge_kw,
            "max_charge_rate_kw": max_charge_kw,
            "display_name": display_name,
            "id_tag": id_tag,
            "vin": vin,
            "license_plate": license_plate,
            "status": vehicle_status,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        self._store.vehicles.append(row)
        return row

    def _insert_session(self, args: tuple) -> Optional[dict[str, Any]]:
        (
            station_id,
            vehicle_id,
            id_token,
            start_time,
            end_time,
            energy_kwh,
            site_id,
            batch_id,
            row_hash,
            user_full_name,
            station_owner,
            status,
        ) = args
        # ON CONFLICT DO NOTHING semantics on (site_id, import_row_hash) WHERE source='import'.
        for existing in self._store.charging_sessions:
            if (
                existing["site_id"] == site_id
                and existing["import_row_hash"] == row_hash
                and existing["source"] == "import"
            ):
                return None
        row = {
            "session_id": str(uuid4()),
            "station_id": station_id,
            "vehicle_id": vehicle_id,
            "id_token": id_token,
            "start_time": start_time,
            "end_time": end_time,
            "energy_delivered_kwh": energy_kwh,
            "site_id": site_id,
            "source": "import",
            "import_batch_id": batch_id,
            "import_row_hash": row_hash,
            "import_user_full_name": user_full_name,
            "import_station_owner": station_owner,
            "import_status": status,
        }
        self._store.charging_sessions.append(row)
        return {"x": 1}

    def _update_site(self, args: tuple) -> dict[str, Any]:
        depot_id = args[0]
        site = self._store.sites.get(depot_id)
        if site is None:
            return None  # type: ignore[return-value]
        # Match update_depot_setup's parameter order from src/db/queries.py.
        (
            _depot_id,
            name,
            latitude,
            longitude,
            tz_name,
            currency,
            utility_id,
            max_grid_kw,
            demand_charge_rate_kw,
            demand_charge_billing_period,
            address_json,
            billing_metadata_json,
            building_load_source_json,
            building_load_assumption_kw,
            charger_vehicle_access_default,
            tariff_type,
            energy_cap_kwh,
            under_cap_rate_per_kwh,
            over_cap_penalty_per_kwh,
            cap_billing_period,
        ) = args
        site.update(
            name=name,
            latitude=latitude,
            longitude=longitude,
            timezone=tz_name,
            currency=currency,
            utility_id=utility_id,
            max_grid_kw=max_grid_kw,
            demand_charge_rate_kw=demand_charge_rate_kw,
            demand_charge_billing_period=demand_charge_billing_period,
            address=address_json,
            billing_metadata=billing_metadata_json,
            building_load_source=building_load_source_json,
            building_load_assumption_kw=building_load_assumption_kw,
            charger_vehicle_access_default=charger_vehicle_access_default,
            tariff_type=tariff_type,
            energy_cap_kwh=energy_cap_kwh,
            under_cap_rate_per_kwh=under_cap_rate_per_kwh,
            over_cap_penalty_per_kwh=over_cap_penalty_per_kwh,
            cap_billing_period=cap_billing_period,
        )
        return dict(site)

    def _upsert_access(self, args: tuple) -> None:
        charger_id, vehicle_id, is_accessible = args
        for row in self._store.charger_vehicle_access:
            if (
                row["charging_station_id"] == charger_id
                and row["vehicle_id"] == vehicle_id
            ):
                row["is_accessible"] = bool(is_accessible)
                return
        self._store.charger_vehicle_access.append(
            {
                "charging_station_id": charger_id,
                "vehicle_id": vehicle_id,
                "is_accessible": bool(is_accessible),
            }
        )


class _FakePool:
    def __init__(self, store: _Store, *, kind: str) -> None:
        self._store = store
        self._kind = kind

    def acquire(self) -> "_FakeAcquire":
        return _FakeAcquire(self._store, self._kind)


class _FakeAcquire:
    def __init__(self, store: _Store, kind: str) -> None:
        self._conn = _FakeConn(store, kind=kind)

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


# --------------------------------------------------------------------------- #
# Fake Kempower client (drop-in for KempowerClient)
# --------------------------------------------------------------------------- #


class _FakeKempower:
    def __init__(
        self,
        *,
        stations: list[dict[str, Any]] | None = None,
        vehicles: list[dict[str, Any]] | None = None,
        transactions_by_station: dict[str, list[dict[str, Any]]] | None = None,
        location: dict[str, Any] | None = None,
        power_group: dict[str, Any] | None = None,
    ) -> None:
        self._stations = stations or []
        self._vehicles = vehicles or []
        self._transactions_by_station = transactions_by_station or {}
        self._location = location or {}
        self._power_group = power_group

    async def __aenter__(self) -> "_FakeKempower":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def get_location(self, _id: str) -> dict[str, Any]:
        return self._location

    async def get_power_group(self, _id: str) -> dict[str, Any]:
        if self._power_group is None:
            raise cli.KempowerClientError("no power group configured")
        return self._power_group

    def iter_charging_stations(self, _location_id: str):
        async def gen():
            for s in self._stations:
                yield s

        return gen()

    def iter_vehicles(self, _location_id: str):
        async def gen():
            for v in self._vehicles:
                yield v

        return gen()

    def iter_transactions(self, *, station_id: str, start_iso: str, end_iso: str):
        async def gen():
            for tx in self._transactions_by_station.get(station_id, []):
                yield tx

        return gen()


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


_DEPOT = "11111111-1111-1111-1111-111111111111"
_ORG = "22222222-2222-2222-2222-222222222222"


def _seed_depot(store: _Store, **overrides: Any) -> dict[str, Any]:
    row = {
        "id": _DEPOT,
        "depot_id": _DEPOT,
        "organization_id": _ORG,
        "name": "Existing Depot",
        "timezone": "Europe/Vilnius",
        "currency": "EUR",
        "max_grid_kw": 500.0,
        "utility_id": "ESO",
        "demand_charge_rate_kw": 5.5,
        "demand_charge_billing_period": "monthly",
        "address": {"line1": "1 Street", "city": "Vilnius", "country": "LT"},
        "billing_metadata": {},
        "building_load_source": {"type": "none"},
        "building_load_assumption_kw": 0.0,
        "charger_vehicle_access_default": "all_to_all",
        "tariff_type": "simple_demand",
        "energy_cap_kwh": None,
        "under_cap_rate_per_kwh": None,
        "over_cap_penalty_per_kwh": None,
        "cap_billing_period": "monthly",
        "latitude": 54.687,
        "longitude": 25.27,
    }
    row.update(overrides)
    store.sites[_DEPOT] = row
    return row


@pytest.fixture
def store() -> _Store:
    s = _Store()
    _seed_depot(s)
    return s


@pytest.fixture
def static_pool(store: _Store) -> _FakePool:
    return _FakePool(store, kind="static")


@pytest.fixture
def ts_pool(store: _Store) -> _FakePool:
    return _FakePool(store, kind="ts")


def _counts() -> cli._Counts:
    return cli._Counts()


# --------------------------------------------------------------------------- #
# Happy path: chargers + vehicles + access matrix + sessions
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_full_onboarding_happy_path(store, static_pool, ts_pool):
    kempower = _FakeKempower(
        stations=[
            {
                "stationId": "KEM-DC-001",
                "name": "Bay 1",
                "maxPowerKw": 150,
                "connectors": [
                    {"connectorId": 1, "type": "CCS"},
                    {"connectorId": 2, "type": "CCS"},
                ],
            },
            {
                "stationId": "KEM-DC-002",
                "name": "Bay 2",
                "maxPowerKw": 150,
                "connectors": [{"connectorId": 1, "type": "CCS2"}],
            },
        ],
        vehicles=[
            {
                "vehicleId": "v-100",
                "name": "Bus 100",
                "netBatterySizeKwh": 300,
                "maxChargePowerKw": 150,
            },
            {
                "vehicleId": "v-101",
                "name": "Bus 101",
                "netBatterySizeKwh": 350,
                "maxChargePowerKw": 150,
            },
        ],
        transactions_by_station={
            "KEM-DC-001": [
                {
                    "txId": "tx-1",
                    "stationId": "KEM-DC-001",
                    "vehicleId": "v-100",
                    "idTag": "rfid-1",
                    "startTime": "2025-03-01T08:00:00Z",
                    "stopTime": "2025-03-01T10:00:00Z",
                    "energyKwh": 100.0,
                },
                {
                    "txId": "tx-2",
                    "stationId": "KEM-DC-001",
                    "vehicleId": "v-101",
                    "idTag": "rfid-2",
                    "startTime": "2025-03-01T11:00:00Z",
                    "stopTime": "2025-03-01T12:30:00Z",
                    "energyKwh": 80.0,
                },
            ],
            "KEM-DC-002": [],
        },
    )

    args = _make_args()
    counts = _counts()
    depot = await cli._load_depot(static_pool, _DEPOT)
    station_map = await cli._import_chargers(
        static_pool,
        kempower,
        depot_id=depot["depot_id"],
        kempower_location_id="loc-1",
        dry_run=False,
        counts=counts,
    )
    vehicle_map = await cli._import_vehicles(
        static_pool,
        kempower,
        depot_id=depot["depot_id"],
        organization_id=depot["organization_id"],
        kempower_location_id="loc-1",
        dry_run=False,
        counts=counts,
    )
    await cli._upsert_access_matrix(
        static_pool,
        depot_id=depot["depot_id"],
        station_map=station_map,
        vehicle_map=vehicle_map,
        dry_run=False,
        counts=counts,
    )
    await cli._backfill_sessions(
        ts_pool,
        kempower,
        depot_id=depot["depot_id"],
        station_map=station_map,
        vehicle_map=vehicle_map,
        backfill_since=args.backfill_since,
        batch_id=uuid4(),
        dry_run=False,
        counts=counts,
    )

    assert counts.chargers_created == 2
    assert counts.chargers_already_linked == 0
    assert counts.vehicles_created == 2
    assert counts.access_rows_upserted == 4  # 2 × 2
    assert counts.sessions_inserted == 2
    assert len(counts.credentials_emitted) == 2

    assert len(store.charging_stations) == 2
    assert len(store.vehicles) == 2
    assert len(store.charger_vehicle_access) == 4
    assert len(store.charging_sessions) == 2

    # Session shape correctness
    sess = sorted(store.charging_sessions, key=lambda r: r["start_time"])
    assert sess[0]["station_id"] == "KEM-DC-001"
    assert sess[0]["source"] == "import"
    assert sess[0]["energy_delivered_kwh"] == 100.0
    expected_hash = compute_import_row_hash(
        depot_id=_DEPOT,
        start_time_utc=datetime(2025, 3, 1, 8, 0, tzinfo=timezone.utc),
        id_tag="rfid-1",
    )
    assert sess[0]["import_row_hash"] == expected_hash


# --------------------------------------------------------------------------- #
# Cross-day idempotency
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_rerun_is_idempotent(store, static_pool, ts_pool):
    kempower = _FakeKempower(
        stations=[
            {
                "stationId": "KEM-DC-001",
                "name": "Bay 1",
                "maxPowerKw": 150,
                "connectors": [{"connectorId": 1, "type": "CCS"}],
            },
        ],
        vehicles=[
            {
                "vehicleId": "v-100",
                "name": "Bus 100",
                "netBatterySizeKwh": 300,
                "maxChargePowerKw": 150,
            },
        ],
        transactions_by_station={
            "KEM-DC-001": [
                {
                    "txId": "tx-1",
                    "stationId": "KEM-DC-001",
                    "vehicleId": "v-100",
                    "idTag": "rfid-1",
                    "startTime": "2025-03-01T08:00:00Z",
                    "stopTime": "2025-03-01T10:00:00Z",
                    "energyKwh": 100.0,
                },
            ],
        },
    )

    async def run_once(batch_id):
        counts = _counts()
        depot = await cli._load_depot(static_pool, _DEPOT)
        station_map = await cli._import_chargers(
            static_pool,
            kempower,
            depot_id=depot["depot_id"],
            kempower_location_id="loc-1",
            dry_run=False,
            counts=counts,
        )
        vehicle_map = await cli._import_vehicles(
            static_pool,
            kempower,
            depot_id=depot["depot_id"],
            organization_id=depot["organization_id"],
            kempower_location_id="loc-1",
            dry_run=False,
            counts=counts,
        )
        await cli._upsert_access_matrix(
            static_pool,
            depot_id=depot["depot_id"],
            station_map=station_map,
            vehicle_map=vehicle_map,
            dry_run=False,
            counts=counts,
        )
        await cli._backfill_sessions(
            ts_pool,
            kempower,
            depot_id=depot["depot_id"],
            station_map=station_map,
            vehicle_map=vehicle_map,
            backfill_since=datetime(2025, 1, 1, tzinfo=timezone.utc),
            batch_id=batch_id,
            dry_run=False,
            counts=counts,
        )
        return counts

    first = await run_once(uuid4())
    second = await run_once(uuid4())  # fresh batch id == "later day"

    assert first.chargers_created == 1
    assert first.vehicles_created == 1
    assert first.sessions_inserted == 1

    # Second run touches nothing new.
    assert second.chargers_created == 0
    assert second.chargers_already_linked == 1
    assert second.vehicles_created == 0
    assert second.vehicles_already_linked == 1
    assert second.sessions_inserted == 0
    assert second.sessions_already_present == 1

    # DB state still has exactly one row of each.
    assert len(store.charging_stations) == 1
    assert len(store.vehicles) == 1
    assert len(store.charging_sessions) == 1


# --------------------------------------------------------------------------- #
# Non-CCS skip
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_non_ccs_charger_is_skipped(store, static_pool, ts_pool):
    kempower = _FakeKempower(
        stations=[
            {
                "stationId": "BAD-1",
                "name": "Bad",
                "maxPowerKw": 50,
                "connectors": [{"connectorId": 1, "type": "CHAdeMO"}],
            },
            {
                "stationId": "GOOD-1",
                "name": "Good",
                "maxPowerKw": 150,
                "connectors": [{"connectorId": 1, "type": "CCS"}],
            },
        ],
    )

    counts = _counts()
    depot = await cli._load_depot(static_pool, _DEPOT)
    station_map = await cli._import_chargers(
        static_pool,
        kempower,
        depot_id=depot["depot_id"],
        kempower_location_id="loc-1",
        dry_run=False,
        counts=counts,
    )

    assert counts.chargers_created == 1
    assert len(counts.chargers_skipped) == 1
    assert "BAD-1" in counts.chargers_skipped[0]
    assert "GOOD-1" in station_map
    assert "BAD-1" not in station_map


# --------------------------------------------------------------------------- #
# Site suggestions
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_site_suggestions_patches_chosen_fields(monkeypatch, store, static_pool):
    # Operator agrees to name + max_grid_kw only.
    # Diff is ordered (name, latitude, longitude, max_grid_kw),
    # so y/n/n/y selects exactly those two fields.
    inputs = iter(["y", "n", "n", "y"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    kempower = _FakeKempower(
        location={
            "name": "Vilnius Depot",
            "address": "5 V Str, Vilnius",
            "lat": 54.69,
            "lng": 25.28,
            "rootPowerGroupId": "pg-1",
        },
        power_group={"limitKw": 800},
    )

    counts = _counts()
    depot = await cli._load_depot(static_pool, _DEPOT)
    await cli._maybe_apply_site_suggestions(
        static_pool,
        kempower,
        depot_row=depot,
        kempower_location_id="loc-1",
        apply=True,
        dry_run=False,
        counts=counts,
    )

    assert counts.site_suggestions  # diff was non-empty
    assert counts.site_patch_applied is True
    site = store.sites[_DEPOT]
    assert site["name"] == "Vilnius Depot"
    assert site["max_grid_kw"] == 800.0
    # Declined fields are untouched (latitude/longitude still seed values).
    assert site["latitude"] == 54.687
    assert site["longitude"] == 25.27


@pytest.mark.asyncio
async def test_site_suggestions_no_diff_writes_nothing(monkeypatch, store, static_pool):
    # Seed depot already matches Kempower exactly — no prompts should fire.
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("input() should not be called when diff is empty"),
    )
    kempower = _FakeKempower(
        location={"name": "Existing Depot", "lat": 54.687, "lng": 25.27},
        power_group={"limitKw": 500},
    )
    counts = _counts()
    depot = await cli._load_depot(static_pool, _DEPOT)
    # address mismatches (seed has structured address dict) — drop it
    kempower._location["address"] = None
    await cli._maybe_apply_site_suggestions(
        static_pool,
        kempower,
        depot_row=depot,
        kempower_location_id="loc-1",
        apply=True,
        dry_run=False,
        counts=counts,
    )
    assert counts.site_patch_applied is False


# --------------------------------------------------------------------------- #
# Wrong-depot guard
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_load_depot_missing_uuid_raises(store, static_pool):
    with pytest.raises(RuntimeError, match="Create it via the dashboard"):
        await cli._load_depot(static_pool, "33333333-3333-3333-3333-333333333333")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_args() -> argparse.Namespace:
    return argparse.Namespace(
        depot_id=_DEPOT,
        kempower_location_id="loc-1",
        backfill_since=datetime(2025, 1, 1, tzinfo=timezone.utc),
        dry_run=False,
        apply_site_suggestions=False,
        kempower_base_url=None,
        kempower_username="u",
        kempower_password="p",
        database_url="postgresql://stub",
        static_database_url="postgresql://stub",
        log_level="INFO",
    )
