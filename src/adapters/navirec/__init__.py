"""Navirec telematics adapter — live SoC/position feed for the fleet.

A background poller (:func:`run_navirec_poll_loop`) pulls the fleet's latest
telematics readings from Navirec into the ``vehicle_telemetry`` hypertable
(migration 044), which ``StateAssembler._get_vehicle_socs`` merges with charger
``telemetry`` (freshest reading per vehicle wins). This gives the optimizer a
live SoC even when a vehicle is unplugged.

Public surface:

- :class:`NavirecClient` / :class:`NavirecClientError` — REST client.
- :func:`normalize_plate`, :class:`VehicleTelemetryReading`,
  :func:`navirec_vehicle_to_reading` — pure mapping helpers shared by the
  poller and the historical backfill CLI.
- :func:`run_navirec_poll_loop`, :func:`poll_once`, :func:`write_depot_readings`,
  :func:`build_plate_map`, :func:`resolve_readings` — the live feed.
"""

from .client import NavirecClient, NavirecClientError
from .mapping import (
    VehicleTelemetryReading,
    navirec_vehicle_id,
    navirec_vehicle_plate,
    navirec_vehicle_to_reading,
    normalize_plate,
)
from .poller import (
    build_plate_map,
    poll_once,
    resolve_readings,
    run_navirec_poll_loop,
    write_depot_readings,
)

__all__ = [
    "NavirecClient",
    "NavirecClientError",
    "VehicleTelemetryReading",
    "navirec_vehicle_id",
    "navirec_vehicle_plate",
    "navirec_vehicle_to_reading",
    "normalize_plate",
    "build_plate_map",
    "poll_once",
    "resolve_readings",
    "run_navirec_poll_loop",
    "write_depot_readings",
]
