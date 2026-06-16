"""Unit tests for the Kempower adapter mappers.

Pure-function coverage — no DB, no HTTP. Golden Kempower-shaped JSON
fixtures → expected normalised payloads. Catches schema drift early.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.adapters.kempower import (
    KEMPOWER_EXTERNAL_ID_PREFIX,
    UnsupportedConnectorError,
    compute_import_row_hash,
    derive_vehicle_type,
    kempower_location_to_site_suggestions,
    kempower_station_to_charger_request,
    kempower_transaction_to_session_row,
    kempower_vehicle_to_identity,
)


# ---------------------------------------------------------------------------
# Fixtures (verbatim docs.kempower.io shapes)
# ---------------------------------------------------------------------------


@pytest.fixture
def ccs_station() -> dict:
    return {
        "stationId": "KEM-DC-001",
        "name": "Bay 1",
        "model": "C-2",
        "serialNumber": "SN-12345",
        "firmwareVersion": "5.0.3",
        "maxPowerKw": 150,
        "connectors": [
            {"connectorId": 1, "type": "CCS2"},
            {"connectorId": 2, "type": "CCS"},
        ],
    }


@pytest.fixture
def bus_vehicle() -> dict:
    # Field names match the ChargEye Vehicles API VehicleDTO schema
    # (docs.kempower.io): id, evModel, fullChargeEnergykWh, maxChargePowerkW.
    return {
        "id": "veh-42",
        "name": "Bus 042",
        "evModel": "eBus Citaro G",
        "fullChargeEnergykWh": 350,
        "maxChargePowerkW": 150,
        "vin": "WMB12345CITARO00042",
        "licensePlate": "JKL-042",
    }


@pytest.fixture
def finished_transaction() -> dict:
    # Field names match the ChargEye Transactions API TxInfo schema
    # (docs.kempower.io): authorizationToken, startTime, endTime,
    # chargedEnergyKwh, status, txId. (There's no driverName / vehicleId at
    # the transaction level — vehicle linkage lives in schedulePlan.evId.)
    return {
        "txId": "tx-99",
        "stationId": "KEM-DC-001",
        "authorizationToken": "rfid-abc-1",
        "startTime": "2025-03-01T08:00:00Z",
        "endTime": "2025-03-01T10:00:00Z",
        "chargedEnergyKwh": 175.5,
        "status": "Ended",
    }


# ---------------------------------------------------------------------------
# Stations
# ---------------------------------------------------------------------------


def test_station_maps_ccs_charger(ccs_station):
    payload = kempower_station_to_charger_request(ccs_station)
    assert payload.display_name == "Bay 1"
    assert payload.vendor == "Kempower"
    assert payload.model == "C-2"
    assert payload.serial_number == "SN-12345"
    assert payload.firmware == "5.0.3"
    assert payload.rated_kw == 150.0
    assert payload.connector_type == "CCS"
    assert payload.connector_count == 2
    assert payload.connector_ids == [1, 2]
    assert "KEM-DC-001" in (payload.network_notes or "")


def test_station_falls_back_to_stationid_when_name_missing(ccs_station):
    ccs_station.pop("name")
    payload = kempower_station_to_charger_request(ccs_station)
    assert payload.display_name == "KEM-DC-001"


def test_station_rejects_missing_connector_type():
    bad = {
        "stationId": "X-0",
        "name": "B",
        "maxPowerKw": 50,
        "connectors": [{"connectorId": 1}],
    }
    with pytest.raises(UnsupportedConnectorError) as exc_info:
        kempower_station_to_charger_request(bad)
    assert "X-0" in str(exc_info.value)


def test_station_rejects_chademo():
    bad = {
        "stationId": "X-1",
        "name": "B",
        "maxPowerKw": 50,
        "connectors": [{"connectorId": 1, "type": "CHAdeMO"}],
    }
    with pytest.raises(UnsupportedConnectorError) as exc_info:
        kempower_station_to_charger_request(bad)
    assert "X-1" in str(exc_info.value)


def test_station_rejects_mixed_connector_types():
    bad = {
        "stationId": "X-2",
        "name": "B",
        "maxPowerKw": 50,
        "connectors": [
            {"connectorId": 1, "type": "CCS"},
            {"connectorId": 2, "type": "Type2"},
        ],
    }
    with pytest.raises(UnsupportedConnectorError):
        kempower_station_to_charger_request(bad)


def test_station_rejects_empty_connectors():
    with pytest.raises(UnsupportedConnectorError):
        kempower_station_to_charger_request(
            {"stationId": "X-3", "name": "B", "maxPowerKw": 50, "connectors": []}
        )


def test_station_synthesises_connector_ids_when_missing():
    payload = kempower_station_to_charger_request(
        {
            "stationId": "X-4",
            "name": "B",
            "maxPowerKw": 50,
            "connectors": [{"type": "CCS"}, {"type": "CCS"}],
        }
    )
    assert payload.connector_count == 2
    assert payload.connector_ids == [1, 2]


def test_station_validation_error_on_zero_kw():
    with pytest.raises(ValidationError):
        kempower_station_to_charger_request(
            {
                "stationId": "X-5",
                "name": "B",
                "maxPowerKw": 0,
                "connectors": [{"connectorId": 1, "type": "CCS"}],
            }
        )


def test_station_handles_non_numeric_connector_ids():
    # ConnectorInfo.connectorId is a string per the OpenAPI spec; the
    # official example for ``GET /stations`` uses ``"charger1_connector2"``.
    # The mapper must not crash on int() — it should fall back to
    # position-based 1..N ids.
    payload = kempower_station_to_charger_request(
        {
            "stationId": "X-6",
            "name": "Trolley",
            "maxPowerKw": 200,
            "connectors": [
                {"connectorId": "charger1_connector1", "type": "CCS", "maxPowerKw": 200},
                {"connectorId": "charger1_connector2", "type": "CCS", "maxPowerKw": 200},
            ],
        }
    )
    assert payload.connector_ids == [1, 2]
    assert payload.connector_count == 2


def test_station_falls_back_to_connector_sum_when_station_maxpower_zero():
    # StationInfo.maxPowerKw is documented "undefined if not known" and the
    # official example shows 0 for many stations. ConnectorInfo.maxPowerKw
    # is required, so we can recover a meaningful rated_kw from the sum.
    payload = kempower_station_to_charger_request(
        {
            "stationId": "X-7",
            "name": "ChargEye 2x150",
            "maxPowerKw": 0,
            "connectors": [
                {"connectorId": 1, "type": "CCS", "maxPowerKw": 150},
                {"connectorId": 2, "type": "CCS", "maxPowerKw": 150},
            ],
        }
    )
    assert payload.rated_kw == 300.0


def test_station_falls_back_to_connector_sum_when_station_maxpower_missing():
    # Same fallback when the field is absent entirely (per spec, it's optional).
    payload = kempower_station_to_charger_request(
        {
            "stationId": "X-8",
            "name": "ChargEye solo",
            "connectors": [
                {"connectorId": 1, "type": "CCS", "maxPowerKw": 200},
            ],
        }
    )
    assert payload.rated_kw == 200.0


# ---------------------------------------------------------------------------
# Vehicles
# ---------------------------------------------------------------------------


def test_vehicle_maps_bus(bus_vehicle):
    payload = kempower_vehicle_to_identity(bus_vehicle)
    assert payload.external_id == f"{KEMPOWER_EXTERNAL_ID_PREFIX}veh-42"
    assert payload.display_name == "Bus 042"
    assert payload.vehicle_type == "bus"
    assert payload.battery_kwh == 350.0
    assert payload.max_charge_kw == 150.0
    assert payload.id_tag is None  # populated later by OCPP
    assert payload.vin == "WMB12345CITARO00042"
    assert payload.license_plate == "JKL-042"
    assert payload.status == "active"


def test_vehicle_missing_battery_size_fails(bus_vehicle):
    bus_vehicle.pop("fullChargeEnergykWh")
    with pytest.raises(ValueError, match="fullChargeEnergykWh"):
        kempower_vehicle_to_identity(bus_vehicle)


def test_vehicle_missing_max_charge_fails(bus_vehicle):
    bus_vehicle.pop("maxChargePowerkW")
    with pytest.raises(ValueError, match="maxChargePowerkW"):
        kempower_vehicle_to_identity(bus_vehicle)


def test_vehicle_missing_vehicle_id_fails():
    with pytest.raises(ValueError, match="'id'"):
        kempower_vehicle_to_identity(
            {"fullChargeEnergykWh": 200, "maxChargePowerkW": 100}
        )


@pytest.mark.parametrize(
    "make,model,expected",
    [
        ("eBus", "Citaro", "bus"),
        ("Mercedes", "Actros", "truck"),
        ("MAN", "TGX", "truck"),
        ("Ford", "Transit", "van"),
        ("Tesla", "Model 3", "car"),
        (None, None, "bus"),
        ("", "", "bus"),
        ("Random", "Brand", "bus"),
    ],
)
def test_derive_vehicle_type(make, model, expected):
    assert derive_vehicle_type(make=make, model=model) == expected


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


_DEPOT = "11111111-1111-1111-1111-111111111111"
_BATCH = UUID("22222222-2222-2222-2222-222222222222")


def test_transaction_maps_finished_session(finished_transaction):
    row = kempower_transaction_to_session_row(
        finished_transaction,
        site_id=_DEPOT,
        station_id="KEM-DC-001",
        vehicle_id="abcdefab-1234-1234-1234-abcdefabcdef",
        batch_id=_BATCH,
    )
    assert row["station_id"] == "KEM-DC-001"
    assert row["vehicle_id"] == "abcdefab-1234-1234-1234-abcdefabcdef"
    assert row["id_token"] == "rfid-abc-1"
    assert row["start_time"] == datetime(2025, 3, 1, 8, 0, tzinfo=timezone.utc)
    assert row["end_time"] == datetime(2025, 3, 1, 10, 0, tzinfo=timezone.utc)
    assert row["energy_delivered_kwh"] == 175.5
    assert row["site_id"] == _DEPOT
    assert row["import_batch_id"] == str(_BATCH)
    assert row["import_status"] == "Ended"
    # TxInfo has no driverName field; we explicitly carry None.
    assert row["import_user_full_name"] is None


def test_transaction_hash_is_deterministic(finished_transaction):
    a = kempower_transaction_to_session_row(
        finished_transaction,
        site_id=_DEPOT,
        station_id="KEM-DC-001",
        vehicle_id=None,
        batch_id=_BATCH,
    )
    b = kempower_transaction_to_session_row(
        finished_transaction,
        site_id=_DEPOT,
        station_id="KEM-DC-001",
        vehicle_id=None,
        batch_id=UUID("33333333-3333-3333-3333-333333333333"),  # different batch
    )
    # Hash excludes batch and vehicle_id — same source row → same hash.
    assert a["import_row_hash"] == b["import_row_hash"]


def test_transaction_hash_matches_main_helper(finished_transaction):
    """Adapter hash must match ``src/api/main.py::_compute_import_row_hash``.

    The XLSX upload path and the Kempower import path share one dedup
    index, so the two helpers must produce byte-identical hashes for
    the same inputs.
    """
    row = kempower_transaction_to_session_row(
        finished_transaction,
        site_id=_DEPOT,
        station_id="KEM-DC-001",
        vehicle_id=None,
        batch_id=_BATCH,
    )
    expected = compute_import_row_hash(
        depot_id=_DEPOT,
        start_time_utc=datetime(2025, 3, 1, 8, 0, tzinfo=timezone.utc),
        id_tag="rfid-abc-1",
    )
    assert row["import_row_hash"] == expected


def test_transaction_without_id_tag_uses_tx_id_in_hash(finished_transaction):
    finished_transaction.pop("authorizationToken")
    row = kempower_transaction_to_session_row(
        finished_transaction,
        site_id=_DEPOT,
        station_id="KEM-DC-001",
        vehicle_id=None,
        batch_id=_BATCH,
    )
    # Persisted id_token is the platform-start sentinel, but the hash
    # token uses the Kempower txId so distinct sessions sharing
    # start-time don't collide.
    assert row["id_token"] == "platform-start"
    expected = compute_import_row_hash(
        depot_id=_DEPOT,
        start_time_utc=datetime(2025, 3, 1, 8, 0, tzinfo=timezone.utc),
        id_tag="kempower-tx:tx-99",
    )
    assert row["import_row_hash"] == expected


def test_transaction_without_id_tag_or_tx_id_fails():
    bad = {
        "startTime": "2025-03-01T08:00:00Z",
        "chargedEnergyKwh": 10,
        "endTime": "2025-03-01T09:00:00Z",
    }
    with pytest.raises(ValueError, match="'idTag'|'txId'|authorizationToken"):
        kempower_transaction_to_session_row(
            bad,
            site_id=_DEPOT,
            station_id="X",
            vehicle_id=None,
            batch_id=_BATCH,
        )


def test_transaction_without_stop_time_keeps_end_none(finished_transaction):
    finished_transaction.pop("endTime")
    row = kempower_transaction_to_session_row(
        finished_transaction,
        site_id=_DEPOT,
        station_id="KEM-DC-001",
        vehicle_id=None,
        batch_id=_BATCH,
    )
    assert row["end_time"] is None


def test_transaction_offset_isoformat_normalises_to_utc():
    tx = {
        "txId": "tx-1",
        "startTime": "2025-03-01T10:00:00+02:00",
        "endTime": "2025-03-01T12:00:00+02:00",
        "chargedEnergyKwh": 25,
        "authorizationToken": "rfid",
    }
    row = kempower_transaction_to_session_row(
        tx,
        site_id=_DEPOT,
        station_id="X",
        vehicle_id=None,
        batch_id=_BATCH,
    )
    assert row["start_time"] == datetime(2025, 3, 1, 8, 0, tzinfo=timezone.utc)
    assert row["end_time"] == datetime(2025, 3, 1, 10, 0, tzinfo=timezone.utc)


def test_transaction_missing_energy_fails():
    with pytest.raises(ValueError, match="chargedEnergyKwh"):
        kempower_transaction_to_session_row(
            {"txId": "x", "startTime": "2025-03-01T00:00:00Z", "authorizationToken": "r"},
            site_id=_DEPOT,
            station_id="S",
            vehicle_id=None,
            batch_id=_BATCH,
        )


# ---------------------------------------------------------------------------
# Site suggestions
# ---------------------------------------------------------------------------


def test_site_suggestions_with_power_group():
    location = {
        "name": "Vilnius Depot",
        "address": "5 Vilnius Street, Vilnius, LT",
        "lat": 54.6872,
        "lng": 25.2797,
    }
    power_group = {"limitKw": 600}
    suggestions = kempower_location_to_site_suggestions(location, power_group)
    assert suggestions == {
        "name": "Vilnius Depot",
        "latitude": 54.6872,
        "longitude": 25.2797,
        "max_grid_kw": 600.0,
    }


def test_site_suggestions_no_power_group():
    location = {"name": "Bare Depot"}
    suggestions = kempower_location_to_site_suggestions(location, None)
    assert suggestions == {"name": "Bare Depot"}


def test_site_suggestions_omits_missing_fields():
    suggestions = kempower_location_to_site_suggestions({}, None)
    assert suggestions == {}
