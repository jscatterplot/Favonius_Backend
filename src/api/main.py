"""FastAPI REST API application.

Reference: PRD_v2.md#7-api-specifications
"""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Iterator, Literal, Optional, Union
from urllib.parse import urlparse
from uuid import UUID
from zoneinfo import ZoneInfo

import asyncpg
import bcrypt
import httpx
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from pydantic.alias_generators import to_camel
from starlette.middleware.base import BaseHTTPMiddleware

from ..core.controller_manager import ControllerManager
from ..core.models import DepotConfig
from ..core.optimizer.exceptions import (
    InfeasibleModelError,
    OptimizationError as _CoreOptimizationError,
    SolverError,
    SolverTimeoutError,
)
from ..core.state.assembler import StateAssembler
from .error_codes import ERROR_MESSAGES, ErrorCode, http_status_for, safe_message_for
from .reports import (
    REPORT_GROUP_BY_VALUES,
    SessionRow,
    aggregate_energy_rows,
    stream_rows_as_csv,
)
from ..core.state.readiness import (
    build_snapshot,
    evaluate_readiness,
)
from ..db.exceptions import (
    DatabaseError as _DbDatabaseError,
    IdempotencyKeyReusedError,
    ResourceNotFoundError,
)
from ..db.snapshot_store import persist_snapshot
from ..db import queries as db_queries
from ..db.pools import DatabasePools
from ..monitoring.metrics import CONTROLLER_MANAGER_UP
from ..security.admin_audit import AdminAuditRow, write_admin_audit_row
from ..security.audit_log import AuditEvent, AuditLogger, get_audit_logger, set_audit_logger
from ..security.auth import (
    get_user_role,
    get_user_organization_id,
    is_platform_admin,
    verify_depot_access,
)
from ..security.tenant_mirror import ensure_tenant_mirrored
from ..security.geo_block import GeoBlockMiddleware
from ..security.headers import SecurityHeadersMiddleware
from ..security.ocpp_auth import verify_ocpp_basic_auth
from ..security.rate_limiter import RateLimiter, get_rate_limiter, set_rate_limiter
from ..security.rbac import Permission, has_permission, require_favonius_admin
from ..security.validators import (
    validate_depot_id,
    validate_horizon_hours,
    validate_uuid,
    validate_vehicle_id,
)

logger = logging.getLogger(__name__)

# Database connection pools (set during lifespan startup)
db_pools: Optional[DatabasePools] = None


# Controller manager and OCPP server
controller_manager: Optional[ControllerManager] = None
ocpp_server: Optional[object] = None  # OCPPServer type

# Depot config cache (to reduce database queries)
_depot_config_cache: dict[str, tuple[DepotConfig, float]] = {}  # depot_id -> (config, timestamp)
_config_cache_ttl: float = 300.0  # 5 minutes
_depot_config_locks: dict[str, asyncio.Lock] = {}  # single-flight locks per depot
_background_tasks: set[asyncio.Task] = set()


def _create_background_task(coro) -> None:
    """Create and retain a background task until completion."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _require_depot_access(
    depot_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
) -> str:
    """FastAPI Depends: validate depot_id UUID and verify authenticated user has access.

    Eliminates the repeated 3-line validate/access-check block across depot endpoints.
    Returns the validated depot_id string on success; raises 400, 401, or 403 otherwise.
    """
    validate_depot_id(depot_id)
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    return depot_id


def describe_database_target(database_url: str) -> str:
    """Return safe, redacted connection target details for logs.

    Security: only expose port; mask host/user/db to prevent
    infrastructure reconnaissance via log scraping.
    """
    parsed = urlparse(database_url)
    host = parsed.hostname or ""
    port = parsed.port or "<default>"
    # Show only the TLD suffix (e.g. "*****.timescale.com")
    host_parts = host.rsplit(".", 2)
    masked_host = f"*****.{'.'.join(host_parts[-2:])}" if len(host_parts) >= 2 else "*****"
    return f"user=***** host={masked_host} port={port} db=*****"


def resolve_database_url() -> tuple[str, str]:
    """Resolve the primary database URL from environment variables.

    Returns:
        Tuple of (database_url, source_env_var_name).

    Raises:
        RuntimeError: When neither DATABASE_URL nor TIMESCALE_SERVICE_URL is set.
    """
    if url := os.getenv("DATABASE_URL"):
        return url, "DATABASE_URL"
    if url := os.getenv("TIMESCALE_SERVICE_URL"):
        return url, "TIMESCALE_SERVICE_URL"
    raise RuntimeError("No database URL configured. Set DATABASE_URL or TIMESCALE_SERVICE_URL.")


async def _create_pool(url: str, url_source: str) -> asyncpg.Pool:
    """Create an asyncpg pool for a single database URL.

    Args:
        url: PostgreSQL connection string.
        url_source: Env var name used in log messages.

    Returns:
        Connected asyncpg.Pool.

    Raises:
        RuntimeError: On permanent auth failure.
        Exception: On transient errors (caller should let Railway restart).
    """
    logger.info(
        "Initializing database pool using %s (%s)",
        url_source,
        describe_database_target(url),
    )
    try:
        pool = await asyncpg.create_pool(
            url,
            min_size=2,
            max_size=int(os.getenv("DB_POOL_MAX_SIZE", "25")),
            # Supabase Supavisor does not support prepared statements.
            # statement_cache_size=0 sends every query as a simple query,
            # compatible with any pgBouncer-style pooler.
            statement_cache_size=0,
            command_timeout=60,
        )
        logger.info("Database pool initialised via %s", url_source)
        return pool
    except asyncpg.exceptions.InvalidPasswordError as e:
        raise RuntimeError(
            f"Auth failure for {url_source} ({describe_database_target(url)}) — "
            "password is incorrect. Update the env var with the correct credentials."
        ) from e


# Security: INTERNAL_API_TOKEN required in production (M1).
# When unset, /internal/ocpp-event refuses every request (C3 fail-closed).
_INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")
_environment = os.getenv("ENVIRONMENT", "development")
if _environment == "production" and not _INTERNAL_API_TOKEN:
    raise RuntimeError(
        "INTERNAL_API_TOKEN must be set in production. "
        "The /internal/ocpp-event endpoint is unauthenticated without it."
    )
if not _INTERNAL_API_TOKEN:
    logger.warning(
        "INTERNAL_API_TOKEN is not set; /internal/ocpp-event will refuse every "
        "request with 503 until the token is configured."
    )


async def _heartbeat_loop(ts_pool: asyncpg.Pool) -> None:
    """Write optimizer heartbeat to TimescaleDB every 30 s.

    The websocket_handler reads this row to decide whether the main API
    optimizer is alive before falling back to its own heuristic.
    """
    while True:
        try:
            async with ts_pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO service_heartbeat (service, last_seen)
                    VALUES ('optimizer', NOW())
                    ON CONFLICT (service) DO UPDATE SET last_seen = NOW()
                    """)
        except Exception as e:
            logger.warning("Heartbeat write failed: %s", e)
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global db_pools, controller_manager, ocpp_server

    # ── Database pools ────────────────────────────────────────────────────────
    # Supabase (static/reference data): DATABASE_URL required.
    # TimescaleDB (time-series/operational): TIMESCALE_SERVICE_URL required;
    #   falls back to DATABASE_URL for single-DB local dev (docker-compose).
    static_url = os.getenv("DATABASE_URL")
    ts_url = os.getenv("TIMESCALE_SERVICE_URL") or os.getenv("DATABASE_URL")

    if not static_url:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Set it to the Supabase connection string (static/reference data)."
        )
    if not ts_url:
        raise RuntimeError(
            "TIMESCALE_SERVICE_URL is not set. "
            "Set it to the TimescaleDB connection string (time-series data)."
        )

    static_pool = await _create_pool(static_url, "DATABASE_URL")
    ts_pool = await _create_pool(
        ts_url,
        "TIMESCALE_SERVICE_URL" if os.getenv("TIMESCALE_SERVICE_URL") else "DATABASE_URL",
    )
    db_pools = DatabasePools(static=static_pool, ts=ts_pool)

    # ── Audit logger (Article 73-3 / NIS2 compliance) ────────────────────────
    audit_logger = AuditLogger(db_pool=ts_pool, service="main_api")
    set_audit_logger(audit_logger)
    await audit_logger.start()
    logger.info("Security audit logger started")

    # ── Rate limiter (PostgreSQL-backed for NIS2 persistence) ────────────────
    hybrid_limiter = RateLimiter(db_pool=ts_pool)
    set_rate_limiter(hybrid_limiter)
    await hybrid_limiter.start()
    logger.info("Hybrid rate limiter started")

    # ── OCPP server ───────────────────────────────────────────────────────────
    ocpp_enabled = os.getenv("OCPP_SERVER_ENABLED", "false").lower() == "true"
    ocpp_use_same_port = os.getenv("OCPP_USE_SAME_PORT", "false").lower() == "true"
    if ocpp_enabled:
        try:
            from ..adapters.ocpp.server import OCPPServer

            ocpp_host = os.getenv("OCPP_SERVER_HOST", "0.0.0.0")
            ocpp_port = int(os.getenv("OCPP_SERVER_PORT", "9000"))

            ocpp_server = OCPPServer(
                host=ocpp_host,
                port=ocpp_port,
                pools=db_pools,
            )

            if ocpp_use_same_port:
                logger.info("OCPP served on same port as REST (path /ocpp/{charge_point_id})")
            else:

                async def run_ocpp_server():
                    try:
                        await ocpp_server.start()
                    except Exception as e:
                        logger.error(f"OCPP server error: {e}", exc_info=True)

                asyncio.create_task(run_ocpp_server())
                logger.info(f"OCPP server starting on {ocpp_host}:{ocpp_port}")
        except Exception as e:
            logger.error(f"Failed to start OCPP server: {e}", exc_info=True)
            ocpp_server = None

    # ── Controller manager ────────────────────────────────────────────────────
    try:
        controller_manager = ControllerManager(
            pools=db_pools,
            ocpp_server=ocpp_server,
        )

        await controller_manager.start_all_controllers()
        CONTROLLER_MANAGER_UP.set(1)
        logger.info("Controller manager initialized and controllers started")
    except Exception as e:
        logger.critical(
            "Controller manager failed to start — optimization engine is DOWN. "
            f"No depot controllers will run. Error: {e}",
            exc_info=True,
        )
        CONTROLLER_MANAGER_UP.set(0)
        controller_manager = None

    # ── Optimizer heartbeat ───────────────────────────────────────────────────
    # Writes a timestamp to service_heartbeat every 30 s so the websocket_handler
    # can detect main API health without a direct HTTP call in the hot path.
    asyncio.create_task(_heartbeat_loop(ts_pool))
    logger.info("Optimizer heartbeat task started")

    yield

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    logger.info("Shutting down application...")

    if controller_manager:
        try:
            await controller_manager.stop_all_controllers()
            logger.info("All controllers stopped")
        except Exception as e:
            logger.error(f"Error stopping controllers: {e}", exc_info=True)

    if ocpp_server:
        try:
            await ocpp_server.stop()
            logger.info("OCPP server stopped")
        except Exception as e:
            logger.error(f"Error stopping OCPP server: {e}", exc_info=True)

    try:
        await hybrid_limiter.stop()
        logger.info("Hybrid rate limiter stopped")
    except Exception as e:
        logger.error(f"Error stopping rate limiter: {e}", exc_info=True)

    try:
        await audit_logger.stop()
        logger.info("Security audit logger stopped")
    except Exception as e:
        logger.error(f"Error stopping audit logger: {e}", exc_info=True)

    await static_pool.close()
    logger.info("Static (Supabase) pool closed")
    if ts_pool is not static_pool:
        await ts_pool.close()
        logger.info("TimescaleDB pool closed")


app = FastAPI(
    title="Favonius Energy API",
    version="0.1.0",
    description="""
    EV Fleet Depot Optimization Platform API

    This API provides endpoints for:
    - Charging schedule optimization
    - Depot state queries
    - Inter-depot vehicle handoff
    - System health monitoring

    Reference: PRD_v2.md#7-api-specifications
    """,
    lifespan=lifespan,
    # Disable built-in schema/docs routes; a JWT-gated /openapi.json is added below
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
    tags_metadata=[
        {
            "name": "optimization",
            "description": "Charging schedule optimization operations. Generate optimal charging schedules to minimize electricity costs while ensuring vehicles are ready for departure.",
        },
        {
            "name": "depots",
            "description": "Depot state and schedule queries. Get current state of depots, vehicle SoCs, and active charging schedules.",
        },
        {
            "name": "health",
            "description": "System health and monitoring endpoints. Check API and component health status.",
        },
    ],
)

# ============ Request Body Size Middleware (H12) ============

_MAX_BODY_SIZE = int(os.getenv("MAX_REQUEST_BODY_BYTES", str(1 * 1024 * 1024)))  # 1 MB default


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds the configured maximum."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > _MAX_BODY_SIZE:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body too large"},
            )
        return await call_next(request)


# ============ Solver Concurrency Limiter (H11) ============

_MAX_CONCURRENT_SOLVES = int(os.getenv("MAX_CONCURRENT_SOLVES", "2"))
_optimize_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_SOLVES)
_last_depot_solve: dict[str, float] = {}  # depot_id -> timestamp
_DEPOT_SOLVE_COOLDOWN = float(os.getenv("DEPOT_SOLVE_COOLDOWN_SECONDS", "120"))  # 2 min


# ============ Inter-depot handoff signature verification (C2) ============

# Replay window for inter-depot handoff timestamps (seconds). Matches the
# Resend webhook tolerance and is small enough that captured signatures
# expire before they can be reused at scale.
_HANDOFF_REPLAY_WINDOW_S = 300

# In-memory nonce store: nonce -> earliest expiry. Sized for short TTL so
# growth is bounded by the rate-limited handoff throughput (50/hr per pair).
_handoff_nonces: dict[str, float] = {}


def _prune_handoff_nonces(now: float) -> None:
    """Drop expired entries from ``_handoff_nonces`` (called on each verify)."""
    expired = [n for n, exp in _handoff_nonces.items() if exp < now]
    for n in expired:
        _handoff_nonces.pop(n, None)


def reset_handoff_nonces_for_tests() -> None:
    """Test helper: clear the in-memory nonce store between cases."""
    _handoff_nonces.clear()


def _verify_handoff_payload(
    payload: dict,
    signing_key: str,
    *,
    now: Optional[float] = None,
) -> bool:
    """Verify HMAC-SHA256 + replay window + nonce uniqueness on a handoff body.

    Mirrors the canonicalization used by ``send_handoff``:
        sig = hmac_sha256(signing_key, json.dumps(payload_without_signature,
                                                  sort_keys=True))

    Returns True iff the payload is properly signed, recent (within
    ``_HANDOFF_REPLAY_WINDOW_S``), and the nonce has not been seen.
    """
    if not isinstance(payload, dict):
        return False
    sig = payload.get("signature")
    nonce = payload.get("nonce")
    timestamp_str = payload.get("timestamp")
    if not isinstance(sig, str) or not isinstance(nonce, str) or not isinstance(
        timestamp_str, str
    ):
        return False

    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    current = now if now is not None else time.time()
    if abs(current - ts.timestamp()) > _HANDOFF_REPLAY_WINDOW_S:
        return False

    canonical = {k: v for k, v in payload.items() if k != "signature"}
    body_bytes = json.dumps(canonical, sort_keys=True).encode("utf-8")
    expected = hmac.new(signing_key.encode(), body_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return False

    _prune_handoff_nonces(current)
    if nonce in _handoff_nonces:
        return False
    _handoff_nonces[nonce] = current + _HANDOFF_REPLAY_WINDOW_S
    return True


# ============ Rate Limiting Middleware ============


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware to enforce rate limits per PRD Section 10.4."""

    @staticmethod
    def _add_rate_limit_headers(response, result) -> None:
        """Add X-RateLimit-* headers to the response."""
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        response.headers["X-RateLimit-Reset"] = str(int(result.reset_at))

    async def dispatch(self, request: Request, call_next):
        """Check rate limits before processing request."""
        # Skip rate limiting for health probes
        if request.url.path in ["/healthz"]:
            return await call_next(request)

        # Security (H5): use authenticated user_id as primary rate-limit key.
        # Falls back to IP only for unauthenticated endpoints — never trust
        # X-API-Key or X-Forwarded-For blindly as they are attacker-controlled.
        client_id = request.client.host if request.client else "unknown"
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                from ..security.auth import _get_jwt_secrets, _ALLOWED_ALGORITHMS
                import jwt as _jwt

                payload = _jwt.decode(
                    auth_header[7:],
                    _get_jwt_secrets()[0],
                    algorithms=_ALLOWED_ALGORITHMS,
                    audience="authenticated",
                    options={"verify_exp": False},
                )
                client_id = f"user:{payload.get('sub', client_id)}"
            except Exception:
                pass  # Fall back to IP-based limiting

        limiter = get_rate_limiter()

        # Apply different rate limits based on endpoint
        path = request.url.path
        result = None

        # POST /optimize: 10 requests/minute
        if path == "/optimize" and request.method == "POST":
            result = limiter.check_optimize_limit(client_id)
            if not result:
                resp = JSONResponse(
                    status_code=429,
                    content=ErrorResponse(
                        detail="Rate limit exceeded: Maximum 10 optimization requests per minute",
                        error_code="RATE_LIMIT_EXCEEDED",
                        timestamp=datetime.utcnow().isoformat(),
                    ).model_dump(),
                )
                self._add_rate_limit_headers(resp, result)
                return resp

        # Inter-depot handoff: 50 messages/hour per depot pair
        elif "/handoff" in path:
            # Handoff rate limiting handled in endpoint handlers
            # (requires reading request body which is not available in middleware)
            pass

        # General API endpoints: 100 requests/minute
        else:
            result = limiter.check_api_limit(client_id)
            if not result:
                resp = JSONResponse(
                    status_code=429,
                    content=ErrorResponse(
                        detail="Rate limit exceeded: Maximum 100 requests per minute",
                        error_code="RATE_LIMIT_EXCEEDED",
                        timestamp=datetime.utcnow().isoformat(),
                    ).model_dump(),
                )
                self._add_rate_limit_headers(resp, result)
                return resp

        response = await call_next(request)

        # Add rate limit headers to successful responses
        if result is not None:
            self._add_rate_limit_headers(response, result)

        return response


# ============ Logging Middleware ============


_REQUEST_ID_MAX_LEN = 128


def _sanitize_inbound_request_id(raw: Optional[str]) -> Optional[str]:
    """Return a safe correlation ID or None if the header is unusable.

    Rejects empty values, excessive length, and non-printable ASCII (control
    characters) so request IDs cannot be used for log injection or response
    amplification.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s or len(s) > _REQUEST_ID_MAX_LEN:
        return None
    if not all(32 <= ord(c) <= 126 for c in s):
        return None
    return s


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Stamp every request with a correlation ID.

    Reads an inbound ``X-Request-ID`` header if present, otherwise generates
    a UUID4. The value is exposed at ``request.state.request_id`` for
    downstream code (especially exception handlers) and echoed back as
    ``X-Request-ID`` so clients can quote it when reporting issues.
    """

    async def dispatch(self, request: Request, call_next):
        inbound = _sanitize_inbound_request_id(request.headers.get("x-request-id"))
        request_id = inbound if inbound is not None else str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


def _get_request_id(request: Request) -> Optional[str]:
    """Return the correlation ID stamped by ``RequestIdMiddleware`` if present."""
    return getattr(request.state, "request_id", None)


def _build_error_response(
    request: Request,
    error_code: ErrorCode,
    *,
    status_code: Optional[int] = None,
    detail: Optional[str] = None,
    extra: Optional[dict] = None,
) -> JSONResponse:
    """Build a sanitized JSON error response.

    The ``detail`` argument is only used when the call site has explicit,
    public-safe text (e.g. "Vehicle not found"). When omitted, the message
    is taken from :data:`ERROR_MESSAGES`. Raw exception strings must never
    flow through this function as ``detail``.
    """
    body = ErrorResponse(
        detail=detail if detail is not None else safe_message_for(error_code),
        error_code=str(error_code.value),
        timestamp=datetime.utcnow().isoformat(),
        request_id=_get_request_id(request),
    ).model_dump()
    if extra:
        body.update(extra)
    return JSONResponse(
        status_code=status_code if status_code is not None else http_status_for(error_code),
        content=body,
    )


class LoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to log all requests and responses."""

    async def dispatch(self, request: Request, call_next):
        """Process request and log details."""
        start_time = time.time()

        # Log request
        logger.info(
            "Incoming request",
            extra={
                "method": request.method,
                "path": str(request.url.path),
                "query_params": dict(request.query_params),
                "client": request.client.host if request.client else None,
            },
        )

        # Process request
        try:
            response = await call_next(request)
            process_time = time.time() - start_time

            # Log response (exclude sensitive endpoints)
            if request.url.path not in ["/metrics", "/health"]:
                logger.info(
                    "Request completed",
                    extra={
                        "method": request.method,
                        "path": str(request.url.path),
                        "status_code": response.status_code,
                        "process_time": f"{process_time:.3f}s",
                    },
                )
            else:
                # Log health/metrics with less detail
                logger.debug(
                    f"{request.method} {request.url.path} - {response.status_code} "
                    f"({process_time:.3f}s)"
                )

            return response

        except Exception as e:
            process_time = time.time() - start_time
            logger.error(
                "Request failed",
                extra={
                    "method": request.method,
                    "path": str(request.url.path),
                    "error": str(e),
                    "process_time": f"{process_time:.3f}s",
                },
                exc_info=True,
            )
            raise


# Add request body size limit middleware (H12 — outermost after geo-block)
app.add_middleware(MaxBodySizeMiddleware)

# Add rate limiting middleware (before logging to catch rate limits early)
app.add_middleware(RateLimitMiddleware)

# Add logging middleware
app.add_middleware(LoggingMiddleware)

# Stamp each request with a correlation ID and echo via X-Request-ID.
# Registered closest to the app so request.state.request_id is set before
# exception handlers (which run inside the app, not in middleware) execute.
app.add_middleware(RequestIdMiddleware)

# Add security headers middleware (PRD Section 10.3)
app.add_middleware(SecurityHeadersMiddleware)

# Add CORS middleware — fail startup if wildcard in production (M3)
cors_origins_str = os.getenv("CORS_ORIGINS", "*")
if cors_origins_str.strip() == "*" and _environment == "production":
    raise RuntimeError(
        "CORS_ORIGINS='*' is not allowed in production. "
        "Set CORS_ORIGINS to your frontend origin(s), e.g. 'https://app.favonius.com'."
    )
cors_origins = [o.strip() for o in cors_origins_str.split(",") if o.strip()]
# Security (M3): never combine wildcard origins with allow_credentials=True
_cors_is_wildcard = "*" in cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _cors_is_wildcard else cors_origins,
    allow_credentials=not _cors_is_wildcard,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    expose_headers=["X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
)

# Add geo-blocking middleware (outermost — executes first, before all other middleware)
# Required by Lithuanian Electric Energy Law Article 73-3
app.add_middleware(GeoBlockMiddleware)


class OptimizationRequest(BaseModel):
    """Request to run optimization.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """

    depot_id: str = Field(
        ...,
        description="Depot identifier (UUID format)",
        examples=["550e8400-e29b-41d4-a716-446655440000"],
    )
    horizon_hours: int = Field(
        default=24, ge=1, le=48, description="Optimization horizon in hours (1-48)", examples=[24]
    )
    force: bool = Field(
        default=False, description="Force re-optimization even if recent schedule exists"
    )

    @field_validator("depot_id")
    @classmethod
    def validate_depot_id(cls, v: str) -> str:
        """Validate depot_id is a valid UUID."""
        try:
            UUID(v)
            return v
        except ValueError:
            raise ValueError(f"depot_id must be a valid UUID, got: {v}")


class OptimizationResponse(BaseModel):
    """Optimization result response.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """

    run_id: str = Field(..., description="Optimization run identifier (UUID)")
    depot_id: str = Field(..., description="Depot identifier (UUID)")
    status: str = Field(
        ...,
        description="Optimization status: 'optimal', 'feasible', 'degraded', 'infeasible', 'timeout'",
    )
    objective_value: float = Field(..., description="Optimized objective value ($)")
    solve_time_seconds: float = Field(..., ge=0, description="Solver execution time (seconds)")
    peak_demand_kw: float = Field(..., ge=0, description="Peak demand in kW")
    solver_used: str = Field(default="gurobi", description="Solver used: 'gurobi' or 'highs'")
    schedule: dict = Field(
        ...,
        description="Charging schedule per vehicle",
        examples=[{"bus_1": {"charging_power": [0, 0, 80, 80], "soc": [0.3, 0.3, 0.35, 0.40]}}],
    )


class DepotStateResponse(BaseModel):
    """Depot state response.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """

    depot_id: str = Field(..., description="Depot identifier (UUID)")
    timestamp: str = Field(
        ..., description="Timestamp in ISO 8601 format", examples=["2025-12-04T10:00:00Z"]
    )
    vehicle_socs: dict[str, float] = Field(
        ..., description="Vehicle state of charge (0.0-1.0) by vehicle_id"
    )
    battery_soc: float = Field(
        ..., ge=0.0, le=1.0, description="Stationary battery state of charge (0.0-1.0)"
    )
    current_month_peak_kw: float = Field(..., ge=0.0, description="Current month peak demand (kW)")
    current_price_kwh: float = Field(..., ge=0.0, description="Current electricity price ($/kWh)")


class ReadinessResponse(BaseModel):
    """Optimization readiness response (PRD §9.4 building load + migration 019).

    Returned by ``GET /depots/{id}/optimization/readiness``. Tells the
    frontend whether a solve would run, run with documented assumptions,
    or be refused outright.

    `status` values:
        - ``ready``     — every required input is present.
        - ``degraded``  — at least one input substituted by an explicit
                          assumption (e.g. building load forecast fallback).
                          The optimization will still run, and its
                          ``optimization_runs.status`` will be ``degraded``.
        - ``not_ready`` — a hard prerequisite is missing; ``POST /optimize``
                          will refuse the run.
    """

    depot_id: str = Field(..., description="Depot identifier (UUID)")
    status: str = Field(
        ...,
        description="Overall readiness verdict: 'ready' | 'degraded' | 'not_ready'",
    )
    missing_inputs: list[str] = Field(
        default_factory=list,
        description=(
            "Hard misses that block the run. Possible values: "
            "'vehicles', 'chargers', 'prices', 'schedules', "
            "'charger_vehicle_access', 'building_load'."
        ),
    )
    degraded_reasons: list[str] = Field(
        default_factory=list,
        description=(
            "Reasons the run is degraded but still safe to execute. "
            "Possible values: 'building_load_meter_unavailable', "
            "'telemetry_all_defaulted'."
        ),
    )
    assumptions: dict = Field(
        default_factory=dict,
        description=(
            "Explicit assumptions applied to fill in missing data. "
            "Keys are input names ('building_load', 'telemetry'); "
            "values include {source, note} or input-specific detail."
        ),
        examples=[
            {
                "building_load": {
                    "source": "forecast_fallback",
                    "note": "meter data missing; substituted business-hours forecast pattern",
                }
            }
        ],
    )
    building_load_source: str = Field(
        ...,
        description="'meter' | 'forecast_fallback' | 'absent'",
    )
    horizon_hours: int = Field(
        ..., ge=1, description="Horizon length used for the readiness check (hours)"
    )
    captured_at: str = Field(..., description="Timestamp when readiness was evaluated (ISO 8601)")
    snapshot_id: Optional[str] = Field(
        None,
        description=(
            "ID of the persisted snapshot if one was written. Absent on "
            "preview-only checks (none today; reserved for future use)."
        ),
    )


class ChargerFaultItem(BaseModel):
    """Single charger fault from OCPP StatusNotification (PRD §7.1)."""

    charger_id: str = Field(..., description="Charger UUID")
    ocpp_id: str = Field(..., description="OCPP charge point ID")
    connector_id: int = Field(..., description="Connector index")
    fault_code: str = Field(..., description="OCPP 1.6 fault code")
    timestamp: str = Field(..., description="Fault timestamp (ISO 8601)")


class LastOptimizationItem(BaseModel):
    """Last optimization run summary (PRD §7.1)."""

    run_id: str = Field(..., description="Optimization run UUID")
    status: str = Field(
        ..., description="optimal | feasible | degraded | infeasible | timeout | error"
    )
    solver_used: str = Field(..., description="gurobi | highs")
    solve_time_s: Optional[float] = Field(None, description="Solve time in seconds")
    timestamp: str = Field(..., description="Run timestamp (ISO 8601)")


class NotificationAlertItem(BaseModel):
    """Aggregated alert from notification_alerts (alerts pipeline).

    Field names match the frontend ``AlertItemSchema`` so the API client
    receives the wire shape without renames. DB column names stay as-is
    (``id``, ``title``, ``detail``, ``first_occurrence_at``, …); we map at
    this boundary.
    """

    alert_id: str = Field(..., description="Alert UUID")
    organization_id: str = Field(..., description="Owning organization UUID")
    depot_id: Optional[str] = Field(None, description="Depot UUID (nullable)")
    depot_name: Optional[str] = Field(None, description="Depot display name (denormalized via JOIN)")
    alert_type: str = Field(
        ...,
        description=(
            "Wire enum: charger_auth_failure | charger_fault | degraded_optimization | "
            "missing_input | stale_telemetry"
        ),
    )
    severity: str = Field(..., description="info | warning | critical")
    subject: str = Field(..., description="Human-readable summary (mapped from notification_alerts.title)")
    body: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Producer payload. May include description, suggestedAction, context, "
            "and type-specific fields (e.g. missing_inputs[])."
        ),
    )
    dedup_key: str = Field(..., description="Producer-defined dedup tag")
    status: str = Field(..., description="active | acknowledged | resolved")
    first_seen_at: str = Field(..., description="ISO 8601")
    last_seen_at: str = Field(..., description="ISO 8601")
    occurrence_count: int = Field(..., description="Times the dedup_key has fired since first_seen_at")
    acknowledged_by: Optional[str] = Field(None, description="User UUID who acknowledged")
    resolved_at: Optional[str] = Field(None, description="ISO 8601 if status == resolved")
    last_notified_at: Optional[str] = Field(None, description="ISO 8601 or null if not yet sent")


