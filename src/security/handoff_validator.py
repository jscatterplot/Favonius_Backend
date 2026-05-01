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

import hashlib
import hmac
import ipaddress
import logging
import os
import socket
from typing import Iterable
from urllib.parse import urlparse

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


def validate_handoff_destination(url: str, env: str) -> None:
    """Reject outbound handoff URLs that violate the SSRF policy.

    Args:
        url: Fully-qualified destination URL (scheme + host + optional port).
        env: Application environment ("development", "staging", "production").

    Raises:
        HTTPException(400): scheme/host invalid, ``http`` outside dev, or the
            host resolves to a private/loopback/link-local/reserved address.
    """
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid handoff scheme: {parsed.scheme!r}; only http/https allowed",
        )
    if parsed.scheme == "http" and env not in _DEV_ENVIRONMENTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Inter-depot handoff requires HTTPS outside development",
        )

    host = parsed.hostname
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
        # Log once with all blocked IPs so an operator can diagnose multi-A
        # records where one entry is private and forces a reject.
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
    "validate_handoff_destination",
    "verify_handoff_signature",
)
