"""FastAPI REST API application.

Reference: PRD_v2.md#7-api-specifications
"""

import asyncio
import hashlib
import hmac
import logging
import math
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse
from uuid import UUID

import asyncpg
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

from ..core.controller_manager import ControllerManager
from ..core.models import DepotConfig
from ..core.optimizer.exceptions import InfeasibleModelError, SolverError, SolverTimeoutError
from ..core.state.assembler import StateAssembler
from ..db import queries as db_queries
from ..db.pools import DatabasePools
from ..monitoring.metrics import CONTROLLER_MANAGER_UP
from ..security.audit_log import AuditEvent, AuditLogger, audit_log_event, get_audit_logger, set_audit_logger
from ..security.auth import get_user_role, verify_depot_access, verify_token
from ..security.geo_block import GeoBlockMiddleware
from ..security.headers import SecurityHeadersMiddleware
from ..security.rate_limiter import RateLimiter, get_rate_limiter, set_rate_limiter
from ..security.rbac import Permission, has_permission, require_favonius_admin, require_permission
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
    user: dict = Depends(verify_token),
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
    raise RuntimeError(
        "No database URL configured. Set DATABASE_URL or TIMESCALE_SERVICE_URL."
    )


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
# Empty string disables auth — only acceptable in dev.
_INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")
_environment = os.getenv("ENVIRONMENT", "development")
if _environment == "production" and not _INTERNAL_API_TOKEN:
    raise RuntimeError(
        "INTERNAL_API_TOKEN must be set in production. "
        "The /internal/ocpp-event endpoint is unauthenticated without it."
    )


async def _heartbeat_loop(ts_pool: asyncpg.Pool) -> None:
    """Write optimizer heartbeat to TimescaleDB every 30 s.

    The websocket_handler reads this row to decide whether the main API
    optimizer is alive before falling back to its own heuristic.
    """
    while True:
        try:
            async with ts_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO service_heartbeat (service, last_seen)
                    VALUES ('optimizer', NOW())
                    ON CONFLICT (service) DO UPDATE SET last_seen = NOW()
                    """
                )
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
    allow_methods=["GET", "POST", "PUT", "DELETE"],
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
    """Error response model matching PRD format."""

    detail: str = Field(..., description="Error message")
    error_code: Optional[str] = Field(None, description="Error code for programmatic handling")
    timestamp: str = Field(..., description="Error timestamp (ISO 8601)")


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


class DepotListResponse(BaseModel):
    """Response from GET /me/depots."""

    depots: list[DepotMetadata] = Field(default_factory=list)


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


class OptimizationError(Exception):
    """Raised when optimization fails."""

    pass


class DatabaseError(Exception):
    """Raised when database operation fails."""

    pass


# ============ Exception Handlers ============


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handle Pydantic validation errors."""
    errors = exc.errors()
    error_details = "; ".join(f"{err['loc']}: {err['msg']}" for err in errors)
    logger.warning(f"Validation error on {request.url.path}: {error_details}")
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ErrorResponse(
            detail=f"Validation error: {error_details}",
            error_code="VALIDATION_ERROR",
            timestamp=datetime.utcnow().isoformat(),
        ).model_dump(),
    )


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    """Handle ValueError exceptions (e.g., invalid UUID, depot not found)."""
    error_msg = str(exc)
    if "not found" in error_msg.lower() or isinstance(exc, DepotNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
        error_code = "DEPOT_NOT_FOUND"
    else:
        status_code = status.HTTP_400_BAD_REQUEST
        error_code = "INVALID_INPUT"

    logger.warning(f"ValueError on {request.url.path}: {error_msg}")
    return JSONResponse(
        status_code=status_code,
        content=ErrorResponse(
            detail=error_msg,
            error_code=error_code,
            timestamp=datetime.utcnow().isoformat(),
        ).model_dump(),
    )


@app.exception_handler(OptimizationError)
async def optimization_error_handler(request: Request, exc: OptimizationError) -> JSONResponse:
    """Handle optimization failures."""
    error_msg = str(exc)
    logger.error(f"Optimization error on {request.url.path}: {error_msg}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            detail=f"Optimization failed: {error_msg}",
            error_code="OPTIMIZATION_ERROR",
            timestamp=datetime.utcnow().isoformat(),
        ).model_dump(),
    )