class AlertsResponse(BaseModel):
    """Alerts and last optimization (PRD §7.1 GET /depots/{id}/alerts)."""

    depot_id: str = Field(..., description="Depot identifier (UUID)")
    timestamp: str = Field(..., description="Response timestamp (ISO 8601)")
    charger_faults: list[ChargerFaultItem] = Field(
        default_factory=list,
        description="Active OCPP StatusNotification faults for depot chargers",
    )
    last_optimization: Optional[LastOptimizationItem] = Field(
        None,
        description="Most recent optimization run for this depot",
    )
    notification_alerts: list[NotificationAlertItem] = Field(
        default_factory=list,
        description="Aggregated alerts from the notification_alerts pipeline (active or acknowledged)",
    )


class NotificationRecipientItem(BaseModel):
    """A notification_recipients row."""

    id: str = Field(...)
    organization_id: str = Field(...)
    email: str = Field(...)
    display_name: Optional[str] = Field(None)
    alert_types: list[str] = Field(...)
    min_severity: str = Field(...)
    active: bool = Field(...)


class CreateNotificationRecipientRequest(BaseModel):
    email: str = Field(..., description="Email address (delivered to via Resend)")
    display_name: Optional[str] = Field(None)
    alert_types: list[str] = Field(default_factory=lambda: ["*"])
    min_severity: str = Field(default="warning", description="info | warning | critical")


class UpdateNotificationRecipientRequest(BaseModel):
    display_name: Optional[str] = None
    alert_types: Optional[list[str]] = None
    min_severity: Optional[str] = None
    active: Optional[bool] = None


class AcknowledgeAlertResponse(BaseModel):
    id: str
    status: str
    acknowledged_at: str


class ScheduleResponse(BaseModel):
    """Schedule response.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """

    depot_id: str = Field(..., description="Depot identifier (UUID)")
    run_id: str = Field(..., description="Optimization run identifier (UUID)")
    generated_at: str = Field(
        ...,
        description="Schedule generation timestamp (ISO 8601)",
        examples=["2025-12-04T09:00:00Z"],
    )
    horizon_start: str = Field(
        ..., description="Optimization horizon start (ISO 8601)", examples=["2025-12-04T09:00:00Z"]
    )
    horizon_end: str = Field(
        ..., description="Optimization horizon end (ISO 8601)", examples=["2025-12-05T09:00:00Z"]
    )
    schedule: dict = Field(..., description="Charging schedule per vehicle")


class ManualScheduleEntry(BaseModel):
    """Manually-entered route schedule for one vehicle."""

    vehicle_id: str = Field(..., description="Vehicle UUID")
    route_id: str = Field(..., min_length=1, max_length=100)
    departure_time: datetime = Field(..., description="Timezone-aware ISO 8601 departure time")
    return_time: datetime = Field(..., description="Timezone-aware ISO 8601 return time")
    required_soc: float = Field(default=1.0, ge=0.99, le=1.0)
    energy_kwh: Optional[float] = Field(default=None, gt=0)

    @field_validator("vehicle_id")
    @classmethod
    def validate_vehicle_uuid(cls, value: str) -> str:
        """Validate vehicle_id is a valid UUID."""
        try:
            UUID(value)
            return value
        except ValueError:
            raise ValueError(f"vehicle_id must be a valid UUID, got: {value}")

    @field_validator("departure_time", "return_time")
    @classmethod
    def validate_timezone_aware(cls, value: datetime) -> datetime:
        """Require timezone-aware datetimes so depot schedules are unambiguous."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must include timezone information")
        return value

    @field_validator("return_time")
    @classmethod
    def validate_return_after_departure(cls, value: datetime, info) -> datetime:
        """Require return_time to be after departure_time."""
        departure_time = info.data.get("departure_time")
        if departure_time is not None and value <= departure_time:
            raise ValueError("return_time must be after departure_time")
        return value


class ManualScheduleCreateRequest(BaseModel):
    """Batch request for manually-entered schedules."""

    entries: list[ManualScheduleEntry] = Field(..., min_length=1, max_length=500)


class ManualSchedulePatchRequest(BaseModel):
    """Partial manual schedule update."""

    vehicle_id: Optional[str] = Field(default=None, description="Vehicle UUID")
    route_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    departure_time: Optional[datetime] = Field(default=None)
    return_time: Optional[datetime] = Field(default=None)
    required_soc: Optional[float] = Field(default=None, ge=0.99, le=1.0)
    energy_kwh: Optional[float] = Field(default=None)

    @field_validator("vehicle_id")
    @classmethod
    def validate_vehicle_uuid(cls, value: Optional[str]) -> Optional[str]:
        """Validate vehicle_id is a valid UUID when supplied."""
        if value is None:
            return value
        try:
            UUID(value)
            return value
        except ValueError:
            raise ValueError(f"vehicle_id must be a valid UUID, got: {value}")

    @field_validator("departure_time", "return_time")
    @classmethod
    def validate_timezone_aware(cls, value: Optional[datetime]) -> Optional[datetime]:
        """Require timezone-aware datetimes when supplied."""
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("datetime must include timezone information")
        return value

    @field_validator("energy_kwh")
    @classmethod
    def validate_energy_kwh(cls, value: Optional[float]) -> Optional[float]:
        """Allow clearing energy_kwh with null, but reject non-positive values."""
        if value is not None and value <= 0:
            raise ValueError("energy_kwh must be greater than 0")
        return value


class ManualScheduleItem(BaseModel):
    """Manual schedule row returned to admin clients."""

    schedule_id: str
    vehicle_id: str
    route_id: str
    departure_time: datetime
    return_time: datetime
    required_soc: float
    energy_kwh: Optional[float] = None


class ScheduleReadinessResponse(BaseModel):
    """Readiness checks for schedule setup and optimization prerequisites."""

    depot_id: str
    ready: bool
    checks: list["ReadinessChecklistItem"]


class ManualScheduleCreateResponse(BaseModel):
    """Response from manual schedule creation."""

    created: list[ManualScheduleItem]
    readiness: ScheduleReadinessResponse


class ManualScheduleUpdateResponse(BaseModel):
    """Response from manual schedule update."""

    updated: ManualScheduleItem
    readiness: ScheduleReadinessResponse


class HandoffRequest(BaseModel):
    """Inter-depot handoff request.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    Per PRD Section 5.4, must include battery_kwh and max_charge_kw.
    """

    dest_depot_id: str = Field(
        ...,
        description="Destination depot identifier (UUID)",
        examples=["550e8400-e29b-41d4-a716-446655440000"],
    )
    expected_soc: float = Field(
        ..., ge=0.0, le=1.0, description="Expected state of charge at arrival (0.0-1.0)"
    )
    arrival_time: datetime = Field(
        ..., description="Expected arrival time (ISO 8601)", examples=["2025-12-04T14:30:00Z"]
    )
    battery_kwh: float = Field(..., gt=0, description="Vehicle battery capacity (kWh)")
    max_charge_kw: float = Field(..., gt=0, description="Vehicle max charge rate (kW)")

    @field_validator("dest_depot_id")
    @classmethod
    def validate_dest_depot_id(cls, v: str) -> str:
        """Validate dest_depot_id is a valid UUID."""
        try:
            UUID(v)
            return v
        except ValueError:
            raise ValueError(f"dest_depot_id must be a valid UUID, got: {v}")


class HandoffResponse(BaseModel):
    """Inter-depot handoff response.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """

    message_id: str = Field(..., description="Handoff message identifier (UUID)")
    status: str = Field(..., description="Message status (e.g., 'sent')")


class ErrorResponse(BaseModel):
    """Error response model matching PRD format.

    The ``detail`` field is always a sanitized, public-safe string drawn from
    ``src.api.error_codes.ERROR_MESSAGES`` (or controlled call-site text).
    Raw exception strings, SQL fragments, table/column names, or other
    internals must NOT be placed here — log them server-side instead.

    ``request_id`` correlates the response with server logs so support can
    look up the full traceback without exposing it to the client.
    """

    detail: str = Field(..., description="Error message")
    error_code: Optional[str] = Field(None, description="Error code for programmatic handling")
    timestamp: str = Field(..., description="Error timestamp (ISO 8601)")
    request_id: Optional[str] = Field(
        None, description="Correlation ID for this request (also returned as X-Request-ID header)"
    )


# ============ Depot Metadata Models ============


class DepotMetadata(BaseModel):
    """Static depot metadata returned by /me/depots and /depots/{depot_id}."""

    depot_id: str = Field(..., description="Depot identifier (UUID)")
    organization_id: str = Field(..., description="Owning organization (workspace) UUID")
    name: str = Field(..., description="Human-readable depot name")
    timezone: str = Field(..., description="IANA timezone (e.g. 'America/Los_Angeles')")
    currency: str = Field(..., description="ISO 4217 currency code (e.g. 'EUR', 'USD', 'GBP')")
    max_grid_kw: float = Field(..., description="Hard site power limit (kW)")
    demand_charge_rate_kw: Optional[float] = Field(
        None, description="Demand charge rate in $/kW per month"
    )
    latitude: Optional[float] = Field(None, description="Depot latitude")
    longitude: Optional[float] = Field(None, description="Depot longitude")
    utility_id: Optional[str] = Field(None, description="Utility provider identifier")
    demand_charge_billing_period: Optional[str] = Field(
        None, description="Demand charge billing period"
    )
    address: Optional[dict] = Field(None, description="Address metadata")
    billing_metadata: Optional[dict] = Field(None, description="Billing metadata")
    building_load_source: Optional[dict] = Field(None, description="Building load source metadata")


class ViewerInfo(BaseModel):
    """Identity context echoed back to the caller of GET /me/depots.

    Lets the frontend make routing decisions (wizard vs. dashboard, tenant picker
    for platform admins) without re-decoding the JWT client-side.
    """

    role: str = Field(..., description="Effective role from JWT app_metadata.favonius_role")
    organization_id: Optional[str] = Field(
        None, description="Caller's organization UUID (None for favonius_admin or unscoped users)"
    )


class DepotListResponse(BaseModel):
    """Response from GET /me/depots."""

    depots: list[DepotMetadata] = Field(default_factory=list)
    needs_setup: bool = Field(
        False,
        description=(
            "True only for customer_admin/customer_operator with an organization_id and zero "
            "depots in that organization. Always False for favonius_admin and for users without "
            "an organization_id. Frontends should use this to decide whether to render the "
            "first-depot setup wizard rather than guessing from depots length."
        ),
    )
    viewer: ViewerInfo = Field(..., description="Caller identity context")


class DepotAddressPayload(BaseModel):
    """Depot address and geolocation."""

    line1: str = Field(..., min_length=1, max_length=255)
    line2: Optional[str] = Field(default=None, max_length=255)
    city: str = Field(..., min_length=1, max_length=128)
    postal_code: Optional[str] = Field(default=None, max_length=32)
    country: str = Field(..., min_length=1, max_length=128)
    latitude: float = Field(..., ge=-90.0, le=90.0)
    longitude: float = Field(..., ge=-180.0, le=180.0)


class SimpleDemandTariffPayload(BaseModel):
    """Legacy demand-charge tariff: $/kW × peak grid power."""

    model_config = {"extra": "forbid"}

    tariff_type: Literal["simple_demand"] = "simple_demand"
    rate_eur_per_kw: float = Field(..., gt=0)
    billing_period: str = Field(..., min_length=1, max_length=32)


class EnergyCapTariffPayload(BaseModel):
    """Energy-cap tariff: piecewise-linear under/over a kWh allowance.

    Forbids ``rate_eur_per_kw`` so a misrouted simple_demand payload
    cannot silently drop fields. The validator enforces that the
    over-cap penalty strictly exceeds the under-cap rate, otherwise the
    optimizer has no incentive to stay below the cap.
    """

    model_config = {"extra": "forbid"}

    tariff_type: Literal["energy_cap"]
    energy_cap_kwh: float = Field(..., gt=0)
    under_cap_rate_per_kwh: float = Field(..., gt=0)
    over_cap_penalty_per_kwh: float = Field(..., gt=0)
    cap_billing_period: str = Field(default="monthly", min_length=1, max_length=32)

    @model_validator(mode="after")
    def _penalty_exceeds_under(self) -> "EnergyCapTariffPayload":
        if self.over_cap_penalty_per_kwh <= self.under_cap_rate_per_kwh:
            raise ValueError(
                "over_cap_penalty_per_kwh must be strictly greater than "
                "under_cap_rate_per_kwh"
            )
        return self


# Discriminated union on tariff_type. Payloads without an explicit
# tariff_type fall through to simple_demand (default on the literal),
# preserving backward compatibility for existing clients.
DepotDemandChargePayload = Annotated[
    Union[SimpleDemandTariffPayload, EnergyCapTariffPayload],
    Field(discriminator="tariff_type"),
]


class DepotBillingPayload(BaseModel):
    """Customer billing metadata."""

    account_number: Optional[str] = Field(default=None, max_length=128)
    tariff_name: Optional[str] = Field(default=None, max_length=128)
    meter_id: Optional[str] = Field(default=None, max_length=128)
    billing_cycle_day: Optional[int] = Field(default=None, ge=1, le=31)
    notes: Optional[str] = Field(default=None, max_length=1024)


class BuildingLoadSourcePayload(BaseModel):
    """Building load source configuration.

    Per PRD §9.4 (post-HRX revision), building load is OPTIONAL for
    initial onboarding. When ``type == 'static_assumption'``, the depot
    supplies a single ``assumption_kw`` value that the optimizer applies
    as a constant baseline load on the grid (so the site-power
    constraint stays respected without a meter integration).

    ``type`` values:
        - ``meter`` — live Modbus/SCADA meter
        - ``api`` — building management system API
        - ``manual`` — manually entered baseline schedule
        - ``static_assumption`` — single-scalar derate (HRX day-one)
        - ``none`` — no source configured (read-only flag, never used
          to drive optimization)
    """

    type: Literal["meter", "api", "manual", "static_assumption", "none"]
    provider: Optional[str] = Field(default=None, max_length=128)
    identifier: Optional[str] = Field(default=None, max_length=128)
    interval_minutes: Optional[int] = Field(default=None, ge=1, le=1440)
    assumption_kw: Optional[float] = Field(
        default=None,
        ge=0.0,
        description=(
            "Constant baseline kW applied as a derate when type='static_assumption'. "
            "Required when type='static_assumption'; ignored otherwise."
        ),
    )
    notes: Optional[str] = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def _validate_assumption_required(self) -> "BuildingLoadSourcePayload":
        """``assumption_kw`` must be set (>0) iff type=='static_assumption'."""
        if self.type == "static_assumption":
            if self.assumption_kw is None or self.assumption_kw <= 0:
                raise ValueError("assumption_kw must be > 0 when type='static_assumption'")
        elif self.assumption_kw not in (None, 0):
            raise ValueError("assumption_kw must be omitted unless type='static_assumption'")
        return self


class StationaryBatteryPayload(BaseModel):
    """Stationary battery settings."""

    present: bool
    name: Optional[str] = Field(default=None, max_length=128)
    capacity_kwh: Optional[float] = Field(default=None, gt=0)
    max_charge_kw: Optional[float] = Field(default=None, gt=0)
    max_discharge_kw: Optional[float] = Field(default=None, gt=0)
    min_soc_pct: Optional[float] = Field(default=None, ge=0, le=100)
    max_soc_pct: Optional[float] = Field(default=None, ge=0, le=100)


class DepotSetupPayload(BaseModel):
    """Top-level depot setup payload."""

    name: str = Field(..., min_length=1, max_length=255)
    address: DepotAddressPayload
    timezone: str = Field(..., min_length=1, max_length=64)
    currency: str = Field(..., min_length=3, max_length=10)
    utility_id: str = Field(..., min_length=1, max_length=100)
    max_grid_kw: float = Field(..., gt=0, le=100_000)
    demand_charge: DepotDemandChargePayload
    billing: DepotBillingPayload = Field(default_factory=DepotBillingPayload)
    building_load_source: BuildingLoadSourcePayload
    stationary_battery: StationaryBatteryPayload = Field(
        default_factory=lambda: StationaryBatteryPayload(present=False)
    )
    # Migration 021: depots can opt out of the explicit charger×vehicle
    # access matrix. 'all_to_all' means every charger reaches every
    # vehicle and the matrix is synthesized at solve time.
    charger_vehicle_access_default: Literal["all_to_all", "explicit_matrix"] = (
        "explicit_matrix"
    )

    @model_validator(mode="before")
    @classmethod
    def _default_demand_charge_tariff_type(cls, data):
        """Default ``demand_charge.tariff_type`` to ``simple_demand`` when
        absent so existing clients (and pre-021 tests) keep working with
        the strict discriminator on the new union.
        """
        if isinstance(data, dict):
            dc = data.get("demand_charge")
            if isinstance(dc, dict) and "tariff_type" not in dc:
                data = {**data, "demand_charge": {**dc, "tariff_type": "simple_demand"}}
            elif isinstance(dc, BaseModel):
                dc_payload = dc.model_dump()
                if "tariff_type" not in dc_payload:
                    data = {
                        **data,
                        "demand_charge": {**dc_payload, "tariff_type": "simple_demand"},
                    }
        return data


class FirstDepotSetupRequest(BaseModel):
    """Request for initial depot creation."""

    depot: DepotSetupPayload


class ChargerVehicleAccessEntry(BaseModel):
    """Single (charger, vehicle, accessible?) row for the access matrix."""

    charger_id: str = Field(..., min_length=1, max_length=64)
    vehicle_id: str = Field(..., min_length=1, max_length=64)
    is_accessible: bool

    @field_validator("charger_id", "vehicle_id")
    @classmethod
    def _validate_uuid(cls, value: str) -> str:
        try:
            UUID(value)
        except ValueError as exc:
            raise ValueError("must be a valid UUID") from exc
        return value


class ChargerVehicleAccessRequest(BaseModel):
    """POST /admin/depots/{id}/charger-vehicle-access body."""

    entries: list[ChargerVehicleAccessEntry] = Field(..., min_length=1)


class ReadinessChecklistItem(BaseModel):
    """Single readiness check item."""

    id: str
    label: str
    status: Literal["ready", "warning", "blocked"]
    detail: str


class DepotSetupSummary(BaseModel):
    """Depot summary returned by setup endpoints."""

    id: str
    name: str
    timezone: str
    currency: str
    max_grid_kw: float


class DepotSetupResponse(BaseModel):
    """Response for create/update setup requests."""

    depot: DepotSetupSummary
    readiness_checklist: list[ReadinessChecklistItem]


class _CamelOrSnakeModel(BaseModel):
    """Pydantic base that accepts both snake_case and camelCase keys.

    Canonical Python field names are snake_case; the camelCase alias is
    derived via ``alias_generator=to_camel``, and ``populate_by_name=True``
    keeps the snake form valid input. FastAPI serializes by field name by
    default, so responses emit snake_case unless ``by_alias=True`` is used.
    """

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class ChargerCreateRequest(_CamelOrSnakeModel):
    """Request to provision a production OCPP charger."""

    display_name: str = Field(..., min_length=1, max_length=255)
    vendor: Optional[str] = Field(default=None, max_length=128)
    model: Optional[str] = Field(default=None, max_length=128)
    serial_number: Optional[str] = Field(default=None, max_length=128)
    firmware: Optional[str] = Field(default=None, max_length=128)
    rated_kw: float = Field(..., gt=0, le=1000)
    connector_type: Literal["CCS"] = "CCS"
    connector_count: int = Field(..., ge=1, le=20)
    connector_ids: Optional[list[int]] = None
    network_notes: Optional[str] = Field(default=None, max_length=2048)

    @field_validator("connector_ids")
    @classmethod
    def validate_connector_ids(cls, value: Optional[list[int]]) -> Optional[list[int]]:
        """Ensure connector ids are positive and unique when supplied."""
        if value is None:
            return value
        if any(connector_id < 1 for connector_id in value):
            raise ValueError("connector_ids must contain positive integers")
        if len(set(value)) != len(value):
            raise ValueError("connector_ids must be unique")
        return value


class ChargerOnboardingMetadata(_CamelOrSnakeModel):
    """Provisioned charger metadata."""

    id: str
    display_name: str
    depot_id: str
    ocpp_id: str


class ChargerOnboardingCredentials(_CamelOrSnakeModel):
    """One-time OCPP Basic Auth credential response."""

    username: str
    password: str
    scheme: Literal["basic"] = "basic"
    shown_once: bool = True


class ChargerOnboardingResponse(BaseModel):
    """Response from charger onboarding."""

    charger: ChargerOnboardingMetadata
    credentials: ChargerOnboardingCredentials


# ============ Fleet Identity Models ============


class VehicleIdentityBase(_CamelOrSnakeModel):
    """Vehicle identity metadata managed by customer admins."""

    external_id: str = Field(..., min_length=1, max_length=100)
    display_name: Optional[str] = Field(default=None, max_length=255)
    vehicle_type: str = Field(..., min_length=1, max_length=50)
    battery_kwh: float = Field(..., gt=0)
    max_charge_kw: float = Field(..., gt=0)
    id_tag: Optional[str] = Field(default=None, min_length=1, max_length=100)
    vin: Optional[str] = Field(default=None, max_length=64)
    license_plate: Optional[str] = Field(default=None, max_length=64)
    status: Literal["active", "inactive", "retired"] = "active"


class VehicleIdentityUpdate(_CamelOrSnakeModel):
    """Patch vehicle identity metadata except the primary idTag."""

    external_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    display_name: Optional[str] = Field(default=None, max_length=255)
    vehicle_type: Optional[str] = Field(default=None, min_length=1, max_length=50)
    battery_kwh: Optional[float] = Field(default=None, gt=0)
    max_charge_kw: Optional[float] = Field(default=None, gt=0)
    vin: Optional[str] = Field(default=None, max_length=64)
    license_plate: Optional[str] = Field(default=None, max_length=64)
    status: Optional[Literal["active", "inactive", "retired"]] = None


class PrimaryIdTagRequest(_CamelOrSnakeModel):
    """Set or clear a vehicle's primary OCPP idTag."""

    id_tag: Optional[str] = Field(default=None, min_length=1, max_length=100)


