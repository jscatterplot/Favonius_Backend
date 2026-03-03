"""FastAPI REST API application.

Reference: PRD_v2.md#7-api-specifications
"""

import asyncio
import logging
import os
import re
import ssl
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional
from uuid import UUID

import asyncpg
import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.base import BaseHTTPMiddleware

from ..core.controller_manager import ControllerManager
from ..core.models import DepotConfig
from ..core.state.assembler import StateAssembler
from ..security.auth import verify_token
from ..security.rate_limiter import rate_limiter

logger = logging.getLogger(__name__)

# Database connection pools
# db_pool → Supabase (static/reference tables: depots, vehicles, chargers, …)
# ts_pool → Timescale (time-series hypertables: telemetry, prices, building_load, …)
db_pool: Optional[asyncpg.Pool] = None
ts_pool: Optional[asyncpg.Pool] = None


def _build_pool_kwargs(url: str) -> tuple[str, dict]:
    """Strip sslmode from *url* and return (clean_url, extra_kwargs) for asyncpg."""
    kwargs: dict = {}
    sslmode_match = re.search(r"[?&]sslmode=([^&#]*)", url)
    if sslmode_match:
        mode = sslmode_match.group(1).lower()
        if mode != "disable":
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kwargs["ssl"] = ctx
        url = re.sub(r"\?sslmode=[^&#]*&?", "?", url)
        url = re.sub(r"&sslmode=[^&#]*", "", url)
        url = url.rstrip("?")
    return url, kwargs


# ============ Input Validation Utilities ============


def validate_uuid(value: str, field_name: str = "id") -> str:
    """Validate UUID format.

    Args:
        value: String to validate
        field_name: Name of the field for error messages

    Returns:
        Validated UUID string

    Raises:
        HTTPException 400: If value is not a valid UUID
    """
    try:
        parsed = UUID(value)
        # Enforce canonical hyphenated lowercase format
        if str(parsed) != value:
            raise ValueError("Non-canonical UUID format")
        return value
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"Invalid {field_name}: must be valid UUID format, got: {value}"
        )


def validate_depot_id(depot_id: str) -> str:
    """Validate depot_id is a valid UUID.

    Args:
        depot_id: Depot identifier to validate

    Returns:
        Validated depot_id

    Raises:
        HTTPException 400: If depot_id is not a valid UUID
    """
    return validate_uuid(depot_id, "depot_id")


def validate_vehicle_id(vehicle_id: str) -> str:
    """Validate vehicle_id is a valid UUID.

    Args:
        vehicle_id: Vehicle identifier to validate

    Returns:
        Validated vehicle_id

    Raises:
        HTTPException 400: If vehicle_id is not a valid UUID
    """
    return validate_uuid(vehicle_id, "vehicle_id")


def validate_horizon_hours(horizon_hours: int) -> int:
    """Validate horizon_hours is in valid range.

    Args:
        horizon_hours: Horizon hours to validate

    Returns:
        Validated horizon_hours

    Raises:
        HTTPException 400: If horizon_hours is out of range
    """
    if not (1 <= horizon_hours <= 48):
        raise HTTPException(
            status_code=400, detail=f"horizon_hours must be between 1 and 48, got: {horizon_hours}"
        )
    return horizon_hours


# Controller manager and OCPP server
controller_manager: Optional[ControllerManager] = None
ocpp_server: Optional[object] = None  # OCPPServer type

