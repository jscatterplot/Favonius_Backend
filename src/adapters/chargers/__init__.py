"""Per-vendor charger log parser registry.

Every vendor's diagnostic archive has its own shape. The OCPP
``GetDiagnostics`` / ``GetLog`` *transport* is standardised — the file
contents are not. This package owns the vendor → parser dispatch so the
reconciler and upload endpoint stay vendor-agnostic.

Adding a new vendor:
    1. Create ``src/adapters/chargers/<vendor>/log_parser.py`` exposing a
       ``parse(blob: bytes) -> Iterable[ChargerLogEntry]`` callable.
    2. Register it in ``_REGISTRY`` below.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable, Optional

from .types import ChargerLogEntry, ChargerLogParseError

ChargerLogParser = Callable[[bytes], Iterable[ChargerLogEntry]]


def _normalize_vendor(vendor: str) -> str:
    """Casefold + strip non-alphanumerics, matching ``_is_abb_vendor``.

    Keeps "ABB E-mobility", "abb", "ABB-emobility" mapping to the same key.
    """
    return re.sub(r"[^a-z0-9]+", "", vendor.strip().lower()) if vendor else ""


def _abb_parser() -> ChargerLogParser:
    """Lazy import so importing the registry doesn't pull tarfile."""
    from .abb.log_parser import parse_abb_diagnostics

    return parse_abb_diagnostics


# Vendor → factory mapping. Factories are lazy so test environments
# without optional deps don't pay an import cost just to register.
_REGISTRY: dict[str, Callable[[], ChargerLogParser]] = {
    "abb": _abb_parser,
}


def get_parser_for_vendor(vendor: Optional[str]) -> Optional[ChargerLogParser]:
    """Return the parser callable for a vendor, or None if unsupported.

    Returning None is *not* an error — the upload endpoint preserves the
    raw blob either way. A future parser onboarding can backfill entries
    without re-fetching from the charger.
    """
    if not vendor:
        return None
    key = _normalize_vendor(vendor)
    if "abb" in key:  # accept "abb", "abbemobility", "abbtterra", etc.
        key = "abb"
    factory = _REGISTRY.get(key)
    if factory is None:
        return None
    return factory()


__all__ = [
    "ChargerLogEntry",
    "ChargerLogParseError",
    "ChargerLogParser",
    "get_parser_for_vendor",
]