@app.exception_handler(DatabaseError)
async def database_error_handler(request: Request, exc: DatabaseError) -> JSONResponse:
    """Handle database operation failures."""
    error_msg = str(exc)
    logger.error(f"Database error on {request.url.path}: {error_msg}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=ErrorResponse(
            detail=f"Database error: {error_msg}",
            error_code="DATABASE_ERROR",
            timestamp=datetime.utcnow().isoformat(),
        ).model_dump(),
    )


@app.exception_handler(asyncpg.PostgresError)
async def postgres_error_handler(request: Request, exc: asyncpg.PostgresError) -> JSONResponse:
    """Handle PostgreSQL-specific errors."""
    logger.error(f"PostgreSQL error on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=ErrorResponse(
            detail="Database operation failed",
            error_code="DATABASE_ERROR",
            timestamp=datetime.utcnow().isoformat(),
        ).model_dump(),
    )


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
                raise HTTPException(status_code=404, detail=error_msg)
            logger.error(f"Invalid depot configuration: {error_msg}")
            raise HTTPException(
                status_code=500, detail=f"Invalid depot configuration: {error_msg}"
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to load depot config: {e}", exc_info=True)
            raise HTTPException(
                status_code=500, detail=f"Failed to load depot configuration: {str(e)}"
            )


@app.websocket("/ocpp/{charge_point_id}")
async def ocpp_websocket(websocket: WebSocket, charge_point_id: str):
    """OCPP 1.6 WebSocket endpoint (same port as REST when OCPP_USE_SAME_PORT=true)."""
    if not charge_point_id or not charge_point_id.strip():
        await websocket.close(code=4000)
        return
    if ocpp_server is None:
        await websocket.close(code=1011)
        return
    await websocket.accept(subprotocol="ocpp1.6")
    try:
        await ocpp_server.handle_websocket(websocket, charge_point_id.strip())
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error(f"OCPP WebSocket error for {charge_point_id}: {e}", exc_info=True)
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

    **Authentication:** Requires JWT token in Authorization header.
    """,
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def list_my_depots(user: dict = Depends(verify_token)):
    """List depots accessible to the authenticated user."""
    from ..security.auth import get_user_organization_id, is_platform_admin

    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        if is_platform_admin(user):
            async with db_pools.static.acquire() as conn:
                depots = await db_queries.get_all_depots(conn)
            return {"depots": depots}

        role = get_user_role(user)
        if role not in ("customer_admin", "customer_operator"):
            return {"depots": []}

        org_id = get_user_organization_id(user)
        if not org_id:
            return {"depots": []}

        async with db_pools.static.acquire() as conn:
            depots = await db_queries.get_depots_for_organization(conn, org_id)
        return {"depots": depots}
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
            "Database error getting depot metadata: %s", e, exc_info=True,
            extra={"depot_id": depot_id},
        )
        raise DatabaseError(f"Database error: {str(e)}")


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
async def run_optimization(request: OptimizationRequest, user: dict = Depends(verify_token)):
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
        except SolverTimeoutError as e:
            raise OptimizationError(f"Optimization timeout: {e}")
        except InfeasibleModelError as e:
            raise OptimizationError(f"Optimization infeasible: {e}")
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
    user: dict = Depends(verify_token),
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
        raise HTTPException(status_code=500, detail=f"Failed to get depot state: {str(e)}")


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
    user: dict = Depends(verify_token),
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
        raise DatabaseError(f"Database error: {str(e)}")
    except Exception as e:
        logger.error(f"Failed to get schedule: {e}", exc_info=True, extra={"depot_id": depot_id})
        raise HTTPException(status_code=500, detail=f"Failed to get schedule: {str(e)}")


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
    user: dict = Depends(verify_token),
):
    """GET /depots/{depot_id}/alerts — charger faults and last optimization (PRD §7.1)."""
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        # Static data: depot existence check + charger ocpp_id → charger_id map
        async with db_pools.static.acquire() as conn:
            depot_check = await conn.fetchval(
                "SELECT 1 FROM depots WHERE depot_id = $1",
                depot_id,
            )
            if not depot_check:
                raise HTTPException(status_code=404, detail=f"Depot {depot_id} not found")

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
            fault_rows = await conn.fetch(
                """
                SELECT DISTINCT ON (station_id, connector_id)
                    station_id, connector_id, status, error_code, timestamp
                FROM connector_status
                WHERE station_id = ANY($1) AND status = 'Faulted'
                ORDER BY station_id, connector_id, timestamp DESC
                """,
                depot_ocpp_ids,
            ) if depot_ocpp_ids else []

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

        now = datetime.utcnow()
        return AlertsResponse(
            depot_id=depot_id,
            timestamp=now.isoformat() + "Z",
            charger_faults=charger_faults,
            last_optimization=last_optimization,
        )

    except HTTPException:
        raise
    except asyncpg.PostgresError as e:
        logger.error(
            f"Database error getting alerts: {e}",
            exc_info=True,
            extra={"depot_id": depot_id},
        )
        raise DatabaseError(f"Database error: {str(e)}")
    except Exception as e:
        logger.error(
            f"Failed to get alerts: {e}",
            exc_info=True,
            extra={"depot_id": depot_id},
        )
        raise HTTPException(status_code=500, detail=f"Failed to get alerts: {str(e)}")


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
    user: dict = Depends(verify_token),
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

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                receive_url = (
                    f"{dest_depot_endpoint}/depots/{request.dest_depot_id}/handoff/receive"
                )
                nonce = str(uuid4())
                timestamp_str = datetime.utcnow().isoformat()
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
                # Security (H4): HMAC-SHA256 signature for mutual auth
                signing_key = os.getenv("HANDOFF_SIGNING_KEY", "")
                if signing_key:
                    import json as _json

                    payload_bytes = _json.dumps(
                        receive_payload, sort_keys=True
                    ).encode()
                    sig = hmac.new(
                        signing_key.encode(), payload_bytes, hashlib.sha256
                    ).hexdigest()
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
        raise DatabaseError(f"Database error: {str(e)}")
    except Exception as e:
        logger.error(
            f"Failed to send handoff: {e}",
            exc_info=True,
            extra={"depot_id": depot_id, "vehicle_id": vehicle_id},
        )
        raise HTTPException(status_code=500, detail=f"Failed to send handoff: {str(e)}")


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
    depot_id: str, request: HandoffReceiveRequest, user: dict = Depends(verify_token)
):
    """Receive inter-depot handoff message.

    Per PRD Section 5.4, the destination depot:
    1. Validates the request
    2. Stores message in interdepot_messages with status='acknowledged'
    3. Returns acknowledgment with acknowledged_at timestamp

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """
    if not db_pools:
        raise DatabaseError("Database not available")

    # Validate UUIDs
    validate_depot_id(depot_id)
    validate_depot_id(request.origin_depot_id)
    validate_vehicle_id(request.vehicle_id)

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
        raise DatabaseError(f"Database error: {str(e)}")
    except Exception as e:
        logger.error(
            f"Failed to receive handoff: {e}",
            exc_info=True,
            extra={"depot_id": depot_id, "vehicle_id": request.vehicle_id},
        )
        raise HTTPException(status_code=500, detail=f"Failed to receive handoff: {str(e)}")


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

    Not exposed in the public OpenAPI schema. Protected by X-Internal-Token header
    when INTERNAL_API_TOKEN env var is set.
    """
    # Security (M11): timing-safe token comparison to prevent timing attacks
    if _INTERNAL_API_TOKEN:
        token = request.headers.get("X-Internal-Token", "")
        if not secrets.compare_digest(token, _INTERNAL_API_TOKEN):
            raise HTTPException(status_code=401, detail="Unauthorized")
    elif _environment == "production":
        raise HTTPException(status_code=503, detail="Internal endpoint not configured")

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
            extra={"charger_id": charger_id, "depot_id": depot_id},
        )
        raise HTTPException(status_code=500, detail=f"Charger restart failed: {e}") from e


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
        raise HTTPException(status_code=422, detail="by_time must be a valid ISO 8601 datetime") from exc

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
    user: dict = Depends(verify_token),
):
    """POST /commands/execute — RBAC-gated depot command dispatcher."""
    validate_depot_id(body.depot_id)
    await verify_depot_access(body.depot_id, user, db_pools.static if db_pools else None)

    spec = _COMMAND_REGISTRY.get(body.command)
    if spec is None:
        raise HTTPException(
            status_code=400, detail=f"Unknown command '{body.command}'. "
            f"Valid commands: {sorted(_COMMAND_REGISTRY)}"
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


# ── OpenAPI schema (admin-only, cached after first generation) ────────────────

_OPENAPI_NOT_CACHED = object()
_cached_openapi_schema: object = _OPENAPI_NOT_CACHED


@app.get(
    "/openapi.json",
    include_in_schema=False,
    summary="OpenAPI schema (admin only)",
)
async def get_openapi_schema(user: dict = Depends(verify_token)):
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
