"""Parse PostgreSQL connection URLs into discrete connection parameters.

Used when ``TIMESCALE_SERVICE_URL`` (or a single ``DATABASE_URL`` in local dev)
carries the full DSN so callers do not need duplicate ``PGHOST``/``PGUSER``/…
variables. Parsing uses ``urllib.parse``; passwords containing ``@`` or other
path-breaking characters should use discrete env vars instead.
"""

from __future__ import annotations

import re
import ssl
from urllib.parse import parse_qs, quote_plus, unquote, urlparse


def is_postgres_url(value: str | None) -> bool:
    """Return True if *value* looks like a postgres DSN."""
    if not value or not isinstance(value, str):
        return False
    v = value.strip()
    return v.startswith("postgres://") or v.startswith("postgresql://")


def parse_postgres_connection_url(url: str) -> dict[str, str | int] | None:
    """Extract host, port, database, user, password, sslmode from a DSN.

    Args:
        url: ``postgres://`` or ``postgresql://`` URI.

    Returns:
        A dict of components, or ``None`` if *url* is not a postgres URI or has
        no hostname.
    """
    if not is_postgres_url(url):
        return None
    normalized = url.strip().replace("postgres://", "postgresql://", 1)
    parsed = urlparse(normalized)
    if not parsed.hostname:
        return None
    user = unquote(parsed.username) if parsed.username else ""
    password = unquote(parsed.password) if parsed.password else ""
    port = int(parsed.port) if parsed.port else 5432
    path = (parsed.path or "").lstrip("/")
    database = path or "postgres"
    q = parse_qs(parsed.query)
    ssl_vals = q.get("sslmode", ["require"])
    sslmode = ssl_vals[0] if ssl_vals else "require"
    return {
        "host": parsed.hostname,
        "port": port,
        "database": database,
        "user": user,
        "password": password,
        "sslmode": str(sslmode),
    }


def merge_timescale_params_from_url(
    *,
    service_url: str | None,
    host: str | None,
    port: int,
    database: str | None,
    user: str | None,
    password: str | None,
    sslmode: str,
    pgport_explicit: bool = False,
) -> tuple[str, int, str, str, str, str]:
    """Fill missing Timescale discrete params from *service_url* when parseable.

    Env-based values take precedence when non-empty (after strip for strings).
    When ``PGPORT`` was not set in the environment, the URL's port is used if
    the URL parses (TigerCloud often uses a non-5432 port).
    """
    h = (host or "").strip()
    d = (database or "").strip()
    u = (user or "").strip()
    pw = (password or "").strip()
    sm = (sslmode or "require").strip() or "require"

    parsed = parse_postgres_connection_url(service_url) if service_url else None
    if not parsed:
        return h, port, d or "tsdb", u, pw, sm

    def _nz(s: str, fallback: str) -> str:
        return s if s else fallback

    out_port = int(parsed["port"]) if not pgport_explicit else port

    return (
        _nz(h, str(parsed["host"])),
        out_port,
        _nz(d, str(parsed["database"])),
        _nz(u, str(parsed["user"])),
        _nz(pw, str(parsed["password"])),
        _nz(sm, str(parsed["sslmode"])),
    )


def build_postgres_dsn(
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    sslmode: str,
) -> str:
    """Build a ``postgresql://`` URI for SQLAlchemy / logging (password URL-encoded)."""
    sm = (sslmode or "require").strip() or "require"
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/"
        f"{quote_plus(database)}?sslmode={quote_plus(sm)}"
    )


def prepare_asyncpg_url_and_ssl(database_url: str) -> tuple[str, ssl.SSLContext | bool | None]:
    """Strip ``sslmode`` from a libpq URI and return an asyncpg-compatible ``ssl`` value.

    ``asyncpg.connect`` does not accept ``sslmode`` in the query string the way
    libpq does; callers should pass the returned tuple as
    ``connect(clean_url, ssl=ssl_arg)``. When *ssl_arg* is ``None``, omit the
    ``ssl`` keyword unless TLS is required by policy.

    Args:
        database_url: Original ``postgres://`` / ``postgresql://`` URI.

    Returns:
        ``(url_without_sslmode_query_param, ssl_config)`` where *ssl_config* is
        ``False``, an ``ssl.SSLContext``, or ``None`` if no ``sslmode`` was present.
    """
    sslmode_match = re.search(r"[?&]sslmode=([^&#]*)", database_url)
    if not sslmode_match:
        return database_url, None

    sslmode = sslmode_match.group(1).lower()
    ssl_config: ssl.SSLContext | bool
    if sslmode == "disable":
        ssl_config = False
    else:
        ctx = ssl.create_default_context()
        if sslmode in {"require", "allow", "prefer"}:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        ssl_config = ctx

    database_url = re.sub(r"\?sslmode=[^&#]*&?", "?", database_url)
    database_url = re.sub(r"&sslmode=[^&#]*", "", database_url)
    database_url = database_url.rstrip("?")
    return database_url, ssl_config


def ssl_context_for_postgres_sslmode(sslmode: str) -> ssl.SSLContext | bool:
    """Build asyncpg-compatible ``ssl`` argument from libpq-style *sslmode*."""
    mode = (sslmode or "require").strip().lower()
    if mode in ("disable", "false", "0"):
        return False
    if mode in ("verify-ca", "verify-full"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = mode == "verify-full"
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx
    if mode in ("require", "allow", "prefer"):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return True
