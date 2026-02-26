"""ENTSO-E bidding zone mappings.

Maps European timezones and country codes to ENTSO-E EIC area codes
used for querying day-ahead electricity prices.

Reference: https://transparency.entsoe.eu/
"""

from __future__ import annotations

# Country code (ISO 3166-1 alpha-2) -> EIC area code
COUNTRY_TO_BIDDING_ZONE: dict[str, str] = {
    "AL": "10YAL-KESH-----5",  # Albania
    "AT": "10YAT-APG------L",  # Austria
    "BE": "10YBE----------2",  # Belgium
    "BA": "10YBA-JPCC-----D",  # Bosnia & Herzegovina
    "BG": "10YCA-BULGARIA-R",  # Bulgaria
    "HR": "10YHR-HEP------M",  # Croatia
    "CZ": "10YCZ-CEPS-----N",  # Czech Republic
    "DK": "10YDK-1--------W",  # Denmark (West - default)
    "EE": "10Y1001A1001A39I",  # Estonia
    "FI": "10YFI-1--------U",  # Finland
    "FR": "10YFR-RTE------C",  # France
    "DE": "10Y1001A1001A82H",  # Germany/Luxembourg
    "GB": "10YGB----------A",  # Great Britain
    "GR": "10YGR-HTSO-----Y",  # Greece
    "HU": "10YHU-MAVIR----U",  # Hungary
    "IE": "10Y1001A1001A59C",  # Ireland (SEM)
    "IT": "10Y1001A1001A73I",  # Italy (North - default)
    "LV": "10YLV-1001A00074",  # Latvia
    "LT": "10YLT-1001A0008Q",  # Lithuania
    "LU": "10YLU-CEGEDEL-NQ",  # Luxembourg
    "MT": "10Y1001A1001A93C",  # Malta
    "ME": "10YCS-CG-TSO---S",  # Montenegro
    "NL": "10YNL----------L",  # Netherlands
    "MK": "10YMK-MEPSO----8",  # North Macedonia
    "NO": "10YNO-1--------2",  # Norway (South-East - default)
    "PL": "10YPL-AREA-----S",  # Poland
    "PT": "10YPT-REN------W",  # Portugal
    "RO": "10YRO-TEL------P",  # Romania
    "RS": "10YCS-SERBIATSOV",  # Serbia
    "SK": "10YSK-SEPS-----K",  # Slovakia
    "SI": "10YSI-ELES-----O",  # Slovenia
    "ES": "10YES-REE------0",  # Spain
    "SE": "10Y1001A1001A46L",  # Sweden (Stockholm - default)
    "CH": "10YCH-SWISSGRIDZ",  # Switzerland
}

# Map IANA timezones to country codes for automatic region detection
TIMEZONE_TO_COUNTRY: dict[str, str] = {
    "Europe/Tirane": "AL",
    "Europe/Vienna": "AT",
    "Europe/Brussels": "BE",
    "Europe/Sarajevo": "BA",
    "Europe/Sofia": "BG",
    "Europe/Zagreb": "HR",
    "Europe/Prague": "CZ",
    "Europe/Copenhagen": "DK",
    "Europe/Tallinn": "EE",
    "Europe/Helsinki": "FI",
    "Europe/Paris": "FR",
    "Europe/Berlin": "DE",
    "Europe/London": "GB",
    "Europe/Athens": "GR",
    "Europe/Budapest": "HU",
    "Europe/Dublin": "IE",
    "Europe/Rome": "IT",
    "Europe/Milan": "IT",
    "Europe/Riga": "LV",
    "Europe/Vilnius": "LT",
    "Europe/Luxembourg": "LU",
    "Europe/Malta": "MT",
    "Europe/Podgorica": "ME",
    "Europe/Amsterdam": "NL",
    "Europe/Skopje": "MK",
    "Europe/Oslo": "NO",
    "Europe/Warsaw": "PL",
    "Europe/Lisbon": "PT",
    "Europe/Bucharest": "RO",
    "Europe/Belgrade": "RS",
    "Europe/Bratislava": "SK",
    "Europe/Ljubljana": "SI",
    "Europe/Madrid": "ES",
    "Europe/Stockholm": "SE",
    "Europe/Zurich": "CH",
    # Additional city aliases
    "Europe/Munich": "DE",
    "Europe/Frankfurt": "DE",
    "Europe/Hamburg": "DE",
    "Europe/Dusseldorf": "DE",
    "Europe/Cologne": "DE",
    "Europe/Marseille": "FR",
    "Europe/Lyon": "FR",
    "Europe/Barcelona": "ES",
}

# Direct timezone -> EIC code mapping (for convenience)
TIMEZONE_TO_BIDDING_ZONE: dict[str, str] = {
    tz: COUNTRY_TO_BIDDING_ZONE[cc]
    for tz, cc in TIMEZONE_TO_COUNTRY.items()
    if cc in COUNTRY_TO_BIDDING_ZONE
}

# European timezone prefixes for quick detection
_EUROPEAN_TZ_PREFIXES = ("Europe/",)


def is_european_timezone(timezone: str) -> bool:
    """Check if a timezone is European.

    Args:
        timezone: IANA timezone string (e.g., 'Europe/Berlin')

    Returns:
        True if the timezone is in Europe
    """
    return timezone.startswith(_EUROPEAN_TZ_PREFIXES)


def get_bidding_zone(timezone: str) -> str | None:
    """Get the ENTSO-E bidding zone EIC code for a timezone.

    Args:
        timezone: IANA timezone string (e.g., 'Europe/Berlin')

    Returns:
        EIC area code string, or None if not a mapped European timezone
    """
    return TIMEZONE_TO_BIDDING_ZONE.get(timezone)


def get_bidding_zone_for_country(country_code: str) -> str | None:
    """Get the ENTSO-E bidding zone EIC code for a country.

    Args:
        country_code: ISO 3166-1 alpha-2 country code (e.g., 'DE')

    Returns:
        EIC area code string, or None if not mapped
    """
    return COUNTRY_TO_BIDDING_ZONE.get(country_code.upper())
