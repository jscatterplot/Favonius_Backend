"""Shared helpers for parsing forwarded client IP headers."""

from __future__ import annotations

import ipaddress
import logging
from typing import Optional


def normalize_forwarded_ip(raw_ip: str) -> Optional[str]:
    """Normalize one forwarded IP candidate, stripping quotes, brackets, ports."""
    value = raw_ip.strip().strip('"')
    if not value:
        return None

    if value.startswith("["):
        host, separator, _port = value[1:].partition("]")
        value = host if separator else value
    elif value.count(":") == 1 and "." in value:
        # IPv4 with port suffix, e.g. "203.0.113.5:54321"
        value = value.rsplit(":", 1)[0]

    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def extract_forwarded_ip(headers: object) -> Optional[str]:
    """Extract the original client IP from common reverse-proxy headers."""
    if not hasattr(headers, "get"):
        return None

    forwarded = headers.get("Forwarded", "") or ""
    for proxy_hop in forwarded.split(","):
        for item in proxy_hop.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key.lower() == "for":
                parsed = normalize_forwarded_ip(value)
                if parsed:
                    return parsed

    x_forwarded_for = headers.get("X-Forwarded-For", "") or ""
    for candidate in x_forwarded_for.split(","):
        parsed = normalize_forwarded_ip(candidate)
        if parsed:
            return parsed

    x_real_ip = headers.get("X-Real-IP", "") or ""
    return normalize_forwarded_ip(x_real_ip) if x_real_ip else None


def parse_ip_networks(
    ranges: str,
    *,
    logger: logging.Logger,
    env_var_name: str,
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse a comma-separated list of CIDR ranges, ignoring invalid entries."""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for raw_range in ranges.split(","):
        raw_range = raw_range.strip()
        if not raw_range:
            continue
        try:
            networks.append(ipaddress.ip_network(raw_range, strict=False))
        except ValueError:
            logger.warning("Invalid %s entry ignored: %s", env_var_name, raw_range)
    return networks