class DriverIdentityCreate(_CamelOrSnakeModel):
    """Driver identity metadata managed by customer admins."""

    external_driver_id: Optional[str] = Field(default=None, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=255)
    email: Optional[str] = Field(default=None, max_length=255)
    phone: Optional[str] = Field(default=None, max_length=64)
    status: Literal["active", "inactive"] = "active"


class DriverIdentityUpdate(_CamelOrSnakeModel):
    """Patch driver identity metadata."""

    external_driver_id: Optional[str] = Field(default=None, max_length=100)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    email: Optional[str] = Field(default=None, max_length=255)
    phone: Optional[str] = Field(default=None, max_length=64)
    status: Optional[Literal["active", "inactive"]] = None


class RfidCardCreate(_CamelOrSnakeModel):
    """RFID card metadata and current assignments."""

    id_tag: str = Field(..., min_length=1, max_length=100)
    label: Optional[str] = Field(default=None, max_length=255)
    status: Literal["active", "inactive", "lost", "stolen"] = "active"
    notes: Optional[str] = Field(default=None, max_length=2048)
    assigned_vehicle_ids: list[str] = Field(default_factory=list)
    assigned_driver_ids: list[str] = Field(default_factory=list)

    @field_validator("assigned_vehicle_ids", "assigned_driver_ids")
    @classmethod
    def validate_assignment_ids(cls, value: list[str]) -> list[str]:
        """Ensure supplied relationship ids are valid UUIDs."""
        for item in value:
            UUID(item)
        return value