# Depot config cache (to reduce database queries)
_depot_config_cache: dict[str, tuple[DepotConfig, float]] = {}  # depot_id -> (config, timestamp)
_config_cache_ttl: float = 300.0  # 5 minutes


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    global db_pool, ts_pool, controller_manager, ocpp_server

    # ── Supabase pool (static/reference tables) ──────────────────────────────
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Set it to a valid PostgreSQL connection string before starting the API."
        )

    try:
        clean_url, ssl_kwargs = _build_pool_kwargs(database_url)
        db_pool = await asyncpg.create_pool(
            clean_url,
            min_size=2,
            max_size=10,
            # Supabase Supavisor does not support prepared statements.
            statement_cache_size=0,
            **ssl_kwargs,
        )
        logger.info("Supabase (static) connection pool initialized")
    except Exception as e:
        logger.error(f"Failed to initialize database pool: {e}")
        raise

    # ── Timescale pool (time-series hypertables) ──────────────────────────────
    timescale_url = os.getenv("TIMESCALE_SERVICE_URL")
    if timescale_url:
        try:
            ts_clean_url, ts_ssl_kwargs = _build_pool_kwargs(timescale_url)
            ts_pool = await asyncpg.create_pool(
                ts_clean_url,
                min_size=2,
                max_size=10,
                statement_cache_size=0,
                **ts_ssl_kwargs,
            )
            logger.info("Timescale (time-series) connection pool initialized")
        except Exception as e:
            # Non-fatal: fall back to Supabase pool for time-series reads/writes.
            logger.warning(f"Timescale pool unavailable, falling back to Supabase pool: {e}")
            ts_pool = None

    # Initialize OCPP server (if enabled)
    ocpp_enabled = os.getenv("OCPP_SERVER_ENABLED", "false").lower() == "true"
    ocpp_use_same_port = os.getenv("OCPP_USE_SAME_PORT", "false").lower() == "true"
    if ocpp_enabled and db_pool:
        try:
            from ..adapters.ocpp.server import OCPPServer

            ocpp_host = os.getenv("OCPP_SERVER_HOST", "0.0.0.0")
            ocpp_port = int(os.getenv("OCPP_SERVER_PORT", "9000"))

            ocpp_server = OCPPServer(
                host=ocpp_host,
                port=ocpp_port,
                pool=db_pool,
                ts_pool=ts_pool,
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

    # Initialize controller manager
    if db_pool:
        try:
            controller_manager = ControllerManager(
                pool=db_pool,
                ts_pool=ts_pool,
                ocpp_server=ocpp_server,
            )

            # Start all controllers
            await controller_manager.start_all_controllers()
            logger.info("Controller manager initialized and controllers started")
        except Exception as e:
            logger.error(f"Failed to initialize controller manager: {e}", exc_info=True)
            controller_manager = None

    yield

    # Graceful shutdown
    logger.info("Shutting down application...")

    # Stop all controllers
    if controller_manager:
        try:
            await controller_manager.stop_all_controllers()
            logger.info("All controllers stopped")
        except Exception as e:
            logger.error(f"Error stopping controllers: {e}", exc_info=True)

    # Stop OCPP server
    if ocpp_server:
        try:
            await ocpp_server.stop()
            logger.info("OCPP server stopped")
        except Exception as e:
            logger.error(f"Error stopping OCPP server: {e}", exc_info=True)

    # Close database connection pools
    if ts_pool:
        await ts_pool.close()
        logger.info("Timescale connection pool closed")
    if db_pool:
        await db_pool.close()
        logger.info("Supabase connection pool closed")


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

# ============ Rate Limiting Middleware ============


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware to enforce rate limits per PRD Section 10.4."""

    async def dispatch(self, request: Request, call_next):
        """Check rate limits before processing request."""
        # Skip rate limiting for health and metrics endpoints
        if request.url.path in ["/health", "/metrics"]:
            return await call_next(request)

        # Extract client identifier (IP address or API key from header)
        client_id = request.client.host if request.client else "unknown"

        # Check for API key in header (if available)
        api_key = request.headers.get("X-API-Key")
        if api_key:
            client_id = f"api_key:{api_key}"

        # Apply different rate limits based on endpoint
        path = request.url.path

        # POST /optimize: 10 requests/minute
        if path == "/optimize" and request.method == "POST":
            if not rate_limiter.check_optimize_limit(client_id):
                return JSONResponse(
                    status_code=429,
                    content=ErrorResponse(
                        detail="Rate limit exceeded: Maximum 10 optimization requests per minute",
                        error_code="RATE_LIMIT_EXCEEDED",
                        timestamp=datetime.utcnow().isoformat(),
                    ).model_dump(),
                )

        # Inter-depot handoff: 50 messages/hour per depot pair
        elif "/handoff" in path:
            # For handoff endpoints, we need to extract depot IDs from the request
            # This is done after rate limit check for send_handoff and receive_handoff endpoints
            # For now, apply general API limit, then check handoff limit in endpoint handlers
            # (handoff limit requires reading request body which is not available in middleware)
            pass  # Handoff rate limiting handled in endpoint handlers

        # General API endpoints: 100 requests/minute
        else:
            if not rate_limiter.check_api_limit(client_id):
                return JSONResponse(
                    status_code=429,
                    content=ErrorResponse(
                        detail="Rate limit exceeded: Maximum 100 requests per minute",
                        error_code="RATE_LIMIT_EXCEEDED",
                        timestamp=datetime.utcnow().isoformat(),
                    ).model_dump(),
                )

        return await call_next(request)


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


# Add rate limiting middleware (before logging to catch rate limits early)
app.add_middleware(RateLimitMiddleware)

# Add logging middleware
app.add_middleware(LoggingMiddleware)

# Add CORS middleware
cors_origins = os.getenv("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins if "*" not in cors_origins else ["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)


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
    chargers, and battery_storage tables. Implements caching to reduce
    database queries.

    Args:
        depot_id: Depot identifier (must be valid UUID)

    Returns:
        DepotConfig object

    Raises:
        HTTPException 404: If depot not found
        HTTPException 500: If configuration is invalid
        HTTPException 503: If database is not available
    """
    if not db_pool:
        raise HTTPException(status_code=503, detail="Database not available")

    # Check cache first
    current_time = time.time()
    if depot_id in _depot_config_cache:
        config, cache_time = _depot_config_cache[depot_id]
        if current_time - cache_time < _config_cache_ttl:
            logger.debug(f"Using cached config for depot {depot_id}")
            return config
        else:
            # Cache expired, remove it
            del _depot_config_cache[depot_id]

    try:
        # Load from database using StateAssembler
        config, _ = await StateAssembler.load_depot_config(db_pool, depot_id)

        # Validate minimum requirements
        if not config.vehicle_capacities:
            raise HTTPException(
                status_code=400, detail=f"Depot {depot_id} has no vehicles configured"
            )
        if config.n_chargers == 0:
            logger.warning(f"Depot {depot_id} has no chargers configured, " "optimization may fail")

        # Cache the config
        _depot_config_cache[depot_id] = (config, current_time)
        logger.info(
            f"Loaded depot config for {depot_id}: "
            f"{len(config.vehicle_capacities)} vehicles, "
            f"{config.n_chargers} chargers"
        )

        return config

    except ValueError as e:
        # Depot not found or invalid configuration
        error_msg = str(e)
        if "not found" in error_msg.lower():
            logger.warning(f"Depot not found: {depot_id}")
            raise HTTPException(status_code=404, detail=error_msg)
        else:
            logger.error(f"Invalid depot configuration: {error_msg}")
            raise HTTPException(status_code=500, detail=f"Invalid depot configuration: {error_msg}")
    except Exception as e:
        logger.error(f"Failed to load depot config: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to load depot configuration: {str(e)}")


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
    if not db_pool:
        raise DatabaseError("Database not available")

    # Input validation (Pydantic handles most, but we add explicit checks)
    validate_depot_id(request.depot_id)
    validate_horizon_hours(request.horizon_hours)

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
            result = await controller.run_optimization("api_request")
        except Exception as opt_error:
            # Handle optimization-specific errors
            error_msg = str(opt_error)
            if "timeout" in error_msg.lower() or "time limit" in error_msg.lower():
                raise OptimizationError(f"Optimization timeout: {error_msg}")
            elif "infeasible" in error_msg.lower():
                raise OptimizationError(f"Optimization infeasible: {error_msg}")
            else:
                raise OptimizationError(f"Optimization failed: {error_msg}")

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
async def get_depot_state(depot_id: str, user: dict = Depends(verify_token)):
    """Get current depot state.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """
    if not db_pool:
        raise DatabaseError("Database not available")

    # Validate depot_id format
    validate_depot_id(depot_id)

    try:
        # Get depot config (raises 404 if depot not found)
        config = await _get_depot_config(depot_id)

        # Assemble state
        assembler = StateAssembler(db_pool, depot_id, config, ts_pool=ts_pool)
        state = await assembler.get_current_state(24)

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
async def get_depot_schedule(depot_id: str, user: dict = Depends(verify_token)):
    """Get current charging schedule.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """
    if not db_pool:
        raise DatabaseError("Database not available")

    # Validate depot_id format
    validate_depot_id(depot_id)

    try:
        # Get latest optimization result from database
        query = """
        SELECT run_id, run_time, schedule_json, horizon_start, horizon_end
        FROM optimization_runs
        WHERE depot_id = $1
        ORDER BY run_time DESC
        LIMIT 1
        """
        async with db_pool.acquire() as conn:
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
async def get_depot_alerts(depot_id: str, user: dict = Depends(verify_token)):
    """GET /depots/{depot_id}/alerts — charger faults and last optimization (PRD §7.1)."""
    if not db_pool:
        raise DatabaseError("Database not available")

    validate_depot_id(depot_id)

    try:
        async with db_pool.acquire() as conn:
            # Check depot exists (e.g. via depots or chargers)
            depot_check = await conn.fetchval(
                "SELECT 1 FROM depots WHERE depot_id = $1",
                depot_id,
            )
            if not depot_check:
                raise HTTPException(status_code=404, detail=f"Depot {depot_id} not found")

            # Last optimization
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

            # Active charger faults: latest status per connector, join chargers for depot
            faults_query = """
            WITH latest AS (
                SELECT DISTINCT ON (station_id, connector_id)
                    station_id, connector_id, status, error_code, timestamp
                FROM connector_status
                ORDER BY station_id, connector_id, timestamp DESC
            )
            SELECT c.charger_id, c.ocpp_id, l.connector_id,
                   COALESCE(l.error_code, 'Unknown') AS fault_code, l.timestamp
            FROM latest l
            JOIN chargers c ON c.ocpp_id = l.station_id AND c.depot_id = $1
            WHERE l.status = 'Faulted'
            """
            fault_rows = await conn.fetch(faults_query, depot_id)

        charger_faults = [
            ChargerFaultItem(
                charger_id=str(r["charger_id"]),
                ocpp_id=r["ocpp_id"],
                connector_id=r["connector_id"],
                fault_code=r["fault_code"],
                timestamp=(
                    r["timestamp"].isoformat()
                    if isinstance(r["timestamp"], datetime)
                    else str(r["timestamp"])
                ),
            )
            for r in fault_rows
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
    depot_id: str, vehicle_id: str, request: HandoffRequest, user: dict = Depends(verify_token)
):
    """Send inter-depot handoff message.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """
    if not db_pool:
        raise DatabaseError("Database not available")

    # Validate UUIDs
    validate_depot_id(depot_id)
    validate_vehicle_id(vehicle_id)
    validate_depot_id(request.dest_depot_id)

    # Check handoff rate limit per PRD Section 10.4 (50 messages/hour per depot pair)
    if not rate_limiter.check_handoff_limit(depot_id, request.dest_depot_id):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded: Maximum 50 handoff messages per hour per depot pair",
        )

    try:
        from uuid import uuid4

        message_id = uuid4()
        departure_time = datetime.utcnow()

        # Get vehicle details for handoff message
        vehicle_query = """
        SELECT external_id, battery_kwh, max_charge_kw
        FROM vehicles
        WHERE vehicle_id = $1 AND depot_id = $2
        """
        async with db_pool.acquire() as conn:
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

        # Store message in database with status='pending'
        query = """
        INSERT INTO interdepot_messages
            (message_id, origin_depot_id, dest_depot_id, vehicle_id,
             departure_time, expected_soc, arrival_time, battery_kwh, max_charge_kw, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        """
        async with db_pool.acquire() as conn:
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

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                receive_url = (
                    f"{dest_depot_endpoint}/depots/{request.dest_depot_id}/handoff/receive"
                )
                receive_payload = {
                    "message_id": str(message_id),
                    "origin_depot_id": depot_id,
                    "vehicle_id": vehicle_id,
                    "external_id": external_id,
                    "expected_soc": request.expected_soc,
                    "arrival_time": request.arrival_time.isoformat(),
                    "battery_kwh": battery_kwh,
                    "max_charge_kw": max_charge_kw,
                }
                response = await client.post(receive_url, json=receive_payload)
                response.raise_for_status()
                ack_data = response.json()

                # Update message status to 'acknowledged'
                update_query = """
                UPDATE interdepot_messages
                SET status = 'acknowledged', acknowledged_at = $1
                WHERE message_id = $2
                """
                async with db_pool.acquire() as conn:
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
    if not db_pool:
        raise DatabaseError("Database not available")

    # Validate UUIDs
    validate_depot_id(depot_id)
    validate_depot_id(request.origin_depot_id)
    validate_vehicle_id(request.vehicle_id)

    # Validate SoC range
    if not (0.0 <= request.expected_soc <= 1.0):
        raise HTTPException(
            status_code=400,
            detail=f"expected_soc must be between 0.0 and 1.0, got {request.expected_soc}",
        )

    # Check handoff rate limit per PRD Section 10.4 (50 messages/hour per depot pair)
    if not rate_limiter.check_handoff_limit(request.origin_depot_id, depot_id):
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
        async with db_pool.acquire() as conn:
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

        # Store message in database with status='acknowledged'
        query = """
        INSERT INTO interdepot_messages
            (message_id, origin_depot_id, dest_depot_id, vehicle_id,
             departure_time, expected_soc, arrival_time, battery_kwh,
             max_charge_kw, status, acknowledged_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        """
        async with db_pool.acquire() as conn:
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


