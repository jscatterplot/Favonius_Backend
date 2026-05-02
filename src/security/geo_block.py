"""Geo-blocking middleware for Article 73-3 compliance.

Lithuanian Electric Energy Law Article 73-3 requires that energy
production/storage systems >100 kW prevent remote access from countries
that pose a threat to Lithuanian national security (Russia, China, Belarus
per the National Security Strategy).

This module provides:
- IP-to-country resolution via MaxMind GeoLite2-Country database
- Configurable country blocklist and IP allowlist
- Fail-closed behavior: if GeoIP resolution fails, the request is blocked
- FastAPI middleware for HTTP endpoints (Main API)
- Standalone check function for WebSocket connections (WebSocket Handler)
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import os
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from .forwarded_ip import extract_forwarded_ip, parse_ip_networks

logger = logging.getLogger(__name__)

# Default blocked countries per Lithuanian National Security Strategy
_DEFAULT_BLOCKED_COUNTRIES = ["RU", "CN", "BY"]

# Default path to the MaxMind GeoLite2-Country database
_DEFAULT_GEOIP_DB_PATH = os.getenv("GEOIP_DB_PATH", "/app/data/GeoLite2-Country.mmdb")

# RFC 6598 carrier-grade NAT (CGNAT) shared address space. Not routable on the
# public internet; used by Railway, Fly.io, Render, and other PaaS providers
# for the internal network between their edge proxy and the application
# container. Python's ipaddress module does NOT classify this as is_private,
# so we treat it explicitly when deciding whether to trust forwarded headers.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")

# Runtime download configuration. Build-time download lives in the Dockerfile;
# this is the second-layer fallback used when the build-time download failed
# (transient MaxMind outage) or the image was built without
# MAXMIND_ACCOUNT_ID / MAXMIND_LICENSE_KEY.
#
# As of MaxMind's 2024 policy change the legacy `?license_key=` query-param
# endpoint is deprecated. Database downloads now require HTTP Basic Auth with
# the account ID as the username and the license key as the password against
# the new `/geoip/databases/{edition_id}/download` endpoint.
_MAXMIND_DOWNLOAD_URL = (
    "https://download.maxmind.com/geoip/databases/GeoLite2-Country/download?suffix=tar.gz"
)
_DEFAULT_DOWNLOAD_RETRIES = 3
_DEFAULT_DOWNLOAD_BACKOFF_S = 5.0
_DEFAULT_DOWNLOAD_TIMEOUT_S = 30.0
_GZIP_MAGIC = b"\x1f\x8b"
_MMDB_FILENAME_SUFFIX = "GeoLite2-Country.mmdb"


def _download_geoip_db(
    db_path: str,
    license_key: str,
    *,
    account_id: str = "",
    retries: int = _DEFAULT_DOWNLOAD_RETRIES,
    backoff_s: float = _DEFAULT_DOWNLOAD_BACKOFF_S,
    timeout_s: float = _DEFAULT_DOWNLOAD_TIMEOUT_S,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Download GeoLite2-Country.mmdb to db_path, with retries.

    Idempotent: returns True immediately if a non-empty file already exists at
    db_path. Returns False (and logs a warning) on any failure — never raises,
    so a missing DB still falls through to the existing fail-closed runtime path.

    Authenticates to MaxMind's database download endpoint via HTTP Basic Auth
    (account ID as username, license key as password) per MaxMind's 2024
    policy change.

    Args:
        db_path: Destination .mmdb path.
        license_key: MaxMind license key. Empty string is a no-op.
        account_id: MaxMind account ID. Empty string is a no-op (paired with
            license_key, both are required by MaxMind's current download API).
        retries: Retry attempts after the initial try (total = retries + 1).
        backoff_s: Linear backoff base; sleep is backoff_s * (attempt + 1).
        timeout_s: Per-request HTTP timeout in seconds.
        sleep: Injectable sleep for tests.

    Returns:
        True if a valid .mmdb is at db_path after this call, False otherwise.
    """
    if not license_key or not account_id:
        return False

    if os.path.exists(db_path) and os.path.getsize(db_path) > 0:
        logger.debug("GeoIP DB already at %s, skipping runtime download", db_path)
        return True

    parent_dir = os.path.dirname(db_path) or "."
    try:
        os.makedirs(parent_dir, exist_ok=True)
    except OSError as exc:
        logger.error("Cannot create GeoIP DB parent dir %s: %s", parent_dir, exc)
        return False

    auth_token = base64.b64encode(f"{account_id}:{license_key}".encode("utf-8")).decode("ascii")
    total_attempts = retries + 1
    last_error = ""

    for attempt in range(total_attempts):
        tmp_path: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
                tmp_path = tmp.name
            request = urllib.request.Request(
                _MAXMIND_DOWNLOAD_URL,
                headers={"Authorization": f"Basic {auth_token}"},
            )
            with urllib.request.urlopen(request, timeout=timeout_s) as resp:  # noqa: S310
                status = getattr(resp, "status", 200)
                if status != 200:
                    raise urllib.error.HTTPError(
                        _MAXMIND_DOWNLOAD_URL,
                        status,
                        getattr(resp, "reason", ""),
                        resp.headers,
                        None,
                    )
                with open(tmp_path, "wb") as out:
                    shutil.copyfileobj(resp, out)

            with open(tmp_path, "rb") as f:
                if f.read(2) != _GZIP_MAGIC:
                    raise ValueError("response is not a gzip archive")

            _extract_mmdb(tmp_path, db_path)
            logger.info(
                "Downloaded GeoIP database to %s (attempt %d/%d)",
                db_path,
                attempt + 1,
                total_attempts,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — never let download crash startup
            last_error = str(exc) or type(exc).__name__
            logger.warning(
                "GeoIP download attempt %d/%d failed: %s",
                attempt + 1,
                total_attempts,
                last_error,
            )
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        if attempt < retries:
            sleep(backoff_s * (attempt + 1))

    logger.error(
        "GeoIP download failed after %d attempts (last error: %s) — "
        "geo-blocking will operate in fail-closed mode",
        total_attempts,
        last_error,
    )
    return False


def _extract_mmdb(archive_path: str, dest_path: str) -> None:
    """Extract the GeoLite2-Country.mmdb member from a tar.gz to dest_path.

    Writes via a temp file in the same directory and atomically renames so a
    partial extraction never replaces a good DB.
    """
    with tarfile.open(archive_path, "r:gz") as tar:
        member = next(
            (m for m in tar.getmembers() if m.name.endswith(_MMDB_FILENAME_SUFFIX)),
            None,
        )
        if member is None:
            raise ValueError(f"{_MMDB_FILENAME_SUFFIX} not found in archive")
        src = tar.extractfile(member)
        if src is None:
            raise ValueError(f"could not read {_MMDB_FILENAME_SUFFIX} from archive")
        dest_dir = os.path.dirname(dest_path) or "."
        with tempfile.NamedTemporaryFile(
            dir=dest_dir, prefix=".geoip-", suffix=".mmdb", delete=False
        ) as staging:
            staging_path = staging.name
            shutil.copyfileobj(src, staging)
        os.replace(staging_path, dest_path)


# Try to import geoip2; if unavailable, the module operates in fail-closed mode
try:
    import geoip2.database
    import geoip2.errors

    GEOIP2_AVAILABLE = True
except ImportError:
    GEOIP2_AVAILABLE = False
    logger.warning(
        "geoip2 package not installed — geo-blocking will operate in "
        "fail-closed mode (all non-allowlisted IPs blocked)"
    )


@dataclass
class GeoBlockConfig:
    """Configuration for geo-blocking.

    Attributes:
        blocked_countries: ISO 3166-1 alpha-2 country codes to block.
        allowed_ips: IP addresses that bypass geo-blocking (e.g. auditor IPs).
        geoip_db_path: Path to the MaxMind GeoLite2-Country.mmdb file.
        enabled: Whether geo-blocking is active.
        fail_closed: If True, block requests when GeoIP resolution fails.
    """

    blocked_countries: list[str] = field(default_factory=list)
    allowed_ips: list[str] = field(default_factory=list)
    geoip_db_path: str = _DEFAULT_GEOIP_DB_PATH
    enabled: bool = True
    fail_closed: bool = True

    @classmethod
    def from_env(cls) -> GeoBlockConfig:
        """Create config from environment variables.

        Environment variables:
            GEO_BLOCK_ENABLED: "true"/"false" (default "true")
            GEO_BLOCK_COUNTRIES: Comma-separated country codes (default "RU,CN,BY")
            GEO_BLOCK_ALLOWLIST: Comma-separated IPs to always allow
            GEOIP_DB_PATH: Path to GeoLite2-Country.mmdb
            GEO_BLOCK_FAIL_CLOSED: "true"/"false" (default "true")
        """
        enabled = os.getenv("GEO_BLOCK_ENABLED", "true").lower() == "true"
        countries_str = os.getenv("GEO_BLOCK_COUNTRIES", "RU,CN,BY")
        blocked_countries = [c.strip().upper() for c in countries_str.split(",") if c.strip()]
        allowlist_str = os.getenv("GEO_BLOCK_ALLOWLIST", "")
        allowed_ips = [ip.strip() for ip in allowlist_str.split(",") if ip.strip()]
        geoip_db_path = os.getenv("GEOIP_DB_PATH", _DEFAULT_GEOIP_DB_PATH)
        fail_closed = os.getenv("GEO_BLOCK_FAIL_CLOSED", "true").lower() == "true"

        return cls(
            blocked_countries=blocked_countries,
            allowed_ips=allowed_ips,
            geoip_db_path=geoip_db_path,
            enabled=enabled,
            fail_closed=fail_closed,
        )


class GeoBlockChecker:
    """Performs IP-to-country resolution and geo-blocking checks.

    Loads the MaxMind GeoLite2-Country database once into memory for
    sub-microsecond lookups. Thread-safe for concurrent use.
    """

    def __init__(self, config: GeoBlockConfig) -> None:
        self.config = config
        self._reader: Optional[object] = None
        self._allowed_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []

        # Parse allowed IPs/CIDRs into network objects for efficient matching
        for ip_str in config.allowed_ips:
            try:
                self._allowed_networks.append(ipaddress.ip_network(ip_str, strict=False))
            except ValueError:
                logger.warning("Invalid IP/CIDR in allowlist, skipping: %s", ip_str)

        # Load GeoIP database. If missing, attempt a runtime download as a
        # fallback for the build-time download in the Dockerfile (e.g. when
        # MaxMind had a transient outage during the build).
        if config.enabled and GEOIP2_AVAILABLE:
            if not os.path.exists(config.geoip_db_path):
                license_key = os.getenv("MAXMIND_LICENSE_KEY", "").strip()
                account_id = os.getenv("MAXMIND_ACCOUNT_ID", "").strip()
                if license_key and account_id:
                    logger.info(
                        "GeoIP DB missing at %s — attempting runtime download",
                        config.geoip_db_path,
                    )
                    _download_geoip_db(
                        config.geoip_db_path,
                        license_key,
                        account_id=account_id,
                    )
                elif license_key and not account_id:
                    logger.warning(
                        "MAXMIND_LICENSE_KEY is set but MAXMIND_ACCOUNT_ID is not — "
                        "MaxMind requires both for database downloads since the 2024 "
                        "policy change. Skipping runtime download."
                    )

            try:
                self._reader = geoip2.database.Reader(config.geoip_db_path)
                logger.info(
                    "GeoIP database loaded from %s — blocking countries: %s",
                    config.geoip_db_path,
                    config.blocked_countries,
                )
            except Exception as e:
                logger.error(
                    "Failed to load GeoIP database from %s: %s — "
                    "geo-blocking will operate in fail-closed mode",
                    config.geoip_db_path,
                    e,
                )
                self._reader = None

    def close(self) -> None:
        """Close the GeoIP database reader."""
        if self._reader is not None and hasattr(self._reader, "close"):
            self._reader.close()
            self._reader = None

    def _is_allowlisted(self, ip_str: str) -> bool:
        """Check if an IP is in the allowlist."""
        try:
            ip = ipaddress.ip_address(ip_str)
            return any(ip in network for network in self._allowed_networks)
        except ValueError:
            return False

    def _is_private_or_loopback(self, ip_str: str) -> bool:
        """Check if an IP is private, loopback, or link-local."""
        try:
            ip = ipaddress.ip_address(ip_str)
            return ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            return False

    def check_ip(self, ip_str: str) -> GeoBlockResult:
        """Check whether an IP address should be blocked.

        Args:
            ip_str: The IP address to check.

        Returns:
            GeoBlockResult with the decision and metadata.
        """
        if not self.config.enabled:
            return GeoBlockResult(blocked=False, reason="geo_blocking_disabled")

        # Allow private/loopback IPs (local development, internal services)
        if self._is_private_or_loopback(ip_str):
            return GeoBlockResult(blocked=False, reason="private_ip")

        # Check allowlist
        if self._is_allowlisted(ip_str):
            return GeoBlockResult(blocked=False, reason="allowlisted", ip=ip_str)

        # If GeoIP is not available, apply fail-closed policy
        if not GEOIP2_AVAILABLE or self._reader is None:
            if self.config.fail_closed:
                logger.warning("GeoIP unavailable, fail-closed: blocking IP %s", ip_str)
                return GeoBlockResult(
                    blocked=True,
                    reason="geoip_unavailable_fail_closed",
                    ip=ip_str,
                )
            return GeoBlockResult(blocked=False, reason="geoip_unavailable_fail_open")

        # Resolve country
        try:
            response = self._reader.country(ip_str)
            country_code = response.country.iso_code
        except geoip2.errors.AddressNotFoundError:
            # IP not in database — apply fail-closed policy
            if self.config.fail_closed:
                logger.warning("IP %s not found in GeoIP database, fail-closed: blocking", ip_str)
                return GeoBlockResult(
                    blocked=True,
                    reason="country_unknown_fail_closed",
                    ip=ip_str,
                )
            return GeoBlockResult(blocked=False, reason="country_unknown_fail_open", ip=ip_str)
        except Exception as e:
            logger.error("GeoIP lookup error for %s: %s", ip_str, e)
            if self.config.fail_closed:
                return GeoBlockResult(
                    blocked=True,
                    reason="geoip_error_fail_closed",
                    ip=ip_str,
                )
            return GeoBlockResult(blocked=False, reason="geoip_error_fail_open", ip=ip_str)

        # Check if country is blocked
        if country_code and country_code.upper() in self.config.blocked_countries:
            logger.warning("Blocked request from %s (country: %s)", ip_str, country_code)
            return GeoBlockResult(
                blocked=True,
                reason="blocked_country",
                ip=ip_str,
                country_code=country_code,
            )

        return GeoBlockResult(
            blocked=False,
            reason="allowed_country",
            ip=ip_str,
            country_code=country_code,
        )


@dataclass
class GeoBlockResult:
    """Result of a geo-blocking check.

    Attributes:
        blocked: Whether the IP should be blocked.
        reason: Machine-readable reason for the decision.
        ip: The IP address that was checked.
        country_code: Resolved ISO 3166-1 alpha-2 country code, if available.
    """

    blocked: bool
    reason: str
    ip: Optional[str] = None
    country_code: Optional[str] = None


# ============ Singleton Checker ============

# Module-level checker instance, initialized lazily
_checker: Optional[GeoBlockChecker] = None


def get_geo_block_checker() -> GeoBlockChecker:
    """Get or create the module-level GeoBlockChecker.

    Uses environment variables for configuration. The checker is created
    once and reused for all subsequent calls.
    """
    global _checker
    if _checker is None:
        config = GeoBlockConfig.from_env()
        _checker = GeoBlockChecker(config)
    return _checker


def check_ip_blocked(ip_str: str) -> GeoBlockResult:
    """Convenience function to check if an IP is blocked.

    Can be called from any service (Main API or WebSocket Handler).
    """
    checker = get_geo_block_checker()
    return checker.check_ip(ip_str)


# ============ Forwarded-IP helpers ============
#
# When the API runs behind a reverse proxy (Railway edge, Cloudflare, nginx,
# the Lithuanian DSO firewall, etc.) the immediate TCP peer is the proxy, not
# the real client. Geo-blocking decisions must be made against the real client
# IP, so we extract it from RFC 7239 ``Forwarded`` or the de-facto standard
# ``X-Forwarded-For`` / ``X-Real-IP`` headers — but only when the peer is
# itself a trusted proxy. Trusting forwarded headers from arbitrary internet
# peers would let attackers spoof their origin country.


def _extract_forwarded_ip(headers: object) -> Optional[str]:
    """Extract the original client IP from common reverse-proxy headers.

    Order of precedence: RFC 7239 ``Forwarded`` > ``X-Forwarded-For`` >
    ``X-Real-IP``. For ``X-Forwarded-For`` with multiple hops, the leftmost
    valid IP is returned (that is the original client).
    """
    return extract_forwarded_ip(headers)


def _parse_ip_networks(
    ranges: str,
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse a comma-separated list of CIDR ranges, ignoring invalid entries."""
    return parse_ip_networks(
        ranges,
        logger=logger,
        env_var_name="GEO_BLOCK_TRUSTED_PROXY_RANGES",
    )


def _is_implicitly_trusted_proxy(ip_str: str) -> bool:
    """Return True when an IP is implicitly trusted to act as a reverse proxy.

    Includes private (RFC 1918 / RFC 4193), loopback, link-local, and CGNAT
    (RFC 6598) addresses. CGNAT is needed for Railway / Render / Fly.io and
    similar PaaS providers whose edge proxy reaches the container over the
    100.64.0.0/10 internal network. Public-internet IPs are never implicitly
    trusted — operators must opt in via ``GEO_BLOCK_TRUSTED_PROXY_RANGES``.
    """
    try:
        ip_addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    if ip_addr.is_private or ip_addr.is_loopback or ip_addr.is_link_local:
        return True
    if isinstance(ip_addr, ipaddress.IPv4Address) and ip_addr in _CGNAT_NETWORK:
        return True
    return False


# ============ FastAPI Middleware ============


class GeoBlockMiddleware(BaseHTTPMiddleware):
    """FastAPI middleware that blocks requests from geo-blocked countries.

    Returns a generic 403 Forbidden response for blocked IPs.
    Does not reveal geo-blocking logic in the response body.
    Logs blocked requests to the security audit log.

    When the request arrives through a trusted reverse proxy, the real client
    IP is extracted from ``Forwarded`` / ``X-Forwarded-For`` / ``X-Real-IP``
    headers. A peer is considered a trusted proxy when (a) it is a private,
    loopback, link-local, or CGNAT address and ``GEO_BLOCK_TRUST_PROXY_HEADERS``
    is true (default), or (b) it is contained in any CIDR listed in
    ``GEO_BLOCK_TRUSTED_PROXY_RANGES``.

    Environment variables:
        GEO_BLOCK_TRUST_PROXY_HEADERS: "true"/"false" (default "true").
            When true, forwarded headers from private/loopback/link-local/CGNAT
            peers are honoured. PaaS providers like Railway require this.
        GEO_BLOCK_TRUSTED_PROXY_RANGES: Comma-separated CIDR list of additional
            peers whose forwarded headers can be trusted (e.g. an on-prem load
            balancer with a public IP).
    """

    def __init__(self, app):
        """Initialise middleware and load trusted-proxy configuration from env."""
        super().__init__(app)
        self._trust_proxy_headers = (
            os.getenv("GEO_BLOCK_TRUST_PROXY_HEADERS", "true").lower() == "true"
        )
        self._trusted_proxy_networks = _parse_ip_networks(
            os.getenv("GEO_BLOCK_TRUSTED_PROXY_RANGES", "")
        )

    def _is_trusted_proxy(self, peer_ip: str) -> bool:
        """Return True when forwarded headers from this peer may be trusted."""
        if self._trust_proxy_headers and _is_implicitly_trusted_proxy(peer_ip):
            return True
        try:
            ip_addr = ipaddress.ip_address(peer_ip)
        except ValueError:
            return False
        return any(ip_addr in network for network in self._trusted_proxy_networks)

    def _resolve_client_ip(self, request: Request) -> Optional[str]:
        """Resolve the effective client IP, honouring trusted proxy headers."""
        peer_ip = request.client.host if request.client else None
        if not peer_ip:
            return None
        if not self._is_trusted_proxy(peer_ip):
            return peer_ip
        forwarded_ip = _extract_forwarded_ip(request.headers)
        return forwarded_ip or peer_ip

    async def dispatch(self, request: Request, call_next):
        """Check geo-blocking before processing the request."""
        client_ip = self._resolve_client_ip(request)

        if client_ip:
            result = check_ip_blocked(client_ip)
            if result.blocked:
                logger.warning(
                    "Geo-blocked HTTP request",
                    extra={
                        "ip": result.ip,
                        "country_code": result.country_code,
                        "reason": result.reason,
                        "path": str(request.url.path),
                        "method": request.method,
                    },
                )
                # Log to security audit trail
                try:
                    import asyncio

                    from .audit_log import audit_log_event

                    asyncio.ensure_future(
                        audit_log_event(
                            event_type="GEO_BLOCK",
                            source_ip=result.ip,
                            country_code=result.country_code,
                            resource=str(request.url.path),
                            details={
                                "reason": result.reason,
                                "method": request.method,
                            },
                        )
                    )
                except Exception:
                    pass  # Audit logging failure must not block the response

                return JSONResponse(
                    status_code=403,
                    content={"detail": "Access denied"},
                )

        return await call_next(request)
