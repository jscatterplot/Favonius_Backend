"""Kempower ChargEye adapter for one-shot depot inventory + history import.

The adapter feeds the inventory (chargers, vehicles, charger↔vehicle access)
and historical charging sessions of a Kempower-managed depot into a
**pre-existing** Favonius depot. The operator creates the depot via the
existing ``POST /admin/depots`` flow first (with the commercial/regulatory
context Kempower doesn't expose: utility, tariff, currency, timezone,
building-load source, battery), and ``scripts/onboard_depot_from_kempower.py``
attaches everything else.

Public surface:

- :class:`KempowerClient` — thin httpx wrapper around the ChargEye REST API.
- :func:`kempower_station_to_charger_request` — Kempower charging-station JSON
  → ``ChargerCreateRequest`` Pydantic model.
- :func:`kempower_vehicle_to_identity` — Kempower vehicle JSON →
  ``VehicleIdentityBase`` Pydantic model.
- :func:`kempower_transaction_to_session_row` — Kempower transaction JSON →
  ``charging_sessions`` row dict (matches migration 030's import shape).
- :func:`kempower_location_to_site_suggestions` — Location + Power Group →
  diff payload for the optional ``--apply-site-suggestions`` flow.
- :func:`derive_vehicle_type`, :func:`compute_import_row_hash`,
  :data:`KEMPOWER_EXTERNAL_ID_PREFIX` — pure helpers shared by the
  adapter and the CLI.
"""

from .client import KempowerClient, KempowerClientError
from .defaults import (
    KEMPOWER_EXTERNAL_ID_PREFIX,
    compute_import_row_hash,
    derive_vehicle_type,
)
from .mapping import (
    KempowerChargerPayload,
    KempowerVehiclePayload,
    UnsupportedConnectorError,
    kempower_location_to_site_suggestions,
    kempower_station_to_charger_request,
    kempower_transaction_to_session_row,
    kempower_vehicle_to_identity,
)

__all__ = [
    "KempowerClient",
    "KempowerClientError",
    "KempowerChargerPayload",
    "KempowerVehiclePayload",
    "KEMPOWER_EXTERNAL_ID_PREFIX",
    "UnsupportedConnectorError",
    "compute_import_row_hash",
    "derive_vehicle_type",
    "kempower_location_to_site_suggestions",
    "kempower_station_to_charger_request",
    "kempower_transaction_to_session_row",
    "kempower_vehicle_to_identity",
]
