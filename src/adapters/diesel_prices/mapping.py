"""Diesel-price source → Favonius shape translation.

Pure functions and dataclasses only — no I/O. Mirrors the Navirec mapping
convention: a few candidate key tuples per field so a probe correction
(``scripts/probe_diesel_prices.py``) is a one-line edit, not a rewrite.

The wholesale ("ex-tax") price is the figure stored as ``price_eur_per_l`` —
that is the apples-to-apples number for a fleet operator who reclaims VAT and
buys via a fuel card. The consumer (incl-tax) price, when present, is retained
in ``raw_fields`` for reference but is not the comparison basis.

Unit / field names are PLACEHOLDERS confirmed by ``scripts/probe_diesel_prices``
against the chosen free source (EU Weekly Oil Bulletin by default; fuel-prices.eu
or Tankerkönig via ``DIESEL_PRICE_SOURCE``).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# Candidate source keys, most-specific first. Pin after the probe.
_COUNTRY_KEYS = ("countryCode", "country", "geo", "nuts0", "memberState", "iso2")
# Ex-tax / wholesale figure (preferred). EU Oil Bulletin publishes a
# "prices excluding taxes/duties" column — that is what we want.
_PRICE_EX_TAX_KEYS = (
    "priceExclTaxes",
    "priceExclТax",
    "netPrice",
    "exTax",
    "wholesale",
    "priceWithoutTaxes",
)
# Consumer / incl-tax figure (kept for reference only).
_PRICE_INC_TAX_KEYS = (
    "priceInclTaxes",
    "consumerPrice",
    "pumpPrice",
    "priceWithTaxes",
    "retail",
    "price",  # last resort — many feeds expose only one "price"
)
_DATE_KEYS = ("date", "referenceDate", "week", "period", "timestamp", "weekOf")

_NON_ALNUM = re.compile(r"[^A-Z0-9]")

# Plausible band for a wholesale diesel price in EUR/litre. Anything outside is
# treated as a unit error (e.g. a feed quoting EUR/1000L ≈ 1500, or ct/L ≈ 150)
# or garbage; see _coerce_price_eur_per_l for the auto-scaling it triggers.
_MIN_PLAUSIBLE_EUR_PER_L = 0.10
_MAX_PLAUSIBLE_EUR_PER_L = 10.0


@dataclass(frozen=True)
class DieselPrice:
    """One normalized wholesale diesel price point.

    ``price_eur_per_l`` is the ex-tax (wholesale) price; ``price_incl_tax_eur_per_l``
    is the consumer price when the source provides both.
    """

    time: datetime
    region: str  # ISO 3166-1 alpha-2 country code, uppercase
    price_eur_per_l: float
    source: str
    price_incl_tax_eur_per_l: Optional[float] = None
    raw_fields: dict[str, Any] = field(default_factory=dict)


def normalize_country(raw: Any) -> str:
    """Canonicalize a country code to uppercase ISO-3166 alpha-2, or ``""``.

    Strips non-alphanumerics and uppercases (so ``"de"``, ``"DE "`` and
    ``"DE-LU"`` → ``"DELU"`` — callers should pin a clean 2-letter source, but
    this never crashes on a malformed value).
    """
    if raw is None:
        return ""
    text = raw if isinstance(raw, str) else str(raw)
    return _NON_ALNUM.sub("", text.upper())


def _first(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """Return the first present, usable value among ``keys`` (skips blanks)."""
    for key in keys:
        if key not in raw:
            continue
        value = raw[key]
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _coerce_price_eur_per_l(value: Any) -> Optional[float]:
    """Coerce a diesel price into EUR/litre, or ``None``.

    Rejects ``bool``, unparseable input, non-finite, and non-positive values.
    Auto-scales two common unit encodings into EUR/L, then range-checks:

    - value in a cents-per-litre band (``> _MAX_PLAUSIBLE_EUR_PER_L`` and
      ``<= 1000``): divide by 100 (e.g. ``150.4`` ct/L → ``1.504`` €/L).
    - value in a per-1000-litre band (``> 1000``): divide by 1000 (the EU Oil
      Bulletin historically tabulated EUR/1000L, e.g. ``1504.0`` → ``1.504``).

    KNOWN LIMITATION (pin via scripts/probe_diesel_prices.py): the scaling is a
    heuristic. Once the chosen source's unit is confirmed, replace the band
    logic with the fixed divisor for that source. The final value must land in
    ``[_MIN_PLAUSIBLE_EUR_PER_L, _MAX_PLAUSIBLE_EUR_PER_L]`` or it's dropped.
    """
    if isinstance(value, bool):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    if price > 1000.0:
        price = price / 1000.0  # EUR/1000L → EUR/L
    elif price > _MAX_PLAUSIBLE_EUR_PER_L:
        price = price / 100.0  # ct/L → EUR/L
    if not (_MIN_PLAUSIBLE_EUR_PER_L <= price <= _MAX_PLAUSIBLE_EUR_PER_L):
        return None
    return price


def _coerce_time(value: Any) -> Optional[datetime]:
    """Parse a source date/timestamp into a tz-aware UTC datetime, or ``None``.

    Accepts an ISO date/datetime string (``"2026-05-20"`` / full ISO) or a
    numeric epoch (s or ms). Never fabricates a time — a record we can't
    time-stamp is dropped by the caller (the table is keyed on time).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # milliseconds
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit() and len(text) >= 10:
            return _coerce_time(int(text))
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        # A bare date ("2026-05-20") parses to midnight; treat as UTC.
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def parse_diesel_record(raw: dict[str, Any], *, source: str) -> Optional[DieselPrice]:
    """Map one source record to a :class:`DieselPrice`, or ``None`` to skip.

    Returns ``None`` when the record lacks a usable country, a parseable
    ex-tax (wholesale) price, or a timestamp — so a bad row is skipped rather
    than aborting the whole ingest cycle, exactly like the Navirec mapper.
    """
    region = normalize_country(_first(raw, _COUNTRY_KEYS))
    if not region:
        return None
    price = _coerce_price_eur_per_l(_first(raw, _PRICE_EX_TAX_KEYS))
    if price is None:
        # Fall back to the incl-tax / generic "price" field so a single-price
        # source still works — but only as the stored figure when no ex-tax
        # column exists at all. (Documented limitation: the comparison is then
        # against a tax-inclusive number; pin the ex-tax key via the probe.)
        price = _coerce_price_eur_per_l(_first(raw, _PRICE_INC_TAX_KEYS))
        if price is None:
            return None
    when = _coerce_time(_first(raw, _DATE_KEYS))
    if when is None:
        return None
    incl = _coerce_price_eur_per_l(_first(raw, _PRICE_INC_TAX_KEYS))
    return DieselPrice(
        time=when,
        region=region,
        price_eur_per_l=price,
        source=source,
        price_incl_tax_eur_per_l=incl,
        raw_fields=dict(raw),
    )