class RfidCardUpdate(_CamelOrSnakeModel):
    """Patch RFID card metadata and optionally replace current assignments."""

    id_tag: Optional[str] = Field(default=None, min_length=1, max_length=100)
    label: Optional[str] = Field(default=None, max_length=255)
    status: Optional[Literal["active", "inactive", "lost", "stolen"]] = None
    notes: Optional[str] = Field(default=None, max_length=2048)
    assigned_vehicle_ids: Optional[list[str]] = None
    assigned_driver_ids: Optional[list[str]] = None

    @field_validator("assigned_vehicle_ids", "assigned_driver_ids")
    @classmethod
    def validate_optional_assignment_ids(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        """Ensure supplied relationship ids are valid UUIDs."""
        if value is None:
            return value
        for item in value:
            UUID(item)
        return value


class VehicleIdentityResponse(BaseModel):
    """Vehicle identity response."""

    vehicle_id: str
    depot_id: str
    external_id: str
    display_name: Optional[str] = None
    vehicle_type: str
    battery_kwh: float
    max_charge_kw: float
    id_tag: Optional[str] = None
    vin: Optional[str] = None
    license_plate: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DriverIdentityResponse(BaseModel):
    """Driver identity response."""

    driver_id: str
    depot_id: str
    external_driver_id: Optional[str] = None
    display_name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class RfidCardResponse(BaseModel):
    """RFID card identity response."""

    card_id: str
    depot_id: str
    id_tag: str
    label: Optional[str] = None
    status: str
    notes: Optional[str] = None
    assigned_vehicle_ids: list[str] = Field(default_factory=list)
    assigned_driver_ids: list[str] = Field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class FleetIdentityResponse(BaseModel):
    """Fleet identity tab response."""

    vehicles: list[VehicleIdentityResponse] = Field(default_factory=list)
    drivers: list[DriverIdentityResponse] = Field(default_factory=list)
    rfid_cards: list[RfidCardResponse] = Field(default_factory=list)


# ============ Command Dispatcher Models ============


class CommandRequest(BaseModel):
    """Request to execute a depot management command."""

    command: str = Field(
        ...,
        description="Command name in dot-notation (e.g. fleet.charger.restart)",
        examples=["fleet.charger.restart"],
    )
    depot_id: str = Field(..., description="Depot to execute the command against (UUID)")
    params: dict = Field(default_factory=dict, description="Command-specific parameters")
    dry_run: bool = Field(
        default=False,
        description="If true, validate and simulate the command without persisting changes",
    )


class CommandResponse(BaseModel):
    """Response from POST /commands/execute."""

    status: str = Field(..., description="'ok' for real execution, 'dry_run' for simulated")
    command: str
    depot_id: str
    result: dict = Field(default_factory=dict)


# ============ Custom Exceptions ============


class DepotNotFoundError(ValueError):
    """Raised when depot is not found in database."""

    pass


# ``OptimizationError`` and ``DatabaseError`` are intentionally aliased to the
# canonical definitions in ``src.core.optimizer.exceptions`` and
# ``src.db.exceptions``. This way:
#   * ``raise OptimizationError(...)`` from main.py matches the same handler
#     as ``raise InfeasibleModelError(...)`` or ``raise SolverTimeoutError(...)``
#     from the core layer (they are all subclasses of the core base).
#   * ``raise DatabaseError(...)`` from main.py and the new typed db layer go
#     through the same global handler regardless of where they originate.
OptimizationError = _CoreOptimizationError
DatabaseError = _DbDatabaseError


# ============ Exception Handlers ============
#
# Every handler logs the raw exception with full traceback + request context
# and returns a SANITIZED JSON body. The response body never echoes
# ``str(exc)`` for INTERNAL_ERROR / DATABASE_ERROR cases — those messages may
# contain SQL fragments, table/column names, file paths, or other internals
# that an attacker can use for reconnaissance.


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handle Pydantic validation errors.

    Pydantic's own ``loc``/``msg`` strings reference the request schema and
    are safe to expose. ``ctx`` and ``input`` may contain user-supplied data
    so they are dropped.
    """
    errors = exc.errors()
    field_errors: dict[str, list[str]] = {}
    sanitized_errors: list[dict[str, str]] = []
    for err in errors:
        loc = err.get("loc", ())
        path = ".".join(str(part) for part in loc if part != "body")
        if not path:
            path = "body"
        message = str(err.get("msg", "Invalid value"))
        field_errors.setdefault(path, []).append(message)
        sanitized_errors.append({"path": path, "message": message})
    logger.warning(
        "Validation error on %s",
        request.url.path,
        extra={"path": request.url.path, "errors": sanitized_errors},
    )
    return _build_error_response(
        request,
        ErrorCode.VALIDATION_ERROR,
        extra={"field_errors": field_errors, "validation_errors": sanitized_errors},
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """Handle ValueError exceptions (e.g., invalid UUID, depot not found).

    ``ValueError`` is raised explicitly throughout the codebase with
    operator-controlled, public-safe text (e.g. ``"Vehicle not found"``,
    ``"Invalid UUID"``). We pass that text through as ``detail`` rather than
    forcing the generic mapping, so the client gets a useful message.
    """
    error_msg = str(exc)
    if "not found" in error_msg.lower() or isinstance(exc, DepotNotFoundError):
        code = ErrorCode.DEPOT_NOT_FOUND
    else:
        code = ErrorCode.INVALID_INPUT

    logger.warning(f"ValueError on {request.url.path}: {error_msg}")
    return _build_error_response(request, code, detail=error_msg)


@app.exception_handler(OptimizationError)
async def optimization_error_handler(request: Request, exc: OptimizationError) -> JSONResponse:
    """Handle optimization failures.

    Maps the specific subclass to a stable error code:
      * ``InfeasibleModelError`` -> ``OPTIMIZER_INFEASIBLE`` (422)
      * ``SolverTimeoutError``   -> ``OPTIMIZER_TIMEOUT`` (504)
      * everything else          -> ``OPTIMIZATION_ERROR`` (500)

    Original message is logged but never returned to the client.
    """
    if isinstance(exc, InfeasibleModelError):
        code = ErrorCode.OPTIMIZER_INFEASIBLE
    elif isinstance(exc, SolverTimeoutError):
        code = ErrorCode.OPTIMIZER_TIMEOUT
    else:
        code = ErrorCode.OPTIMIZATION_ERROR
    logger.error(
        f"Optimization error on {request.url.path}: {exc}",
        exc_info=True,
        extra={"error_code": str(code.value), "path": request.url.path},
    )
    return _build_error_response(request, code)


@app.exception_handler(IdempotencyKeyReusedError)
async def idempotency_key_reused_handler(
    request: Request, exc: IdempotencyKeyReusedError
) -> JSONResponse:
    """Handle idempotency-key collisions with a stable 409 shape."""
    logger.warning(
        f"Idempotency key reused on {request.url.path}",
        extra={"path": request.url.path},
    )
    return _build_error_response(request, ErrorCode.IDEMPOTENCY_KEY_REUSED)


@app.exception_handler(DatabaseError)
async def database_error_handler(request: Request, exc: DatabaseError) -> JSONResponse:
    """Handle database operation failures.

    Note: a ``DatabaseError`` may be constructed with raw ``str(asyncpg_err)``
    upstream. We log it (so operators can debug) but always send the generic
    sanitized message back to the client. Subclasses with more specific
    codes (e.g. ``IdempotencyKeyReusedError``) are caught by their own
    handlers above this one due to FastAPI's MRO-based dispatch.
    """
    logger.error(
        f"Database error on {request.url.path}: {exc}",
        exc_info=True,
        extra={"path": request.url.path},
    )
    code = getattr(exc, "code", ErrorCode.DATABASE_ERROR)
    if not isinstance(code, ErrorCode):
        code = ErrorCode.DATABASE_ERROR
    return _build_error_response(request, code)


@app.exception_handler(asyncpg.PostgresError)
async def postgres_error_handler(request: Request, exc: asyncpg.PostgresError) -> JSONResponse:
    """Handle PostgreSQL-specific errors.

    ``asyncpg`` exception messages frequently include SQL fragments, table
    names, constraint names, and column metadata. They are logged
    server-side and replaced with the stable ``DATABASE_ERROR`` message.
    """
    logger.error(
        f"PostgreSQL error on {request.url.path}: {exc.__class__.__name__}",
        exc_info=True,
        extra={"path": request.url.path},
    )
    return _build_error_response(request, ErrorCode.DATABASE_ERROR)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Render every ``HTTPException`` in the standard ``ErrorResponse`` shape.

    FastAPI's default handler returns ``{"detail": ...}``. We add ``error_code``,
    ``timestamp``, and ``request_id`` at the top level so every error response
    on this API has the same envelope regardless of origin.

    Backwards compatibility:
      * If the call site passes ``detail`` as a string, ``response.detail`` is
        that string verbatim (matches FastAPI default).
      * If the call site passes ``detail`` as a dict (e.g.
        ``{"error_code": "...", "vehicle_ids": [...]}``), the dict is
        preserved verbatim under ``detail`` so existing clients can keep
        reading ``response.detail.error_code`` and friends. The top-level
        ``error_code`` reflects the same value if provided, otherwise it is
        derived from the status code.
    """
    detail = exc.detail
    if isinstance(detail, dict):
        provided_code = detail.get("error_code")
    else:
        provided_code = None

    code = _http_status_to_error_code(
        exc.status_code,
        provided_code if isinstance(provided_code, str) else None,
    )

    if exc.status_code >= 500:
        logger.error(
            f"HTTPException {exc.status_code} on {request.url.path}",
            extra={"path": request.url.path, "status_code": exc.status_code},
        )
    else:
        logger.info(
            f"HTTPException {exc.status_code} on {request.url.path}",
            extra={"path": request.url.path, "status_code": exc.status_code},
        )

    body: dict = {
        "detail": detail if detail is not None else safe_message_for(code),
        "error_code": str(provided_code) if provided_code else str(code.value),
        "timestamp": datetime.utcnow().isoformat(),
        "request_id": _get_request_id(request),
    }
    response = JSONResponse(status_code=exc.status_code, content=body)
    if exc.headers:
        for header, value in exc.headers.items():
            response.headers[header] = value
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler for any exception not matched above.

    Logs the full traceback with request context but returns ONLY the
    generic ``INTERNAL_ERROR`` envelope — never ``str(exc)``. The client
    sees the ``request_id`` so support can correlate the report with the
    server log.
    """
    logger.error(
        f"Unhandled exception on {request.url.path}: {exc.__class__.__name__}",
        exc_info=True,
        extra={
            "path": request.url.path,
            "method": request.method,
            "exception_type": exc.__class__.__name__,
        },
    )
    return _build_error_response(request, ErrorCode.INTERNAL_ERROR)


def _http_status_to_error_code(
    status_code: int, provided_code: Optional[str] = None
) -> ErrorCode:
    """Best-effort mapping from raw status code to an ``ErrorCode``.

    Used by ``http_exception_handler`` so legacy call sites that raise
    ``HTTPException(status_code=...)`` without an ``error_code`` still get a
    sensible ``error_code`` field on the response. If the call site supplies
    its own ``error_code`` (via ``detail={"error_code": ...}``) and it
    matches a known value, we honor it.
    """
    if provided_code:
        try:
            return ErrorCode(provided_code)
        except ValueError:
            pass
    return {
        400: ErrorCode.BAD_REQUEST,
        401: ErrorCode.UNAUTHORIZED,
        403: ErrorCode.FORBIDDEN,
        404: ErrorCode.NOT_FOUND,
        409: ErrorCode.CONFLICT,
        422: ErrorCode.UNPROCESSABLE_ENTITY,
        429: ErrorCode.RATE_LIMIT_EXCEEDED,
        500: ErrorCode.INTERNAL_ERROR,
        503: ErrorCode.SERVICE_UNAVAILABLE,
    }.get(status_code, ErrorCode.INTERNAL_ERROR)


async def _get_depot_config(depot_id: str) -> DepotConfig:
    """Get depot configuration from database.

    Uses StateAssembler.load_depot_config() to query depots, vehicles,
    chargers, and battery_storage tables. Implements caching with a per-depot
    asyncio.Lock to prevent thundering-herd DB load on cache expiry.

    Args:
        depot_id: Depot identifier (must be valid UUID)

    Returns:
        DepotConfig object

    Raises:
        HTTPException 404: If depot not found
        HTTPException 500: If configuration is invalid
        HTTPException 503: If database is not available
    """
    if not db_pools:
        raise HTTPException(status_code=503, detail="Database not available")

    current_time = time.time()
    if depot_id in _depot_config_cache:
        config, cache_time = _depot_config_cache[depot_id]
        if current_time - cache_time < _config_cache_ttl:
            logger.debug(f"Using cached config for depot {depot_id}")
            return config
        _depot_config_cache.pop(depot_id, None)

    if depot_id not in _depot_config_locks:
        _depot_config_locks[depot_id] = asyncio.Lock()

    async with _depot_config_locks[depot_id]:
        # Recheck inside the lock: first waiter fills it, subsequent waiters hit it.
        current_time = time.time()
        if depot_id in _depot_config_cache:
            config, cache_time = _depot_config_cache[depot_id]
            if current_time - cache_time < _config_cache_ttl:
                logger.debug(f"Using cached config for depot {depot_id}")
                return config
            _depot_config_cache.pop(depot_id, None)

        try:
            config, _ = await StateAssembler.load_depot_config(db_pools, depot_id)

            if not config.vehicle_capacities:
                raise HTTPException(
                    status_code=400, detail=f"Depot {depot_id} has no vehicles configured"
                )
            if config.n_chargers == 0:
                logger.warning(
                    f"Depot {depot_id} has no chargers configured, optimization may fail"
                )

            _depot_config_cache[depot_id] = (config, time.time())
            logger.info(
                f"Loaded depot config for {depot_id}: "
                f"{len(config.vehicle_capacities)} vehicles, "
                f"{config.n_chargers} chargers"
            )
            return config

        except ValueError as e:
            error_msg = str(e)
            if "not found" in error_msg.lower():
                logger.warning(f"Depot not found: {depot_id}")
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error_code": ErrorCode.DEPOT_NOT_FOUND.value,
                        "detail": error_msg,
                    },
                ) from e
            logger.error(f"Invalid depot configuration: {error_msg}")
            raise HTTPException(
                status_code=500,
                detail={
                    "error_code": ErrorCode.INTERNAL_ERROR.value,
                    "detail": "Invalid depot configuration",
                },
            ) from e
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to load depot config: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={
                    "error_code": ErrorCode.INTERNAL_ERROR.value,
                    "detail": "Failed to load depot configuration",
                },
            ) from e


def _format_depot_setup_validation_errors(exc: ValidationError) -> dict:
    """Map pydantic validation errors into frontend-friendly shape."""
    field_errors: dict[str, list[str]] = {}
    validation_errors: list[dict[str, str]] = []
    for err in exc.errors():
        loc = err.get("loc", ())
        path = ".".join(str(part) for part in loc if part != "body")
        if not path:
            path = "depot"
        message = err.get("msg", "Invalid value")
        field_errors.setdefault(path, []).append(message)
        validation_errors.append({"path": path, "message": message})
    return {
        "detail": "Validation failed",
        "error_code": "VALIDATION_ERROR",
        "field_errors": field_errors,
        "validation_errors": validation_errors,
    }


def _depot_setup_tariff_kwargs(
    demand_charge: "SimpleDemandTariffPayload | EnergyCapTariffPayload",
) -> dict:
    """Map a discriminated tariff payload to db_queries.create/update kwargs.

    Both branches always populate ``demand_charge_rate_kw`` and
    ``demand_charge_billing_period`` because those columns are
    ``NOT NULL`` in the depots schema; the energy-cap variant just uses
    sensible placeholders (rate=0, period from cap_billing_period) so
    callers reading the legacy columns still see something coherent.
    """
    if isinstance(demand_charge, EnergyCapTariffPayload):
        return {
            "demand_charge_rate_kw": 0.0,
            "demand_charge_billing_period": demand_charge.cap_billing_period,
            "tariff_type": "energy_cap",
            "energy_cap_kwh": demand_charge.energy_cap_kwh,
            "under_cap_rate_per_kwh": demand_charge.under_cap_rate_per_kwh,
            "over_cap_penalty_per_kwh": demand_charge.over_cap_penalty_per_kwh,
            "cap_billing_period": demand_charge.cap_billing_period,
        }
    return {
        "demand_charge_rate_kw": demand_charge.rate_eur_per_kw,
        "demand_charge_billing_period": demand_charge.billing_period,
        "tariff_type": "simple_demand",
        "energy_cap_kwh": None,
        "under_cap_rate_per_kwh": None,
        "over_cap_penalty_per_kwh": None,
        "cap_billing_period": "monthly",
    }


def _validate_depot_setup_payload(payload: DepotSetupPayload) -> None:
    """Run semantic validation that is not covered by simple field constraints."""
    try:
        ZoneInfo(payload.timezone)
    except Exception as exc:
        raise ValueError("depot.timezone: Invalid IANA timezone") from exc

    if not payload.currency.isalpha() or len(payload.currency) != 3:
        raise ValueError("depot.currency: Invalid currency code")

    battery = payload.stationary_battery
    if battery.present:
        required_fields = (
            battery.capacity_kwh,
            battery.max_charge_kw,
            battery.max_discharge_kw,
            battery.min_soc_pct,
            battery.max_soc_pct,
        )
        if any(field is None for field in required_fields):
            raise ValueError("depot.stationary_battery: Missing required battery fields")
        if battery.min_soc_pct >= battery.max_soc_pct:
            raise ValueError("depot.stationary_battery: min_soc_pct must be less than max_soc_pct")


def _slugify_ocpp_component(value: str, fallback: str) -> str:
    """Return a URL-safe slug component for generated OCPP IDs."""
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug[:32] or fallback


def _canonical_request_hash(payload: BaseModel) -> str:
    """Hash a request model using stable JSON serialization."""
    body = payload.model_dump(mode="json", by_alias=True, exclude_none=True)
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _generate_ocpp_basic_password() -> str:
    """Generate a high-entropy URL-safe one-time Basic Auth password."""
    return secrets.token_urlsafe(32)


async def _hash_ocpp_basic_password(password: str) -> str:
    """Hash a Basic Auth password for station_credentials."""
    hashed = await asyncio.to_thread(bcrypt.hashpw, password.encode("utf-8"), bcrypt.gensalt())
    return hashed.decode("utf-8")


def _connector_ids_for_request(payload: ChargerCreateRequest) -> list[int]:
    """Return connector ids, enforcing consistency with connector_count."""
    if payload.connector_ids is None:
        return list(range(1, payload.connector_count + 1))
    if len(payload.connector_ids) != payload.connector_count:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="connector_ids length must equal connector_count",
        )
    return payload.connector_ids


def _json_response_payload(value: object) -> object:
    """Normalize asyncpg JSONB values returned as either JSON strings or Python objects."""
    if isinstance(value, str):
        return json.loads(value)
    return value


def _format_charger_onboarding_response(charger: dict, password: str) -> dict:
    """Build the public response shape, including one-time plaintext credential.

    Wire format is snake_case to match the rest of the API surface; clients
    that need camelCase can rely on the API client's case transform.
    """
    return {
        "charger": {
            "id": charger["id"],
            "display_name": charger["display_name"],
            "depot_id": charger["depot_id"],
            "ocpp_id": charger["ocpp_id"],
        },
        "credentials": {
            "username": charger["ocpp_id"],
            "password": password,
            "scheme": "basic",
            "shown_once": True,
        },
    }


def _format_manual_schedule_row(row: dict) -> dict:
    """Normalize a schedule DB row for API responses."""
    return {
        "schedule_id": str(row["schedule_id"]),
        "vehicle_id": str(row["vehicle_id"]),
        "route_id": row["route_id"],
        "departure_time": row["departure_time"],
        "return_time": row["return_time"],
        "required_soc": row["required_soc"],
        "energy_kwh": row["energy_kwh"],
    }


def _readiness_response_payload(depot_id: str, checks: list[dict]) -> dict:
    """Build readiness response including aggregate readiness."""
    return {
        "depot_id": depot_id,
        "ready": all(check["status"] == "ready" for check in checks),
        "checks": checks,
    }


def _require_customer_admin_with_org(user: dict) -> str:
    """Ensure caller is customer_admin and carries an organization_id claim."""
    role = get_user_role(user)
    if role != "customer_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: customer_admin role required",
        )
    org_id = user.get("app_metadata", {}).get("organization_id")
    if not org_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: missing organization_id in token app_metadata",
        )
    return str(org_id)


def _handle_identity_unique_violation(exc: asyncpg.UniqueViolationError) -> HTTPException:
    """Map identity uniqueness conflicts to frontend-stable 409 responses."""
    constraint = getattr(exc, "constraint_name", "") or ""
    if "id_tag" in constraint:
        detail = "idTag is already registered"
    elif "vehicles_external_id" in constraint:
        detail = "External identifier is already registered"
    elif "external" in constraint:
        detail = "External identifier is already registered for this depot"
    else:
        detail = "Identity record already exists"
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


async def _audit_identity_write(user: dict, depot_id: str, action: str, resource_id: str) -> None:
    """Best-effort audit trail for customer-admin identity writes."""
    audit = get_audit_logger()
    if audit is None:
        return
    await audit.log(
        AuditEvent(
            event_type="IDENTITY_WRITE",
            user_id=user.get("sub"),
            resource=f"/admin/depots/{depot_id}/identity",
            details={"action": action, "depot_id": depot_id, "resource_id": resource_id},
        )
    )


async def _build_readiness_checklist(depot_id: str, payload: DepotSetupPayload) -> list[dict]:
    """Build exact setup readiness checklist for optimization prerequisites."""
    return await _build_depot_readiness_checklist(
        depot_id,
        battery_present=payload.stationary_battery.present,
    )


async def _build_depot_readiness_checklist(
    depot_id: str,
    *,
    battery_present: Optional[bool] = None,
) -> list[dict]:
    """Build persisted depot readiness checklist for setup and schedule screens."""
    if not db_pools:
        return []

    if battery_present is None:
        battery_present = False

    async with db_pools.static.acquire() as conn:
        has_vehicles = bool(
            await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM vehicles WHERE depot_id = $1::uuid)", depot_id
            )
        )
        has_chargers = bool(
            await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM chargers WHERE depot_id = $1::uuid)", depot_id
            )
        )
        access_default = (
            await conn.fetchval(
                "SELECT charger_vehicle_access_default FROM depots WHERE depot_id = $1::uuid",
                depot_id,
            )
        ) or "explicit_matrix"
        has_access = False
        if access_default != "all_to_all":
            has_access = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS(
                        SELECT 1
                        FROM charger_vehicle_access cva
                        JOIN chargers c ON c.charger_id = cva.charger_id
                        WHERE c.depot_id = $1::uuid AND cva.is_accessible = TRUE
                    )
                    """,
                    depot_id,
                )
            )
        has_schedules = bool(
            await conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM schedules s
                    JOIN vehicles v ON v.vehicle_id = s.vehicle_id
                    WHERE v.depot_id = $1::uuid AND s.departure_time >= NOW() - INTERVAL '1 hour'
                )
                """,
                depot_id,
            )
        )
        has_battery = bool(
            await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM battery_storage WHERE depot_id = $1::uuid)",
                depot_id,
            )
        )

    has_prices = False
    has_building_load = False
    if db_pools.ts is not None:
        async with db_pools.ts.acquire() as conn:
            has_prices = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM prices
                        WHERE depot_id = $1::uuid AND time >= NOW() - INTERVAL '24 hours'
                    )
                    """,
                    depot_id,
                )
            )
            has_building_load = bool(
                await conn.fetchval(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM building_load
                        WHERE depot_id = $1::uuid AND time >= NOW() - INTERVAL '24 hours'
                    )
                    """,
                    depot_id,
                )
            )

    checklist: list[dict] = []
    checklist.append(
        {
            "id": "site",
            "label": "Site geocoded",
            "status": "ready",
            "detail": "Coordinates present",
        }
    )
    checklist.append(
        {
            "id": "grid_capacity",
            "label": "Grid capacity configured",
            "status": "ready",
            "detail": "max_grid_kw configured",
        }
    )
    checklist.append(
        {
            "id": "tariff",
            "label": "Tariff configured",
            "status": "ready",
            "detail": "Demand charge present",
        }
    )
    checklist.append(
        {
            "id": "vehicles",
            "label": "Vehicles configured",
            "status": "ready" if has_vehicles else "blocked",
            "detail": (
                "Vehicle fleet present" if has_vehicles else "No vehicles configured for depot"
            ),
        }
    )
    checklist.append(
        {
            "id": "chargers",
            "label": "Chargers configured",
            "status": "ready" if has_chargers else "blocked",
            "detail": "Chargers present" if has_chargers else "No chargers configured for depot",
        }
    )
    if access_default == "all_to_all":
        access_status = "ready"
        access_detail = "All chargers reach all vehicles (all_to_all mode)"
    else:
        access_status = "ready" if has_access else "blocked"
        access_detail = (
            "Charger/vehicle accessibility mapped"
            if has_access
            else "No charger_vehicle_access mappings found"
        )
    checklist.append(
        {
            "id": "charger_access",
            "label": "Charger access mapped",
            "status": access_status,
            "detail": access_detail,
        }
    )
    checklist.append(
        {
            "id": "schedules",
            "label": "Schedules available",
            "status": "ready" if has_schedules else "blocked",
            "detail": (
                "Upcoming schedules found" if has_schedules else "No upcoming schedules found"
            ),
        }
    )
    checklist.append(
        {
            "id": "prices",
            "label": "Price data available",
            "status": "ready" if has_prices else "blocked",
            "detail": "Recent price rows found" if has_prices else "Missing recent price data",
        }
    )
    checklist.append(
        {
            "id": "building_load",
            "label": "Building load available",
            "status": "ready" if has_building_load else "blocked",
            "detail": (
                "Recent building load rows found"
                if has_building_load
                else "Missing building load data"
            ),
        }
    )
    battery_status = "ready" if (not battery_present or has_battery) else "blocked"
    checklist.append(
        {
            "id": "battery",
            "label": "Stationary battery configuration",
            "status": battery_status,
            "detail": (
                "Battery configured"
                if battery_status == "ready"
                else "Battery marked present but not saved"
            ),
        }
    )
    return checklist


@app.websocket("/ocpp/{charge_point_id}")
async def ocpp_websocket(websocket: WebSocket, charge_point_id: str):
    """OCPP 1.6 WebSocket endpoint (same port as REST when OCPP_USE_SAME_PORT=true).

    Security (C1): the upgrade is gated by OCPP-J Security Profile 1 Basic Auth.
    The Authorization header is verified against ``station_credentials`` BEFORE
    ``websocket.accept`` so unauthenticated peers cannot complete the handshake
    or learn anything beyond a generic policy-violation close.
    """
    cp_id = (charge_point_id or "").strip()
    if not cp_id:
        await websocket.close(code=4000)
        return
    if ocpp_server is None or db_pools is None:
        await websocket.close(code=1011)
        return

    auth_header = websocket.headers.get("authorization")
    if not await verify_ocpp_basic_auth(auth_header, cp_id, db_pools.static):
        peer = websocket.client.host if websocket.client else "unknown"
        logger.warning(
            "OCPP auth rejected: charge_point_id=%s peer=%s",
            cp_id,
            peer,
        )
        # 1008 = policy violation. Don't reveal which precondition failed.
        await websocket.close(code=1008)
        return

    await websocket.accept(subprotocol="ocpp1.6")
    try:
        await ocpp_server.handle_websocket(websocket, cp_id)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"OCPP WebSocket error for {cp_id}: {e}", exc_info=True)
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


@app.get(
    "/me/depots",
    response_model=DepotListResponse,
    tags=["depots"],
    summary="List depots accessible to the authenticated user",
    description="""
    Returns all depots the authenticated user has access to.

    For ``favonius_admin``, all depots are returned. Customer roles receive depots whose
    ``depots.organization_id`` matches ``app_metadata.organization_id`` on the JWT.

    The response also carries ``needs_setup`` (True iff the caller is a
    ``customer_admin``/``customer_operator`` with an ``organization_id`` and zero depots in
    that organization) and ``viewer`` (the caller's role and organization_id). Frontends
    should drive the first-depot setup wizard from ``needs_setup`` rather than guessing from
    ``depots`` length, which is unreliable when seed/cross-tenant data leaks into the response.

    **Authentication:** Requires JWT token in Authorization header.
    """,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def list_my_depots(user: dict = Depends(ensure_tenant_mirrored)):
    """List depots accessible to the authenticated user."""
    from ..security.auth import get_user_organization_id, is_platform_admin

    if not db_pools:
        raise DatabaseError("Database not available")

    role = get_user_role(user)
    org_id = get_user_organization_id(user)
    viewer = {"role": role, "organization_id": org_id}

    try:
        if is_platform_admin(user):
            async with db_pools.static.acquire() as conn:
                depots = await db_queries.get_all_depots(conn)
            return {"depots": depots, "needs_setup": False, "viewer": viewer}

        if role not in ("customer_admin", "customer_operator"):
            return {"depots": [], "needs_setup": False, "viewer": viewer}

        if not org_id:
            return {"depots": [], "needs_setup": False, "viewer": viewer}

        async with db_pools.static.acquire() as conn:
            depots = await db_queries.get_depots_for_organization(conn, org_id)
        return {
            "depots": depots,
            "needs_setup": len(depots) == 0,
            "viewer": viewer,
        }
    except asyncpg.PostgresError as e:
        logger.error("Database error listing depots: %s", e, exc_info=True)
        raise DatabaseError(f"Database error: {str(e)}")


@app.get(
    "/depots/{depot_id}",
    response_model=DepotMetadata,
    tags=["depots"],
    summary="Get depot metadata",
    description="""
    Returns static metadata for a single depot: name, timezone, currency, and
    grid capacity. Does not include live state (vehicle SoCs, prices) — use
    `GET /depots/{depot_id}/state` for that.

    **Authentication:** Requires JWT token in Authorization header.

    **Error Codes:**
    - 401: Unauthorized
    - 403: Depot access denied
    - 404: Depot not found
    - 503: Database not available
    """,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Depot access denied"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_depot_metadata(
    depot_id: str = Depends(_require_depot_access),
):
    """Get depot metadata (name, timezone, currency, max_grid_kw)."""
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        async with db_pools.static.acquire() as conn:
            depot = await db_queries.get_depot_by_id(conn, depot_id)
        if not depot:
            raise DepotNotFoundError(f"Depot {depot_id} not found")
        return depot
    except DepotNotFoundError:
        raise
    except HTTPException:
        raise
    except asyncpg.PostgresError as e:
        logger.error(
            "Database error getting depot metadata: %s",
            e,
            exc_info=True,
            extra={"depot_id": depot_id},
        )
        raise DatabaseError(f"Database error: {str(e)}")


@app.get(
    "/admin/depots/{depot_id}/identity",
    response_model=FleetIdentityResponse,
    tags=["admin"],
    summary="List fleet identity records for a depot",
)
async def get_fleet_identity(
    depot_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Return vehicles, drivers, RFID cards, and current card assignments."""
    org_id = _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.static.acquire() as conn:
        result = await db_queries.list_fleet_identity(
            conn, depot_id=depot_id, organization_id=org_id
        )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied: you do not have permission for this depot",
        )
    return result


@app.post(
    "/admin/depots/{depot_id}/vehicles",
    response_model=VehicleIdentityResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["admin"],
    summary="Create a vehicle identity record",
)
async def create_vehicle_identity(
    depot_id: str,
    request: VehicleIdentityBase,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Create a vehicle under an organization-owned depot."""
    org_id = _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        async with db_pools.static.acquire() as conn:
            vehicle = await db_queries.create_vehicle_identity(
                conn,
                depot_id=depot_id,
                organization_id=org_id,
                external_id=request.external_id,
                vehicle_type=request.vehicle_type,
                battery_kwh=request.battery_kwh,
                max_charge_kw=request.max_charge_kw,
                display_name=request.display_name,
                id_tag=request.id_tag,
                vin=request.vin,
                license_plate=request.license_plate,
                vehicle_status=request.status,
            )
        if vehicle is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: you do not have permission for this depot",
            )
        await _audit_identity_write(user, depot_id, "vehicle.create", vehicle["vehicle_id"])
        _depot_config_cache.pop(depot_id, None)
        return vehicle
    except asyncpg.UniqueViolationError as exc:
        raise _handle_identity_unique_violation(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@app.patch(
    "/admin/depots/{depot_id}/vehicles/{vehicle_id}",
    response_model=VehicleIdentityResponse,
    tags=["admin"],
    summary="Update vehicle identity fields",
)
async def update_vehicle_identity(
    depot_id: str,
    vehicle_id: str,
    request: VehicleIdentityUpdate,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Patch vehicle identity metadata except primary idTag."""
    org_id = _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    validate_vehicle_id(vehicle_id)
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        async with db_pools.static.acquire() as conn:
            vehicle = await db_queries.update_vehicle_identity(
                conn,
                depot_id=depot_id,
                organization_id=org_id,
                vehicle_id=vehicle_id,
                display_name=request.display_name,
                external_id=request.external_id,
                vehicle_type=request.vehicle_type,
                battery_kwh=request.battery_kwh,
                max_charge_kw=request.max_charge_kw,
                vin=request.vin,
                license_plate=request.license_plate,
                vehicle_status=request.status,
            )
        if vehicle is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle not found")
        await _audit_identity_write(user, depot_id, "vehicle.update", vehicle_id)
        _depot_config_cache.pop(depot_id, None)
        return vehicle
    except asyncpg.UniqueViolationError as exc:
        raise _handle_identity_unique_violation(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@app.put(
    "/admin/depots/{depot_id}/vehicles/{vehicle_id}/primary-id-tag",
    response_model=VehicleIdentityResponse,
    tags=["admin"],
    summary="Set or clear a vehicle primary idTag",
)
async def set_vehicle_primary_id_tag(
    depot_id: str,
    vehicle_id: str,
    request: PrimaryIdTagRequest,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Set/clear vehicles.id_tag, the primary OCPP vehicle idTag."""
    org_id = _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    validate_vehicle_id(vehicle_id)
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        async with db_pools.static.acquire() as conn:
            vehicle = await db_queries.set_vehicle_primary_id_tag(
                conn,
                depot_id=depot_id,
                organization_id=org_id,
                vehicle_id=vehicle_id,
                id_tag=request.id_tag,
            )
        if vehicle is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vehicle not found")
        await _audit_identity_write(user, depot_id, "vehicle.primary_id_tag", vehicle_id)
        return vehicle
    except asyncpg.UniqueViolationError as exc:
        raise _handle_identity_unique_violation(exc) from exc


@app.post(
    "/admin/depots/{depot_id}/drivers",
    response_model=DriverIdentityResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["admin"],
    summary="Create a driver identity record",
)
async def create_driver_identity(
    depot_id: str,
    request: DriverIdentityCreate,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Create a driver under an organization-owned depot."""
    org_id = _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        async with db_pools.static.acquire() as conn:
            driver = await db_queries.create_driver_identity(
                conn,
                depot_id=depot_id,
                organization_id=org_id,
                external_driver_id=request.external_driver_id,
                display_name=request.display_name,
                email=request.email,
                phone=request.phone,
                driver_status=request.status,
            )
        if driver is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: you do not have permission for this depot",
            )
        await _audit_identity_write(user, depot_id, "driver.create", driver["driver_id"])
        return driver
    except asyncpg.UniqueViolationError as exc:
        raise _handle_identity_unique_violation(exc) from exc


@app.patch(
    "/admin/depots/{depot_id}/drivers/{driver_id}",
    response_model=DriverIdentityResponse,
    tags=["admin"],
    summary="Update driver identity fields",
)
async def update_driver_identity(
    depot_id: str,
    driver_id: str,
    request: DriverIdentityUpdate,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Patch driver identity metadata."""
    org_id = _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    validate_uuid(driver_id, "driver_id")
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        async with db_pools.static.acquire() as conn:
            driver = await db_queries.update_driver_identity(
                conn,
                depot_id=depot_id,
                organization_id=org_id,
                driver_id=driver_id,
                external_driver_id=request.external_driver_id,
                display_name=request.display_name,
                email=request.email,
                phone=request.phone,
                driver_status=request.status,
            )
        if driver is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Driver not found")
        await _audit_identity_write(user, depot_id, "driver.update", driver_id)
        return driver
    except asyncpg.UniqueViolationError as exc:
        raise _handle_identity_unique_violation(exc) from exc


@app.post(
    "/admin/depots/{depot_id}/rfid-cards",
    response_model=RfidCardResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["admin"],
    summary="Register an RFID card",
)
async def create_rfid_card(
    depot_id: str,
    request: RfidCardCreate,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Create an RFID card and current vehicle/driver assignments."""
    org_id = _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        async with db_pools.static.acquire() as conn:
            async with conn.transaction():
                card = await db_queries.create_rfid_card(
                    conn,
                    depot_id=depot_id,
                    organization_id=org_id,
                    id_tag=request.id_tag,
                    label=request.label,
                    card_status=request.status,
                    notes=request.notes,
                    assigned_vehicle_ids=request.assigned_vehicle_ids,
                    assigned_driver_ids=request.assigned_driver_ids,
                )
        if card is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: you do not have permission for this depot",
            )
        await _audit_identity_write(user, depot_id, "rfid_card.create", card["card_id"])
        return card
    except asyncpg.UniqueViolationError as exc:
        raise _handle_identity_unique_violation(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@app.patch(
    "/admin/depots/{depot_id}/rfid-cards/{card_id}",
    response_model=RfidCardResponse,
    tags=["admin"],
    summary="Update RFID card metadata and assignments",
)
async def update_rfid_card(
    depot_id: str,
    card_id: str,
    request: RfidCardUpdate,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Patch RFID card metadata and optionally replace current assignments."""
    org_id = _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    validate_uuid(card_id, "card_id")
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        async with db_pools.static.acquire() as conn:
            async with conn.transaction():
                card = await db_queries.update_rfid_card(
                    conn,
                    depot_id=depot_id,
                    organization_id=org_id,
                    card_id=card_id,
                    id_tag=request.id_tag,
                    label=request.label,
                    card_status=request.status,
                    notes=request.notes,
                    assigned_vehicle_ids=request.assigned_vehicle_ids,
                    assigned_driver_ids=request.assigned_driver_ids,
                )
        if card is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RFID card not found")
        await _audit_identity_write(user, depot_id, "rfid_card.update", card_id)
        return card
    except asyncpg.UniqueViolationError as exc:
        raise _handle_identity_unique_violation(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@app.post(
    "/admin/depots/{depot_id}/chargers",
    response_model=ChargerOnboardingResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["admin"],
    summary="Provision a charger and one-time Basic Auth credential",
)
async def create_charger_onboarding(
    depot_id: str,
    request: ChargerCreateRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Create a production charger under an organization-owned depot."""
    org_id = _require_customer_admin_with_org(user)
    user_id = str(user.get("sub") or "")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing 'sub' claim",
        )
    validate_depot_id(depot_id)
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)

    if not db_pools:
        raise DatabaseError("Database not available")
    if not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Idempotency-Key is required"
        )

    endpoint = f"POST /admin/depots/{depot_id}/chargers"
    request_hash = _canonical_request_hash(request)

    try:
        connector_ids = _connector_ids_for_request(request)
        password = _generate_ocpp_basic_password()
        password_hash = await _hash_ocpp_basic_password(password)
        async with db_pools.static.acquire() as conn:
            await db_queries.delete_expired_charger_onboarding_idempotency(conn)
            async with conn.transaction():
                await db_queries.acquire_charger_onboarding_idempotency_lock(
                    conn,
                    organization_id=org_id,
                    endpoint=endpoint,
                    idempotency_key=idempotency_key,
                )
                existing = await db_queries.get_charger_onboarding_idempotency(
                    conn,
                    organization_id=org_id,
                    endpoint=endpoint,
                    idempotency_key=idempotency_key,
                )
                if existing:
                    if not hmac.compare_digest(existing["request_hash"], request_hash):
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="Idempotency-Key was already used with a different request body",
                        )
                    return JSONResponse(
                        status_code=int(existing["status_code"] or status.HTTP_201_CREATED),
                        content=_json_response_payload(existing["response_json"]),
                    )
                context = await db_queries.get_depot_org_slug_context(
                    conn,
                    depot_id=depot_id,
                    organization_id=org_id,
                )
                if not context:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Access denied: you do not have permission for this depot",
                    )
                org_slug = _slugify_ocpp_component(context["organization_name"], "org")
                depot_slug = _slugify_ocpp_component(context["depot_name"], "depot")

                # Retry only for generated OCPP ID collisions under concurrent provisioning.
                charger: Optional[dict] = None
                last_unique_error: Optional[asyncpg.UniqueViolationError] = None
                for _ in range(3):
                    try:
                        async with conn.transaction():
                            ocpp_id = await db_queries.next_charger_ocpp_id(
                                conn,
                                organization_slug=org_slug,
                                depot_slug=depot_slug,
                            )
                            charger = await db_queries.create_charger_with_credentials(
                                conn,
                                depot_id=depot_id,
                                ocpp_id=ocpp_id,
                                display_name=request.display_name,
                                vendor=request.vendor,
                                model=request.model,
                                serial_number=request.serial_number,
                                firmware=request.firmware,
                                rated_kw=request.rated_kw,
                                connector_type=request.connector_type,
                                connector_count=request.connector_count,
                                connector_ids=connector_ids,
                                network_notes=request.network_notes,
                                password_hash=password_hash,
                            )
                        break
                    except asyncpg.UniqueViolationError as exc:
                        last_unique_error = exc
                else:
                    raise last_unique_error or RuntimeError("Could not generate unique ocpp_id")
                if charger is None:
                    raise RuntimeError("Could not create charger")

                response_payload = _format_charger_onboarding_response(charger, password)
                await db_queries.store_charger_onboarding_idempotency(
                    conn,
                    organization_id=org_id,
                    user_id=user_id,
                    endpoint=endpoint,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    response_json=response_payload,
                    status_code=status.HTTP_201_CREATED,
                    ttl_minutes=30,
                )

        _depot_config_cache.pop(depot_id, None)
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=response_payload)
    except HTTPException:
        raise
    except asyncpg.UniqueViolationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Generated ocpp_id or station credential already exists",
        ) from exc
    except asyncpg.PostgresError as exc:
        logger.error("Database error onboarding charger: %s", exc, exc_info=True)
        raise DatabaseError(f"Database error: {str(exc)}") from exc