async def check_database_health() -> str:
    """Check database connectivity.

    Returns:
        "healthy" if database is accessible, "unavailable" otherwise
    """
    if not db_pool:
        return "unavailable"

    try:
        async with db_pool.acquire() as conn:
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

    # Database unavailability means every real endpoint returns 503; the pod is
    # non-functional.  Signal this to load balancers and Railway probes by
    # returning 503 so they stop routing traffic here.
    # A missing Gurobi license is not fatal (HiGHS fallback) so we stay 200 in
    # that case and let the "degraded" status field surface the issue.
    overall_status = "healthy" if db_status == "healthy" else "degraded"

    if db_status != "healthy":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": overall_status,
        "timestamp": datetime.utcnow().isoformat(),
        "components": {
            "database": db_status,
            "ocpp_server": ocpp_status,
            "gurobi_license": gurobi_status,
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
async def list_controllers(user: dict = Depends(verify_token)):
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
async def get_controller_health(depot_id: str, user: dict = Depends(verify_token)):
    """Get controller health status."""
    if not controller_manager:
        raise HTTPException(status_code=503, detail="Controller manager not initialized")

    validate_depot_id(depot_id)

    health = await controller_manager.health_check()

    if depot_id not in health:
        raise HTTPException(status_code=404, detail=f"Controller not found for depot {depot_id}")

    return {
        "depot_id": depot_id,
        **health[depot_id],
    }
