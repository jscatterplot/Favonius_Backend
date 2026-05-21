"""Shared helpers for parsing forwarded client IP headers.

When the API or WebSocket handler runs behind a reverse proxy (Railway edge,
Cloudflare, on-prem nginx, the Lithuanian DSO firewall), the immediate TCP
peer is the proxy, not the real client. Geo-blocking decisions and audit
logs must use the real client IP, which the proxy advertises via the
``X-Forwarded-For`` / ``Forwarded`` / ``X-Real-IP`` headers.

The naive "leftmost X-Forwarded-For wins" pattern is unsafe: when the proxy
*appends* its view (the common nginx and most-PaaS default), an attacker
can prepend `X-Forwarded-For: 8.8.8.8` and the leftmost lookup returns the
spoofed value. :func:`extract_forwarded_ip` walks the chain right-to-left
instead, skipping entries that are themselves trusted proxies, and returns
the rightmost non-trusted entry — that is the real origin client.

Callers must first verify the immediate TCP peer is a trusted proxy before
passing headers to this function. The trust gate stays in the caller; the
parser only consults trust info to decide which chain entries are
intermediate proxies and which is the real client.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Iterable, Optional, Union

# RFC 6598 carrier-grade NAT shared address space. Treated as implicitly
# trusted when callers opt into private-proxy trust — Railway, Render,
# Fly.io and other PaaS providers route between their edge and the
# container over this range. Python's ``ipaddress`` does not classify it
# as ``is_private`` so we check it explicitly.
#
# Note: RFC 1918 private, loopback, and link-local addresses are NOT
# implicitly trusted as chain hops. On-prem deployments where the real
# client lives behind a private-IP load balancer would otherwise let an
# attacker spoof ``X-Forwarded-For: 8.8.8.8`` and have the LB-appended
# private client IP get skipped as a "proxy" — surfacing the spoof. For
# those topologies, declare the LB's CIDR explicitly in
# ``GEO_BLOCK_TRUSTED_PROXY_RANGES`` / ``OCPP_TRUSTED_PROXY_RANGES``.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

_Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]


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


def _is_implicitly_trusted_chain_hop(
    ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address],
) -> bool:
    """True only for RFC 6598 CGNAT — the PaaS-edge-to-container address space.

    RFC 1918 private, loopback, and link-local are deliberately excluded:
    they can be legitimate *client* IPs in on-prem / VPN / Docker / K8s
    deployments, and auto-skipping them as proxies would let an attacker
    spoof their origin by prepending ``X-Forwarded-For: 8.8.8.8`` so the
    LB-appended private client IP gets discarded as a "proxy hop".
    Such deployments declare their proxy CIDR explicitly via
    ``trusted_networks``.
    """
    return isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NETWORK


def _is_trusted_proxy_entry(
    ip_str: str,
    trusted_networks: tuple[_Network, ...],
    trust_implicit_private: bool,
) -> bool:
    """True when this chain entry represents a known intermediate proxy."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    explicitly_trusted = any(
        ip.version == net.version and ip in net for net in trusted_networks
    )
    if explicitly_trusted:
        return True
    # Do not blanket-trust private/loopback/link-local chain entries when
    # explicit proxy CIDRs are configured: private-network clients are common
    # in VPN/on-prem deployments and must remain eligible as the resolved
    # origin IP. Implicit trust is retained only for legacy "no CIDRs"
    # deployments where the caller intentionally opts in.
    if trust_implicit_private and _is_implicitly_trusted_chain_hop(ip):
        return True
    return False


