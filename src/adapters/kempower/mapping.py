"""Kempower ChargEye JSON → Favonius Pydantic / DB-row mappers.

Pure functions. No DB, no HTTP, no Anthropic. Validation happens at Pydantic
construction time — callers get the surfaced errors from there, not from a
hand-rolled normaliser.

The Pydantic models here (:class:`KempowerChargerPayload`,
:class:`KempowerVehiclePayload`) intentionally mirror the validation rules
of the live admin-API models in ``src/api/main.py`` (``ChargerCreateRequest``
and ``VehicleIdentityBase``) but are **defined locally** to avoid pulling
the entire FastAPI app into the import tree — a CLI / unit-test friendly
shape. If you tighten a constraint in ``src/api/main.py``, mirror it here.

All field names follow the public ChargEye reference (``stationId``,
``maxPowerKw``, ``netBatterySizeKwh``, …). When the live API uses a
slightly different shape, only this module needs to change — the CLI and
service-layer call sites are independent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from .defaults import (
    KEMPOWER_EXTERNAL_ID_PREFIX,
    compute_import_row_hash,
    derive_vehicle_type,
)


class KempowerChargerPayload(BaseModel):
    """Normalised Kempower charger ready to be handed to ``create_charger_with_credentials``.

    Mirrors the validation rules of ``src.api.main.ChargerCreateRequest``
    so the CLI's failure modes match the live admin endpoint.
    """

    display_name: str = Field(..., min_length=1, max_length=255)
    vendor: Optional[str] = Field(default=None, max_length=128)
    model: Optional[str] = Field(default=None, max_length=128)
    serial_number: Optional[str] = Field(default=None, max_length=128)
    firmware: Optional[str] = Field(default=None, max_length=128)
    rated_kw: float = Field(..., gt=0, le=1000)
    connector_type: Literal["CCS"] = "CCS"
    connector_count: int = Field(..., ge=1, le=20)
    connector_ids: Optional[list[int]] = None
    network_notes: Optional[str] = Field(default=None, max_length=2048)

    @field_validator("connector_ids")
    @classmethod
    def _validate_connector_ids(cls, value: Optional[list[int]]) -> Optional[list[int]]:
        if value is None:
            return value
        if any(connector_id < 1 for connector_id in value):
            raise ValueError("connector_ids must contain positive integers")
        if len(set(value)) != len(value):
            raise ValueError("connector_ids must be unique")
        return value


class KempowerVehiclePayload(BaseModel):
    """Normalised Kempower vehicle ready to be handed to ``create_vehicle_identity``.

    Mirrors the validation rules of ``src.api.main.VehicleIdentityBase``.
    """

    external_id: str = Field(..., min_length=1, max_length=100)
    display_name: Optional[str] = Field(default=None, max_length=255)
    vehicle_type: str = Field(..., min_length=1, max_length=50)
    battery_kwh: float = Field(..., gt=0)
    max_charge_kw: float = Field(..., gt=0)
    id_tag: Optional[str] = Field(default=None, min_length=1, max_length=100)
    vin: Optional[str] = Field(default=None, max_length=64)
    license_plate: Optional[str] = Field(default=None, max_length=64)
    status: Literal["active", "inactive", "retired"] = "active"


class UnsupportedConnectorError(ValueError):
    """Raised when a Kempower station declares a non-CCS connector.

    The Favonius MVP is CCS-only (PRD §3.2). Non-CCS stations are skipped
    by the CLI with a warning rather than aborting the whole import.
    """


def kempower_station_to_charger_request(
    station: dict[str, Any],
) -> KempowerChargerPayload:
    """Map one Kempower ``ChargingStation`` JSON object to ``KempowerChargerPayload``.

    Raises ``UnsupportedConnectorError`` if any connector advertises a type
    other than CCS — the CLI catches it and emits a per-station skip notice.
    Raises ``pydantic.ValidationError`` if required fields are absent or
    out of range; the CLI surfaces it as a per-station failure.
    """
    connectors = station.get("connectors") or []
    if not connectors:
        raise UnsupportedConnectorError(
            f"Kempower station {station.get('stationId')!r} has no connectors"
        )
    connector_types = {(c.get("type") or "").upper() for c in connectors}
    # Accept the canonical CCS variants. Kempower's enum hasn't been pinned
    # — some deployments report "CCS", some "CCS1", "CCS2", "CCS_TYPE_2".
    if not all(t.startswith("CCS") for t in connector_types):
        raise UnsupportedConnectorError(
            f"Kempower station {station.get('stationId')!r} has non-CCS connectors: "
            f"{sorted(connector_types)}"
        )

    connector_ids = sorted(
        {int(c["connectorId"]) for c in connectors if "connectorId" in c}
    )
    if not connector_ids:
        # Connector list present but no numeric ids — fall back to a 1-based
        # sequence matching len(connectors). ChargerCreateRequest validates
        # uniqueness and positivity.
        connector_ids = list(range(1, len(connectors) + 1))

    return KempowerChargerPayload(
        display_name=station.get("name")
        or station.get("stationId")
        or "Kempower charger",
        vendor="Kempower",
        model=station.get("model"),
        serial_number=station.get("serialNumber"),
        firmware=station.get("firmwareVersion"),
        rated_kw=float(station["maxPowerKw"]),
        connector_type="CCS",
        connector_count=len(connector_ids),
        connector_ids=connector_ids,
        network_notes=f"Imported from Kempower stationId={station.get('stationId')}",
    )


def kempower_vehicle_to_identity(vehicle: dict[str, Any]) -> KempowerVehiclePayload:
    """Map one Kempower ``Vehicle`` JSON object to ``KempowerVehiclePayload``.

    ``external_id`` is prefixed with ``kempower:`` so two customers using
    the same vehicle ``id`` in their respective ChargEye tenants
    can co-exist under the Favonius global ``vehicles.external_id`` UNIQUE.
    Field names mirror the ChargEye Vehicles API ``VehicleDTO`` schema
    (``id``, ``fullChargeEnergykWh``, ``maxChargePowerkW`` — note the
    lowercase ``k`` in the last two, per docs.kempower.io).
    """
    raw_id = vehicle.get("id")
    if raw_id is None or raw_id == "":
        raise ValueError("Kempower vehicle is missing required field 'id'")
    if vehicle.get("fullChargeEnergykWh") is None:
        raise ValueError(
            f"Kempower vehicle {raw_id!r} is missing required field 'fullChargeEnergykWh'"
        )
    if vehicle.get("maxChargePowerkW") is None:
        raise ValueError(
            f"Kempower vehicle {raw_id!r} is missing required field 'maxChargePowerkW'"
        )

    return KempowerVehiclePayload(
        external_id=f"{KEMPOWER_EXTERNAL_ID_PREFIX}{raw_id}",
        display_name=vehicle.get("name"),
        vehicle_type=derive_vehicle_type(
            make=None,
            model=vehicle.get("evModel"),
        ),
        battery_kwh=float(vehicle["fullChargeEnergykWh"]),
        max_charge_kw=float(vehicle["maxChargePowerkW"]),
        # id_tag is left None on backfill — OCPP populates it the first time
        # the vehicle plugs into the Favonius OCPP server with an RFID.
        id_tag=None,
        vin=vehicle.get("vin"),
        license_plate=vehicle.get("licensePlate"),
        status="active",
    )


def kempower_transaction_to_session_row(
    transaction: dict[str, Any],
    *,
    site_id: str,
    station_id: str,
    vehicle_id: str | None,
    batch_id: UUID,
) -> dict[str, Any]:
    """Map one Kempower ``Transaction`` JSON object to a ``charging_sessions`` row.

    Returns a dict matching the INSERT shape used by the existing import
    path in ``src/api/main.py`` (the XLSX upload handler). Caller is
    responsible for the actual INSERT; this function only normalises and
    computes the dedup hash so the row goes through
    ``charging_sessions_import_dedup_idx``.

    ``station_id`` is the OCPP id (text) that we wrote into
    ``charging_stations.station_id`` — joinable for reconciliation —
    rather than the placeholder ``imported:<depot_id>`` the XLSX path
    uses. ``vehicle_id`` is the Favonius UUID (string); pass ``None``
    when the Kempower transaction references a vehicle the import
    didn't create (e.g. a one-off guest charge).
    """
    start_raw = transaction.get("startTime")
    if not start_raw:
        raise ValueError("Kempower transaction is missing required field 'startTime'")
    start_time_utc = _parse_iso_utc(start_raw)

    end_raw = transaction.get("endTime")
    end_time_utc = _parse_iso_utc(end_raw) if end_raw else None

    energy_kwh_raw = transaction.get("chargedEnergyKwh")
    if energy_kwh_raw is None:
        raise ValueError(
            f"Kempower transaction {transaction.get('txId')!r} is missing 'chargedEnergyKwh'"
        )
    energy_kwh = float(energy_kwh_raw)

    # Match the XLSX path's behaviour: when the source row has no
    # RFID/auth token, classify it as platform-initiated. The XLSX path uses
    # a richer hash-token to avoid collisions across distinct
    # platform-initiated sessions sharing minute-level start time — we
    # don't have those discriminators here, so we fall back to the
    # Kempower transaction id, which is globally unique on their side.
    raw_id_tag = transaction.get("authorizationToken")
    if raw_id_tag:
        id_token = str(raw_id_tag)
        hash_id_token = id_token
    else:
        tx_id = transaction.get("txId")
        if tx_id is None:
            raise ValueError(
                "Kempower transaction has neither 'idTag' nor 'txId'; "
                "cannot construct a stable import row hash"
            )
        id_token = "platform-start"
        hash_id_token = f"kempower-tx:{tx_id}"

    row_hash = compute_import_row_hash(
        depot_id=site_id,
        start_time_utc=start_time_utc,
        id_tag=hash_id_token,
    )

    return {
        "station_id": station_id,
        "vehicle_id": vehicle_id,
        "id_token": id_token,
        "start_time": start_time_utc,
        "end_time": end_time_utc,
        "energy_delivered_kwh": energy_kwh,
        "site_id": site_id,
        "import_batch_id": str(batch_id),
        "import_row_hash": row_hash,
        "import_user_full_name": None,
        "import_station_owner": None,
        "import_status": transaction.get("status"),
    }


def kempower_location_to_site_suggestions(
    location: dict[str, Any],
    power_group: dict[str, Any] | None,
) -> dict[str, Any]:
    """Distil Kempower Location + root Power Group into a diff payload.

    The output is **not** a Favonius depot patch — it's a sparse dict of
    suggested values the CLI shows alongside the existing depot's values.
    The operator picks which fields (if any) to PATCH; the CLI builds the
    actual patch from the operator's selection.

    Returned keys (omitted when Kempower has no value):

    - ``name``: ``Location.name``
    - ``latitude`` / ``longitude``: ``Location.lat`` / ``Location.lng``
    - ``max_grid_kw``: ``power_group.limitKw`` if present
    """
    suggestions: dict[str, Any] = {}
    if location.get("name"):
        suggestions["name"] = location["name"]
    lat, lng = location.get("lat"), location.get("lng")
    if lat is not None:
        suggestions["latitude"] = float(lat)
    if lng is not None:
        suggestions["longitude"] = float(lng)
    if power_group and power_group.get("limitKw") is not None:
        suggestions["max_grid_kw"] = float(power_group["limitKw"])
    return suggestions


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Accepts the Kempower-shaped ``"...Z"`` suffix and offset forms alike.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        # Treat naive timestamps as UTC — Kempower documents the API as
        # UTC and the rare naive value (test fixtures, hand-rolled CSV)
        # is unambiguous in that context.
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
