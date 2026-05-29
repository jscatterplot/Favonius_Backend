#!/usr/bin/env python3
"""Read-only probe of the Navirec telematics API.

Authenticates with the configured credentials and prints the *shape* of the
responses so the field-name constants in ``src/adapters/navirec/mapping.py``
(``_PLATE_KEYS``, ``_SOC_KEYS``, …) and the endpoint paths in
``src/adapters/navirec/client.py`` can be pinned to reality. Makes only GET
requests — no writes anywhere.

Usage::

    NAVIREC_API_KEY=... python scripts/probe_navirec_api.py
    NAVIREC_USERNAME=... NAVIREC_PASSWORD=... python scripts/probe_navirec_api.py

Set ``NAVIREC_API_BASE_URL`` to point at a sandbox if needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.adapters.navirec import NavirecClient  # noqa: E402
from src.adapters.navirec.mapping import (  # noqa: E402
    _ODOMETER_KEYS,
    _coerce_odometer,
    _first,
    navirec_vehicle_id,
    navirec_vehicle_plate,
    navirec_vehicle_to_reading,
)

logger = logging.getLogger("probe_navirec")


async def _amain() -> int:
    client = NavirecClient()
    auth_mode = "api_key" if client._api_key else "username/password"  # noqa: SLF001
    print(f"Auth mode: {auth_mode}")
    print(f"Base URL : {client._base_url}")  # noqa: SLF001

    try:
        count = 0
        first: dict | None = None
        async for vehicle in client.iter_vehicles():
            if first is None:
                first = vehicle
            count += 1
            if count >= 50:
                break

        print(f"\nFetched {count} vehicle object(s).")
        if first is None:
            print("No vehicles returned — check account scope / endpoint path.")
            return 0

        print("\nFirst vehicle object keys:")
        print("  " + ", ".join(sorted(first.keys())))
        print("\nFirst vehicle object (pretty):")
        print(json.dumps(first, indent=2, default=str)[:2000])

        print("\nMapper interpretation of the first object:")
        print(f"  plate     → {navirec_vehicle_plate(first)!r}")
        print(f"  navirec_id→ {navirec_vehicle_id(first)!r}")
        raw_odo = _first(first, _ODOMETER_KEYS)
        print(f"  odometer  → raw={raw_odo!r} coerced_km={_coerce_odometer(raw_odo)!r}")
        reading = navirec_vehicle_to_reading(first)
        print(f"  reading   → {reading!r}")
        if reading is None:
            print(
                "  ⚠ mapper returned None — plate, SoC, or odometer field name likely "
                "differs; update _PLATE_KEYS / _SOC_KEYS / _ODOMETER_KEYS in mapping.py."
            )
        elif raw_odo is None:
            print(
                "  ⚠ no odometer field matched — distance reporting needs it; "
                "pin _ODOMETER_KEYS in mapping.py against the keys printed above, "
                "and confirm the UNIT (km vs m) before trusting _coerce_odometer."
            )
    finally:
        await client.aclose()
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return asyncio.run(_amain())
    except Exception:  # noqa: BLE001
        logger.exception("Probe failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