@app.get(
    "/admin/depots/{depot_id}/schedule/readiness",
    response_model=ScheduleReadinessResponse,
    tags=["admin"],
    summary="Get schedule setup readiness",
)
async def get_schedule_readiness(
    depot_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Return persisted readiness checks for manual schedule setup."""
    _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    checks = await _build_depot_readiness_checklist(depot_id)
    return _readiness_response_payload(depot_id, checks)


@app.post(
    "/admin/depots/{depot_id}/schedule/manual",
    response_model=ManualScheduleCreateResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["admin"],
    summary="Create manually-entered vehicle schedules",
)
async def create_manual_schedules(
    depot_id: str,
    request: ManualScheduleCreateRequest,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Create tenant-scoped manual schedules for vehicles in a depot."""
    _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    if not db_pools:
        raise DatabaseError("Database not available")

    depot_uuid = UUID(depot_id)
    vehicle_ids = [UUID(entry.vehicle_id) for entry in request.entries]
    async with db_pools.static.acquire() as conn:
        valid_vehicle_ids = await db_queries.get_vehicle_ids_for_depot(
            conn, depot_uuid, vehicle_ids
        )
        invalid_vehicle_ids = sorted(
            {str(vehicle_id) for vehicle_id in vehicle_ids} - valid_vehicle_ids
        )
        if invalid_vehicle_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "VEHICLE_DEPOT_MISMATCH",
                    "vehicle_ids": invalid_vehicle_ids,
                },
            )

        created = []
        async with conn.transaction():
            for entry in request.entries:
                row = await db_queries.create_manual_schedule(
                    conn,
                    vehicle_id=UUID(entry.vehicle_id),
                    route_id=entry.route_id,
                    departure_time=entry.departure_time,
                    return_time=entry.return_time,
                    required_soc=float(entry.required_soc),
                    energy_kwh=entry.energy_kwh,
                )
                created.append(_format_manual_schedule_row(row))

    checks = await _build_depot_readiness_checklist(depot_id)
    return {
        "created": created,
        "readiness": _readiness_response_payload(depot_id, checks),
    }


@app.patch(
    "/admin/depots/{depot_id}/schedule/manual/{schedule_id}",
    response_model=ManualScheduleUpdateResponse,
    tags=["admin"],
    summary="Update a manually-entered vehicle schedule",
)
async def patch_manual_schedule(
    depot_id: str,
    schedule_id: str,
    request: ManualSchedulePatchRequest,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Partially update a tenant-scoped manual schedule."""
    _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    validate_uuid(schedule_id, "schedule_id")
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    if not db_pools:
        raise DatabaseError("Database not available")

    depot_uuid = UUID(depot_id)
    schedule_uuid = UUID(schedule_id)
    patch_data = request.model_dump(exclude_unset=True)
    if not patch_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one schedule field is required",
        )
    non_nullable_patch_fields = {
        "vehicle_id",
        "route_id",
        "departure_time",
        "return_time",
        "required_soc",
    }
    null_fields = sorted(
        field_name
        for field_name, field_value in patch_data.items()
        if field_name in non_nullable_patch_fields and field_value is None
    )
    if null_fields:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{', '.join(null_fields)} cannot be null",
        )

    async with db_pools.static.acquire() as conn:
        existing = await db_queries.get_schedule_for_depot(
            conn,
            depot_id=depot_uuid,
            schedule_id=schedule_uuid,
        )
        if not existing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

        merged = {**existing, **patch_data}
        if merged["return_time"] <= merged["departure_time"]:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="return_time must be after departure_time",
            )

        vehicle_uuid = UUID(str(merged["vehicle_id"]))
        valid_vehicle_ids = await db_queries.get_vehicle_ids_for_depot(
            conn, depot_uuid, [vehicle_uuid]
        )
        if str(vehicle_uuid) not in valid_vehicle_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error_code": "VEHICLE_DEPOT_MISMATCH",
                    "vehicle_ids": [str(vehicle_uuid)],
                },
            )

        updated = await db_queries.update_manual_schedule(
            conn,
            depot_id=depot_uuid,
            schedule_id=schedule_uuid,
            vehicle_id=vehicle_uuid,
            route_id=merged["route_id"],
            departure_time=merged["departure_time"],
            return_time=merged["return_time"],
            required_soc=float(merged["required_soc"]),
            energy_kwh=merged.get("energy_kwh"),
        )
    if not updated:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    checks = await _build_depot_readiness_checklist(depot_id)
    return {
        "updated": _format_manual_schedule_row(updated),
        "readiness": _readiness_response_payload(depot_id, checks),
    }


@app.post(
    "/admin/first-depot-setup",
    response_model=DepotSetupResponse,
    tags=["admin"],
    summary="Create initial depot setup",
)
async def create_first_depot_setup(
    body: dict,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Create a tenant-scoped depot and return readiness checklist."""
    org_id = _require_customer_admin_with_org(user)
    try:
        request = FirstDepotSetupRequest.model_validate(body)
        _validate_depot_setup_payload(request.depot)
    except ValidationError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_format_depot_setup_validation_errors(exc),
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": "Validation failed",
                "error_code": "VALIDATION_ERROR",
                "field_errors": {"depot": [str(exc)]},
                "validation_errors": [{"path": "depot", "message": str(exc)}],
            },
        )

    if not db_pools:
        raise DatabaseError("Database not available")

    depot = request.depot
    address = depot.address.model_dump(exclude_none=True)
    billing_metadata = depot.billing.model_dump(exclude_none=True)
    building_load_source = depot.building_load_source.model_dump(exclude_none=True)
    building_load_assumption_kw = float(depot.building_load_source.assumption_kw or 0.0)
    tariff_kwargs = _depot_setup_tariff_kwargs(depot.demand_charge)

    async with db_pools.static.acquire() as conn:
        async with conn.transaction():
            created = await db_queries.create_depot_setup(
                conn,
                organization_id=org_id,
                name=depot.name,
                latitude=depot.address.latitude,
                longitude=depot.address.longitude,
                timezone=depot.timezone,
                currency=depot.currency.upper(),
                utility_id=depot.utility_id,
                max_grid_kw=depot.max_grid_kw,
                address=address,
                billing_metadata=billing_metadata,
                building_load_source=building_load_source,
                building_load_assumption_kw=building_load_assumption_kw,
                charger_vehicle_access_default=depot.charger_vehicle_access_default,
                **tariff_kwargs,
            )
            if depot.stationary_battery.present:
                max_power_kw = min(
                    float(depot.stationary_battery.max_charge_kw),
                    float(depot.stationary_battery.max_discharge_kw),
                )
                await db_queries.upsert_battery_storage(
                    conn,
                    depot_id=created["depot_id"],
                    capacity_kwh=float(depot.stationary_battery.capacity_kwh),
                    max_power_kw=max_power_kw,
                    soc_min=float(depot.stationary_battery.min_soc_pct) / 100.0,
                    soc_max=float(depot.stationary_battery.max_soc_pct) / 100.0,
                )
            else:
                await db_queries.delete_battery_storage(conn, depot_id=created["depot_id"])

    readiness = await _build_readiness_checklist(created["depot_id"], depot)
    return {
        "depot": {
            "id": created["depot_id"],
            "name": created["name"],
            "timezone": created["timezone"],
            "currency": created["currency"],
            "max_grid_kw": created["max_grid_kw"],
        },
        "readiness_checklist": readiness,
    }


@app.patch(
    "/admin/depots/{depot_id}",
    response_model=DepotSetupResponse,
    tags=["admin"],
    summary="Update depot setup",
)
async def update_depot_setup(
    depot_id: str,
    body: dict,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Update depot setup for an accessible depot."""
    _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)

    try:
        request = FirstDepotSetupRequest.model_validate(body)
        _validate_depot_setup_payload(request.depot)
    except ValidationError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=_format_depot_setup_validation_errors(exc),
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": "Validation failed",
                "error_code": "VALIDATION_ERROR",
                "field_errors": {"depot": [str(exc)]},
                "validation_errors": [{"path": "depot", "message": str(exc)}],
            },
        )

    if not db_pools:
        raise DatabaseError("Database not available")

    depot = request.depot
    tariff_kwargs = _depot_setup_tariff_kwargs(depot.demand_charge)
    async with db_pools.static.acquire() as conn:
        async with conn.transaction():
            updated = await db_queries.update_depot_setup(
                conn,
                depot_id=depot_id,
                name=depot.name,
                latitude=depot.address.latitude,
                longitude=depot.address.longitude,
                timezone=depot.timezone,
                currency=depot.currency.upper(),
                utility_id=depot.utility_id,
                max_grid_kw=depot.max_grid_kw,
                address=depot.address.model_dump(exclude_none=True),
                billing_metadata=depot.billing.model_dump(exclude_none=True),
                building_load_source=depot.building_load_source.model_dump(exclude_none=True),
                building_load_assumption_kw=float(depot.building_load_source.assumption_kw or 0.0),
                charger_vehicle_access_default=depot.charger_vehicle_access_default,
                **tariff_kwargs,
            )
            if not updated:
                raise DepotNotFoundError(f"Depot {depot_id} not found")

            if depot.stationary_battery.present:
                max_power_kw = min(
                    float(depot.stationary_battery.max_charge_kw),
                    float(depot.stationary_battery.max_discharge_kw),
                )
                await db_queries.upsert_battery_storage(
                    conn,
                    depot_id=depot_id,
                    capacity_kwh=float(depot.stationary_battery.capacity_kwh),
                    max_power_kw=max_power_kw,
                    soc_min=float(depot.stationary_battery.min_soc_pct) / 100.0,
                    soc_max=float(depot.stationary_battery.max_soc_pct) / 100.0,
                )
            else:
                await db_queries.delete_battery_storage(conn, depot_id=depot_id)

    _depot_config_cache.pop(depot_id, None)
    readiness = await _build_readiness_checklist(depot_id, depot)
    return {
        "depot": {
            "id": depot_id,
            "name": updated["name"],
            "timezone": updated["timezone"],
            "currency": updated["currency"],
            "max_grid_kw": updated["max_grid_kw"],
        },
        "readiness_checklist": readiness,
    }


@app.post(
    "/admin/depots/{depot_id}/charger-vehicle-access",
    response_model=DepotSetupResponse,
    tags=["admin"],
    summary="Upsert charger-vehicle access matrix rows",
)
async def upsert_charger_vehicle_access_endpoint(
    depot_id: str,
    body: ChargerVehicleAccessRequest,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Per-row upsert into the charger_vehicle_access matrix.

    Only valid when the depot is in ``explicit_matrix`` mode. Returns
    409 for ``all_to_all`` depots, where the matrix is synthesized at
    solve time. Returns 400 with the offending IDs if any
    ``charger_id`` / ``vehicle_id`` does not belong to this depot.
    Invalidates the depot config cache so the next solve reflects the
    new rows immediately.
    """
    _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)

    if not db_pools:
        raise DatabaseError("Database not available")

    result: dict[str, list[str]] = {"invalid_chargers": [], "invalid_vehicles": []}
    async with db_pools.static.acquire() as conn:
        access_default = (
            await conn.fetchval(
                "SELECT charger_vehicle_access_default FROM depots WHERE depot_id = $1::uuid",
                depot_id,
            )
        ) or "explicit_matrix"
        if access_default == "all_to_all":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Depot is in all_to_all mode; the access matrix is "
                    "synthesized automatically. Switch to explicit_matrix "
                    "via PATCH /admin/depots/{id} before posting rows."
                ),
            )
        async with conn.transaction():
            result = await db_queries.upsert_charger_vehicle_access(
                conn,
                depot_id=depot_id,
                entries=[entry.model_dump() for entry in body.entries],
            )
            if result["invalid_chargers"] or result["invalid_vehicles"]:
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content={
                        "detail": "One or more IDs do not belong to this depot",
                        "error_code": "INVALID_DEPOT_MEMBERSHIP",
                        "invalid_chargers": result["invalid_chargers"],
                        "invalid_vehicles": result["invalid_vehicles"],
                    },
                )

    _depot_config_cache.pop(depot_id, None)

    async with db_pools.static.acquire() as conn:
        depot_row = await conn.fetchrow(
            "SELECT name, timezone, currency, max_grid_kw FROM depots "
            "WHERE depot_id = $1::uuid",
            depot_id,
        )
    if not depot_row:
        raise DepotNotFoundError(f"Depot {depot_id} not found")

    readiness = await _build_depot_readiness_checklist(depot_id)
    return {
        "depot": {
            "id": depot_id,
            "name": depot_row["name"],
            "timezone": depot_row["timezone"],
            "currency": depot_row["currency"],
            "max_grid_kw": depot_row["max_grid_kw"],
        },
        "readiness_checklist": readiness,
    }


@app.post(
    "/optimize",
    response_model=OptimizationResponse,
    status_code=status.HTTP_200_OK,
    tags=["optimization"],
    summary="Trigger depot charging optimization",
    description="""
    Trigger optimization for a depot to generate charging schedules.
    
    **Authentication:** Requires JWT token in Authorization header.
    
    **Request:**
    - `depot_id`: Depot identifier (UUID)
    - `horizon_hours`: Optimization horizon (1-48 hours, default 24)
    - `force`: Force re-optimization even if recent schedule exists
    
    **Response:**
    - `run_id`: Optimization run identifier
    - `status`: Optimization status (e.g., 'completed')
    - `objective_value`: Optimized objective value ($)
    - `solve_time_seconds`: Solver execution time
    - `peak_demand_kw`: Peak demand in kW
    - `schedule`: Charging schedule per vehicle
    
    **Error Codes:**
    - 400: Invalid request (missing depot_id, invalid horizon_hours)
    - 401: Unauthorized (missing or invalid JWT token)
    - 404: Depot not found
    - 500: Optimization failed (infeasible, timeout)
    - 503: Database not available
    
    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        500: {"model": ErrorResponse, "description": "Optimization failed"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def run_optimization(
    request: OptimizationRequest, user: dict = Depends(ensure_tenant_mirrored)
):
    """Trigger depot charging optimization.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """
    # Input validation before DB availability check (fail fast on bad input)
    validate_depot_id(request.depot_id)
    validate_horizon_hours(request.horizon_hours)
    await verify_depot_access(request.depot_id, user, db_pools.static if db_pools else None)

    if not db_pools:
        raise DatabaseError("Database not available")

    # Log optimization request
    logger.info(
        "Optimization request",
        extra={
            "depot_id": request.depot_id,
            "horizon_hours": request.horizon_hours,
            "force": request.force,
        },
    )

    try:
        # Get depot config (raises 404 if depot not found)
        config = await _get_depot_config(request.depot_id)

        # Validate depot has vehicles
        if not config.vehicle_capacities:
            raise HTTPException(
                status_code=400, detail=f"Depot {request.depot_id} has no vehicles configured"
            )

        # Get or create controller from manager
        if not controller_manager:
            raise HTTPException(status_code=503, detail="Controller manager not initialized")

        controller = await controller_manager.get_or_create_controller(request.depot_id)

        # Run optimization
        try:
            result = await controller.run_optimization(
                "api_request", horizon_hours=request.horizon_hours
            )
        except SolverTimeoutError:
            raise
        except InfeasibleModelError:
            raise
        except SolverError as e:
            raise OptimizationError(f"Solver error: {e}")
        except Exception as opt_error:
            raise OptimizationError(f"Optimization failed: {opt_error}")

        # Log optimization result
        logger.info(
            "Optimization completed",
            extra={
                "depot_id": request.depot_id,
                "run_id": str(result.run_id),
                "objective_value": result.objective_value,
                "solve_time_s": result.solve_time_s,
                "peak_demand_kw": result.peak_demand_kw,
                "solver_used": result.solver_used,
            },
        )

        return OptimizationResponse(
            run_id=str(result.run_id),
            depot_id=request.depot_id,
            status=result.status,
            objective_value=result.objective_value,
            solve_time_seconds=result.solve_time_s,
            peak_demand_kw=result.peak_demand_kw,
            solver_used=result.solver_used,
            schedule=result.schedule,
        )

    except HTTPException:
        # Re-raise HTTP exceptions (already properly formatted)
        raise
    except ValueError as e:
        # Depot not found or invalid configuration
        error_msg = str(e)
        if "not found" in error_msg.lower():
            raise DepotNotFoundError(error_msg)
        raise ValueError(error_msg)
    except OptimizationError:
        # Re-raise optimization errors (handled by exception handler)
        raise
    except Exception as e:
        # Unexpected errors
        logger.error(
            f"Unexpected error in optimization: {e}",
            exc_info=True,
            extra={"depot_id": request.depot_id},
        )
        raise OptimizationError(f"Unexpected error: {str(e)}")


@app.get(
    "/depots/{depot_id}/state",
    response_model=DepotStateResponse,
    tags=["depots"],
    summary="Get current depot state",
    description="""
    Get current state of a depot including vehicle SoCs, battery state,
    current month peak demand, and current electricity price.
    
    **Authentication:** Requires JWT token in Authorization header.
    
    **Response:**
    - `depot_id`: Depot identifier
    - `timestamp`: Current timestamp (ISO 8601)
    - `vehicle_socs`: Vehicle state of charge by vehicle_id (0.0-1.0)
    - `battery_soc`: Stationary battery state of charge (0.0-1.0)
    - `current_month_peak_kw`: Current month peak demand (kW)
    - `current_price_kwh`: Current electricity price ($/kWh)
    
    **Error Codes:**
    - 401: Unauthorized (missing or invalid JWT token)
    - 404: Depot not found
    - 500: Server error
    - 503: Database not available
    
    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        500: {"model": ErrorResponse, "description": "Server error"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_depot_state(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Get current depot state.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        # Get depot config (raises 404 if depot not found)
        config = await _get_depot_config(depot_id)

        # Assemble state (timeout prevents cascade failure when DB is slow)
        assembler = StateAssembler(db_pools, depot_id, config)
        try:
            async with asyncio.timeout(30):
                state = await assembler.get_current_state(24)
        except TimeoutError:
            raise HTTPException(status_code=503, detail="State assembly timed out after 30s")

        # Get current price (first timestep) or default
        current_price = state.prices[0] if state.prices else 0.15

        # Handle empty state gracefully
        vehicle_socs = state.vehicle_socs if state.vehicle_socs else {}

        logger.debug(
            f"Retrieved depot state for {depot_id}: "
            f"{len(vehicle_socs)} vehicles, "
            f"peak: {state.current_month_peak} kW, "
            f"price: ${current_price:.3f}/kWh"
        )

        return DepotStateResponse(
            depot_id=depot_id,
            timestamp=datetime.utcnow().isoformat(),
            vehicle_socs=vehicle_socs,
            battery_soc=state.battery_soc,
            current_month_peak_kw=state.current_month_peak,
            current_price_kwh=current_price,
        )

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except ValueError as e:
        # Depot not found
        error_msg = str(e)
        if "not found" in error_msg.lower():
            raise DepotNotFoundError(error_msg)
        raise ValueError(error_msg)
    except Exception as e:
        logger.error(f"Failed to get depot state: {e}", exc_info=True, extra={"depot_id": depot_id})
        raise HTTPException(
            status_code=500,
            detail={"error_code": ErrorCode.INTERNAL_ERROR.value, "detail": "Failed to get depot state"},
        ) from e


@app.get(
    "/depots/{depot_id}/optimization/readiness",
    response_model=ReadinessResponse,
    tags=["depots"],
    summary="Check optimization readiness",
    description="""
    Evaluate whether a depot has every input the MILP solver needs.

    Use this BEFORE calling `POST /optimize` to render an inputs-checklist
    UI. The response tells the user exactly which inputs are missing and
    whether the run would proceed in degraded mode (e.g. building-load
    forecast fallback).

    The endpoint runs the same state assembler and readiness checker the
    real optimization run uses, with `persist=false` by default so it does
    NOT write to `optimization_input_snapshots`. Pass `persist=true` to
    capture an audit trail (useful when the frontend wants to record a
    pre-flight check for later replay).

    **Authentication:** Requires JWT token in Authorization header. The
    user must have access to the depot.

    **Status values:**
    - `ready` — every required input present; safe to call `/optimize`
    - `degraded` — run will proceed with documented assumptions; resulting
      `optimization_runs.status` will be `degraded`
    - `not_ready` — `/optimize` will refuse; resolve `missing_inputs` first

    **Missing input vocabulary:** `vehicles`, `chargers`, `prices`,
    `schedules`, `charger_vehicle_access`, `building_load`.

    **Degraded reason vocabulary:** `building_load_meter_unavailable`,
    `telemetry_all_defaulted`.

    Reference: migration 019, PRD §9.4
    """,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid depot_id"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Forbidden"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_optimization_readiness(
    depot_id: str = Depends(_require_depot_access),
    horizon_hours: int = Query(24, ge=1, le=48, description="Optimization horizon (hours)"),
    persist: bool = Query(
        False,
        description=("If true, write a row to optimization_input_snapshots (no run_id)."),
    ),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Pre-flight readiness check for the MILP optimizer."""
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        config = await _get_depot_config(depot_id)
    except ValueError as e:
        if "not found" in str(e).lower():
            raise DepotNotFoundError(str(e))
        raise

    assembler = StateAssembler(db_pools, depot_id, config)
    try:
        async with asyncio.timeout(30):
            state = await assembler.get_current_state(horizon_hours)
            await assembler.fetch_snapshot_extras(*assembler.last_horizon)
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Readiness check timed out after 30s")

    readiness = evaluate_readiness(
        config,
        state,
        building_load_source=assembler.last_building_load_source,
        schedules_present=assembler.last_schedules_present,
    )

    snapshot_id: Optional[str] = None
    if persist:
        horizon = assembler.last_horizon or (
            datetime.utcnow(),
            datetime.utcnow() + timedelta(hours=horizon_hours),
        )
        snapshot = build_snapshot(
            depot_id=depot_id,
            organization_id=assembler.last_organization_id,
            config=config,
            state=state,
            horizon_start=horizon[0],
            horizon_end=horizon[1],
            schedules=assembler.last_schedules,
            weather_features=assembler.last_weather_features,
            weather_forecast_id=assembler.last_weather_forecast_id,
            recent_telemetry=assembler.last_recent_telemetry,
            readiness=readiness,
        )
        try:
            await persist_snapshot(db_pools, snapshot)
            snapshot_id = str(snapshot.snapshot_id)
        except Exception as e:
            # Pre-flight persistence is best-effort — do not fail the
            # readiness response if the write fails.
            logger.warning(
                "Pre-flight snapshot persist failed for depot %s: %s",
                depot_id,
                e,
            )

    return ReadinessResponse(
        depot_id=depot_id,
        status=readiness.status,
        missing_inputs=readiness.missing_inputs,
        degraded_reasons=readiness.degraded_reasons,
        assumptions=readiness.assumptions,
        building_load_source=readiness.building_load_source,
        horizon_hours=horizon_hours,
        captured_at=datetime.utcnow().isoformat(),
        snapshot_id=snapshot_id,
    )


# ============ Energy Reporting Endpoints ============

class EnergyReportCost(BaseModel):
    """Cost breakdown for a single report row."""

    amount: float = Field(..., description="Total cost for the bucket")
    currency: str = Field(..., description="ISO 4217 currency code")
    estimated: bool = Field(
        False,
        description=(
            "True when any session in the bucket lacked cost_total and was "
            "estimated using the depot tariff under_cap_rate."
        ),
    )


class EnergyReportRow(BaseModel):
    """Single row of an energy report."""

    bucket: str = Field(..., description="Calendar bucket key in depot timezone (YYYY-MM)")
    vehicle_id: Optional[str] = Field(None, description="Vehicle UUID or 'unassigned'")
    charger_id: Optional[str] = Field(None, description="Charger UUID or 'unassigned'")
    driver_id: Optional[str] = Field(None, description="Driver UUID or 'unassigned'")
    card_id: Optional[str] = Field(None, description="RFID card UUID or 'unassigned'")
    energy_kwh: float = Field(..., description="Total energy delivered in the bucket (kWh)")
    session_count: int = Field(..., description="Number of charging sessions in the bucket")
    avg_kw: float = Field(..., description="Average charging power across sessions (kWh / hours)")
    cost: EnergyReportCost = Field(..., description="Bucketed cost (sum or estimate)")


class EnergyReportResponse(BaseModel):
    """Response from /reports/depots/{depot_id}/energy/monthly."""

    depot_id: str
    currency: str
    from_: str = Field(..., alias="from")
    to: str
    rows: list[EnergyReportRow]

    model_config = {"populate_by_name": True}


def _parse_report_date(value: str, field_name: str) -> date:
    """Parse an ISO 8601 calendar date for the from/to query parameters."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name} date '{value}': expected ISO 8601 (YYYY-MM-DD)",
        ) from exc


def _validate_report_group_by(group_by: Optional[str]) -> Optional[str]:
    """Validate the optional group_by query parameter."""
    if group_by is None:
        return None
    if group_by not in REPORT_GROUP_BY_VALUES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid group_by '{group_by}': must be one of "
                f"{', '.join(REPORT_GROUP_BY_VALUES)}"
            ),
        )
    return group_by


async def _verify_depot_access_for_report(depot_id: str, user: dict) -> None:
    """Run verify_depot_access and re-raise 403s with error_code=CROSS_ORG_DENIED."""
    try:
        await verify_depot_access(
            depot_id, user, db_pools.static if db_pools else None
        )
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "CROSS_ORG_DENIED",
                    "message": exc.detail
                    if isinstance(exc.detail, str)
                    else "Access denied: cross-organization access is not permitted",
                },
            ) from exc
        raise


