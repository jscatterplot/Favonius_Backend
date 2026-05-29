#!/usr/bin/env python3
"""Read-only probe of the configured diesel-price source.

Fetches one page from the chosen source (``DIESEL_PRICE_SOURCE``) and prints the
*shape* of the records so the field-name constants in
``src/adapters/diesel_prices/mapping.py`` (``_COUNTRY_KEYS``, ``_PRICE_EX_TAX_KEYS``,
…) and the endpoint paths in ``src/adapters/diesel_prices/client.py`` can be
pinned to reality — and so the price UNIT (EUR/L vs ct/L vs EUR/1000L) can be
confirmed before trusting ``_coerce_price_eur_per_l``. Makes only GET requests —
no writes anywhere.

Usage::

    DIESEL_PRICE_SOURCE=eu_oil_bulletin python scripts/probe_diesel_prices.py
    DIESEL_PRICE_SOURCE=fuel_prices_eu DIESEL_PRICE_API_KEY=... \\
        DIESEL_PRICE_API_BASE_URL=... python scripts/probe_diesel_prices.py
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

from src.adapters.diesel_prices.client import DieselPriceClient  # noqa: E402
from src.adapters.diesel_prices.mapping import (  # noqa: E402
    _COUNTRY_KEYS,
    _DATE_KEYS,
    _PRICE_EX_TAX_KEYS,
    _PRICE_INC_TAX_KEYS,
    _coerce_price_eur_per_l,
    _first,
    normalize_country,
    parse_diesel_record,
)

logger = logging.getLogger("probe_diesel_prices")


async def _amain() -> int:
    client = DieselPriceClient()
    print(f"Source   : {client.source}")
    print(f"Base URL : {client._base_url}")  # noqa: SLF001
    print(f"Path     : {client._prices_path}")  # noqa: SLF001

    try:
        count = 0
        first: dict | None = None
        async for record in client.iter_prices():
            if first is None:
                first = record
            count += 1
            if count >= 50:
                break

        print(f"\nFetched {count} record(s).")
        if first is None:
            print("No records returned — check source / endpoint path / auth.")
            return 0

        print("\nFirst record keys:")
        print("  " + ", ".join(sorted(first.keys())))
        print("\nFirst record (pretty):")
        print(json.dumps(first, indent=2, default=str)[:2000])

        print("\nMapper interpretation of the first record:")
        country_raw = _first(first, _COUNTRY_KEYS)
        ex_tax_raw = _first(first, _PRICE_EX_TAX_KEYS)
        inc_tax_raw = _first(first, _PRICE_INC_TAX_KEYS)
        date_raw = _first(first, _DATE_KEYS)
        print(f"  country   → raw={country_raw!r} normalized={normalize_country(country_raw)!r}")
        print(
            f"  ex-tax    → raw={ex_tax_raw!r} coerced_eur_per_l={_coerce_price_eur_per_l(ex_tax_raw)!r}"
        )
        print(
            f"  incl-tax  → raw={inc_tax_raw!r} coerced_eur_per_l={_coerce_price_eur_per_l(inc_tax_raw)!r}"
        )
        print(f"  date      → raw={date_raw!r}")
        record = parse_diesel_record(first, source=client.source)
        print(f"  parsed    → {record!r}")
        if record is None:
            print(
                "  ⚠ mapper returned None — country, price, or date field name likely "
                "differs; update _COUNTRY_KEYS / _PRICE_*_KEYS / _DATE_KEYS in mapping.py."
            )
        else:
            print(
                "  NOTE: confirm the price UNIT above is EUR/L. If the source reports "
                "ct/L or EUR/1000L, _coerce_price_eur_per_l auto-scales — but pin the "
                "exact divisor once known rather than relying on the heuristic."
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