def _select_client_ip(
    chain: list[str],
    trusted_networks: tuple[_Network, ...],
    trust_implicit_private: bool,
) -> Optional[str]:
    """Return the rightmost non-trusted IP in a forwarded chain.

    Callers without trust info (``trusted_networks=()`` and
    ``trust_implicit_private=False``) get the legacy leftmost-wins behaviour
    so older call sites do not silently change semantics. With trust info,
    the chain is walked right-to-left and the first non-trusted entry is
    returned. When every entry is a known proxy we fall back to the
    leftmost entry — purely internal traffic still surfaces "the claimed
    origin" rather than nothing.
    """
    if not chain:
        return None
    if not (trusted_networks or trust_implicit_private):
        return chain[0]
    for candidate in reversed(chain):
        if not _is_trusted_proxy_entry(candidate, trusted_networks, trust_implicit_private):
            return candidate
    return chain[0]


def _parse_forwarded_header_chain(forwarded: str) -> list[str]:
    """Parse RFC 7239 ``Forwarded`` header into an ordered list of ``for=`` IPs."""
    result: list[str] = []
    for proxy_hop in forwarded.split(","):
        for item in proxy_hop.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key.lower() == "for":
                parsed = normalize_forwarded_ip(value)
                if parsed:
                    result.append(parsed)
                break
    return result


def _parse_xff_chain(xff: str) -> list[str]:
    """Parse ``X-Forwarded-For`` into an ordered list of valid IPs."""
    result: list[str] = []
    for candidate in xff.split(","):
        parsed = normalize_forwarded_ip(candidate)
        if parsed:
            result.append(parsed)
    return result


def extract_forwarded_ip(
    headers: object,
    *,
    trusted_networks: Iterable[_Network] = (),
    trust_implicit_private: bool = False,
) -> Optional[str]:
    """Extract the original client IP from forwarded-header chains.

    Walks ``X-Forwarded-For`` (and RFC 7239 ``Forwarded``) right-to-left,
    skipping entries that are themselves trusted proxies, and returns the
    rightmost non-trusted entry — the original client. Falls back to
    ``X-Real-IP`` when neither chain header is present.

    Args:
        headers: A mapping-like object exposing case-insensitive ``.get(name)``.
        trusted_networks: CIDRs that act as reverse proxies for this
            deployment. Entries in the chain whose IPs fall in any of these
            ranges are treated as intermediate hops, not the client.
        trust_implicit_private: When True, RFC 6598 CGNAT addresses
            (100.64.0.0/10) are treated as intermediate proxies — this
            covers Railway, Render, Fly.io, and similar PaaS providers.
            RFC 1918 private, loopback, and link-local addresses are
            deliberately NOT implicitly trusted because they may be real
            client IPs on on-prem / VPN / Docker / K8s networks; declare
            those proxy CIDRs explicitly in ``trusted_networks``.

    Returns:
        The resolved client IP, or ``None`` when no valid header is present.

    Safety:
        Callers MUST first verify the immediate TCP peer is a trusted proxy
        before invoking this function. Otherwise an arbitrary internet peer
        can supply any value as the "client" and bypass geo-blocking or
        allowlists.
    """
    if not hasattr(headers, "get"):
        return None

    networks = tuple(trusted_networks)

    forwarded_chain = _parse_forwarded_header_chain(headers.get("Forwarded", "") or "")
    chosen = _select_client_ip(forwarded_chain, networks, trust_implicit_private)
    if chosen:
        return chosen

    xff_chain = _parse_xff_chain(headers.get("X-Forwarded-For", "") or "")
    chosen = _select_client_ip(xff_chain, networks, trust_implicit_private)
    if chosen:
        return chosen

    x_real_ip = headers.get("X-Real-IP", "") or ""
    return normalize_forwarded_ip(x_real_ip) if x_real_ip else None


def parse_ip_networks(
    ranges: str,
    *,
    logger: logging.Logger,
    env_var_name: str,
) -> list[_Network]:
    """Parse a comma-separated list of CIDR ranges, ignoring invalid entries."""
    networks: list[_Network] = []
    for raw_range in ranges.split(","):
        raw_range = raw_range.strip()
        if not raw_range:
            continue
        try:
            networks.append(ipaddress.ip_network(raw_range, strict=False))
        except ValueError:
            logger.warning("Invalid %s entry ignored: %s", env_var_name, raw_range)
    return networks