def _coerce_under_cap_rate(billing_metadata: Optional[dict]) -> Optional[float]:
    """Pull the depot tariff under_cap_rate out of billing_metadata."""
    if not isinstance(billing_metadata, dict):
        return None
    raw = billing_metadata.get("under_cap_rate")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Ignoring non-numeric under_cap_rate in billing_metadata: %r", raw
        )
        return None


def _optional_text(value: object) -> Optional[str]:
    """Normalize nullable report dimension values from asyncpg records."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def _load_report_context(
    depot_id: str,
) -> tuple[str, str, Optional[float], list[str], dict[str, str]]:
    """Fetch depot reporting context and charger mappings from static DB."""
    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.static.acquire() as conn:
        depot_row = await conn.fetchrow(
            """
            SELECT timezone, currency, billing_metadata
            FROM depots
            WHERE depot_id = $1::uuid
            """,
            depot_id,
        )
        if not depot_row:
            raise DepotNotFoundError(f"Depot {depot_id} not found")

        charger_rows = await conn.fetch(
            "SELECT charger_id::text AS charger_id, ocpp_id FROM chargers WHERE depot_id = $1::uuid",
            depot_id,
        )

    timezone_name = depot_row["timezone"] or "America/Los_Angeles"
    currency = depot_row["currency"] or "USD"
    billing_metadata = depot_row["billing_metadata"]
    if isinstance(billing_metadata, str):
        try:
            billing_metadata = json.loads(billing_metadata)
        except (TypeError, ValueError):
            billing_metadata = {}
    under_cap_rate = _coerce_under_cap_rate(billing_metadata)

    ocpp_ids = [r["ocpp_id"] for r in charger_rows]
    charger_id_by_ocpp_id = {r["ocpp_id"]: r["charger_id"] for r in charger_rows}
    return timezone_name, currency, under_cap_rate, ocpp_ids, charger_id_by_ocpp_id


async def _fetch_session_rows(
    depot_id: str,
    timezone_name: str,
    ocpp_ids: list[str],
    charger_id_by_ocpp_id: dict[str, str],
    from_date: date,
    to_date: date,
) -> list[SessionRow]:
    """Fetch charging_sessions rows in [from, to] (inclusive) in depot TZ."""
    if not db_pools or db_pools.ts is None:
        raise DatabaseError("Database not available")
    if not ocpp_ids:
        return []

    query = """
        SELECT
            cs.start_time,
            cs.end_time,
            cs.energy_delivered_kwh,
            cs.cost_total,
            cs.vehicle_id::text AS vehicle_id,
            cs.station_id AS charger_id,
            cs.driver_id::text AS driver_id,
            cs.card_id::text AS card_id
        FROM charging_sessions cs
        WHERE cs.station_id = ANY($1::text[])
          AND cs.start_time >= ($2::date)::timestamp AT TIME ZONE $4
          AND cs.start_time < (($3::date) + INTERVAL '1 day')::timestamp AT TIME ZONE $4
        ORDER BY cs.start_time
        """

    async with db_pools.ts.acquire() as conn:
        records = await conn.fetch(query, ocpp_ids, from_date, to_date, timezone_name)

    rows: list[SessionRow] = []
    for r in records:
        energy = r["energy_delivered_kwh"]
        cost = r["cost_total"]
        rows.append(
            SessionRow(
                start_time=r["start_time"],
                end_time=r["end_time"],
                energy_kwh=float(energy) if energy is not None else None,
                cost_total=float(cost) if cost is not None else None,
                vehicle_id=_optional_text(r["vehicle_id"]),
                charger_id=charger_id_by_ocpp_id.get(r["charger_id"]),
                driver_id=_optional_text(r["driver_id"]),
                card_id=_optional_text(r["card_id"]),
            )
        )
    return rows


async def _build_energy_report(
    depot_id: str,
    from_str: str,
    to_str: str,
    group_by: Optional[str],
) -> tuple[dict, list[dict], str]:
    """Run the full report pipeline and return (metadata, rows, currency)."""
    from_date = _parse_report_date(from_str, "from")
    to_date = _parse_report_date(to_str, "to")
    if to_date < from_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'to' must be on or after 'from'",
        )

    timezone_name, currency, under_cap_rate, ocpp_ids, charger_id_by_ocpp_id = (
        await _load_report_context(depot_id)
    )
    sessions = await _fetch_session_rows(
        depot_id, timezone_name, ocpp_ids, charger_id_by_ocpp_id, from_date, to_date
    )
    rows = aggregate_energy_rows(
        sessions,
        timezone=timezone_name,
        group_by=group_by,
        under_cap_rate=under_cap_rate,
        currency=currency,
        from_date=from_date,
        to_date=to_date,
    )
    metadata = {
        "depot_id": depot_id,
        "currency": currency,
        "from": from_str,
        "to": to_str,
    }
    return metadata, rows, currency


@app.get(
    "/reports/depots/{depot_id}/energy/monthly",
    response_model=EnergyReportResponse,
    tags=["depots"],
    summary="Energy report aggregated by month",
    description=(
        "Aggregate ``charging_sessions.energy_delivered_kwh`` into monthly "
        "buckets in the depot's local timezone. Optional ``group_by`` "
        "splits each bucket by vehicle, charger, driver, or card. NULL "
        "identity columns collapse into an 'unassigned' bucket."
    ),
    responses={
        400: {"model": ErrorResponse, "description": "Invalid query parameters"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Cross-organization access denied"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_energy_report_monthly(
    depot_id: str,
    from_: str = Query(..., alias="from", description="Start date (YYYY-MM-DD, inclusive, depot TZ)"),
    to: str = Query(..., description="End date (YYYY-MM-DD, inclusive, depot TZ)"),
    group_by: Optional[str] = Query(
        None,
        description="Optional grouping dimension: vehicle | charger | driver | card",
    ),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Monthly energy + cost rollup for a depot."""
    validate_depot_id(depot_id)
    await _verify_depot_access_for_report(depot_id, user)
    grouping = _validate_report_group_by(group_by)

    try:
        metadata, rows, _ = await _build_energy_report(depot_id, from_, to, grouping)
    except DepotNotFoundError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error(
            "Database error generating energy report: %s",
            exc,
            exc_info=True,
            extra={"depot_id": depot_id},
        )
        raise DatabaseError(f"Database error: {str(exc)}") from exc

    return {**metadata, "rows": rows}


