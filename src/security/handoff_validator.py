"""Handoff destination SSRF guard and HMAC helpers.

Two responsibilities live here:

1. ``validate_handoff_destination`` — block outbound handoff calls that target
   private/loopback/link-local addresses or use ``http`` outside development.
   Without this guard any tenant could trick the API into making requests
   against internal infrastructure (cloud metadata, RDS, etc.) by registering
   a depot with a hostname that resolves to a private IP.

2. ``compute_handoff_signature`` / ``verify_handoff_signature`` — small
   ``hmac.compare_digest``-based helpers for the inter-depot signature scheme.
   Sender and receiver share a single canonicalisation: the raw request body
   bytes. The signature travels in the ``X-Handoff-Signature`` header so it
   does not pollute the JSON body schema.

Reference: Audit finding H4 (handoff SSRF + HMAC).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import os
import socket
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

_PRIVATE_HOSTS_ENV = "HANDOFF_ALLOW_PRIVATE_HOSTS"
_DEV_ENVIRONMENTS = {"development", "test"}


def _resolve_addresses(host: str) -> list[ipaddress._BaseAddress]:
    """Resolve every A/AAAA record for ``host`` to ``ipaddress`` objects.

    A literal IP is returned as a single-element list. ``socket.gaierror`` is
    surfaced to the caller as an empty list so the validator can reject the
    URL with a 400 — better to refuse than to silently fall through.
    """
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return []

    addresses: list[ipaddress._BaseAddress] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            addresses.append(ipaddress.ip_address(ip_str))
        except ValueError:
            continue
    return addresses


def _is_blocked_address(addr: ipaddress._BaseAddress) -> bool:
    """Return True for addresses we never want a handoff to reach."""
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _allow_private_hosts(env: str) -> bool:
    """Honour ``HANDOFF_ALLOW_PRIVATE_HOSTS`` only inside dev/test environments.

    Production and staging ignore the flag entirely; private hosts there are
    always rejected so an operator misconfiguration cannot open the door.
    """
    if env not in _DEV_ENVIRONMENTS:
        return False
    return os.getenv(_PRIVATE_HOSTS_ENV, "false").lower() == "true"


def _host_header_value(hostname: str, port: int | None, scheme: str) -> str:
    """HTTP Host header value (include brackets for IPv6, optional non-default port)."""
    h = f"[{hostname}]" if ":" in hostname else hostname
    default = 443 if scheme == "https" else 80
    if port is None or port == default:
        return h
    return f"{h}:{port}"


def _ip_literal_for_url(addr: ipaddress._BaseAddress) -> str:
    """Netloc fragment for the resolved address (bracket IPv6)."""
    return f"[{addr.compressed}]" if addr.version == 6 else str(addr)


def _pinned_handoff_request_parts(url: str, env: str) -> tuple[str, str, dict[str, str]]:
    """Validate URL for SSRF policy and return an IP-pinned request URL + Host + httpx extensions.

    Connecting to the pinned IP avoids a second DNS lookup (DNS rebinding) while
    preserving the original server name via the Host header and TLS SNI
    (``extensions['sni_hostname']`` for HTTPS).
    """
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme not in {"http", "https"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid handoff scheme: {scheme!r}; only http/https allowed",
        )
    if scheme == "http" and env not in _DEV_ENVIRONMENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inter-depot handoff requires HTTPS outside development",
        )

    host = parts.hostname
    if not host:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Handoff URL is missing a hostname",
        )

    addresses = _resolve_addresses(host)
    if not addresses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not resolve handoff host: {host}",
        )

    allow_private = _allow_private_hosts(env)
    blocked = [str(a) for a in addresses if _is_blocked_address(a)]
    if blocked and not allow_private:
        logger.warning(
            "Handoff destination rejected: host=%s blocked_addresses=%s env=%s",
            host,
            blocked,
            env,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Handoff host {host!r} resolves to a disallowed address "
                f"({blocked[0]}); refusing to send"
            ),
        )

    pin_addr = addresses[0]
    netloc = _ip_literal_for_url(pin_addr)
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    request_url = urlunsplit((scheme, netloc, parts.path, parts.query, parts.fragment))
    host_header = _host_header_value(host, parts.port, scheme)
    extensions: dict[str, str] = {}
    if scheme == "https":
        # TLS verification uses this name; URL uses IP in netloc (see httpx docs).
        extensions["sni_hostname"] = host
    return request_url, host_header, extensions


def validate_handoff_destination(url: str, env: str) -> None:
    """Reject outbound handoff URLs that violate the SSRF policy.

    Args:
        url: Fully-qualified destination URL (scheme + host + optional port).
        env: Application environment ("development", "staging", "production").

    Raises:
        HTTPException(400): scheme/host invalid, ``http`` outside dev, or the
            host resolves to a private/loopback/link-local/reserved address.
    """
    _pinned_handoff_request_parts(url, env)


async def prepare_handoff_http_target(url: str, env: str) -> tuple[str, str, dict[str, str]]:
    """Like ``validate_handoff_destination`` but returns pinned URL parts for ``httpx``.

    DNS resolution runs in a thread pool so the event loop is not blocked.
    """
    return await asyncio.to_thread(_pinned_handoff_request_parts, url, env)


def compute_handoff_signature(secret: bytes, body: bytes) -> str:
    """HMAC-SHA256 hex digest of ``body`` keyed by ``secret``."""
    return hmac.new(secret, body, hashlib.sha256).hexdigest()


def verify_handoff_signature(secret: bytes, body: bytes, signature_hex: str) -> bool:
    """Constant-time check of an incoming handoff signature.

    Returns False on type mismatches or any HMAC mismatch.
    """
    if not isinstance(signature_hex, str) or not signature_hex:
        return False
    expected = compute_handoff_signature(secret, body)
    try:
        return hmac.compare_digest(expected, signature_hex)
    except (TypeError, ValueError):
        return False


__all__: Iterable[str] = (
    "compute_handoff_signature",
    "prepare_handoff_http_target",
    "validate_handoff_destination",
    "verify_handoff_signature",
)