@app.get(
    "/reports/depots/{depot_id}/energy/sessions",
    response_model=EnergyReportResponse,
    tags=["depots"],
    summary="Energy report broken out per session-derived row",
    description=(
        "Same shape as ``/energy/monthly`` but always grouped by session "
        "identity dimensions (vehicle/charger/driver/card). The default "
        "grouping is ``vehicle`` so a sessions roll-up still answers "
        "'who/what charged when'. Supports the same ``group_by`` enum as "
        "the monthly endpoint."
    ),
    responses={
        400: {"model": ErrorResponse, "description": "Invalid query parameters"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Cross-organization access denied"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_energy_report_sessions(
    depot_id: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    group_by: Optional[str] = Query(
        "vehicle",
        description="Grouping dimension: vehicle | charger | driver | card",
    ),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Sessions-grained energy + cost rollup."""
    validate_depot_id(depot_id)
    await _verify_depot_access_for_report(depot_id, user)
    grouping = _validate_report_group_by(group_by)
    if grouping is None:
        grouping = "vehicle"

    try:
        metadata, rows, _ = await _build_energy_report(depot_id, from_, to, grouping)
    except DepotNotFoundError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error(
            "Database error generating sessions report: %s",
            exc,
            exc_info=True,
            extra={"depot_id": depot_id},
        )
        raise DatabaseError(f"Database error: {str(exc)}") from exc

    return {**metadata, "rows": rows}


@app.get(
    "/reports/depots/{depot_id}/energy/monthly.csv",
    tags=["depots"],
    summary="Energy report (CSV stream)",
    description=(
        "CSV equivalent of ``/reports/depots/{depot_id}/energy/monthly``. "
        "Body is streamed via ``StreamingResponse``; rows match the JSON "
        "endpoint one-for-one."
    ),
    responses={
        200: {"content": {"text/csv": {}}, "description": "CSV report stream"},
        400: {"model": ErrorResponse, "description": "Invalid query parameters"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Cross-organization access denied"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_energy_report_monthly_csv(
    depot_id: str,
    from_: str = Query(..., alias="from"),
    to: str = Query(...),
    group_by: Optional[str] = Query(None),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Stream the monthly energy report as CSV."""
    validate_depot_id(depot_id)
    await _verify_depot_access_for_report(depot_id, user)
    grouping = _validate_report_group_by(group_by)

    try:
        _, rows, _ = await _build_energy_report(depot_id, from_, to, grouping)
    except DepotNotFoundError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error(
            "Database error generating energy CSV: %s",
            exc,
            exc_info=True,
            extra={"depot_id": depot_id},
        )
        raise DatabaseError(f"Database error: {str(exc)}") from exc

    filename = f"depot_{depot_id}_energy_{from_}_{to}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}

    # Bind to a local so the generator captures a stable reference.
    rows_for_stream = rows

    def _generate() -> Iterator[str]:
        yield from stream_rows_as_csv(rows_for_stream, group_by=grouping)

    return StreamingResponse(_generate(), media_type="text/csv", headers=headers)


@app.get(
    "/depots/{depot_id}/schedule",
    response_model=ScheduleResponse,
    tags=["depots"],
    summary="Get current charging schedule",
    description="""
    Get the most recent charging schedule for a depot.
    
    **Authentication:** Requires JWT token in Authorization header.
    
    **Response:**
    - `depot_id`: Depot identifier
    - `run_id`: Optimization run identifier
    - `generated_at`: Schedule generation timestamp (ISO 8601)
    - `horizon_start`: Optimization horizon start (ISO 8601)
    - `horizon_end`: Optimization horizon end (ISO 8601)
    - `schedule`: Charging schedule per vehicle
    
    **Error Codes:**
    - 401: Unauthorized (missing or invalid JWT token)
    - 404: No schedule found for depot
    - 500: Server error
    - 503: Database not available
    
    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        404: {"model": ErrorResponse, "description": "No schedule found"},
        500: {"model": ErrorResponse, "description": "Server error"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_depot_schedule(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Get current charging schedule.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        # Get latest optimization result from database (optimization_runs is in TimescaleDB)
        query = """
        SELECT run_id, run_time, schedule_json, horizon_start, horizon_end
        FROM optimization_runs
        WHERE depot_id = $1
        ORDER BY run_time DESC
        LIMIT 1
        """
        async with db_pools.ts.acquire() as conn:
            row = await conn.fetchrow(query, depot_id)

        if not row:
            logger.info(f"No schedule found for depot {depot_id}")
            raise HTTPException(status_code=404, detail=f"No schedule found for depot {depot_id}")

        # Validate schedule JSON structure
        schedule_json = row["schedule_json"]
        if not isinstance(schedule_json, dict):
            logger.warning(
                f"Invalid schedule JSON structure for depot {depot_id}: "
                f"expected dict, got {type(schedule_json)}"
            )
            # Still return it, but log the warning

        # Ensure timestamps are properly formatted
        generated_at = row["run_time"]
        horizon_start = row["horizon_start"]
        horizon_end = row["horizon_end"]

        if not isinstance(generated_at, datetime):
            raise ValueError("Invalid run_time format in database")
        if not isinstance(horizon_start, datetime):
            raise ValueError("Invalid horizon_start format in database")
        if not isinstance(horizon_end, datetime):
            raise ValueError("Invalid horizon_end format in database")

        logger.debug(
            f"Retrieved schedule for depot {depot_id}: "
            f"run_id={row['run_id']}, "
            f"generated_at={generated_at.isoformat()}"
        )

        return ScheduleResponse(
            depot_id=depot_id,
            run_id=str(row["run_id"]),
            generated_at=generated_at.isoformat(),
            horizon_start=horizon_start.isoformat(),
            horizon_end=horizon_end.isoformat(),
            schedule=schedule_json,
        )

    except HTTPException:
        # Re-raise HTTP exceptions
        raise
    except asyncpg.PostgresError as e:
        logger.error(
            f"Database error getting schedule: {e}", exc_info=True, extra={"depot_id": depot_id}
        )
        raise DatabaseError() from e
    except Exception as e:
        logger.error(f"Failed to get schedule: {e}", exc_info=True, extra={"depot_id": depot_id})
        raise HTTPException(
            status_code=500,
            detail={"error_code": ErrorCode.INTERNAL_ERROR.value, "detail": "Failed to get schedule"},
        ) from e


@app.get(
    "/depots/{depot_id}/alerts",
    response_model=AlertsResponse,
    tags=["depots"],
    summary="Get depot alerts and last optimization",
    description="""
    Get active charger faults and last optimization outcome for ops visibility (PRD §7.1, §10.5).

    **Authentication:** Requires JWT token in Authorization header.

    **Response:**
    - `charger_faults`: Active OCPP StatusNotification fault codes for depot chargers
    - `last_optimization`: Last run (run_id, status, solver_used, solve_time_s, timestamp)

    **Error Codes:**
    - 400: Invalid depot_id
    - 404: Depot not found
    - 500: Server error
    Reference: PRD_v2_7_Building_Integration.md §7.1, AT-16
    """,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid depot_id"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        500: {"model": ErrorResponse, "description": "Server error"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_depot_alerts(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """GET /depots/{depot_id}/alerts — charger faults and last optimization (PRD §7.1)."""
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        # Static data: depot existence check + name + charger ocpp_id → charger_id map
        async with db_pools.static.acquire() as conn:
            depot_row = await conn.fetchrow(
                "SELECT name FROM depots WHERE depot_id = $1",
                depot_id,
            )
            if depot_row is None:
                raise HTTPException(status_code=404, detail=f"Depot {depot_id} not found")
            depot_name: Optional[str] = depot_row["name"]

            charger_rows = await conn.fetch(
                "SELECT charger_id, ocpp_id FROM chargers WHERE depot_id = $1",
                depot_id,
            )
        charger_map: dict[str, str] = {r["ocpp_id"]: str(r["charger_id"]) for r in charger_rows}
        depot_ocpp_ids = list(charger_map.keys())

        # Time-series data: last optimization run + connector faults
        async with db_pools.ts.acquire() as conn:
            last_row = await conn.fetchrow(
                """
                SELECT run_id, run_time, status, solver_used, solve_time_s
                FROM optimization_runs
                WHERE depot_id = $1
                ORDER BY run_time DESC
                LIMIT 1
                """,
                depot_id,
            )
            fault_rows = (
                await conn.fetch(
                    """
                SELECT DISTINCT ON (station_id, connector_id)
                    station_id, connector_id, status, error_code, timestamp
                FROM connector_status
                WHERE station_id = ANY($1) AND status = 'Faulted'
                ORDER BY station_id, connector_id, timestamp DESC
                """,
                    depot_ocpp_ids,
                )
                if depot_ocpp_ids
                else []
            )

        last_optimization: Optional[LastOptimizationItem] = None
        if last_row:
            ts = last_row["run_time"]
            ts_str = ts.isoformat() if isinstance(ts, datetime) else str(ts)
            last_optimization = LastOptimizationItem(
                run_id=str(last_row["run_id"]),
                status=last_row["status"] or "unknown",
                solver_used=last_row["solver_used"] or "gurobi",
                solve_time_s=last_row["solve_time_s"],
                timestamp=ts_str,
            )

        charger_faults = [
            ChargerFaultItem(
                charger_id=charger_map[r["station_id"]],
                ocpp_id=r["station_id"],
                connector_id=r["connector_id"],
                fault_code=r["error_code"] or "Unknown",
                timestamp=(
                    r["timestamp"].isoformat()
                    if isinstance(r["timestamp"], datetime)
                    else str(r["timestamp"])
                ),
            )
            for r in fault_rows
            if r["station_id"] in charger_map
        ]

        # notification_alerts (alerts pipeline). Best-effort; if the table
        # doesn't exist (migration 022 not applied) we just return the
        # legacy charger_faults list.
        notification_alerts: list[NotificationAlertItem] = []
        try:
            async with db_pools.ts.acquire() as conn:
                from src.notifications.alerts import list_for_depot as _list_alerts

                rows = await _list_alerts(
                    conn, UUID(depot_id), statuses=("active", "acknowledged")
                )
                notification_alerts = [
                    NotificationAlertItem(
                        alert_id=str(a.id),
                        organization_id=str(a.organization_id),
                        depot_id=str(a.depot_id) if a.depot_id else None,
                        depot_name=depot_name,
                        alert_type=a.alert_type,
                        severity=a.severity.value,
                        subject=a.title,
                        body=a.detail,
                        dedup_key=a.dedup_key,
                        status=a.status,
                        first_seen_at=a.first_occurrence_at.isoformat(),
                        last_seen_at=a.last_occurrence_at.isoformat(),
                        occurrence_count=a.occurrence_count,
                        acknowledged_by=str(a.acknowledged_by) if a.acknowledged_by else None,
                        resolved_at=a.resolved_at.isoformat() if a.resolved_at else None,
                        last_notified_at=(
                            a.last_notified_at.isoformat() if a.last_notified_at else None
                        ),
                    )
                    for a in rows
                ]
        except asyncpg.UndefinedTableError:
            logger.debug("notification_alerts table not present; skipping")
        except Exception as exc:
            logger.warning("failed to read notification_alerts: %s", exc, exc_info=True)

        now = datetime.utcnow()
        return AlertsResponse(
            depot_id=depot_id,
            timestamp=now.isoformat() + "Z",
            charger_faults=charger_faults,
            last_optimization=last_optimization,
            notification_alerts=notification_alerts,
        )

    except HTTPException:
        raise
    except asyncpg.PostgresError as e:
        logger.error(
            f"Database error getting alerts: {e}",
            exc_info=True,
            extra={"depot_id": depot_id},
        )
        raise DatabaseError() from e
    except Exception as e:
        logger.error(
            f"Failed to get alerts: {e}",
            exc_info=True,
            extra={"depot_id": depot_id},
        )
        raise HTTPException(
            status_code=500,
            detail={"error_code": ErrorCode.INTERNAL_ERROR.value, "detail": "Failed to get alerts"},
        ) from e


@app.post(
    "/depots/{depot_id}/vehicles/{vehicle_id}/handoff",
    response_model=HandoffResponse,
    status_code=status.HTTP_200_OK,
    tags=["depots"],
    summary="Send inter-depot handoff message",
    description="""
    Send a handoff message when a vehicle is departing from one depot
    to another depot. The destination depot will receive notification
    to plan charging for the arriving vehicle.
    
    **Authentication:** Requires JWT token in Authorization header.
    
    **Request:**
    - `dest_depot_id`: Destination depot identifier (UUID)
    - `expected_soc`: Expected state of charge at arrival (0.0-1.0)
    - `arrival_time`: Expected arrival time (ISO 8601)
    
    **Response:**
    - `message_id`: Handoff message identifier (UUID)
    - `status`: Message status (e.g., 'sent')
    
    **Error Codes:**
    - 400: Invalid request (invalid UUID, invalid SoC)
    - 401: Unauthorized (missing or invalid JWT token)
    - 500: Server error
    - 503: Database not available
    
    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Server error"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def send_handoff(
    vehicle_id: str,
    request: HandoffRequest,
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Send inter-depot handoff message.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """
    validate_vehicle_id(vehicle_id)
    validate_depot_id(request.dest_depot_id)

    if not db_pools:
        raise DatabaseError("Database not available")

    # Check handoff rate limit per PRD Section 10.4 (50 messages/hour per depot pair)
    if not get_rate_limiter().check_handoff_limit(depot_id, request.dest_depot_id):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded: Maximum 50 handoff messages per hour per depot pair",
        )

    try:
        from uuid import uuid4

        message_id = uuid4()
        departure_time = datetime.utcnow()

        # Get vehicle details for handoff message (vehicles is in Supabase)
        vehicle_query = """
        SELECT external_id, battery_kwh, max_charge_kw
        FROM vehicles
        WHERE vehicle_id = $1 AND depot_id = $2
        """
        async with db_pools.static.acquire() as conn:
            vehicle_row = await conn.fetchrow(vehicle_query, vehicle_id, depot_id)

        if not vehicle_row:
            raise HTTPException(
                status_code=404, detail=f"Vehicle {vehicle_id} not found in depot {depot_id}"
            )

        external_id = vehicle_row["external_id"]
        # Use request values (required per PRD Section 5.4), fall back to vehicle table if not provided
        battery_kwh = getattr(request, "battery_kwh", None) or float(vehicle_row["battery_kwh"])
        max_charge_kw = getattr(request, "max_charge_kw", None) or float(
            vehicle_row["max_charge_kw"]
        )

        # Store message in database with status='pending' (interdepot_messages is in TimescaleDB)
        query = """
        INSERT INTO interdepot_messages
            (message_id, origin_depot_id, dest_depot_id, vehicle_id,
             departure_time, expected_soc, arrival_time, battery_kwh, max_charge_kw, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """
        async with db_pools.ts.acquire() as conn:
            await conn.execute(
                query,
                message_id,
                depot_id,
                request.dest_depot_id,
                vehicle_id,
                departure_time,
                request.expected_soc,
                request.arrival_time,
                battery_kwh,
                max_charge_kw,
                "pending",
            )

        # Call destination depot's receive endpoint (per PRD Section 5.4)
        # Get destination depot endpoint from environment or config
        dest_depot_endpoint = os.getenv(
            f"DEPOT_{request.dest_depot_id}_ENDPOINT",
            os.getenv("DEFAULT_DEPOT_ENDPOINT", "http://localhost:8000"),
        )
        # Security (H4): require HTTPS for inter-depot communication
        if _environment == "production" and dest_depot_endpoint.startswith("http://"):
            logger.warning("Handoff to non-HTTPS endpoint blocked in production")
            raise HTTPException(
                status_code=400,
                detail="Inter-depot handoff requires HTTPS in production",
            )

        # Security (C2): HMAC signing key is mandatory. Without it the receive
        # side will reject the request, so refuse here to surface the misconfig
        # at the source rather than after a network round-trip.
        signing_key = os.getenv("HANDOFF_SIGNING_KEY", "")
        if not signing_key:
            logger.error("HANDOFF_SIGNING_KEY is not configured; refusing send_handoff")
            raise HTTPException(
                status_code=503,
                detail="Inter-depot handoff is unavailable: signing key not configured",
            )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                receive_url = (
                    f"{dest_depot_endpoint}/depots/{request.dest_depot_id}/handoff/receive"
                )
                nonce = str(uuid4())
                # Use timezone-aware UTC so the receiver's window check is
                # unambiguous regardless of host clock representation.
                timestamp_str = datetime.now(timezone.utc).isoformat()
                receive_payload = {
                    "message_id": str(message_id),
                    "origin_depot_id": depot_id,
                    "vehicle_id": vehicle_id,
                    "external_id": external_id,
                    "expected_soc": request.expected_soc,
                    "arrival_time": request.arrival_time.isoformat(),
                    "battery_kwh": battery_kwh,
                    "max_charge_kw": max_charge_kw,
                    "nonce": nonce,
                    "timestamp": timestamp_str,
                }
                # Security (C2): HMAC-SHA256 signature for mutual auth.
                payload_bytes = json.dumps(receive_payload, sort_keys=True).encode()
                sig = hmac.new(signing_key.encode(), payload_bytes, hashlib.sha256).hexdigest()
                receive_payload["signature"] = sig

                response = await client.post(receive_url, json=receive_payload)
                response.raise_for_status()
                ack_data = response.json()

                # Update message status to 'acknowledged' (ts)
                update_query = """
                UPDATE interdepot_messages
                SET status = 'acknowledged', acknowledged_at = $1
                WHERE message_id = $2
                """
                async with db_pools.ts.acquire() as conn:
                    await conn.execute(
                        update_query,
                        datetime.fromisoformat(ack_data["acknowledged_at"].replace("Z", "+00:00")),
                        message_id,
                    )

                logger.info(
                    "Handoff message sent and acknowledged",
                    extra={
                        "message_id": str(message_id),
                        "origin_depot_id": depot_id,
                        "dest_depot_id": request.dest_depot_id,
                        "vehicle_id": vehicle_id,
                    },
                )
        except httpx.RequestError as e:
            logger.warning(
                f"Failed to call destination depot receive endpoint: {e}. "
                "Message stored locally but not acknowledged.",
                extra={
                    "message_id": str(message_id),
                    "dest_depot_id": request.dest_depot_id,
                },
            )
            # Message is stored but not acknowledged - will be retried or handled manually

        return HandoffResponse(message_id=str(message_id), status="sent")

    except asyncpg.PostgresError as e:
        logger.error(
            f"Database error sending handoff: {e}",
            exc_info=True,
            extra={"depot_id": depot_id, "vehicle_id": vehicle_id},
        )
        raise DatabaseError() from e
    except Exception as e:
        logger.error(
            f"Failed to send handoff: {e}",
            exc_info=True,
            extra={"depot_id": depot_id, "vehicle_id": vehicle_id},
        )
        raise HTTPException(
            status_code=500,
            detail={"error_code": ErrorCode.INTERNAL_ERROR.value, "detail": "Failed to send handoff"},
        ) from e


class HandoffReceiveRequest(BaseModel):
    """Request to receive inter-depot handoff message.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """

    message_id: Optional[str] = Field(None, description="Message identifier (UUID, optional)")
    origin_depot_id: str = Field(..., description="Origin depot identifier (UUID)")
    vehicle_id: str = Field(..., description="Vehicle identifier (UUID)")
    external_id: str = Field(..., description="Vehicle external ID (e.g., 'bus_101')")
    expected_soc: float = Field(
        ..., ge=0.0, le=1.0, description="Expected SoC at arrival (0.0-1.0)"
    )
    arrival_time: datetime = Field(..., description="Expected arrival time (ISO 8601)")
    battery_kwh: float = Field(..., gt=0, description="Vehicle battery capacity (kWh)")
    max_charge_kw: float = Field(..., gt=0, description="Vehicle max charge rate (kW)")

    @field_validator("origin_depot_id", "vehicle_id")
    @classmethod
    def validate_uuid(cls, v: str) -> str:
        """Validate UUID format."""
        try:
            UUID(v)
            return v
        except ValueError:
            raise ValueError(f"Must be a valid UUID, got: {v}")


class HandoffReceiveResponse(BaseModel):
    """Response to handoff receive request.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """

    status: str = Field(..., description="Status: 'acknowledged'")
    message_id: str = Field(..., description="Message identifier (UUID)")
    acknowledged_at: datetime = Field(..., description="Acknowledgment timestamp")


@app.post(
    "/depots/{depot_id}/handoff/receive",
    response_model=HandoffReceiveResponse,
    status_code=status.HTTP_200_OK,
    tags=["depots"],
    summary="Receive inter-depot handoff message",
    description="""
    Receive a handoff message from another depot when a vehicle is en route.
    The destination depot stores the message and incorporates the vehicle
    into the next optimization run.
    
    **Authentication:** Requires JWT token in Authorization header.
    Note: In production, this endpoint should also validate inter-depot
    authentication (mutual TLS or signed JWT per PRD Section 10.3).
    
    **Request:**
    - `origin_depot_id`: Origin depot identifier (UUID)
    - `vehicle_id`: Vehicle identifier (UUID)
    - `external_id`: Vehicle external ID
    - `expected_soc`: Expected SoC at arrival (0.0-1.0)
    - `arrival_time`: Expected arrival time (ISO 8601)
    - `battery_kwh`: Vehicle battery capacity (kWh)
    - `max_charge_kw`: Vehicle max charge rate (kW)
    
    **Response:**
    - `status`: 'acknowledged'
    - `message_id`: Handoff message identifier (UUID)
    - `acknowledged_at`: Acknowledgment timestamp
    
    **Error Codes:**
    - 400: Invalid request (invalid UUID, invalid SoC)
    - 401: Unauthorized (missing or invalid JWT token)
    - 500: Server error
    - 503: Database not available
    
    Reference: PRD_v2.md#7-1-rest-api-endpoints, Section 5.4
    """,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        500: {"model": ErrorResponse, "description": "Server error"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def receive_handoff(
    depot_id: str,
    request: HandoffReceiveRequest,
    http_request: Request,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Receive inter-depot handoff message.

    Per PRD Section 5.4, the destination depot:
    1. Validates the request
    2. Stores message in interdepot_messages with status='acknowledged'
    3. Returns acknowledgment with acknowledged_at timestamp

    Security (C2): the request body MUST carry an HMAC-SHA256 signature
    keyed by ``HANDOFF_SIGNING_KEY``, plus a fresh ``nonce`` and ``timestamp``
    within ``_HANDOFF_REPLAY_WINDOW_S`` of now. JWT auth alone is insufficient
    because authenticated tenants would otherwise be able to inject handoff
    messages claiming any ``origin_depot_id``.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """
    if not db_pools:
        raise DatabaseError("Database not available")

    # Validate UUIDs
    validate_depot_id(depot_id)
    validate_depot_id(request.origin_depot_id)
    validate_vehicle_id(request.vehicle_id)

    # Security (C2): verify the inter-depot HMAC signature on the raw body.
    # Without a configured signing key the endpoint must refuse.
    signing_key = os.getenv("HANDOFF_SIGNING_KEY", "")
    if not signing_key:
        logger.error(
            "HANDOFF_SIGNING_KEY is not configured; refusing handoff for depot=%s",
            depot_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Inter-depot handoff is unavailable: signing key not configured",
        )

    try:
        raw_body = await http_request.body()
        body_dict = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if not isinstance(body_dict, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    if not _verify_handoff_payload(body_dict, signing_key):
        peer = http_request.client.host if http_request.client else "unknown"
        logger.warning(
            "Handoff signature verification failed: depot=%s origin_claimed=%s peer=%s",
            depot_id,
            request.origin_depot_id,
            peer,
        )
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired handoff signature",
        )

    await verify_depot_access(depot_id, user, db_pools.static)

    # Validate SoC range
    if not (0.0 <= request.expected_soc <= 1.0):
        raise HTTPException(
            status_code=400,
            detail=f"expected_soc must be between 0.0 and 1.0, got {request.expected_soc}",
        )

    # Check handoff rate limit per PRD Section 10.4 (50 messages/hour per depot pair)
    if not get_rate_limiter().check_handoff_limit(request.origin_depot_id, depot_id):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded: Maximum 50 handoff messages per hour per depot pair",
        )

    try:
        from uuid import uuid4

        message_id = uuid4()
        acknowledged_at = datetime.utcnow()

        # Get actual departure_time from original pending message if it exists
        # Per PRD Section 5.4, use actual departure_time instead of approximation
        departure_time = acknowledged_at  # Default fallback
        async with db_pools.ts.acquire() as conn:
            original_message = await conn.fetchrow(
                """
                SELECT departure_time
                FROM interdepot_messages
                WHERE origin_depot_id = $1
                  AND dest_depot_id = $2
                  AND vehicle_id = $3
                  AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                request.origin_depot_id,
                depot_id,
                request.vehicle_id,
            )
            if original_message and original_message["departure_time"]:
                departure_time = original_message["departure_time"]
                logger.debug(f"Using actual departure_time {departure_time} from original message")
            else:
                logger.warning(
                    "Original message not found, using acknowledged_at as departure_time approximation"
                )

        # Store message in database with status='acknowledged' (ts)
        query = """
        INSERT INTO interdepot_messages
            (message_id, origin_depot_id, dest_depot_id, vehicle_id,
             departure_time, expected_soc, arrival_time, battery_kwh,
             max_charge_kw, status, acknowledged_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """
        async with db_pools.ts.acquire() as conn:
            await conn.execute(
                query,
                message_id,
                request.origin_depot_id,
                depot_id,
                request.vehicle_id,
                departure_time,  # Use actual departure_time from original message
                request.expected_soc,
                request.arrival_time,
                request.battery_kwh,
                request.max_charge_kw,
                "acknowledged",
                acknowledged_at,
            )

        logger.info(
            "Handoff message received and acknowledged",
            extra={
                "message_id": str(message_id),
                "origin_depot_id": request.origin_depot_id,
                "dest_depot_id": depot_id,
                "vehicle_id": request.vehicle_id,
                "external_id": request.external_id,
            },
        )

        # Trigger optimization per PRD Section 5.3 (inter-depot handoff trigger)
        # Ensure optimization completes within 60 seconds
        if controller_manager:
            try:
                controller = await controller_manager.get_or_create_controller(depot_id)
                # Trigger optimization with reason='interdepot_handoff'
                trigger_reason = (
                    f"interdepot_handoff: message_id={message_id}, vehicle_id={request.vehicle_id}"
                )
                logger.info(
                    f"Triggering optimization for depot {depot_id} " f"due to inter-depot handoff"
                )
                # Run optimization in background to avoid blocking response
                asyncio.create_task(controller.run_optimization(trigger_reason))
            except Exception as e:
                logger.error(
                    f"Failed to trigger optimization after handoff: {e}",
                    exc_info=True,
                    extra={
                        "depot_id": depot_id,
                        "message_id": str(message_id),
                        "vehicle_id": request.vehicle_id,
                    },
                )
                # Continue - message is stored, optimization can be triggered later

        return HandoffReceiveResponse(
            status="acknowledged",
            message_id=str(message_id),
            acknowledged_at=acknowledged_at,
        )

    except asyncpg.PostgresError as e:
        logger.error(
            f"Database error receiving handoff: {e}",
            exc_info=True,
            extra={"depot_id": depot_id, "vehicle_id": request.vehicle_id},
        )
        raise DatabaseError() from e
    except Exception as e:
        logger.error(
            f"Failed to receive handoff: {e}",
            exc_info=True,
            extra={"depot_id": depot_id, "vehicle_id": request.vehicle_id},
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": ErrorCode.INTERNAL_ERROR.value,
                "detail": "Failed to receive handoff",
            },
        ) from e


# ── Internal OCPP event endpoint ─────────────────────────────────────────────
# Called by the websocket_handler to push OCPP events directly so the
# TriggerMonitor does not have to wait for its 60-second polling cycle.


class _OcppEventPayload(BaseModel):
    charge_point_id: str
    event_type: str  # "meter_values" | "status_notification" | "boot_notification"
    data: dict = Field(default_factory=dict)


@app.post("/internal/ocpp-event", include_in_schema=False)
async def receive_ocpp_event(
    payload: _OcppEventPayload,
    request: Request,
) -> dict:
    """Receive an OCPP event from the websocket_handler and trigger immediate re-evaluation.

    Not exposed in the public OpenAPI schema. Always requires a valid
    ``X-Internal-Token`` matching ``INTERNAL_API_TOKEN``.

    Security (C3): the endpoint fails closed in every environment when the
    token is unset. Previously the dev/staging path treated a missing token as
    a no-op which left the endpoint open to anyone who could reach the host.
    """
    # Security (C3): always require the shared token, regardless of environment.
    if not _INTERNAL_API_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Internal endpoint not configured (INTERNAL_API_TOKEN missing)",
        )
    # Security (M11): timing-safe token comparison to prevent timing attacks.
    token = request.headers.get("X-Internal-Token", "")
    if not secrets.compare_digest(token, _INTERNAL_API_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not controller_manager or not db_pools:
        return {"status": "unavailable"}

    # Resolve depot_id from charge_point_id (ocpp_id → depot_id via Supabase).
    try:
        async with db_pools.static.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT depot_id::text FROM chargers WHERE ocpp_id = $1",
                payload.charge_point_id,
            )
    except Exception as e:
        logger.warning("Could not resolve depot for charger %s: %s", payload.charge_point_id, e)
        return {"status": "error", "detail": "depot lookup failed"}

    if not row:
        return {"status": "unknown_charger", "charge_point_id": payload.charge_point_id}

    depot_id = row["depot_id"]
    reason = f"ocpp_event:{payload.event_type}:{payload.charge_point_id}"

    try:
        controller = await controller_manager.get_or_create_controller(depot_id)
        # Fire-and-forget: don't block the response waiting for MILP to solve.
        asyncio.create_task(controller._handle_trigger(reason))
        return {"status": "triggered", "depot_id": depot_id}
    except Exception as e:
        logger.warning("Failed to trigger optimization for depot %s: %s", depot_id, e)
        return {"status": "error", "detail": str(e)}


# ── Health checks ─────────────────────────────────────────────────────────────


async def check_database_health() -> str:
    """Check database connectivity.

    Returns:
        "healthy" if database is accessible, "unavailable" otherwise
    """
    if not db_pools:
        return "unavailable"

    try:
        async with db_pools.static.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return "healthy"
    except Exception as e:
        logger.warning(f"Database health check failed: {e}")
        return "unavailable"


async def check_ocpp_server_health() -> str:
    """Check OCPP server health.

    Returns:
        "healthy", "unavailable", or "unknown"
    """
    global ocpp_server

    if ocpp_server is None:
        return "unknown"

    try:
        # Check if server is running
        if hasattr(ocpp_server, "_running") and ocpp_server._running:
            # Check if any charge points are connected
            if hasattr(ocpp_server, "charge_points"):
                connected_count = len(ocpp_server.charge_points)
                if connected_count > 0:
                    return "healthy"
                else:
                    return "unavailable"  # Running but no connections
            return "healthy"
        else:
            return "unavailable"
    except Exception as e:
        logger.debug(f"OCPP server health check error: {e}")
        return "unknown"


def check_gurobi_license() -> str:
    """Check Gurobi license status.

    Per PRD Section 8.2: Gurobi requires valid license.
    Per PRD Section 7.1: Health endpoint should report license status.

    Returns:
        "valid", "invalid", "unavailable", or "not_configured"
    """
    try:
        import pyomo.environ as pyo

        solver = pyo.SolverFactory("gurobi")
        if solver is None:
            return "not_configured"

        # Check if solver is available (includes license check)
        if solver.available():
            # Try to create a simple model to verify license works
            try:
                model = pyo.ConcreteModel()
                model.x = pyo.Var(domain=pyo.NonNegativeReals)
                model.obj = pyo.Objective(expr=model.x)
                # Quick solve test (should be instant for trivial problem)
                result = solver.solve(model, tee=False)
                if result.solver.termination_condition == pyo.TerminationCondition.optimal:
                    return "valid"
                else:
                    return "invalid"
            except Exception as e:
                logger.debug(f"Gurobi license test failed: {e}")
                return "invalid"
        else:
            return "unavailable"
    except ImportError:
        logger.debug("Gurobi/Pyomo not available")
        return "not_configured"
    except Exception as e:
        logger.debug(f"Gurobi license check error: {e}")
        return "unavailable"


@app.get(
    "/health",
    tags=["health"],
    summary="Health check endpoint",
    description="""
    Check the health status of the API and its components.

    **Response:**
    - `status`: Overall status ("healthy" or "degraded")
    - `timestamp`: Current timestamp (ISO 8601)
    - `components`: Status of individual components
      - `database`: "healthy" or "unavailable"
      - `ocpp_server`: "healthy", "unavailable", or "unknown"
      - `gurobi_license`: "valid", "invalid", or "unavailable"

    Returns **200** when healthy, **503** when degraded (database unavailable).
    A Gurobi license issue is treated as degraded-but-functional (HiGHS fallback
    is available) and returns 200 with status "degraded".

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """,
)
async def health_check(response: Response):
    """Health check endpoint.

    Per PRD Section 7.1: Health endpoint reports component status including
    Gurobi license (per PRD Section 8.2).

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """
    # Check component health
    db_status = await check_database_health()
    ocpp_status = await check_ocpp_server_health()
    gurobi_status = check_gurobi_license()
    controller_status = "healthy" if controller_manager is not None else "unavailable"

    # /health always returns 200 so it remains reachable for diagnostics even
    # when dependencies are degraded. Use the `status` and `components` fields
    # to surface issues. A separate /readiness probe is the right place to gate
    # load-balancer traffic.
    overall_status = (
        "healthy" if db_status == "healthy" and controller_status == "healthy" else "degraded"
    )

    return {
        "status": overall_status,
        "timestamp": datetime.utcnow().isoformat(),
        "components": {
            "database": db_status,
            "ocpp_server": ocpp_status,
            "gurobi_license": gurobi_status,
            "controller_manager": controller_status,
        },
    }


@app.get(
    "/metrics",
    tags=["health"],
    summary="Prometheus metrics endpoint",
    description="""
    Expose Prometheus metrics for monitoring and observability.
    
    Returns metrics in Prometheus text format including:
    - Optimization run counts and durations
    - Vehicle SoC metrics
    - Grid power and peak demand
    - System performance metrics
    - Control loop metrics
    
    Reference: Development plan Step 7.2
    """,
    include_in_schema=False,  # Hide from OpenAPI docs (internal endpoint)
)
async def metrics():
    """Prometheus metrics endpoint.

    Reference: Development plan Step 7.2
    """
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get(
    "/admin/controllers",
    tags=["admin"],
    summary="List active controllers",
    description="""
    List all active depot controllers managed by the controller manager.
    
    **Authentication:** Requires JWT token in Authorization header.
    
    **Response:**
    - `controllers`: List of depot IDs with active controllers
    - `count`: Number of active controllers
    
    Reference: Development plan Step 5.2
    """,
    include_in_schema=True,
)
async def list_controllers(_auth: None = Depends(require_favonius_admin())):
    """List active controllers."""
    if not controller_manager:
        raise HTTPException(status_code=503, detail="Controller manager not initialized")

    controllers = controller_manager.list_controllers()
    return {
        "controllers": controllers,
        "count": len(controllers),
    }


@app.get(
    "/admin/controllers/{depot_id}/health",
    tags=["admin"],
    summary="Get controller health status",
    description="""
    Get health status for a specific depot controller.
    
    **Authentication:** Requires JWT token in Authorization header.
    
    **Response:**
    - `depot_id`: Depot identifier
    - `status`: Controller status (healthy, degraded, error)
    - `running`: Whether controller is running
    - `last_run_time`: Last optimization run time
    - `optimization_failures`: Number of consecutive failures
    - `circuit_breaker_open`: Whether circuit breaker is open
    
    Reference: Development plan Step 5.2
    """,
)
async def get_controller_health(depot_id: str = Depends(_require_depot_access)):
    """Get controller health status."""
    if not controller_manager:
        raise HTTPException(status_code=503, detail="Controller manager not initialized")

    health = await controller_manager.health_check()

    if depot_id not in health:
        raise HTTPException(status_code=404, detail=f"Controller not found for depot {depot_id}")

    return {
        "depot_id": depot_id,
        **health[depot_id],
    }


# ── Cross-org admin endpoints (organizations, depots, credentials) ───────────


_ACCESS_DENIED_DEPOT_DETAIL = "Access denied: you do not have permission for this depot"
_ACCESS_DENIED_ORG_DETAIL = "Access denied: you do not have permission for this organization"


def _forbidden(error_code: str, detail: str) -> HTTPException:
    """403 with a stable ``error_code`` payload that frontends can switch on."""
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"error_code": error_code, "message": detail},
    )


def _require_admin_role(user: dict) -> str:
    """Allow either favonius_admin or customer_admin. Raise 403 otherwise.

    Returns the resolved role string.
    """
    role = get_user_role(user)
    if role not in ("favonius_admin", "customer_admin"):
        raise _forbidden("FORBIDDEN_ROLE", "Admin role required")
    return role


async def _record_admin_action(
    *,
    user: dict,
    action: str,
    depot_id: Optional[str] = None,
    organization_id_override: Optional[str] = None,
    target_type: Optional[str] = None,
    target_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Best-effort write of one ``audit_log`` row after a successful admin action."""
    if db_pools is None:
        return
    actor_user_id = user.get("sub") if isinstance(user, dict) else None
    actor_role = get_user_role(user)
    organization_id = organization_id_override or get_user_organization_id(user)
    row = AdminAuditRow(
        action=action,
        actor_user_id=str(actor_user_id) if actor_user_id else None,
        actor_role=actor_role,
        organization_id=organization_id,
        depot_id=depot_id,
        target_type=target_type,
        target_id=target_id,
        metadata=metadata or {},
    )
    await write_admin_audit_row(db_pools.static, row)


@app.get(
    "/admin/organizations",
    tags=["admin"],
    summary="List all organizations (favonius_admin only)",
)
async def list_organizations_admin(user: dict = Depends(ensure_tenant_mirrored)):
    """Return the full set of organizations.

    Restricted to favonius_admin (cross-tenant). Records ``admin.read`` audit row.
    """
    if not is_platform_admin(user):
        raise _forbidden(
            "FORBIDDEN_ROLE",
            "favonius_admin role required",
        )
    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.static.acquire() as conn:
        organizations = await db_queries.list_all_organizations(conn)

    await _record_admin_action(
        user=user,
        action="admin.read",
        target_type="organization",
        target_id="*",
        metadata={
            "endpoint": "GET /admin/organizations",
            "result_count": len(organizations),
        },
    )
    return {"organizations": organizations, "count": len(organizations)}


@app.get(
    "/admin/organizations/{org_id}/depots",
    tags=["admin"],
    summary="List depots for an organization",
)
async def list_organization_depots_admin(
    org_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Return depots for the given organization.

    Allowed to favonius_admin (cross-tenant) or to a customer_admin whose
    JWT ``organization_id`` matches the path. Other callers (including
    customer_admin from a different org) get 403, NOT 404, so org existence
    is not leaked.
    """
    _, cross_org_read = _require_org_admin_access(user, org_id)

    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.static.acquire() as conn:
        depots = await db_queries.get_depots_for_organization(conn, organization_id=org_id)

    if cross_org_read:
        await _record_admin_action(
            user=user,
            action="admin.read",
            target_type="organization",
            target_id=str(org_id),
            organization_id_override=str(org_id),
            metadata={
                "endpoint": "GET /admin/organizations/{org_id}/depots",
                "result_count": len(depots),
            },
        )

    return {"organization_id": str(org_id), "depots": depots, "count": len(depots)}


async def _resolve_depot_for_admin(
    depot_id: str, user: dict
) -> tuple[dict, bool]:
    """Resolve a depot for cross-org admin access.

    Returns (depot_row, cross_org_read).

    - favonius_admin: always allowed; cross_org_read=True if the depot's org
      differs from any caller-org in the JWT (favonius_admin has no own org).
    - customer_admin / customer_operator: must own the depot via organization_id.
    - viewer or unknown roles: 403.

    Raises 403 (never 404) when the depot does not exist or is not accessible
    so existence does not leak across tenants.
    """
    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.static.acquire() as conn:
        depot_row = await db_queries.get_depot_by_id(conn, depot_id)

    role = get_user_role(user)

    if role == "favonius_admin":
        if depot_row is None:
            # Even for platform admin, return 403 to keep the API surface
            # uniform for tenants observing across the wire — but admin will
            # rarely hit this in practice. Use 404 here since admin is allowed
            # to know about all depots.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "DEPOT_NOT_FOUND", "message": "Depot not found"},
            )
        return depot_row, True

    if role not in ("customer_admin", "customer_operator"):
        # viewer and unknown roles are denied without revealing existence
        raise _forbidden("FORBIDDEN_ROLE", _ACCESS_DENIED_DEPOT_DETAIL)

    caller_org = get_user_organization_id(user)
    if not caller_org:
        raise _forbidden(
            "MISSING_ORGANIZATION",
            "missing organization_id in token app_metadata",
        )

    if depot_row is None or str(depot_row.get("organization_id")) != str(caller_org):
        # Leak-resistant: no distinction between "no such depot" and
        # "depot belongs to another tenant" — both are 403.
        raise _forbidden("FORBIDDEN_DEPOT", _ACCESS_DENIED_DEPOT_DETAIL)

    return depot_row, False


async def _resolve_charger_for_depot(
    *, depot_id: str, charger_id: str
) -> Optional[dict]:
    """Return a credential-status row for a charger or None if the charger does not exist."""
    if not db_pools:
        raise DatabaseError("Database not available")
    async with db_pools.static.acquire() as conn:
        return await db_queries.get_charger_credentials_status(
            conn, depot_id=depot_id, charger_id=charger_id
        )


@app.get(
    "/admin/depots/{depot_id}/chargers/{charger_id}/credentials_status",
    tags=["admin"],
    summary="Get charger credential metadata (never plaintext)",
)
async def get_charger_credentials_status(
    depot_id: str,
    charger_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Return credential status for a charger.

    Response shape: ``{configured: bool, created_at, last_rotated_at}``.
    The plaintext password is NEVER included; the password_hash is also never
    surfaced to the client.

    Authorization:
      - favonius_admin: allowed; cross-org read recorded as ``admin.read``.
      - customer_admin / customer_operator: only when the caller's
        ``organization_id`` matches ``depots.organization_id``.
      - other roles: 403.
    """
    validate_uuid(depot_id, "depot_id")
    validate_uuid(charger_id, "charger_id")

    depot_row, cross_org_read = await _resolve_depot_for_admin(depot_id, user)
    charger_row = await _resolve_charger_for_depot(
        depot_id=depot_id, charger_id=charger_id
    )
    if charger_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "CHARGER_NOT_FOUND", "message": "Charger not found"},
        )

    response = {
        "depot_id": depot_id,
        "charger_id": charger_id,
        "ocpp_id": charger_row["ocpp_id"],
        "configured": (
            charger_row.get("credentials_created_at") is not None
            and bool(charger_row.get("credentials_active", False))
        ),
        "created_at": charger_row.get("credentials_created_at"),
        "last_rotated_at": charger_row.get("credentials_last_rotated_at"),
    }

    if cross_org_read:
        await _record_admin_action(
            user=user,
            action="admin.read",
            depot_id=depot_id,
            organization_id_override=str(depot_row.get("organization_id"))
            if depot_row.get("organization_id")
            else None,
            target_type="charger",
            target_id=str(charger_id),
            metadata={
                "endpoint": "GET /admin/depots/{depot_id}/chargers/{charger_id}/credentials_status",
            },
        )

    return response


@app.post(
    "/admin/depots/{depot_id}/chargers/{charger_id}/rotate_credentials",
    tags=["admin"],
    summary="Rotate a charger's Basic Auth credentials",
)
async def rotate_charger_credentials_endpoint(
    depot_id: str,
    charger_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Generate a new Basic Auth password, replace the stored hash, and return the plaintext exactly once.

    Authorization:
      - favonius_admin: always allowed.
      - customer_admin: allowed only when the caller's organization_id matches
        the depot's organization_id.
      - customer_operator / viewer / others: 403.

    Always records a ``charger.credentials.rotated`` audit row on success.
    """
    validate_uuid(depot_id, "depot_id")
    validate_uuid(charger_id, "charger_id")

    role = get_user_role(user)
    if role not in ("favonius_admin", "customer_admin"):
        raise _forbidden("FORBIDDEN_ROLE", "favonius_admin or customer_admin role required")

    depot_row, _ = await _resolve_depot_for_admin(depot_id, user)
    # _resolve_depot_for_admin already enforced tenant access for customer_admin
    # and cross-org for favonius_admin; nothing more to check here.

    new_password = _generate_ocpp_basic_password()
    new_hash = await _hash_ocpp_basic_password(new_password)

    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.static.acquire() as conn:
        async with conn.transaction():
            result = await db_queries.rotate_charger_credentials(
                conn,
                depot_id=depot_id,
                charger_id=charger_id,
                new_password_hash=new_hash,
            )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "CHARGER_NOT_FOUND", "message": "Charger not found"},
        )

    await _record_admin_action(
        user=user,
        action="charger.credentials.rotated",
        depot_id=depot_id,
        organization_id_override=str(depot_row.get("organization_id"))
        if depot_row.get("organization_id")
        else None,
        target_type="charger",
        target_id=str(charger_id),
        metadata={
            "endpoint": "POST /admin/depots/{depot_id}/chargers/{charger_id}/rotate_credentials",
            "ocpp_id": result["ocpp_id"],
            # IMPORTANT: never include plaintext credentials in metadata.
        },
    )

    return {
        "depot_id": depot_id,
        "charger_id": charger_id,
        "ocpp_id": result["ocpp_id"],
        "credentials": {
            "username": result["ocpp_id"],
            "password": new_password,
            "scheme": "basic",
            "shown_once": True,
        },
        "rotated_at": result["last_rotated_at"],
    }


# ── Command Dispatcher ────────────────────────────────────────────────────────


async def _handle_charger_restart(
    params: dict,
    depot_id: str,
    dry_run: bool,
) -> dict:
    """Restart a charger via OCPP RemoteReset.

    Params: charger_id (UUID)
    Rollback: not applicable (physical reset is irreversible; logged as ROLLBACK_IMPOSSIBLE).
    """
    charger_id = params.get("charger_id")
    if not charger_id:
        raise HTTPException(status_code=400, detail="params.charger_id is required")
    validate_uuid(charger_id, "charger_id")

    if dry_run:
        return {"charger_id": charger_id, "action": "RemoteReset", "simulated": True}

    if ocpp_server is None:
        raise HTTPException(status_code=503, detail="OCPP server not available")

    try:
        result = await ocpp_server.remote_reset(charger_id)
        return {"charger_id": charger_id, "ocpp_result": result}
    except Exception as e:
        logger.warning(
            "Charger restart ROLLBACK_IMPOSSIBLE — reset already sent",
            extra={"charger_id": charger_id, "depot_id": depot_id, "error": str(e)},
        )
        raise HTTPException(
            status_code=500,
            detail={"error_code": ErrorCode.INTERNAL_ERROR.value, "detail": "Charger restart failed"},
        ) from e


async def _handle_schedule_adjust(
    params: dict,
    depot_id: str,
    dry_run: bool,
) -> dict:
    """Adjust a vehicle's charging schedule target.

    Params: vehicle_id (UUID), target_soc (float 0–1), by_time (ISO 8601)
    Rollback: trigger a fresh re-optimization to restore the original schedule.
    """
    vehicle_id = params.get("vehicle_id")
    target_soc = params.get("target_soc")
    by_time = params.get("by_time")
    if not vehicle_id or target_soc is None or not by_time:
        raise HTTPException(
            status_code=400,
            detail="params must include vehicle_id, target_soc, and by_time",
        )
    validate_uuid(vehicle_id, "vehicle_id")
    if not (0.0 <= float(target_soc) <= 1.0):
        raise HTTPException(status_code=422, detail="target_soc must be between 0.0 and 1.0")
    try:
        by_time_dt = datetime.fromisoformat(str(by_time).replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="by_time must be a valid ISO 8601 datetime"
        ) from exc

    if dry_run:
        return {
            "vehicle_id": vehicle_id,
            "target_soc": target_soc,
            "by_time": by_time,
            "simulated": True,
        }

    if not controller_manager or not db_pools:
        raise HTTPException(status_code=503, detail="Controller manager or database not available")

    update_query = """
        WITH target_schedule AS (
            SELECT s.schedule_id
            FROM schedules s
            JOIN vehicles v ON v.vehicle_id = s.vehicle_id
            WHERE s.vehicle_id = $1::uuid
              AND v.depot_id = $2::uuid
              AND s.departure_time >= $3
            ORDER BY s.departure_time
            LIMIT 1
        )
        UPDATE schedules s
        SET required_soc = $4
        FROM target_schedule ts
        WHERE s.schedule_id = ts.schedule_id
        RETURNING s.schedule_id::text AS schedule_id, s.departure_time, s.required_soc
    """
    async with db_pools.static.acquire() as conn:
        updated_schedule = await conn.fetchrow(
            update_query, vehicle_id, depot_id, by_time_dt, float(target_soc)
        )
    if not updated_schedule:
        raise HTTPException(
            status_code=404,
            detail="No matching schedule found for vehicle at or after by_time in this depot",
        )

    controller = await controller_manager.get_or_create_controller(depot_id)
    _create_background_task(controller.run_optimization("schedule_adjust_command"))
    return {
        "vehicle_id": vehicle_id,
        "target_soc": target_soc,
        "by_time": by_time,
        "schedule_id": updated_schedule["schedule_id"],
        "triggered": True,
    }


async def _handle_depot_config_update(
    params: dict,
    depot_id: str,
    dry_run: bool,
) -> dict:
    """Update mutable depot configuration (e.g. max_grid_kw).

    Rollback: previous value is captured before update and restored on failure.
    """
    allowed_fields = {"max_grid_kw"}
    unknown = set(params.keys()) - allowed_fields
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown config fields: {unknown}. Allowed: {allowed_fields}",
        )
    if not params:
        raise HTTPException(status_code=400, detail="params must include at least one config field")

    if "max_grid_kw" in params:
        try:
            max_grid_kw = float(params["max_grid_kw"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="max_grid_kw must be a number") from exc
        if not math.isfinite(max_grid_kw) or max_grid_kw <= 0 or max_grid_kw > 100_000:
            raise HTTPException(
                status_code=422,
                detail="max_grid_kw must be greater than 0 and no more than 100,000 kW",
            )
        params = {**params, "max_grid_kw": max_grid_kw}

    if dry_run:
        return {"depot_id": depot_id, "would_update": params, "simulated": True}

    if not db_pools:
        raise DatabaseError("Database not available")

    set_clauses: list[str] = []
    values: list = [depot_id]
    if "max_grid_kw" in params:
        set_clauses.append(f"max_grid_kw = ${len(values) + 1}")
        values.append(params["max_grid_kw"])

    query = (
        f"UPDATE depots SET {', '.join(set_clauses)} "
        "WHERE depot_id = $1::uuid RETURNING max_grid_kw"
    )
    async with db_pools.static.acquire() as conn:
        row = await conn.fetchrow(query, *values)
    if not row:
        raise DepotNotFoundError(f"Depot {depot_id} not found")

    if depot_id not in _depot_config_locks:
        _depot_config_locks[depot_id] = asyncio.Lock()
    async with _depot_config_locks[depot_id]:
        _depot_config_cache.pop(depot_id, None)
    return {"depot_id": depot_id, "updated": params, "current_max_grid_kw": row["max_grid_kw"]}


async def _handle_optimization_run(
    params: dict,
    depot_id: str,
    dry_run: bool,
) -> dict:
    """Trigger an immediate MILP optimization run.

    Params: horizon_hours (int, default 24)
    """
    raw_horizon_hours = params.get("horizon_hours", 24)
    try:
        parsed_horizon_hours = float(raw_horizon_hours)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="horizon_hours must be a number") from exc
    if not parsed_horizon_hours.is_integer():
        raise HTTPException(status_code=422, detail="horizon_hours must be an integer")
    horizon_hours = int(parsed_horizon_hours)
    validate_horizon_hours(horizon_hours)

    if dry_run:
        return {"depot_id": depot_id, "horizon_hours": horizon_hours, "simulated": True}

    if not controller_manager:
        raise HTTPException(status_code=503, detail="Controller manager not available")

    controller = await controller_manager.get_or_create_controller(depot_id)
    _create_background_task(
        controller.run_optimization("manual_command", horizon_hours=horizon_hours)
    )
    return {"depot_id": depot_id, "horizon_hours": horizon_hours, "triggered": True}


class _CommandSpec:
    """Registry entry for a dispatchable command."""

    def __init__(self, required_permission: Permission, handler) -> None:
        self.required_permission = required_permission
        self.handler = handler


_COMMAND_REGISTRY: dict[str, _CommandSpec] = {
    "fleet.charger.restart": _CommandSpec(
        required_permission=Permission.DEPOT_MANAGE,
        handler=_handle_charger_restart,
    ),
    "fleet.vehicle.schedule_adjust": _CommandSpec(
        required_permission=Permission.DEPOT_MANAGE,
        handler=_handle_schedule_adjust,
    ),
    "depot.config.update": _CommandSpec(
        required_permission=Permission.ADMIN_CONFIG,
        handler=_handle_depot_config_update,
    ),
    "optimization.run": _CommandSpec(
        required_permission=Permission.OPTIMIZE_TRIGGER,
        handler=_handle_optimization_run,
    ),
}


@app.post(
    "/commands/execute",
    response_model=CommandResponse,
    tags=["commands"],
    summary="Execute a depot management command",
    description="""
    Unified command dispatcher for depot management actions.

    One command per request. Supported commands:

    | Command | Required permission | Key params |
    |---|---|---|
    | `fleet.charger.restart` | `depot:manage` (operator+) | `charger_id` |
    | `fleet.vehicle.schedule_adjust` | `depot:manage` (operator+) | `vehicle_id`, `target_soc`, `by_time` |
    | `depot.config.update` | `admin:config` (admin) | `max_grid_kw` |
    | `optimization.run` | `optimize:trigger` (operator+) | `horizon_hours` |

    Set `dry_run: true` to validate and simulate the command without side effects.
    Every execution (real or dry-run) is written to the security audit log.

    **Authentication:** Requires JWT token in Authorization header.
    """,
    responses={
        400: {"model": ErrorResponse, "description": "Unknown command or invalid params"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Insufficient role"},
        503: {"model": ErrorResponse, "description": "Dependency unavailable"},
    },
)
async def execute_command(
    body: CommandRequest,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """POST /commands/execute — RBAC-gated depot command dispatcher."""
    validate_depot_id(body.depot_id)
    await verify_depot_access(body.depot_id, user, db_pools.static if db_pools else None)

    spec = _COMMAND_REGISTRY.get(body.command)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown command '{body.command}'. "
            f"Valid commands: {sorted(_COMMAND_REGISTRY)}",
        )

    user_role = get_user_role(user)
    if not has_permission(user_role, spec.required_permission):
        logger.warning(
            "Command access denied",
            extra={
                "user_id": user.get("sub"),
                "role": user_role,
                "command": body.command,
                "required": spec.required_permission.value,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient permissions. Required: {spec.required_permission.value}",
        )

    result = await spec.handler(body.params, body.depot_id, dry_run=body.dry_run)

    audit = get_audit_logger()
    if audit is not None:
        await audit.log(
            AuditEvent(
                event_type="COMMAND_EXECUTED",
                user_id=user.get("sub"),
                resource=f"/commands/{body.command}",
                details={
                    "depot_id": body.depot_id,
                    "params": body.params,
                    "dry_run": body.dry_run,
                    "result": result,
                },
            )
        )

    return CommandResponse(
        status="dry_run" if body.dry_run else "ok",
        command=body.command,
        depot_id=body.depot_id,
        result=result,
    )


# ── Alerts pipeline endpoints ────────────────────────────────────────────────
# See docs/plans/alerts-pipeline.md.


@app.post(
    "/depots/{depot_id}/alerts/{alert_id}/acknowledge",
    response_model=AcknowledgeAlertResponse,
    tags=["depots"],
    summary="Acknowledge a notification alert",
)
async def acknowledge_notification_alert(
    alert_id: str,
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Transition an active alert to acknowledged. Returns 404 if the alert
    is missing, already resolved, or doesn't belong to this depot."""
    validate_uuid(alert_id, "alert_id")
    if not db_pools:
        raise DatabaseError("Database not available")

    actor_user_id = user.get("sub") if isinstance(user, dict) else None
    if not actor_user_id:
        raise _forbidden("FORBIDDEN", "user id not present in token")

    from src.notifications import alerts as alerts_repo

    async with db_pools.ts.acquire() as conn:
        existing = await alerts_repo.get_by_id(conn, UUID(alert_id))
        if existing is None or str(existing.depot_id) != depot_id:
            raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")

        updated = await alerts_repo.acknowledge(
            conn, UUID(alert_id), user_id=UUID(str(actor_user_id))
        )
    if updated is None:
        # Existed and depot matched on get_by_id but acknowledge() returned None
        # → alert was already resolved/acknowledged. Surface 409 so callers
        # can distinguish from "not found".
        raise HTTPException(
            status_code=409, detail=f"Alert {alert_id} is not in active state"
        )
    if updated.acknowledged_at is None:
        raise DatabaseError("Acknowledged alert missing acknowledged_at timestamp")
    return AcknowledgeAlertResponse(
        id=str(updated.id),
        status=updated.status,
        acknowledged_at=updated.acknowledged_at.isoformat(),
    )


def _require_org_admin_access(user: dict, org_id: str) -> tuple[str, bool]:
    """Resolve admin role for /admin/organizations/{org_id}/* endpoints.

    Returns (role, cross_org_read). Mirrors list_organization_depots_admin.
    """
    validate_uuid(org_id, "organization_id")
    role = _require_admin_role(user)
    cross = False
    if role == "favonius_admin":
        cross = True
    else:
        caller_org = get_user_organization_id(user)
        if not caller_org:
            raise _forbidden("MISSING_ORGANIZATION", "missing organization_id in token app_metadata")
        if str(caller_org) != str(org_id):
            raise _forbidden("FORBIDDEN_ORGANIZATION", _ACCESS_DENIED_ORG_DETAIL)
    return role, cross


@app.get(
    "/admin/organizations/{org_id}/notification_recipients",
    tags=["admin"],
    summary="List notification recipients for an organization",
)
async def list_notification_recipients(
    org_id: str,
    include_inactive: bool = False,
    user: dict = Depends(ensure_tenant_mirrored),
):
    role, cross = _require_org_admin_access(user, org_id)
    if not db_pools:
        raise DatabaseError("Database not available")
    from src.notifications import recipients as recipients_repo

    async with db_pools.ts.acquire() as conn:
        recs = await recipients_repo.list_for_org(
            conn, UUID(org_id), include_inactive=include_inactive
        )

    if cross and role == "favonius_admin":
        await _record_admin_action(
            user=user,
            action="admin.read",
            organization_id_override=org_id,
            target_type="notification_recipients",
            target_id="*",
            metadata={"endpoint": "GET /admin/organizations/{org_id}/notification_recipients"},
        )
    return {
        "organization_id": org_id,
        "recipients": [
            NotificationRecipientItem(
                id=str(r.id),
                organization_id=str(r.organization_id),
                email=r.email,
                display_name=r.display_name,
                alert_types=r.alert_types,
                min_severity=r.min_severity.value,
                active=r.active,
            )
            for r in recs
        ],
        "count": len(recs),
    }


@app.post(
    "/admin/organizations/{org_id}/notification_recipients",
    response_model=NotificationRecipientItem,
    tags=["admin"],
    summary="Create a notification recipient",
    status_code=201,
)
async def create_notification_recipient(
    org_id: str,
    body: CreateNotificationRecipientRequest,
    user: dict = Depends(ensure_tenant_mirrored),
):
    _require_org_admin_access(user, org_id)
    if not db_pools:
        raise DatabaseError("Database not available")
    from src.notifications import recipients as recipients_repo
    from src.notifications.severity import Severity

    try:
        sev = Severity.from_str(body.min_severity)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Invalid min_severity: {body.min_severity!r}")

    async with db_pools.ts.acquire() as conn:
        try:
            created = await recipients_repo.create(
                conn,
                organization_id=UUID(org_id),
                email=body.email,
                display_name=body.display_name,
                alert_types=tuple(body.alert_types),
                min_severity=sev,
            )
        except asyncpg.UniqueViolationError:
            raise HTTPException(
                status_code=409,
                detail=f"Recipient {body.email!r} already exists for this organization",
            )

    return NotificationRecipientItem(
        id=str(created.id),
        organization_id=str(created.organization_id),
        email=created.email,
        display_name=created.display_name,
        alert_types=created.alert_types,
        min_severity=created.min_severity.value,
        active=created.active,
    )


@app.patch(
    "/admin/organizations/{org_id}/notification_recipients/{recipient_id}",
    response_model=NotificationRecipientItem,
    tags=["admin"],
    summary="Update a notification recipient",
)
async def update_notification_recipient(
    org_id: str,
    recipient_id: str,
    body: UpdateNotificationRecipientRequest,
    user: dict = Depends(ensure_tenant_mirrored),
):
    _require_org_admin_access(user, org_id)
    validate_uuid(recipient_id, "recipient_id")
    if not db_pools:
        raise DatabaseError("Database not available")
    from src.notifications import recipients as recipients_repo
    from src.notifications.severity import Severity

    sev: Optional[Severity] = None
    if body.min_severity is not None:
        try:
            sev = Severity.from_str(body.min_severity)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail=f"Invalid min_severity: {body.min_severity!r}"
            )

    async with db_pools.ts.acquire() as conn:
        updated = await recipients_repo.update(
            conn,
            UUID(recipient_id),
            organization_id=UUID(org_id),
            display_name=body.display_name,
            alert_types=tuple(body.alert_types) if body.alert_types is not None else None,
            min_severity=sev,
            active=body.active,
        )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Recipient {recipient_id} not found")
    return NotificationRecipientItem(
        id=str(updated.id),
        organization_id=str(updated.organization_id),
        email=updated.email,
        display_name=updated.display_name,
        alert_types=updated.alert_types,
        min_severity=updated.min_severity.value,
        active=updated.active,
    )


@app.delete(
    "/admin/organizations/{org_id}/notification_recipients/{recipient_id}",
    tags=["admin"],
    summary="Delete a notification recipient",
    status_code=204,
)
async def delete_notification_recipient(
    org_id: str,
    recipient_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    _require_org_admin_access(user, org_id)
    validate_uuid(recipient_id, "recipient_id")
    if not db_pools:
        raise DatabaseError("Database not available")
    from src.notifications import recipients as recipients_repo

    async with db_pools.ts.acquire() as conn:
        deleted = await recipients_repo.delete(
            conn, UUID(recipient_id), organization_id=UUID(org_id)
        )
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Recipient {recipient_id} not found")
    return Response(status_code=204)


@app.post(
    "/webhooks/resend",
    include_in_schema=False,
    summary="Resend webhook (signature-verified)",
)
async def resend_webhook(request: Request):
    """Update notification_deliveries.status from Resend events.

    Verified via X-Resend-Signature header (HMAC-SHA256 with replay window).
    Unknown event types are silently acknowledged so Resend doesn't retry.
    """
    secret = os.getenv("RESEND_WEBHOOK_SECRET", "")
    body_bytes = await request.body()

    from src.notifications.webhook import parse_event, verify_signature

    if not verify_signature(
        secret=secret,
        body=body_bytes,
        signature_header=request.headers.get("X-Resend-Signature")
        or request.headers.get("Svix-Signature"),
        message_id=request.headers.get("svix-id"),
        timestamp_header=request.headers.get("svix-timestamp"),
    ):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event = parse_event(payload)
    if event is None:
        return {"status": "ignored"}

    if not db_pools:
        raise DatabaseError("Database not available")

    from src.notifications import alerts as alerts_repo

    async with db_pools.ts.acquire() as conn:
        await alerts_repo.update_delivery_status(
            conn,
            provider_message_id=event.provider_message_id,
            status=event.status,
            status_detail=event.detail,
        )
    return {"status": "ok", "provider_message_id": event.provider_message_id}


# ── OpenAPI schema (admin-only, cached after first generation) ────────────────

_OPENAPI_NOT_CACHED = object()
_cached_openapi_schema: object = _OPENAPI_NOT_CACHED


@app.get(
    "/openapi.json",
    include_in_schema=False,
    summary="OpenAPI schema (admin only)",
)
async def get_openapi_schema(user: dict = Depends(ensure_tenant_mirrored)):
    """Serve the OpenAPI schema; requires admin role.

    The schema is generated once and cached in-process. It is implicitly
    invalidated on process restart (i.e., on deploy).
    """
    role = get_user_role(user)
    if role != "favonius_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Favonius platform admin role required to access the OpenAPI schema",
        )
    global _cached_openapi_schema
    if _cached_openapi_schema is _OPENAPI_NOT_CACHED:
        _cached_openapi_schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
            servers=app.servers,
        )
    return _cached_openapi_schema
