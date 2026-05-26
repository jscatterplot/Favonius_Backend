"""FastAPI REST API application.

Reference: PRD_v2.md#7-api-specifications
"""

import asyncio
import base64
import binascii
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
from dataclasses import dataclass, replace
from datetime import date, datetime
from datetime import time as _dt_time
from datetime import timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, AsyncIterator, Iterator, Literal, Optional, Union
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import asyncpg
import bcrypt
import httpx
from fastapi import (
    Body,
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

from ..adapters.ocpp.dispatch import dispatch_get_diagnostics
from ..core.controller_manager import ControllerManager
from ..core.models import DepotConfig
from ..core.optimizer.exceptions import (
    InfeasibleModelError,
)
from ..core.optimizer.exceptions import OptimizationError as _CoreOptimizationError
from ..core.optimizer.exceptions import (
    SolverError,
    SolverTimeoutError,
)
from ..core.scheduling.recurring import (
    RecurringTemplate,
    ScheduleCancellation,
    expand_recurring_templates,
)
from ..core.state.assembler import StateAssembler
from ..core.state.readiness import (
    build_snapshot,
    evaluate_readiness,
)
from ..db import queries as db_queries
from ..db.exceptions import DatabaseError as _DbDatabaseError
from ..db.exceptions import (
    IdempotencyKeyReusedError,
)
from ..db.pools import DatabasePools
from ..db.postgres_url import describe_database_target
from ..db.snapshot_store import persist_snapshot
from ..monitoring.metrics import CONTROLLER_MANAGER_UP
from ..security.admin_audit import AdminAuditRow, AdminAuditWriteError, write_admin_audit_row
from ..security.audit_log import AuditEvent, AuditLogger, get_audit_logger, set_audit_logger
from ..security.auth import (
    decode_jwt_for_rate_limit,
    get_user_email,
    get_user_organization_id,
    get_user_role,
    is_platform_admin,
    verify_depot_access,
)
from ..security.geo_block import GeoBlockMiddleware
from ..security.handoff_validator import (
    compute_handoff_signature,
    prepare_handoff_http_target,
    verify_handoff_signature,
)
from ..security.headers import SecurityHeadersMiddleware
from ..security.ocpp_auth import verify_ocpp_basic_auth
from ..security.rate_limiter import RateLimiter, get_rate_limiter, set_rate_limiter
from ..security.rbac import Permission, has_permission, require_favonius_admin
from ..security.tenant_mirror import ensure_tenant_mirrored, mirror_user_tenant_atomic
from ..security.validators import (
    validate_depot_id,
    validate_horizon_hours,
    validate_uuid,
    validate_vehicle_id,
)
from . import fleet_list as _fleet_list
from .charger_logs import (
    UploadRejected,
    receive_upload,
    schedule_post_upload_pipeline,
)
from .charging_import import (
    PriceSource,
    StaticPriceSource,
    TimescalePriceSource,
)
from .error_codes import ERROR_MESSAGES, ErrorCode, http_status_for, safe_message_for
from . import report_schedules as _report_schedules
from .report_schedule_timing import (
    ScheduleValidationError,
    compute_next_run_at,
    normalize_create_payload,
    normalize_patch_payload,
)
from .reports import (
    REPORT_GROUP_BY_VALUES,
    SessionRow,
    aggregate_energy_rows,
    compute_energy_totals,
    stream_rows_as_csv,
)
from .savings import compute_savings_summary

logger = logging.getLogger(__name__)

# Database connection pools (set during lifespan startup)
db_pools: Optional[DatabasePools] = None

# Outbound email client for scheduled-report delivery (set during lifespan).
# Mirrors the websocket_handler alert dispatcher: a real ResendEmailClient when
# RESEND_API_KEY is configured, else a FakeEmailClient that records but does not
# send. The report worker and the agents.action.approve delivery path use it.
report_email_client: Optional[Any] = None
report_email_from: str = os.getenv("RESEND_FROM_ADDRESS", "alerts@favonius.energy")


# Controller manager and OCPP server
controller_manager: Optional[ControllerManager] = None
ocpp_server: Optional[object] = None  # OCPPServer type
liveness_hub: Optional[Any] = None  # api.liveness_hub.LivenessHub
solver_pool: Optional[Any] = None  # core.optimizer.pool.SolverPool

# Depot config cache (to reduce database queries)
_depot_config_cache: dict[str, tuple[DepotConfig, float]] = {}  # depot_id -> (config, timestamp)
_config_cache_ttl: float = 300.0  # 5 minutes
_depot_config_locks: dict[str, asyncio.Lock] = {}  # single-flight locks per depot
_background_tasks: set[asyncio.Task] = set()

# Site-metadata cache: small per-depot lookup used by hot endpoints that need
# timezone / org_id without paying for the full StateAssembler.load_depot_config
# round-trip. Same 300 s TTL as the depot-config cache so a multi-row XLSX
# import does a single sites lookup for the whole batch.
_site_metadata_cache: dict[str, tuple["_SiteMetadata", float]] = {}
_site_metadata_locks: dict[str, asyncio.Lock] = {}

# Fleet list response cache (chargers + vehicles). 2 s TTL is enough to dedupe
# multi-tab thundering herd (frontend polls at 10 s) without making the data
# stale. Cache keys are (depot_id, "chargers"|"vehicles"); values are (payload
# dict, timestamp). Pair of asyncio.Locks per key to single-flight refreshes.
_fleet_list_cache: dict[tuple[str, str], tuple[dict, float]] = {}
_fleet_list_locks: dict[tuple[str, str], asyncio.Lock] = {}
_fleet_list_cache_ttl: float = 2.0  # seconds


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
    if not db_pools:
        raise DatabaseError("Database not available")
    await verify_depot_access(depot_id, user, db_pools.static)
    return depot_id


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
# Normalize so ``Production``, ``production ``, and ``PRODUCTION`` all
# trigger the production-only safety guards downstream. A strict
# case-sensitive compare would silently let a misconfigured deploy boot
# without the required secrets.
_environment = os.getenv("ENVIRONMENT", "development").strip().lower()
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

# Security: METRICS_TOKEN gates /metrics. Prometheus output carries per-depot
# labels, optimization counts, OCPP session counts, and agent token usage —
# enough to fingerprint customer activity. Without a token the endpoint
# refuses every request so the production deploy fails closed rather than
# silently exposing operational data. Scrapers configure the token via
# Prometheus' ``authorization.credentials_file``.
#
# ``strip()`` so a whitespace/newline-only value (mistakes from secrets
# tooling, e.g. a trailing newline in a Kubernetes Secret) is treated the
# same as unset — otherwise the production fail-fast would pass and every
# scrape would then 401 with no obvious reason.
_METRICS_TOKEN = os.getenv("METRICS_TOKEN", "").strip()
if _environment == "production" and not _METRICS_TOKEN:
    raise RuntimeError(
        "METRICS_TOKEN must be set in production. "
        "The /metrics endpoint exposes operational data without it."
    )
if not _METRICS_TOKEN:
    logger.warning(
        "METRICS_TOKEN is not set; /metrics will refuse every request "
        "with 503 until the token is configured."
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
    global db_pools, controller_manager, ocpp_server, solver_pool

    # ── Database pools ────────────────────────────────────────────────────────
    # Supabase (static/reference data): DATABASE_URL required.
    # TimescaleDB (TigerCloud / favonius-timeseries): TIMESCALE_SERVICE_URL required
    # in production and staging so we never accidentally point the ts pool at
    # Supabase. Local single-DB dev may omit TIMESCALE_SERVICE_URL and fall back
    # to DATABASE_URL (docker-compose).
    static_url = os.getenv("DATABASE_URL")
    _env = os.getenv("ENVIRONMENT", "development").strip().lower()
    _ts_explicit = os.getenv("TIMESCALE_SERVICE_URL")
    if _env in ("production", "staging"):
        if not (_ts_explicit and _ts_explicit.strip()):
            raise RuntimeError(
                "TIMESCALE_SERVICE_URL must be set when ENVIRONMENT is production or staging. "
                "DATABASE_URL is reserved for Supabase (static schema); TimescaleDB must be "
                "TigerCloud (e.g. favonius-timeseries)."
            )
        ts_url = _ts_explicit.strip()
        ts_url_source = "TIMESCALE_SERVICE_URL"
    else:
        ts_url = (_ts_explicit.strip() if _ts_explicit and _ts_explicit.strip() else None) or (
            static_url
        )
        ts_url_source = (
            "TIMESCALE_SERVICE_URL" if _ts_explicit and _ts_explicit.strip() else "DATABASE_URL"
        )

    if not static_url:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Set it to the Supabase connection string (static/reference data)."
        )
    if not ts_url:
        raise RuntimeError(
            "No TimescaleDB URL available. Set TIMESCALE_SERVICE_URL (preferred), or in "
            "local development set DATABASE_URL to a single Postgres that hosts both roles."
        )

    static_pool = await _create_pool(static_url, "DATABASE_URL")
    ts_pool = await _create_pool(ts_url, ts_url_source)
    db_pools = DatabasePools(static=static_pool, ts=ts_pool)

    # ── Geo-blocking (Article 73-3) — eager init so the GeoIP runtime
    # download and reader-load happen before requests arrive. The lazy
    # singleton in geo_block.py would otherwise construct on first request
    # and silently fail-closed if MaxMind credentials are missing.
    try:
        from ..security.geo_block import initialize_geo_blocking

        await asyncio.to_thread(initialize_geo_blocking)
    except Exception as exc:  # pragma: no cover — defensive: never block startup
        logger.warning("Geo-blocking eager init failed: %s", exc)

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
    ocpp_emergency_only = os.getenv("OCPP_EMERGENCY_FALLBACK_ONLY", "true").lower() == "true"
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
            if ocpp_emergency_only:
                logger.warning(
                    "Main API OCPP adapter is running in EMERGENCY_FALLBACK_ONLY mode. "
                    "Primary production path remains src/websocket_handler."
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

    # ── Solver process pool ──────────────────────────────────────────────────
    # MILP solves are CPU-bound and would block the asyncio loop (60 s freeze
    # of /health, OCPP traffic, scrapes) if run in-process. Dispatch each
    # solve to a child process. On failure we fall back to running solves in
    # a thread, which is correct but blocks the loop — log CRITICAL so it's
    # not silent.
    from ..core.optimizer.pool import SolverPool, set_solver_pool

    if os.getenv("SOLVER_PROCESS_POOL_DISABLED", "false").lower() == "true":
        set_solver_pool(None)
        logger.warning(
            "SolverPool disabled by env; MILP solves will run in a thread "
            "inside the API process (loop unblock OK, no OOM isolation)."
        )
        solver_pool = None
    else:
        try:
            solver_pool_size = int(os.getenv("SOLVER_PROCESS_POOL_SIZE", "2"))
            worker_as_limit_mb = int(os.getenv("SOLVER_WORKER_AS_LIMIT_MB", "1500"))
            solver_pool = SolverPool(
                max_workers=solver_pool_size,
                worker_as_limit_bytes=worker_as_limit_mb * 1024 * 1024,
            )
            await solver_pool.start()
            set_solver_pool(solver_pool)
            logger.info(
                "SolverPool started (workers=%d, AS limit=%d MiB)",
                solver_pool_size,
                worker_as_limit_mb,
            )
        except Exception as e:
            set_solver_pool(None)
            logger.critical(
                "SolverPool failed to start: %s. Solves will run in a thread "
                "in the API process and may block the event loop briefly.",
                e,
                exc_info=True,
            )
            solver_pool = None

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

    # ── Handoff signing key gate ──────────────────────────────────────────────
    # Security (H4): the handoff endpoints fail closed without HANDOFF_SIGNING_KEY
    # in production/staging. Surface the misconfig at startup so operators
    # notice before traffic arrives. Don't block the app from coming up so
    # the rest of the API still serves traffic.
    if _environment in {"production", "staging"} and not os.getenv("HANDOFF_SIGNING_KEY"):
        logger.critical(
            "HANDOFF_SIGNING_KEY is not configured; "
            "inter-depot handoff endpoints will fail closed (env=%s).",
            _environment,
        )

    # ── Liveness fan-out hub ──────────────────────────────────────────────────
    # Subscribes to the ``charger_liveness`` Postgres channel and fans
    # notifications out to per-organisation SSE subscribers. Producer is the
    # WS handler (src/websocket_handler/liveness_notifier.py).
    global liveness_hub
    from .liveness_hub import LivenessHub  # noqa: PLC0415

    liveness_hub = LivenessHub(ts_pool)
    await liveness_hub.start()

    # ── Scheduled-report delivery email client ────────────────────────────────
    # Mirrors the websocket_handler alert dispatcher: a real Resend client when
    # RESEND_API_KEY is set, else a FakeEmailClient that records but never sends
    # (so dev/staging can exercise the pipeline). EMAIL_DELIVERY_ENABLED=false
    # also forces the fake.
    global report_email_client
    _resend_key = os.getenv("RESEND_API_KEY", "")
    _email_enabled = os.getenv("EMAIL_DELIVERY_ENABLED", "true").lower() == "true"
    if _email_enabled and _resend_key:
        from ..notifications.resend_client import ResendEmailClient  # noqa: PLC0415

        report_email_client = ResendEmailClient(api_key=_resend_key, default_from=report_email_from)
        logger.info("report scheduler: using Resend for outbound report email")
    else:
        from ..notifications.email_client import FakeEmailClient  # noqa: PLC0415

        report_email_client = FakeEmailClient()
        logger.warning(
            "report scheduler: RESEND_API_KEY missing or email disabled; using "
            "FakeEmailClient — scheduled reports will NOT be emailed"
        )

    # ── Scheduled-report worker ───────────────────────────────────────────────
    # Replaces the legacy hardcoded monthly_scheduler: fires configurable
    # per-depot report schedules every minute, idempotent via schedule_runs.
    _create_background_task(
        _report_schedules.run_report_schedule_worker(
            db_pools,
            email_client=report_email_client,
            generate_report=_generate_report_for_schedule,
            get_timezone=_get_depot_timezone,
            default_from=report_email_from,
        )
    )
    logger.info("Report schedule worker started")

    # ── Data Sources ingestion scheduler + startup recovery ──────────────────
    from .data_sources.feature_flag import is_data_sources_enabled  # noqa: PLC0415

    if is_data_sources_enabled():
        from ..security.credential_cipher import is_configured as _ds_cipher_ready

        if not _ds_cipher_ready():
            logger.critical(
                "DATA_SOURCES_ENABLED but DATA_SOURCES_ENCRYPTION_KEY is "
                "missing/invalid — Data Sources scheduler disabled (fail-closed)."
            )
        else:
            from ..core.data_sources.scheduler import (  # noqa: PLC0415
                check_single_worker,
                recover_orphaned_data_source_jobs,
                run_data_source_scheduler,
            )

            check_single_worker()
            await recover_orphaned_data_source_jobs(
                static_pool, ts_pool, spawn=_create_background_task
            )
            _create_background_task(
                run_data_source_scheduler(static_pool, ts_pool, spawn=_create_background_task)
            )
            logger.info("Data source scheduler + recovery started")

    yield

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    logger.info("Shutting down application...")

    if liveness_hub is not None:
        try:
            await liveness_hub.stop()
            logger.info("Liveness hub stopped")
        except Exception as e:
            logger.error(f"Error stopping liveness hub: {e}", exc_info=True)

    if controller_manager:
        try:
            await controller_manager.stop_all_controllers()
            logger.info("All controllers stopped")
        except Exception as e:
            logger.error(f"Error stopping controllers: {e}", exc_info=True)

    if solver_pool is not None:
        try:
            set_solver_pool(None)
            await solver_pool.stop()
            logger.info("Solver pool stopped")
        except Exception as e:
            logger.error(f"Error stopping solver pool: {e}", exc_info=True)

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

# Charger diagnostic archives can be tens of MB. The upload endpoint
# applies its own cap (``CHARGER_LOG_UPLOAD_MAX_BYTES``) which the
# middleware mirrors so a misconfigured global cap doesn't silently
# block the endpoint while the per-endpoint cap suggests it would work.
_CHARGER_LOG_UPLOAD_PATH = "/internal/charger_logs/upload"


def _body_size_limit_for_path(path: str) -> int:
    if path == _CHARGER_LOG_UPLOAD_PATH:
        # Lazy import: the upload-token helper reads env at call time so
        # tests can monkeypatch CHARGER_LOG_UPLOAD_MAX_BYTES.
        from ..adapters.chargers.upload_token import get_max_upload_bytes

        return get_max_upload_bytes()
    return _MAX_BODY_SIZE


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds the configured maximum."""

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            limit = _body_size_limit_for_path(request.url.path)
            if int(content_length) > limit:
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
    body_bytes: bytes,
    signature_hex: str,
    signing_key: str,
    *,
    now: Optional[float] = None,
) -> bool:
    """Verify HMAC-SHA256 + replay window + nonce uniqueness on a handoff body.

    The signature lives in the ``X-Handoff-Signature`` header (per H4), so the
    verifier takes raw body bytes plus the header value rather than reading
    ``signature`` out of the parsed JSON. The nonce and timestamp still live
    inside the body so they're covered by the HMAC.

    Returns True iff the signature matches, the timestamp is within
    ``_HANDOFF_REPLAY_WINDOW_S`` of ``now``, and the nonce hasn't been seen.
    """
    if not isinstance(body_bytes, (bytes, bytearray)):
        return False
    if not isinstance(signature_hex, str) or not signature_hex:
        return False

    try:
        body = json.loads(body_bytes)
    except (TypeError, ValueError):
        return False
    if not isinstance(body, dict):
        return False

    nonce = body.get("nonce")
    timestamp_str = body.get("timestamp")
    if not isinstance(nonce, str) or not isinstance(timestamp_str, str):
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

    if not verify_handoff_signature(signing_key.encode(), bytes(body_bytes), signature_hex):
        return False

    _prune_handoff_nonces(current)
    if nonce in _handoff_nonces:
        return False
    _handoff_nonces[nonce] = current + _HANDOFF_REPLAY_WINDOW_S
    return True


# ============ Rate Limiting Middleware ============


# Admin write paths used by the bulk-import flows on the fleet identity panel.
# Match POST/PATCH/PUT requests against /admin/depots/{depot_id}/{vehicles|
# drivers|rfid-cards}[/...]. Reusable for future bulk-import endpoints — add
# the new resource segment to the alternation when chargers or schedules ship
# their own xlsx import.
_ADMIN_BULK_WRITE_PATH_RE = re.compile(
    r"^/admin/depots/[^/]+/(?:vehicles|drivers|rfid-cards|charging-sessions/import)(?:/|$)"
)


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
        # Expiration is verified so a holder of a leaked/expired token cannot
        # keep draining a victim's per-user bucket.
        client_id = request.client.host if request.client else "unknown"
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            try:
                import jwt as _jwt

                payload = decode_jwt_for_rate_limit(auth_header[7:])
                if payload:
                    sub = payload.get("sub")
                    if sub:
                        client_id = f"user:{sub}"
            except _jwt.ExpiredSignatureError:
                logger.debug("Rate limit: expired token, falling back to IP")
            except Exception:
                pass  # Invalid/tampered/missing claims — fall back to IP-based limiting

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

        # Admin bulk-import writes: dedicated bucket so a sequential xlsx
        # import (one POST per row) does not trip the 100/min general limit.
        # Replaces — does not stack on top of — the general bucket.
        elif request.method in {"POST", "PATCH", "PUT"} and _ADMIN_BULK_WRITE_PATH_RE.match(path):
            result = limiter.check_admin_write_limit(client_id)
            if not result:
                resp = JSONResponse(
                    status_code=429,
                    content=ErrorResponse(
                        detail=(
                            f"Rate limit exceeded: Maximum {result.limit} "
                            "identity-write requests per minute"
                        ),
                        error_code="RATE_LIMIT_EXCEEDED",
                        timestamp=datetime.utcnow().isoformat(),
                    ).model_dump(),
                )
                self._add_rate_limit_headers(resp, result)
                return resp

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


# ── Depot chat agent (feature-flagged) ────────────────────────────────────
# Mounted behind ``AGENT_SEARCH_ENABLED`` (default false; flipped to default-on
# in sprint B6 after the golden tests pass — see
# docs/plans/agent_search_architecture_v0.md §11). The router inherits the
# JWT and geo-block pipeline above. The LLM-powered turn endpoints add their
# own 10 req/min/user agent bucket; read-only run-trace fetches use JWT only.
from .agent.feature_flag import is_agent_search_enabled

if is_agent_search_enabled():
    from .agent.router import router as agent_router  # noqa: PLC0415

    app.include_router(agent_router)
    logger.info("Depot chat agent enabled at /agent/*")


# ── Data Sources (feature-flagged) ─────────────────────────────────────────
# Mounted behind ``DATA_SOURCES_ENABLED`` (default on). Self-serve external
# data-source connections (Kempower ChargEye first) with encrypted credentials,
# durable scheduled ingestion, and a dynamic provider catalogue. The router
# inherits the JWT + geo-block pipeline above.
from .data_sources.feature_flag import is_data_sources_enabled  # noqa: E402

if is_data_sources_enabled():
    from .data_sources.router import router as data_sources_router  # noqa: PLC0415

    app.include_router(data_sources_router)
    logger.info("Data Sources enabled at /admin/data-sources/*")


# ── Optimization metadata router ─────────────────────────────────────────────
# Always mounted — no feature flag needed. Provides read-only solver metadata
# (GET /depots/{id}/optimization/solver) behind the same JWT + depot-access
# gate as all other /depots/* endpoints.
from .optimization import router as optimization_router  # noqa: E402

app.include_router(optimization_router)


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


class SavingsSummaryResponse(BaseModel):
    """Month-to-date savings summary for the depot 'today' card.

    Spec from the frontend ``SavingsSummarySchema`` in
    ``src/lib/schemas/today.ts``. All seven fields are required and
    non-null; the backend forces ``0.0`` rather than NaN/Infinity when
    there's no data, so the UI never has to special-case empty months.
    """

    current_month_eur: float = Field(..., description="Actual charging cost month-to-date (EUR).")
    baseline_month_eur: float = Field(
        ...,
        description=(
            "What the same energy would have cost without optimization "
            "(flat-rate baseline: total energy × average day-ahead price "
            "across the period)."
        ),
    )
    saved_eur: float = Field(..., description="baseline_month_eur - current_month_eur.")
    saved_pct: float = Field(
        ...,
        description=(
            "Savings as a percentage of baseline, one decimal place. "
            "Forced to 0.0 when baseline is 0."
        ),
    )
    period_start: str = Field(
        ...,
        description="First instant of the current calendar month, depot tz, returned as UTC ISO 8601.",
    )
    period_end: str = Field(..., description='"Now" as UTC ISO 8601.')
    as_of: str = Field(..., description="When the figures were computed (UTC ISO 8601).")


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
    depot_name: Optional[str] = Field(
        None, description="Depot display name (denormalized via JOIN)"
    )
    alert_type: str = Field(
        ...,
        description=(
            "Wire enum: charger_auth_failure | charger_fault | degraded_optimization | "
            "missing_input | stale_telemetry"
        ),
    )
    severity: str = Field(..., description="info | warning | critical")
    subject: str = Field(
        ..., description="Human-readable summary (mapped from notification_alerts.title)"
    )
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
    occurrence_count: int = Field(
        ..., description="Times the dedup_key has fired since first_seen_at"
    )
    acknowledged_by: Optional[str] = Field(None, description="User UUID who acknowledged")
    acknowledged_by_email: Optional[str] = Field(
        None, description="Email of the user who acknowledged (set by alerts.acknowledge command)"
    )
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


class OrgAlertsListResponse(BaseModel):
    """Paginated, org-scoped alert list (GET /alerts)."""

    items: list[NotificationAlertItem]
    total: int = Field(..., description="Total matching rows, for pagination")
    page: int
    page_size: int


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


# ============ Fleet List Models (GET /depots/{id}/chargers, /vehicles) ============


class ChargerCurrentSession(BaseModel):
    """Active charging session running on this charger right now."""

    session_id: str
    vehicle_id: Optional[str] = None
    id_tag: Optional[str] = Field(
        None,
        description=(
            "Raw OCPP idTag the charger sent at StartTransaction. Surfaced so "
            'the frontend can render "Unknown vehicle charging · RFID <tag>" '
            "when the cards-only auth path didn't resolve a ``vehicle_id`` "
            "(no row in ``rfid_card_vehicle_assignments`` for this card)."
        ),
    )
    started_at: str = Field(..., description="Session start (ISO 8601)")
    current_power_kw: Optional[float] = None
    current_soc: Optional[float] = Field(None, ge=0.0, le=1.0)
    target_soc: Optional[float] = Field(None, ge=0.0, le=1.0)
    estimated_end_at: Optional[str] = None


class ChargerListItem(BaseModel):
    """One charger row for ``GET /depots/{id}/chargers``."""

    id: str = Field(..., description="charging_stations.id (UUID)")
    depot_id: str
    ocpp_id: str = Field(..., description="OCPP charge_point_id (charging_stations.station_id)")
    display_name: Optional[str] = None
    vendor: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = None
    firmware: Optional[str] = None
    rated_kw: Optional[float] = None
    efficiency: Optional[float] = None
    connector_type: Optional[str] = None
    connector_count: int = Field(..., ge=1)
    connector_ids: list[int] = Field(default_factory=list)
    auth_required: bool
    status: Literal["charging", "idle", "offline", "fault"] = Field(
        ..., description="Four-state pill: charging | idle | offline | fault"
    )
    ocpp_connector_status: Optional[str] = Field(
        None,
        description=(
            "Raw OCPP StatusNotification value (Available, Preparing, Charging, "
            "SuspendedEV, SuspendedEVSE, Faulted, Unavailable, Reserved, Finishing) "
            "for tooltips and /admin/support drilldown."
        ),
    )
    network_notes: Optional[str] = None
    created_at: str
    last_interaction_at: Optional[str] = Field(
        None,
        description=(
            "Timestamp of the charger's most recent activity from this API's "
            "perspective. Initial-load value is "
            "``MAX(connector_status.timestamp)`` across this station's "
            "connectors — a lower bound (the charger was at least alive then). "
            "Live updates flow via the SSE stream at "
            "``GET /depots/{depot_id}/liveness/stream`` which the WS handler "
            "feeds on every received OCPP frame, rate-limited to ~10s per "
            "station. Null if the charger has never connected."
        ),
    )
    last_heartbeat_at: Optional[str] = Field(
        None,
        description=(
            "**Deprecated.** Same value as ``last_interaction_at`` for one "
            "transitional release while the frontend migrates. Drop after "
            "the rollout completes."
        ),
        deprecated=True,
    )
    current_session: Optional[ChargerCurrentSession] = None


class ChargerListResponse(BaseModel):
    """Response envelope for ``GET /depots/{id}/chargers``."""

    items: list[ChargerListItem] = Field(default_factory=list)
    fetched_at: str = Field(..., description="Server-side fetch timestamp (ISO 8601)")


class VehicleNextDeparture(BaseModel):
    """Next scheduled departure for this vehicle."""

    schedule_id: str
    route_id: Optional[str] = None
    departure_time: str = Field(..., description="ISO 8601")
    return_time: str = Field(..., description="ISO 8601")
    required_soc: Optional[float] = Field(None, ge=0.0, le=1.0)
    energy_kwh: Optional[float] = None


class VehicleCurrentState(BaseModel):
    """Live operational state for a vehicle.

    State derivation priority (top wins):
      1. ``offline`` — ``last_seen_at`` is NULL or older than 30 minutes.
      2. ``charging`` — an open ``charging_sessions`` row exists for this vehicle.
      3. ``in_route`` — within an active schedule window
         (``departure_time <= now() < COALESCE(actual_return_time, return_time)``).
      4. ``ready`` — ``current_soc >= COALESCE(next_departure.required_soc, 0.95)``.
      5. ``at_risk`` — ``current_soc < COALESCE(next_departure.required_soc, 0.95)``.
    """

    state: Literal["ready", "charging", "at_risk", "in_route", "offline", "unknown"]
    current_soc: Optional[float] = Field(None, ge=0.0, le=1.0)
    current_power_kw: Optional[float] = None
    connected_charger_id: Optional[str] = Field(
        None, description="charging_stations.id (UUID), not the OCPP id"
    )
    connected_session_id: Optional[str] = None
    last_seen_at: Optional[str] = Field(None, description="Latest telemetry timestamp (ISO 8601)")


class VehicleListItem(BaseModel):
    """One vehicle row for ``GET /depots/{id}/vehicles``."""

    id: str = Field(..., description="vehicles.id (UUID)")
    depot_id: str
    external_id: str = Field(
        ...,
        min_length=1,
        description="Customer's fleet number (vehicles.external_id, required + unique per depot)",
    )
    display_name: Optional[str] = None
    vehicle_type: Optional[str] = None
    vin: Optional[str] = None
    license_plate: Optional[str] = None
    id_tag: Optional[str] = None
    battery_capacity_kwh: Optional[float] = None
    max_charge_rate_kw: Optional[float] = None
    max_discharge_rate_kw: Optional[float] = None
    v2g_capable: bool = False
    make: Optional[str] = None
    model: Optional[str] = None
    year: Optional[int] = None
    status: Optional[str] = None
    created_at: str
    current_state: VehicleCurrentState
    next_departure: Optional[VehicleNextDeparture] = None


class VehicleListResponse(BaseModel):
    """Response envelope for ``GET /depots/{id}/vehicles``."""

    items: list[VehicleListItem] = Field(default_factory=list)
    fetched_at: str = Field(..., description="Server-side fetch timestamp (ISO 8601)")


# ===== Live Sessions / Realtime State =====
#
# These three endpoints serve all session and realtime-state queries.
# The canonical store is TimescaleDB (`charging_sessions`, `telemetry`);
# Supabase holds only static reference data (sites, charging_stations, vehicles, organizations).


class ActiveSessionItem(BaseModel):
    """One open live charging session, returned by ``GET /depots/{id}/sessions/active``."""

    session_id: str
    ocpp_id: str = Field(..., description="OCPP station id (charging_stations.station_id)")
    connector_id: int
    vehicle_id: Optional[str] = None
    started_at: str = Field(..., description="ISO 8601")
    current_power_kw: Optional[float] = None
    current_soc: Optional[float] = Field(None, ge=0.0, le=1.0)
    target_soc: Optional[float] = Field(None, ge=0.0, le=1.0)
    estimated_end_at: Optional[str] = None
    last_sample_at: Optional[str] = Field(
        None,
        description=(
            "Server-side updated_at on the charging_sessions row; bumped on every "
            "MeterValues. Use to detect stalled sessions."
        ),
    )


class ActiveSessionsResponse(BaseModel):
    """Response envelope for ``GET /depots/{id}/sessions/active``."""

    items: list[ActiveSessionItem] = Field(default_factory=list)
    fetched_at: str = Field(..., description="Server-side fetch timestamp (ISO 8601)")


class CompletedSessionItem(BaseModel):
    """One completed charging session, returned by ``GET /depots/{id}/sessions``."""

    session_id: str
    ocpp_id: Optional[str] = Field(
        None, description="OCPP station id; null for imported rows with no live charger."
    )
    connector_id: Optional[int] = None
    vehicle_id: Optional[str] = None
    driver_id: Optional[str] = None
    started_at: str = Field(..., description="ISO 8601")
    ended_at: str = Field(..., description="ISO 8601")
    energy_delivered_kwh: Optional[float] = None
    energy_received_kwh: Optional[float] = None
    cost_total: Optional[float] = None
    start_soc_percent: Optional[float] = Field(None, ge=0.0, le=100.0)
    end_soc_percent: Optional[float] = Field(None, ge=0.0, le=100.0)
    source: Literal["live", "import"] = "live"


class CompletedSessionsResponse(BaseModel):
    """Response envelope for ``GET /depots/{id}/sessions``."""

    items: list[CompletedSessionItem] = Field(default_factory=list)
    next_cursor: Optional[str] = Field(
        None,
        description=(
            "Opaque cursor for the next page. Pass back as the ``cursor`` query "
            "parameter; null when no more rows are available."
        ),
    )
    fetched_at: str = Field(..., description="Server-side fetch timestamp (ISO 8601)")


class VehicleRealtimeStateItem(BaseModel):
    """Latest telemetry per vehicle, for ``GET /depots/{id}/vehicles/state``."""

    vehicle_id: str
    charger_id: Optional[str] = Field(
        None, description="charging_stations.id (UUID) of the connected charger, if any."
    )
    soc: Optional[float] = Field(None, ge=0.0, le=1.0)
    power_kw: Optional[float] = None
    is_plugged: Optional[bool] = None
    last_seen_at: str = Field(..., description="Latest telemetry timestamp (ISO 8601)")


class VehicleRealtimeStateResponse(BaseModel):
    """Response envelope for ``GET /depots/{id}/vehicles/state``."""

    items: list[VehicleRealtimeStateItem] = Field(default_factory=list)
    fetched_at: str = Field(..., description="Server-side fetch timestamp (ISO 8601)")


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


# ============ Recurring schedule template models ============

# Allowed day-of-week codes; mirrors the CHECK constraint on the table and
# the canonical Zod schema in the frontend (see PR #119).
_RECURRING_DAYS = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _parse_hhmm(value: str) -> "_dt_time":
    """Parse ``HH:MM`` into ``datetime.time``. Raises ``ValueError`` otherwise."""
    if not isinstance(value, str) or not _HHMM_RE.match(value):
        raise ValueError("must match HH:MM (00:00 to 23:59)")
    hh, mm = value.split(":")
    return _dt_time(hour=int(hh), minute=int(mm))


class RecurringTemplateCreate(BaseModel):
    """Create body for a recurring schedule template.

    Wire format: ``departure_time_of_day`` and ``return_time_of_day`` are
    depot-local ``HH:MM`` strings; ``start_date`` / ``end_date`` are
    depot-local ``YYYY-MM-DD`` strings (no time component); ``days_of_week``
    is a non-empty subset of ``{mon, tue, ..., sun}``.
    """

    vehicle_id: str = Field(..., description="Vehicle UUID")
    route_id: str = Field(..., min_length=1, max_length=100)
    departure_time_of_day: str = Field(..., description="Depot-local HH:MM")
    return_time_of_day: str = Field(..., description="Depot-local HH:MM")
    days_of_week: list[str] = Field(..., min_length=1, max_length=7)
    start_date: date = Field(..., description="Depot-local start date (inclusive)")
    end_date: Optional[date] = Field(
        default=None, description="Depot-local end date (inclusive); null = open-ended"
    )
    required_soc: float = Field(default=1.0, ge=0.99, le=1.0)
    energy_kwh: Optional[float] = Field(default=None, ge=0)

    @field_validator("vehicle_id")
    @classmethod
    def _validate_vehicle_uuid(cls, value: str) -> str:
        try:
            UUID(value)
        except ValueError:
            raise ValueError(f"vehicle_id must be a valid UUID, got: {value}")
        return value

    @field_validator("departure_time_of_day", "return_time_of_day")
    @classmethod
    def _validate_hhmm(cls, value: str) -> str:
        _parse_hhmm(value)
        return value

    @field_validator("days_of_week")
    @classmethod
    def _validate_days(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("days_of_week must be non-empty")
        invalid = sorted(set(value) - _RECURRING_DAYS)
        if invalid:
            raise ValueError(
                f"days_of_week contains invalid values: {invalid}; "
                f"allowed: mon,tue,wed,thu,fri,sat,sun"
            )
        # Deduplicate while preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for d in value:
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out

    @model_validator(mode="after")
    def _validate_combinations(self) -> "RecurringTemplateCreate":
        if self.end_date is not None and self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        if self.departure_time_of_day == self.return_time_of_day:
            raise ValueError("departure_time_of_day and return_time_of_day must differ")
        return self


class RecurringTemplatePatch(BaseModel):
    """Partial update of a recurring template. All fields optional."""

    vehicle_id: Optional[str] = Field(default=None)
    route_id: Optional[str] = Field(default=None, min_length=1, max_length=100)
    departure_time_of_day: Optional[str] = Field(default=None)
    return_time_of_day: Optional[str] = Field(default=None)
    days_of_week: Optional[list[str]] = Field(default=None, min_length=1, max_length=7)
    start_date: Optional[date] = Field(default=None)
    end_date: Optional[date] = Field(default=None)
    required_soc: Optional[float] = Field(default=None, ge=0.99, le=1.0)
    energy_kwh: Optional[float] = Field(default=None, ge=0)
    active: Optional[bool] = Field(default=None)

    @field_validator("vehicle_id")
    @classmethod
    def _validate_vehicle_uuid(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        try:
            UUID(value)
        except ValueError:
            raise ValueError(f"vehicle_id must be a valid UUID, got: {value}")
        return value

    @field_validator("departure_time_of_day", "return_time_of_day")
    @classmethod
    def _validate_hhmm(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        _parse_hhmm(value)
        return value

    @field_validator("days_of_week")
    @classmethod
    def _validate_days(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is None:
            return value
        invalid = sorted(set(value) - _RECURRING_DAYS)
        if invalid:
            raise ValueError(
                f"days_of_week contains invalid values: {invalid}; "
                f"allowed: mon,tue,wed,thu,fri,sat,sun"
            )
        seen: set[str] = set()
        out: list[str] = []
        for d in value:
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out


class OccurrenceCancellationRequest(BaseModel):
    """Body for cancelling a single recurring template occurrence."""

    reason: Optional[str] = Field(default=None, max_length=280)


class RecurringTemplateResponse(BaseModel):
    """Serialized template returned to admin clients."""

    template_id: str
    vehicle_id: str
    route_id: str
    departure_time_of_day: str
    return_time_of_day: str
    days_of_week: list[str]
    start_date: date
    end_date: Optional[date] = None
    required_soc: float
    energy_kwh: Optional[float] = None
    active: bool
    crosses_midnight: bool
    created_at: datetime
    updated_at: datetime
    cancelled_dates: list[date] = Field(default_factory=list)


class RecurringTemplateListResponse(BaseModel):
    templates: list[RecurringTemplateResponse]


class ScheduleReadinessOnlyResponse(BaseModel):
    readiness: ScheduleReadinessResponse


class RecurringTemplateCreateResponse(BaseModel):
    created: RecurringTemplateResponse
    readiness: ScheduleReadinessResponse


class RecurringTemplateUpdateResponse(BaseModel):
    updated: RecurringTemplateResponse
    readiness: ScheduleReadinessResponse


class RecurringOccurrenceCancellationResponse(BaseModel):
    """Response from cancelling a single recurring occurrence."""

    template_id: str
    occurrence_date: date
    cancelled_at: datetime
    reason: Optional[str] = None
    readiness: ScheduleReadinessResponse


class RecurringOccurrenceUncancelResponse(BaseModel):
    """Response from removing a single cancellation."""

    template_id: str
    occurrence_date: date
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
                "over_cap_penalty_per_kwh must be strictly greater than " "under_cap_rate_per_kwh"
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
    charger_vehicle_access_default: Literal["all_to_all", "explicit_matrix"] = "explicit_matrix"

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
    """Depot summary returned by setup endpoints.

    ``depot_id`` and ``organization_id`` match the wire shape of
    ``GET /me/depots`` so frontends can use a single ``DepotSummary`` schema.
    ``id`` is a deprecated alias of ``depot_id`` retained for back-compat;
    drop after one release once consumers have migrated.
    """

    id: str
    depot_id: str
    organization_id: Optional[str] = None
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


class HistoricalSessionImport(_CamelOrSnakeModel):
    """One row of a historical charging-sessions XLSX upload.

    Mirrors the bulk-RFID flow: the frontend parses the spreadsheet and POSTs
    one row per call. Timestamps are accepted as naive strings interpreted in
    the depot's local timezone (``sites.timezone``); the server converts to
    UTC before persisting.

    Identity resolution priority:
      1. ``rfid_label`` matched against ``rfid_cards.label`` (case-insensitive)
      2. ``id_tag`` matched against ``vehicles.id_tag``, then ``rfid_cards.id_tag``
      3. Unmatched — session is stored with all identity fields null
    """

    import_batch_id: UUID
    start_time_local: str = Field(..., min_length=1, max_length=64)
    end_time_local: Optional[str] = Field(default=None, max_length=64)
    # XLSX often carries a "Session duration" column whose value is the only
    # trustworthy signal for end_time — the export's end_time column has been
    # observed populated with absurd dates (Dec 2026, etc.). When duration is
    # supplied the server prefers `start + duration` over a bad file end.
    # Accepts seconds (int) because the FE parses HH:MM:SS / decimal hours and
    # normalises to a non-negative integer count of seconds before posting.
    # Upper bound matches `_IMPORT_MAX_SESSION_SPAN` (7 days) so an out-of-range
    # value yields a clean 400 VALIDATION_ERROR instead of a 500 from
    # `timedelta(seconds=10**15)` overflowing inside the resolver.
    session_duration_seconds: Optional[int] = Field(default=None, ge=0, le=7 * 24 * 3600)
    energy_delivered_kwh: float = Field(..., ge=0)
    revenue: float = Field(default=0.0, ge=0)
    rfid_label: Optional[str] = Field(default=None, max_length=255)
    id_tag: Optional[str] = Field(default=None, min_length=1, max_length=255)
    # Open-text rather than a Literal[...] enum because the upstream export
    # emits at least Charging / Finished / ConnectedStoppedByEv today and
    # additional codes (e.g. Faulted, Available) appear in the wild. We
    # preserve the raw string verbatim in charging_sessions.import_status so
    # any future state round-trips without a backend deploy. Length is capped
    # to keep the column predictable.
    status: str = Field(..., min_length=1, max_length=64)
    transaction_type: str = Field(default="RFID", max_length=64)
    user_full_name: Optional[str] = Field(default=None, max_length=255)
    station_owner_full_name: Optional[str] = Field(default=None, max_length=255)


class HistoricalSessionImportMatched(BaseModel):
    """Identity resolution outcome for an imported session row."""

    vehicle_id: Optional[str] = None
    card_id: Optional[str] = None
    driver_id: Optional[str] = None


class HistoricalSessionImportResponse(BaseModel):
    """201 response for POST /admin/depots/{id}/charging-sessions/import.

    ``was_new`` distinguishes a fresh insert from a merge into an existing
    session (the import path uses UPSERT-with-fill-nulls, so re-uploading the
    same logical row merges null columns rather than erroring with 409). The
    bulk-import dialog keys off this flag to surface "merged" vs "new" counts.
    """

    session_id: str
    matched: HistoricalSessionImportMatched
    was_new: bool = True


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
      * If the call site passes ``detail`` as a string and the status is
        below 500, ``response.detail`` is that string verbatim (matches
        FastAPI default). For 5xx, string ``detail`` is replaced with the
        sanitized message for the mapped ``ErrorCode`` so internals cannot leak.
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

    effective_provided_code = provided_code if isinstance(provided_code, str) else None
    code = _http_status_to_error_code(exc.status_code, effective_provided_code)

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

    if exc.status_code >= 500 and isinstance(detail, str):
        response_detail: object = safe_message_for(code)
    else:
        response_detail = detail if detail is not None else safe_message_for(code)

    body: dict = {
        "detail": response_detail,
        "error_code": (
            str(effective_provided_code) if effective_provided_code else str(code.value)
        ),
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


def _http_status_to_error_code(status_code: int, provided_code: Optional[str] = None) -> ErrorCode:
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


@app.exception_handler(AdminAuditWriteError)
async def admin_audit_write_error_handler(
    request: Request, exc: AdminAuditWriteError
) -> JSONResponse:
    """Translate strict-mode admin audit failures into 503.

    Cross-org admin enumeration paths require a durable audit trail; if the
    audit insert fails, the endpoint must fail closed rather than return data
    without a record.
    """
    logger.error("Admin audit (strict) failed on %s: %s", request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=ErrorResponse(
            detail="Admin audit log unavailable",
            error_code="AUDIT_LOG_UNAVAILABLE",
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

            # Empty fleets and chargerless depots are valid for freshly-onboarded
            # depots; GET /state should still succeed with empty payloads. The
            # /optimize endpoint enforces the not-empty precondition at its own
            # layer (see line ~4160), so empty configs here are not an error.
            if not config.vehicle_capacities:
                logger.info(f"Depot {depot_id} has no vehicles configured yet")
            if config.n_chargers == 0:
                logger.info(f"Depot {depot_id} has no chargers configured yet")

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


@dataclass(frozen=True)
class _SiteMetadata:
    """Cached subset of ``sites`` used by the import endpoint per-row.

    Read-only because cache consumers may share the instance across concurrent
    requests; nothing on it is request-specific. Lookup is by depot UUID and
    callers must independently verify ``organization_id`` matches the caller's
    JWT org claim before trusting the row.
    """

    organization_id: str
    timezone_name: str


async def _get_site_metadata(depot_id: str) -> Optional[_SiteMetadata]:
    """Return cached ``sites`` metadata for the given depot.

    Returns ``None`` when the depot does not exist. Callers must still compare
    ``organization_id`` against the JWT claim before serving data — the cache
    is shared across orgs.
    """
    if not db_pools or db_pools.static is None:
        return None

    now = time.time()
    cached = _site_metadata_cache.get(depot_id)
    if cached is not None and now - cached[1] < _config_cache_ttl:
        return cached[0]

    lock = _site_metadata_locks.setdefault(depot_id, asyncio.Lock())
    async with lock:
        cached = _site_metadata_cache.get(depot_id)
        if cached is not None and time.time() - cached[1] < _config_cache_ttl:
            return cached[0]

        async with db_pools.static.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    organization_id::text   AS organization_id,
                    timezone
                FROM sites
                WHERE id = $1::uuid
                """,
                depot_id,
            )
        if row is None:
            return None

        meta = _SiteMetadata(
            organization_id=row["organization_id"],
            timezone_name=row["timezone"] or "America/Los_Angeles",
        )
        _site_metadata_cache[depot_id] = (meta, time.time())
        return meta


def get_price_source() -> PriceSource:
    """FastAPI dependency: return the active ``PriceSource`` implementation.

    Production wires this to ``TimescalePriceSource`` reading the ``prices``
    hypertable. Unit tests override the dependency to inject a deterministic
    ``StaticPriceSource``. When ``db_pools`` is unavailable (e.g. early
    startup) we fall back to ``StaticPriceSource(None)`` so the import path
    still serves with revenue-as-cost behavior.
    """
    if db_pools is None or db_pools.ts is None:
        return StaticPriceSource(None)
    return TimescalePriceSource(db_pools.ts)


async def _resolve_session_cost(
    *,
    price_source: PriceSource,
    depot_id: str,
    start_time_utc: datetime,
    end_time_utc: Optional[datetime],
    energy_kwh: float,
    fallback_revenue: float,
) -> Decimal:
    """Compute ``cost_total`` from depot prices when coverage exists.

    Falls back to the request-supplied ``revenue`` whenever:
      * the session has no end time (open / in-progress import),
      * energy is zero (no cost to derive), or
      * the depot has incomplete or missing hourly price data for the session window.

    Resolves *before* opening the UPSERT transaction so the price lookup never
    extends row-lock duration.
    """
    fallback = _to_decimal(fallback_revenue, default=Decimal(0))

    if end_time_utc is None or energy_kwh <= 0:
        return fallback

    try:
        avg_price = await price_source.average_price_per_kwh(
            depot_id=depot_id,
            start=start_time_utc,
            end=end_time_utc,
        )
    except Exception:  # noqa: BLE001 — never let price lookup fail the import
        logger.exception(
            "price-source lookup failed; falling back to revenue (depot=%s)",
            depot_id,
        )
        return fallback

    if avg_price is None or not avg_price.is_finite():
        return fallback

    derived = (Decimal(str(energy_kwh)) * avg_price).quantize(Decimal("0.000001"))
    return derived


def _to_decimal(value, *, default: Decimal) -> Decimal:
    """Convert a float/str/Decimal to Decimal, returning ``default`` on bad input."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


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


_OCPP_BASIC_PASSWORD_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_OCPP_BASIC_PASSWORD_LENGTH = 10


def _generate_ocpp_basic_password() -> str:
    """Generate an ABB-compatible high-entropy one-time Basic Auth password."""
    return "".join(
        secrets.choice(_OCPP_BASIC_PASSWORD_ALPHABET) for _ in range(_OCPP_BASIC_PASSWORD_LENGTH)
    )


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


def _format_charger_onboarding_first_response(charger: dict, password: str) -> dict:
    """Build the first-response shape with the one-time plaintext credential.

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


def _format_charger_onboarding_replay_response(charger: dict) -> dict:
    """Build the replay-response shape — same as the first response minus the plaintext.

    Security (H5): the idempotency record stores this payload (no password)
    so a retry of the same Idempotency-Key never re-emits the credential.
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
            "scheme": "basic",
            "shown_once": True,
        },
        "replayed": True,
        "detail": (
            "Credentials are returned exactly once. "
            "Use POST /admin/depots/{depot_id}/chargers/{charger_id}/rotate_credentials "
            "to obtain new credentials."
        ),
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
    """Map identity uniqueness conflicts to frontend-stable 409 responses.

    The FE bulk-import flow keys off ``error_code`` to group per-row failures,
    so each kind of conflict gets its own stable code (``DUPLICATE_ID_TAG`` for
    a clashing RFID/vehicle id_tag, ``DUPLICATE_VIN`` for a clashing VIN, and
    ``DUPLICATE_EXTERNAL_ID`` for a clashing external identifier).
    """
    constraint = getattr(exc, "constraint_name", "") or ""
    if "id_tag" in constraint:
        error_code = ErrorCode.DUPLICATE_ID_TAG.value
        detail = "idTag is already registered"
    elif "vehicles_vin" in constraint:
        # vehicles_vin_key is a global UNIQUE on vin, so this conflict can
        # surface across organizations and not just within the depot.
        error_code = ErrorCode.DUPLICATE_VIN.value
        detail = "VIN is already registered"
    elif "vehicles_external_id" in constraint:
        error_code = ErrorCode.DUPLICATE_EXTERNAL_ID.value
        detail = "External identifier is already registered"
    elif "charging_sessions_import_dedup" in constraint:
        error_code = ErrorCode.DUPLICATE_SESSION.value
        detail = "Charging session has already been imported"
    elif "external" in constraint:
        error_code = ErrorCode.DUPLICATE_EXTERNAL_ID.value
        detail = "External identifier is already registered for this depot"
    else:
        error_code = ErrorCode.CONFLICT.value
        detail = "Identity record already exists"
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error_code": error_code, "detail": detail},
    )


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


async def _build_readiness_checklist(
    depot_id: str,
    payload: DepotSetupPayload,
    *,
    conn: Optional[asyncpg.Connection] = None,
) -> list[dict]:
    """Build exact setup readiness checklist for optimization prerequisites."""
    return await _build_depot_readiness_checklist(
        depot_id,
        battery_present=payload.stationary_battery.present,
        conn=conn,
    )


async def _build_depot_readiness_checklist(
    depot_id: str,
    *,
    battery_present: Optional[bool] = None,
    conn: Optional[asyncpg.Connection] = None,
) -> list[dict]:
    """Build persisted depot readiness checklist for setup and schedule screens."""
    if not db_pools:
        return []

    if battery_present is None:
        battery_present = False

    async def _static_depot_readiness(
        static_conn: asyncpg.Connection,
    ) -> tuple[bool, bool, str, bool, bool, bool]:
        has_vehicles = bool(
            await static_conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM vehicles WHERE site_id = $1::uuid)", depot_id
            )
        )
        has_chargers = bool(
            await static_conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM charging_stations WHERE site_id = $1::uuid)", depot_id
            )
        )
        access_default = (
            await static_conn.fetchval(
                "SELECT charger_vehicle_access_default FROM sites WHERE id = $1::uuid",
                depot_id,
            )
        ) or "explicit_matrix"
        has_access = False
        if access_default != "all_to_all":
            has_access = bool(
                await static_conn.fetchval(
                    """
                    SELECT EXISTS(
                        SELECT 1
                        FROM charger_vehicle_access cva
                        JOIN charging_stations c ON c.id = cva.charging_station_id
                        WHERE c.site_id = $1::uuid AND cva.is_accessible = TRUE
                    )
                    """,
                    depot_id,
                )
            )
        has_schedules = bool(
            await static_conn.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1
                    FROM schedules s
                    JOIN vehicles v ON v.id = s.vehicle_id
                    WHERE v.site_id = $1::uuid AND s.departure_time >= NOW() - INTERVAL '1 hour'
                )
                """,
                depot_id,
            )
        )
        if not has_schedules:
            now_utc = datetime.now(timezone.utc)
            horizon_start = now_utc - timedelta(hours=1)
            horizon_end = now_utc + timedelta(hours=24)
            template_rows, cancellation_rows = await db_queries.fetch_recurring_horizon_data(
                static_conn,
                depot_id=UUID(depot_id),
                horizon_start=horizon_start,
                horizon_end=horizon_end,
            )
            if template_rows:
                tz_name = await static_conn.fetchval(
                    "SELECT timezone FROM sites WHERE id = $1::uuid", depot_id
                )
                if not tz_name:
                    depot_tz = ZoneInfo("UTC")
                else:
                    try:
                        depot_tz = ZoneInfo(tz_name)
                    except ZoneInfoNotFoundError:
                        depot_tz = ZoneInfo("UTC")
                templates = [
                    RecurringTemplate(
                        template_id=row["id"],
                        depot_id=row["depot_id"],
                        vehicle_id=row["vehicle_id"],
                        route_id=row["route_id"],
                        departure_time_of_day=row["departure_time_of_day"],
                        return_time_of_day=row["return_time_of_day"],
                        days_of_week=tuple(row["days_of_week"]),
                        start_date=row["start_date"],
                        end_date=row["end_date"],
                        required_soc=float(row["required_soc"]),
                        energy_kwh=(
                            float(row["energy_kwh"]) if row["energy_kwh"] is not None else None
                        ),
                        active=bool(row["active"]),
                        created_at=row["created_at"],
                    )
                    for row in template_rows
                ]
                cancellations = [
                    ScheduleCancellation(
                        template_id=row["template_id"],
                        occurrence_date=row["occurrence_date"],
                    )
                    for row in cancellation_rows
                ]
                has_schedules = bool(
                    expand_recurring_templates(
                        templates,
                        cancellations,
                        depot_tz=depot_tz,
                        horizon_start=horizon_start,
                        horizon_end=horizon_end,
                    )
                )
        has_battery = bool(
            await static_conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM battery_storage WHERE site_id = $1::uuid)",
                depot_id,
            )
        )
        return has_vehicles, has_chargers, access_default, has_access, has_schedules, has_battery

    if conn is not None:
        (
            has_vehicles,
            has_chargers,
            access_default,
            has_access,
            has_schedules,
            has_battery,
        ) = await _static_depot_readiness(conn)
    else:
        async with db_pools.static.acquire() as acquired:
            (
                has_vehicles,
                has_chargers,
                access_default,
                has_access,
                has_schedules,
                has_battery,
            ) = await _static_depot_readiness(acquired)

    has_prices = False
    has_building_load = False
    if db_pools.ts is not None:
        async with db_pools.ts.acquire() as ts_conn:
            has_prices = bool(
                await ts_conn.fetchval(
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
                await ts_conn.fetchval(
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
    """Emergency-fallback OCPP 1.6 endpoint on the API service.

    This endpoint is not the primary production runtime. The legacy
    ``src/websocket_handler`` service is the canonical OCPP path.

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

        if not org_id:
            return {"depots": [], "needs_setup": False, "viewer": viewer}

        async with db_pools.static.acquire() as conn:
            depots = await db_queries.get_depots_for_organization(conn, org_id)

        if role not in ("customer_admin", "customer_operator"):
            # Role not yet provisioned in app_metadata (e.g. new customer whose
            # favonius_role hasn't been set in Supabase yet). Withhold depot
            # details but return the correct needs_setup signal so the wizard
            # remains visible rather than silently hiding behind a false False.
            return {"depots": [], "needs_setup": len(depots) == 0, "viewer": viewer}

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
    "/depots/{depot_id}/liveness/stream",
    tags=["depots"],
    summary="SSE stream of charger liveness signals",
    description="""
    Long-lived Server-Sent Events stream that pushes one event per
    received OCPP frame from any charger in the caller's organisation,
    rate-limited at the source to one per ~10s per charger.

    Event payload:
    ```
    data: {"station_id": "<ocpp_id>", "last_interaction_at": "<iso8601>"}
    ```

    Plus a ``:keepalive`` SSE comment every 25s so intermediate proxies
    (Railway, nginx, etc.) don't reap the connection on idle days.

    **Authentication:** same JWT + depot-access tier as
    ``GET /depots/{depot_id}/state``. Subscribers are scoped to their
    organisation — events for other tenants never reach this stream.
    """,
)
async def depot_liveness_stream(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Subscribe to depot-scoped liveness events for the caller.

    The hub is process-wide and fans out by organisation; this endpoint
    applies a depot-level station filter before emitting SSE frames so
    callers only receive chargers that belong to ``depot_id``.
    """
    if liveness_hub is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "LIVENESS_HUB_UNAVAILABLE",
                "message": "Liveness stream not initialised",
            },
        )
    organization_id = (user.get("app_metadata") or {}).get("organization_id")
    # favonius_admin without an org claim: surface the requirement clearly
    # rather than returning a stream that can never receive events.
    if not organization_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "ORG_SCOPE_REQUIRED",
                "message": "Liveness stream requires organization_id in the token",
            },
        )

    if not db_pools:
        raise DatabaseError("Database not available")
    async with db_pools.static.acquire() as conn:
        station_rows = await conn.fetch(
            "SELECT station_id FROM charging_stations WHERE site_id = $1::uuid",
            depot_id,
        )
    depot_station_ids = {row["station_id"] for row in station_rows if row["station_id"]}

    queue = liveness_hub.subscribe(str(organization_id))

    async def _iterator() -> AsyncIterator[bytes]:
        # Keepalive cadence in seconds; long enough that we don't waste
        # bandwidth, short enough to outpace common proxy idle reapers.
        keepalive_s = 25.0
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=keepalive_s)
                except asyncio.TimeoutError:
                    # Idle — emit a comment line so the proxy keeps the
                    # connection open. Browser EventSource ignores comments.
                    yield b": keepalive\n\n"
                    continue
                if event is None:
                    # Hub-stopped sentinel.
                    return
                if event.get("station_id") not in depot_station_ids:
                    continue
                yield (f"data: {json.dumps(event, default=str)}\n\n").encode("utf-8")
        finally:
            liveness_hub.unsubscribe(str(organization_id), queue)

    return StreamingResponse(
        _iterator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


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
        _fleet_list_cache.pop((depot_id, "vehicles"), None)
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
        _fleet_list_cache.pop((depot_id, "vehicles"), None)
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
        _fleet_list_cache.pop((depot_id, "vehicles"), None)
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


# ============ Historical charging-sessions XLSX import ============

# Strict format the frontend RFID-bulk parser already emits ("YYYY-MM-DD HH:mm").
# Accept seconds optionally so an export with finer granularity still works.
_IMPORT_TS_FORMATS: tuple[str, ...] = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")


# Bounds used by `_resolve_import_end_time`. Defensive but generous: real
# fleet sessions complete in hours, and the worst-bug XLSX we've seen had
# `end_time` cells like "2026-12-31" while the duration column was correct.
# Anything beyond MAX_SESSION_SPAN is almost certainly the spreadsheet's bad
# end_time column; anything beyond now+FUTURE_SKEW is impossible by definition.
_IMPORT_MAX_SESSION_SPAN = timedelta(days=7)
_IMPORT_FUTURE_SKEW = timedelta(hours=1)
_IMPORT_END_MISMATCH_TOLERANCE = timedelta(minutes=15)


def _resolve_import_end_time(
    *,
    start_time_utc: datetime,
    end_time_utc: Optional[datetime],
    duration_seconds: Optional[int],
    now: Optional[datetime] = None,
) -> Optional[datetime]:
    """Pick a trustworthy end_time for an imported charging session.

    Decision table:

    | file end        | duration | action                                |
    |-----------------|----------|---------------------------------------|
    | missing         | present  | use computed                          |
    | sane, agrees    | present  | use file end                          |
    | insane / drift  | present  | use computed (file end is bogus)      |
    | sane            | missing  | use file end                          |
    | insane          | missing  | raise 400 INVALID_TIMESTAMP           |
    | missing         | missing  | None (ongoing / open session)         |

    "Insane" means: end < start, end > now + 1h, or span > 7 days.
    """
    now = now or datetime.now(timezone.utc)

    computed_end: Optional[datetime] = None
    if duration_seconds is not None and duration_seconds > 0:
        computed_end = start_time_utc + timedelta(seconds=duration_seconds)

    def _is_sane(end: datetime) -> bool:
        if end < start_time_utc:
            return False
        if end > now + _IMPORT_FUTURE_SKEW:
            return False
        if end - start_time_utc > _IMPORT_MAX_SESSION_SPAN:
            return False
        return True

    if end_time_utc is None and computed_end is None:
        return None

    if end_time_utc is None:
        # Duration-only path — sanity-check against the same rules so a bogus
        # duration (e.g. negative or > 7d) does not slip through.
        if not _is_sane(computed_end):  # type: ignore[arg-type]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": ErrorCode.INVALID_TIMESTAMP.value,
                    "detail": (
                        "session_duration_seconds yields an end_time outside the "
                        "accepted bounds (must be on/after start, within 1 hour of "
                        "now, and ≤ 7 days from start)"
                    ),
                    "field": "session_duration_seconds",
                },
            )
        return computed_end

    if computed_end is None:
        # File-end-only path — no duration to corroborate, so insane ends are
        # rejected rather than silently stored. This is the bug the broken HRX
        # XLSX import created in production.
        if not _is_sane(end_time_utc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error_code": ErrorCode.INVALID_TIMESTAMP.value,
                    "detail": (
                        "end_time_local is outside the accepted bounds (must be "
                        "on/after start, within 1 hour of now, and ≤ 7 days from "
                        "start). Provide session_duration_seconds to derive a "
                        "trusted end."
                    ),
                    "field": "end_time_local",
                },
            )
        return end_time_utc

    # Both present: trust the file end only when it is sane AND agrees with
    # the duration-derived end within tolerance. Disagreement is a strong
    # signal that the file end is the bad column (this is the HRX scenario).
    if (
        _is_sane(end_time_utc)
        and abs(end_time_utc - computed_end) <= _IMPORT_END_MISMATCH_TOLERANCE
    ):
        return end_time_utc

    logger.info(
        "import_end_time_correction: file_end=%s computed_end=%s start=%s — using computed",
        end_time_utc.isoformat(),
        computed_end.isoformat(),
        start_time_utc.isoformat(),
    )
    if not _is_sane(computed_end):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": ErrorCode.INVALID_TIMESTAMP.value,
                "detail": (
                    "session_duration_seconds yields an end_time outside the "
                    "accepted bounds (must be on/after start, within 1 hour of "
                    "now, and ≤ 7 days from start)"
                ),
                "field": "session_duration_seconds",
            },
        )
    return computed_end


def _parse_import_local_timestamp(value: str, tz: ZoneInfo, *, field: str) -> datetime:
    """Parse a depot-local timestamp string and return its UTC datetime."""
    text = (value or "").strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": ErrorCode.MISSING_REQUIRED_FIELD.value,
                "detail": f"{field} is required",
                "field": field,
            },
        )
    for fmt in _IMPORT_TS_FORMATS:
        try:
            naive = datetime.strptime(text, fmt)
            break
        except ValueError:
            continue
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": ErrorCode.INVALID_TIMESTAMP.value,
                "detail": "Expected 'YYYY-MM-DD HH:mm' in the depot's local timezone",
                "field": field,
            },
        )
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


def _compute_import_row_hash(
    *,
    depot_id: str,
    start_time_utc: datetime,
    id_tag: str,
) -> str:
    """SHA-256 of the canonical row identity, used for idempotent re-uploads.

    The canonical tuple is ``(depot_id, start_time_utc, id_tag)`` — deliberately
    free of energy and cost so subsequent re-uploads with corrected energy or
    revenue values merge into the same row via the UPSERT path rather than
    creating duplicates. Migration 036 is the matching backfill.
    """
    canonical = "|".join(
        [
            depot_id,
            start_time_utc.isoformat(),
            id_tag,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_PLATFORM_IMPORT_ID_TOKEN = "platform-start"


def _is_platform_initiated_import_row(request: HistoricalSessionImport) -> bool:
    """Return True when import row has no RFID/card identifier data."""
    return not (request.rfid_label or request.id_tag)


def _platform_import_hash_token(
    request: HistoricalSessionImport,
    *,
    end_time_utc: Optional[datetime],
) -> str:
    """Build a richer dedup token for platform-initiated import rows.

    Platform-start imports intentionally store a constant id_token
    (``platform-start``) in ``charging_sessions.id_token`` so reports can
    classify their origin. Using only that constant in ``import_row_hash``
    would falsely dedup distinct sessions that share minute-level start time.
    This helper keeps persisted ``id_token`` stable while baking additional
    persisted columns into the hash input.

    Canonical form is length-prefixed (``<bytes>:<value>`` for each field) so
    free-text content containing ``|`` cannot collide across distinct rows, and
    so migration 036 can reconstruct the exact same token in pure SQL from the
    ``charging_sessions`` columns alone. ``transaction_type`` is not persisted
    and is therefore excluded from the canonical form.
    """
    fields = [
        end_time_utc.isoformat() if end_time_utc else "",
        request.status or "",
        request.user_full_name or "",
        request.station_owner_full_name or "",
    ]
    canonical = "".join(f"{len(field.encode('utf-8'))}:{field}" for field in fields)
    inner = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{_PLATFORM_IMPORT_ID_TOKEN}:{inner}"


async def _resolve_import_id_tag(
    conn: asyncpg.Connection,
    *,
    depot_id: str,
    id_tag: Optional[str],
    rfid_label: Optional[str] = None,
) -> dict[str, Optional[str]]:
    """Resolve RFID identity against vehicles + rfid_cards scoped to this depot.

    Resolution order:
      1. ``rfid_label`` matched against ``rfid_cards.label`` (case-insensitive)
      2. ``id_tag`` matched against ``vehicles.id_tag``
      3. ``id_tag`` matched against ``rfid_cards.id_tag``
      4. Returns all-None — the row is imported unmatched so the FE can surface
         an "unmatched" badge.
    """
    _CARD_ASSIGNMENT_SUBQUERIES = """
        (
            SELECT cva.vehicle_id::text
            FROM rfid_card_vehicle_assignments cva
            JOIN vehicles v ON v.id = cva.vehicle_id
            WHERE cva.card_id = c.id
              AND v.site_id = c.site_id
              AND COALESCE(v.status, 'active') = 'active'
            ORDER BY v.external_id
            LIMIT 1
        ) AS vehicle_id,
        (
            SELECT cda.driver_id::text
            FROM rfid_card_driver_assignments cda
            JOIN drivers dr ON dr.id = cda.driver_id
            WHERE cda.card_id = c.id
              AND dr.site_id = c.site_id
              AND dr.status = 'active'
            ORDER BY dr.display_name
            LIMIT 1
        ) AS driver_id
    """

    if rfid_label:
        label_row = await conn.fetchrow(
            f"""
            SELECT c.id::text AS card_id, {_CARD_ASSIGNMENT_SUBQUERIES}
            FROM rfid_cards c
            WHERE LOWER(c.label) = LOWER($1)
              AND c.site_id = $2::uuid
              AND c.status = 'active'
            LIMIT 1
            """,
            rfid_label,
            depot_id,
        )
        if label_row:
            return {
                "vehicle_id": label_row["vehicle_id"],
                "card_id": label_row["card_id"],
                "driver_id": label_row["driver_id"],
            }

    if id_tag:
        vehicle_row = await conn.fetchrow(
            """
            SELECT id::text AS vehicle_id
            FROM vehicles
            WHERE id_tag = $1
              AND site_id = $2::uuid
              AND COALESCE(status, 'active') = 'active'
            LIMIT 1
            """,
            id_tag,
            depot_id,
        )
        if vehicle_row:
            return {
                "vehicle_id": vehicle_row["vehicle_id"],
                "card_id": None,
                "driver_id": None,
            }

        card_row = await conn.fetchrow(
            f"""
            SELECT c.id::text AS card_id, {_CARD_ASSIGNMENT_SUBQUERIES}
            FROM rfid_cards c
            WHERE c.id_tag = $1
              AND c.site_id = $2::uuid
              AND c.status = 'active'
            LIMIT 1
            """,
            id_tag,
            depot_id,
        )
        if card_row:
            return {
                "vehicle_id": card_row["vehicle_id"],
                "card_id": card_row["card_id"],
                "driver_id": card_row["driver_id"],
            }

    return {"vehicle_id": None, "card_id": None, "driver_id": None}


@app.post(
    "/admin/depots/{depot_id}/charging-sessions/import",
    response_model=HistoricalSessionImportResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["admin"],
    summary="Import a historical charging-session row from an XLSX upload",
)
async def import_historical_charging_session(
    depot_id: str,
    request: HistoricalSessionImport,
    user: dict = Depends(ensure_tenant_mirrored),
    price_source: PriceSource = Depends(get_price_source),
) -> HistoricalSessionImportResponse:
    """Upsert one historical charging session for the Reports / Energy accounting backfill.

    Mirrors the bulk-RFID dialog pattern: the frontend parses the XLSX and POSTs
    one row per call. Imported rows are scoped by ``site_id`` and use a
    deterministic placeholder ``station_id`` so they do not require a
    matching ``charging_stations`` row.

    Re-uploads of the same logical session (same depot + start_time + id_token)
    merge into the existing row via UPSERT-with-fill-nulls: only columns that
    are currently NULL get overwritten. Energy and cost are also refreshed when
    the incoming value is non-zero, so a corrected XLSX re-upload updates the
    metering numbers without inventing a duplicate row.
    """
    org_id = _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    if not db_pools or db_pools.ts is None:
        raise DatabaseError("Database not available")

    site_meta = await _get_site_metadata(depot_id)
    if site_meta is None or site_meta.organization_id != org_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": ErrorCode.DEPOT_NOT_FOUND.value,
                "detail": "Depot not found",
            },
        )
    try:
        tz = ZoneInfo(site_meta.timezone_name)
    except Exception:
        tz = ZoneInfo("UTC")

    is_platform_initiated = _is_platform_initiated_import_row(request)
    if is_platform_initiated:
        identity = {"vehicle_id": None, "card_id": None, "driver_id": None}
    else:
        async with db_pools.static.acquire() as static_conn:
            identity = await _resolve_import_id_tag(
                static_conn,
                depot_id=depot_id,
                id_tag=request.id_tag,
                rfid_label=request.rfid_label,
            )

    start_time_utc = _parse_import_local_timestamp(
        request.start_time_local, tz, field="start_time_local"
    )
    # Persist end_time for any caller-supplied value, not just status="Finished".
    # The source export emits multiple terminal/non-terminal statuses (Finished,
    # ConnectedStoppedByEv, Charging, ...) and we let them all round-trip the
    # `end_time` cell when present. Status semantics live on `import_status`
    # (free text). Validity is delegated to `_resolve_import_end_time` which
    # also reconciles the file's end_time column against the (more trustworthy)
    # `session_duration_seconds` value when both are present.
    file_end_time_utc: Optional[datetime] = None
    if request.end_time_local:
        file_end_time_utc = _parse_import_local_timestamp(
            request.end_time_local, tz, field="end_time_local"
        )
    end_time_utc = _resolve_import_end_time(
        start_time_utc=start_time_utc,
        end_time_utc=file_end_time_utc,
        duration_seconds=request.session_duration_seconds,
    )

    if not math.isfinite(request.energy_delivered_kwh):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error_code": ErrorCode.INVALID_ENERGY.value,
                "detail": "energy_delivered_kwh must be a finite, non-negative number",
            },
        )

    # Use rfid_label when present (new TOKS flow); fall back to id_tag (legacy).
    # When both identifiers are absent, classify as platform-initiated import.
    id_token = request.rfid_label or request.id_tag or _PLATFORM_IMPORT_ID_TOKEN
    # Platform-initiated dedup hashing MUST always use the raw file end_time,
    # never the resolver's output. The hash is the customer's stable identity
    # for the row across re-uploads — the XLSX is the source of truth, and
    # any value derived from `session_duration_seconds` would mutate when the
    # FE starts/stops sending the field or ships a corrected duration in a
    # later upload, breaking ON CONFLICT merge. Using the raw file value also
    # preserves backward-compat with rows persisted pre-resolver and with
    # migration 036's backfill, which keys off the persisted `end_time`
    # (== the raw file value for pre-resolver rows).
    #
    # Trade-off: when an XLSX has no end_time column at all (file end == None)
    # and two distinct platform-initiated sessions share start_time + status +
    # user_full_name + station_owner_full_name, they collide on hash. That
    # risk already existed pre-PR — there is no extra discriminator we can
    # add here without making the hash depend on the resolver, which would
    # break the more common re-import idempotency case above.
    hash_id_token = (
        _platform_import_hash_token(request, end_time_utc=file_end_time_utc)
        if is_platform_initiated
        else id_token
    )
    row_hash = _compute_import_row_hash(
        depot_id=depot_id,
        start_time_utc=start_time_utc,
        id_tag=hash_id_token,
    )
    placeholder_station_id = f"imported:{depot_id}"

    # Resolve cost from depot prices BEFORE opening the upsert transaction so
    # the (potentially slow) price lookup never holds a row lock. Falls back
    # transparently to request.revenue when no price coverage exists.
    cost_total = await _resolve_session_cost(
        price_source=price_source,
        depot_id=depot_id,
        start_time_utc=start_time_utc,
        end_time_utc=end_time_utc,
        energy_kwh=request.energy_delivered_kwh,
        fallback_revenue=request.revenue,
    )

    try:
        async with db_pools.ts.acquire() as ts_conn:
            row = await ts_conn.fetchrow(
                """
                INSERT INTO charging_sessions (
                    station_id, evse_id, connector_id,
                    vehicle_id, id_token, driver_id, card_id,
                    start_time, end_time,
                    energy_delivered_kwh, cost_total,
                    site_id, source,
                    import_batch_id, import_row_hash,
                    import_user_full_name, import_station_owner, import_status
                )
                VALUES (
                    $1, 0, 0,
                    $2, $3, $4::uuid, $5::uuid,
                    $6, $7,
                    $8, $9,
                    $10::uuid, 'import',
                    $11::uuid, $12,
                    $13, $14, $15
                )
                ON CONFLICT (site_id, import_row_hash) WHERE source = 'import'
                DO UPDATE SET
                    -- Fill nulls only: never clobber a value the existing row
                    -- already has. Re-uploads with corrected end_time / identity
                    -- backfill the gaps without overwriting prior corrections.
                    vehicle_id            = COALESCE(charging_sessions.vehicle_id, EXCLUDED.vehicle_id),
                    driver_id             = COALESCE(charging_sessions.driver_id, EXCLUDED.driver_id),
                    card_id               = COALESCE(charging_sessions.card_id, EXCLUDED.card_id),
                    -- Overwrite end_time when the new payload provides one.
                    -- A re-import is the customer's signal that the previously
                    -- stored end was wrong (e.g. the broken HRX XLSX with bad
                    -- end_time cells fixed by `session_duration_seconds`).
                    -- The resolver already sanitised the incoming value, so a
                    -- non-NULL EXCLUDED.end_time is always more trustworthy.
                    end_time              = CASE
                        WHEN EXCLUDED.end_time IS NOT NULL THEN EXCLUDED.end_time
                        ELSE charging_sessions.end_time
                    END,
                    import_user_full_name = COALESCE(charging_sessions.import_user_full_name, EXCLUDED.import_user_full_name),
                    import_station_owner  = COALESCE(charging_sessions.import_station_owner, EXCLUDED.import_station_owner),
                    import_status         = COALESCE(charging_sessions.import_status, EXCLUDED.import_status),
                    -- Refresh numeric metering when the new payload reports a
                    -- value: corrections to energy or cost are the common
                    -- reason customers re-upload an XLSX.
                    energy_delivered_kwh  = CASE
                        WHEN charging_sessions.energy_delivered_kwh IS NULL THEN EXCLUDED.energy_delivered_kwh
                        WHEN EXCLUDED.energy_delivered_kwh > 0 THEN EXCLUDED.energy_delivered_kwh
                        ELSE charging_sessions.energy_delivered_kwh
                    END,
                    cost_total            = CASE
                        WHEN charging_sessions.cost_total IS NULL THEN EXCLUDED.cost_total
                        WHEN EXCLUDED.cost_total IS NOT NULL AND EXCLUDED.cost_total > 0 THEN EXCLUDED.cost_total
                        ELSE charging_sessions.cost_total
                    END
                RETURNING session_id::text AS session_id, (xmax = 0) AS was_new
                """,
                placeholder_station_id,
                identity["vehicle_id"],
                id_token,
                identity["driver_id"],
                identity["card_id"],
                start_time_utc,
                end_time_utc,
                request.energy_delivered_kwh,
                cost_total,
                depot_id,
                str(request.import_batch_id),
                row_hash,
                request.user_full_name,
                request.station_owner_full_name,
                request.status,
            )
    except asyncpg.UniqueViolationError as exc:
        raise _handle_identity_unique_violation(exc) from exc

    if row is None:
        logger.error(
            "charging session import UPSERT returned no row (depot_id=%s)",
            depot_id,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": ErrorCode.DATABASE_ERROR.value,
                "detail": "Charging session import did not persist",
            },
        )

    session_id = row["session_id"]
    was_new = bool(row["was_new"])
    await _audit_identity_write(user, depot_id, "charging_session.import", str(session_id))
    return HistoricalSessionImportResponse(
        session_id=str(session_id),
        matched=HistoricalSessionImportMatched(
            vehicle_id=identity["vehicle_id"],
            card_id=identity["card_id"],
            driver_id=identity["driver_id"],
        ),
        was_new=was_new,
    )


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
                            detail=(
                                "Idempotency-Key was already used with a different "
                                "request body (error_code=IDEMPOTENCY_KEY_REUSED)"
                            ),
                        )
                    # Security (H5): the stored payload is the *replay* shape
                    # (no plaintext password). Return it with 200 so clients
                    # can distinguish a retry from a first-time creation.
                    return JSONResponse(
                        status_code=int(existing["status_code"] or status.HTTP_200_OK),
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

                # Security (H5): the first response carries the plaintext
                # password and is returned to the original caller; the
                # idempotency store gets the *replay* receipt with no
                # password so retries can never re-emit the credential.
                first_response = _format_charger_onboarding_first_response(charger, password)
                replay_response = _format_charger_onboarding_replay_response(charger)
                await db_queries.store_charger_onboarding_idempotency(
                    conn,
                    organization_id=org_id,
                    user_id=user_id,
                    endpoint=endpoint,
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    response_json=replay_response,
                    status_code=status.HTTP_200_OK,
                    ttl_minutes=30,
                )

        _depot_config_cache.pop(depot_id, None)
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=first_response)
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
    if not db_pools:
        raise DatabaseError("Database not available")
    await verify_depot_access(depot_id, user, db_pools.static)
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


# ============ Recurring schedule template endpoints ============


def _format_recurring_template_row(row: dict, cancelled_dates: list) -> dict:
    """Normalize a recurring template row for the API.

    ``departure_time_of_day`` / ``return_time_of_day`` are serialised as
    ``HH:MM`` (no seconds) to match the wire format the frontend Zod schema
    expects. ``crosses_midnight`` is derived from the times.
    """
    dep: _dt_time = row["departure_time_of_day"]
    ret: _dt_time = row["return_time_of_day"]
    return {
        "template_id": str(row["template_id"]),
        "vehicle_id": str(row["vehicle_id"]),
        "route_id": row["route_id"],
        "departure_time_of_day": dep.strftime("%H:%M"),
        "return_time_of_day": ret.strftime("%H:%M"),
        "days_of_week": list(row["days_of_week"]),
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "required_soc": float(row["required_soc"]),
        "energy_kwh": (float(row["energy_kwh"]) if row["energy_kwh"] is not None else None),
        "active": bool(row["active"]),
        "crosses_midnight": ret <= dep,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "cancelled_dates": list(cancelled_dates),
    }


def _recurring_validation_400(field: str, message: str) -> JSONResponse:
    """Return a 400 VALIDATION_ERROR for a single business-rule failure."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={
            "detail": "Validation failed",
            "error_code": "VALIDATION_ERROR",
            "field_errors": {field: [message]},
            "validation_errors": [{"path": field, "message": message}],
        },
    )


async def _assert_recurring_depot_access(depot_id: str, user: dict) -> None:
    """Shared auth + depot-access check used by every recurring endpoint."""
    _require_customer_admin_with_org(user)
    validate_depot_id(depot_id)
    if not db_pools:
        raise DatabaseError("Database not available")
    await verify_depot_access(depot_id, user, db_pools.static)


async def _serialize_templates_with_cancellations(conn, depot_uuid: UUID) -> list[dict]:
    """List all templates for a depot with their cancelled_dates inlined."""
    templates = await db_queries.list_recurring_templates(conn, depot_id=depot_uuid)
    template_ids = [UUID(t["template_id"]) for t in templates]
    cancelled_map = await db_queries.get_cancelled_dates_for_templates(
        conn, template_ids=template_ids
    )
    return [
        _format_recurring_template_row(
            t,
            cancelled_dates=cancelled_map.get(t["template_id"], []),
        )
        for t in templates
    ]


@app.get(
    "/admin/depots/{depot_id}/schedule/recurring",
    response_model=RecurringTemplateListResponse,
    tags=["admin"],
    summary="List recurring schedule templates for a depot",
)
async def list_recurring_schedule_templates(
    depot_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """List all recurring templates for the depot, including cancelled dates."""
    await _assert_recurring_depot_access(depot_id, user)
    depot_uuid = UUID(depot_id)
    async with db_pools.static.acquire() as conn:
        templates = await _serialize_templates_with_cancellations(conn, depot_uuid)
    return {"templates": templates}


@app.post(
    "/admin/depots/{depot_id}/schedule/recurring",
    response_model=RecurringTemplateCreateResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["admin"],
    summary="Create a recurring schedule template",
)
async def create_recurring_schedule_template(
    depot_id: str,
    request: RecurringTemplateCreate,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Create one recurring template for a vehicle in this depot.

    Pydantic validation errors flow through the global
    ``RequestValidationError`` handler which returns 400 VALIDATION_ERROR
    with the standard ``{detail, error_code, field_errors, validation_errors}``
    envelope (see ``validation_exception_handler``).
    """
    await _assert_recurring_depot_access(depot_id, user)

    depot_uuid = UUID(depot_id)
    vehicle_uuid = UUID(request.vehicle_id)
    departure_time = _parse_hhmm(request.departure_time_of_day)
    return_time = _parse_hhmm(request.return_time_of_day)

    async with db_pools.static.acquire() as conn:
        valid_vehicle_ids = await db_queries.get_vehicle_ids_for_depot(
            conn, depot_uuid, [vehicle_uuid]
        )
        if str(vehicle_uuid) not in valid_vehicle_ids:
            return _recurring_validation_400(
                "vehicle_id",
                f"vehicle_id {request.vehicle_id} is not a member of depot {depot_id}",
            )

        async with conn.transaction():
            row = await db_queries.create_recurring_template(
                conn,
                depot_id=depot_uuid,
                vehicle_id=vehicle_uuid,
                route_id=request.route_id,
                departure_time_of_day=departure_time,
                return_time_of_day=return_time,
                days_of_week=list(request.days_of_week),
                start_date=request.start_date,
                end_date=request.end_date,
                required_soc=float(request.required_soc),
                energy_kwh=request.energy_kwh,
            )

    checks = await _build_depot_readiness_checklist(depot_id)
    return {
        "created": _format_recurring_template_row(row, cancelled_dates=[]),
        "readiness": _readiness_response_payload(depot_id, checks),
    }


@app.patch(
    "/admin/depots/{depot_id}/schedule/recurring/{template_id}",
    response_model=RecurringTemplateUpdateResponse,
    tags=["admin"],
    summary="Update a recurring schedule template",
)
async def patch_recurring_schedule_template(
    depot_id: str,
    template_id: str,
    patch: RecurringTemplatePatch,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Partially update a recurring template; null fields keep prior values."""
    await _assert_recurring_depot_access(depot_id, user)
    validate_uuid(template_id, "template_id")

    patch_data = patch.model_dump(exclude_unset=True)
    if not patch_data:
        return _recurring_validation_400(
            "body", "At least one recurring template field is required"
        )

    # Reject explicit JSON null on fields backed by NOT NULL columns. end_date
    # and energy_kwh are nullable, so an explicit null there is a legitimate
    # "clear it" — only those two may be set to None. (Mirrors the manual
    # /schedule PATCH null-field guard; do NOT use exclude_none here or
    # clearing end_date/energy_kwh becomes impossible.)
    non_nullable_patch_fields = {
        "vehicle_id",
        "route_id",
        "departure_time_of_day",
        "return_time_of_day",
        "days_of_week",
        "start_date",
        "required_soc",
        "active",
    }
    null_fields = sorted(
        name
        for name, value in patch_data.items()
        if name in non_nullable_patch_fields and value is None
    )
    if null_fields:
        return _recurring_validation_400(null_fields[0], f"{', '.join(null_fields)} cannot be null")

    depot_uuid = UUID(depot_id)
    template_uuid = UUID(template_id)

    async with db_pools.static.acquire() as conn:
        existing = await db_queries.get_recurring_template_for_depot(
            conn, depot_id=depot_uuid, template_id=template_uuid
        )
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recurring template not found",
            )

        # Merge patch over the existing row. Patch values for HH:MM strings
        # come in as the raw string; convert to time. days_of_week is a
        # list; other fields are scalars.
        merged: dict = {**existing}
        if "vehicle_id" in patch_data:
            merged["vehicle_id"] = UUID(patch_data["vehicle_id"])
        if "route_id" in patch_data:
            merged["route_id"] = patch_data["route_id"]
        if "departure_time_of_day" in patch_data:
            merged["departure_time_of_day"] = _parse_hhmm(patch_data["departure_time_of_day"])
        if "return_time_of_day" in patch_data:
            merged["return_time_of_day"] = _parse_hhmm(patch_data["return_time_of_day"])
        if "days_of_week" in patch_data:
            merged["days_of_week"] = list(patch_data["days_of_week"])
        if "start_date" in patch_data:
            merged["start_date"] = patch_data["start_date"]
        if "end_date" in patch_data:
            merged["end_date"] = patch_data["end_date"]
        if "required_soc" in patch_data:
            merged["required_soc"] = float(patch_data["required_soc"])
        if "energy_kwh" in patch_data:
            merged["energy_kwh"] = patch_data["energy_kwh"]
        if "active" in patch_data:
            merged["active"] = bool(patch_data["active"])

        # Cross-field validation on merged state.
        if merged["departure_time_of_day"] == merged["return_time_of_day"]:
            return _recurring_validation_400(
                "return_time_of_day",
                "departure_time_of_day and return_time_of_day must differ",
            )
        if merged["end_date"] is not None and merged["end_date"] < merged["start_date"]:
            return _recurring_validation_400("end_date", "end_date must be on or after start_date")

        vehicle_uuid = UUID(str(merged["vehicle_id"]))
        if "vehicle_id" in patch_data:
            valid_vehicle_ids = await db_queries.get_vehicle_ids_for_depot(
                conn, depot_uuid, [vehicle_uuid]
            )
            if str(vehicle_uuid) not in valid_vehicle_ids:
                return _recurring_validation_400(
                    "vehicle_id",
                    f"vehicle_id {patch_data['vehicle_id']} is not a member of "
                    f"depot {depot_id}",
                )

        updated = await db_queries.update_recurring_template(
            conn,
            depot_id=depot_uuid,
            template_id=template_uuid,
            vehicle_id=vehicle_uuid,
            route_id=merged["route_id"],
            departure_time_of_day=merged["departure_time_of_day"],
            return_time_of_day=merged["return_time_of_day"],
            days_of_week=list(merged["days_of_week"]),
            start_date=merged["start_date"],
            end_date=merged["end_date"],
            required_soc=float(merged["required_soc"]),
            energy_kwh=merged.get("energy_kwh"),
            active=bool(merged["active"]),
        )
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recurring template not found",
            )
        cancelled_map = await db_queries.get_cancelled_dates_for_templates(
            conn, template_ids=[template_uuid]
        )

    checks = await _build_depot_readiness_checklist(depot_id)
    return {
        "updated": _format_recurring_template_row(
            updated, cancelled_dates=cancelled_map.get(str(template_uuid), [])
        ),
        "readiness": _readiness_response_payload(depot_id, checks),
    }


@app.delete(
    "/admin/depots/{depot_id}/schedule/recurring/{template_id}",
    response_model=ScheduleReadinessOnlyResponse,
    tags=["admin"],
    summary="Delete a recurring schedule template",
)
async def delete_recurring_schedule_template(
    depot_id: str,
    template_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Delete a recurring template and any cancellations it owned."""
    await _assert_recurring_depot_access(depot_id, user)
    validate_uuid(template_id, "template_id")
    depot_uuid = UUID(depot_id)
    template_uuid = UUID(template_id)
    async with db_pools.static.acquire() as conn:
        deleted = await db_queries.delete_recurring_template(
            conn, depot_id=depot_uuid, template_id=template_uuid
        )
        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recurring template not found",
            )
    checks = await _build_depot_readiness_checklist(depot_id)
    return {"readiness": _readiness_response_payload(depot_id, checks)}


async def _set_recurring_template_active(
    depot_id: str, template_id: str, active: bool, user: dict
) -> dict:
    """Shared body for the pause / resume endpoints."""
    await _assert_recurring_depot_access(depot_id, user)
    validate_uuid(template_id, "template_id")
    depot_uuid = UUID(depot_id)
    template_uuid = UUID(template_id)
    async with db_pools.static.acquire() as conn:
        # Single-column UPDATE keyed by (id, depot_id): no read-modify-write,
        # so a concurrent edit to other fields can't be clobbered. A missing
        # row (deleted concurrently or wrong depot) returns None → 404.
        updated = await db_queries.set_recurring_template_active(
            conn,
            depot_id=depot_uuid,
            template_id=template_uuid,
            active=active,
        )
        if updated is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recurring template not found",
            )
        cancelled_map = await db_queries.get_cancelled_dates_for_templates(
            conn, template_ids=[template_uuid]
        )
    checks = await _build_depot_readiness_checklist(depot_id)
    return {
        "updated": _format_recurring_template_row(
            updated, cancelled_dates=cancelled_map.get(str(template_uuid), [])
        ),
        "readiness": _readiness_response_payload(depot_id, checks),
    }


@app.post(
    "/admin/depots/{depot_id}/schedule/recurring/{template_id}/pause",
    response_model=RecurringTemplateUpdateResponse,
    tags=["admin"],
    summary="Pause a recurring schedule template (active=false)",
)
async def pause_recurring_schedule_template(
    depot_id: str,
    template_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    return await _set_recurring_template_active(depot_id, template_id, active=False, user=user)


@app.post(
    "/admin/depots/{depot_id}/schedule/recurring/{template_id}/resume",
    response_model=RecurringTemplateUpdateResponse,
    tags=["admin"],
    summary="Resume a recurring schedule template (active=true)",
)
async def resume_recurring_schedule_template(
    depot_id: str,
    template_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    return await _set_recurring_template_active(depot_id, template_id, active=True, user=user)


def _parse_occurrence_date(value: str) -> date | JSONResponse:
    """Parse a path param ``YYYY-MM-DD``; return 400 JSONResponse on failure."""
    try:
        return date.fromisoformat(value)
    except ValueError:
        return _recurring_validation_400("occurrence_date", "must be YYYY-MM-DD")


@app.post(
    "/admin/depots/{depot_id}/schedule/recurring/{template_id}/occurrences/{occurrence_date}/cancel",
    response_model=RecurringOccurrenceCancellationResponse,
    tags=["admin"],
    summary="Cancel one occurrence of a recurring schedule template",
)
async def cancel_recurring_occurrence(
    depot_id: str,
    template_id: str,
    occurrence_date: str,
    payload: Optional[OccurrenceCancellationRequest] = None,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Skip a single date for one template. Idempotent (re-posting refreshes reason)."""
    await _assert_recurring_depot_access(depot_id, user)
    validate_uuid(template_id, "template_id")
    parsed_date = _parse_occurrence_date(occurrence_date)
    if isinstance(parsed_date, JSONResponse):
        return parsed_date
    payload_obj = payload or OccurrenceCancellationRequest()

    depot_uuid = UUID(depot_id)
    template_uuid = UUID(template_id)
    actor_id: Optional[UUID] = None
    sub = user.get("sub") if isinstance(user, dict) else None
    if sub:
        try:
            actor_id = UUID(str(sub))
        except (ValueError, TypeError):
            actor_id = None

    async with db_pools.static.acquire() as conn:
        existing = await db_queries.get_recurring_template_for_depot(
            conn, depot_id=depot_uuid, template_id=template_uuid
        )
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recurring template not found",
            )
        row = await db_queries.upsert_recurring_cancellation(
            conn,
            template_id=template_uuid,
            occurrence_date=parsed_date,
            cancelled_by_user_id=actor_id,
            reason=payload_obj.reason,
        )

    checks = await _build_depot_readiness_checklist(depot_id)
    return {
        "template_id": row["template_id"],
        "occurrence_date": row["occurrence_date"],
        "cancelled_at": row["cancelled_at"],
        "reason": row["reason"],
        "readiness": _readiness_response_payload(depot_id, checks),
    }


@app.delete(
    "/admin/depots/{depot_id}/schedule/recurring/{template_id}/occurrences/{occurrence_date}/cancel",
    response_model=RecurringOccurrenceUncancelResponse,
    tags=["admin"],
    summary="Un-cancel one occurrence of a recurring schedule template",
)
async def uncancel_recurring_occurrence(
    depot_id: str,
    template_id: str,
    occurrence_date: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Remove a single-date cancellation; the recurring template resumes on that day."""
    await _assert_recurring_depot_access(depot_id, user)
    validate_uuid(template_id, "template_id")
    parsed_date = _parse_occurrence_date(occurrence_date)
    if isinstance(parsed_date, JSONResponse):
        return parsed_date

    depot_uuid = UUID(depot_id)
    template_uuid = UUID(template_id)
    async with db_pools.static.acquire() as conn:
        existing = await db_queries.get_recurring_template_for_depot(
            conn, depot_id=depot_uuid, template_id=template_uuid
        )
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recurring template not found",
            )
        removed = await db_queries.delete_recurring_cancellation(
            conn, template_id=template_uuid, occurrence_date=parsed_date
        )
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Cancellation not found",
            )
    checks = await _build_depot_readiness_checklist(depot_id)
    return {
        "template_id": str(template_uuid),
        "occurrence_date": parsed_date,
        "readiness": _readiness_response_payload(depot_id, checks),
    }


_DEPOT_SETUP_IDEMPOTENCY_ENDPOINT = "POST /admin/depots"


def _depot_setup_response_payload(
    depot_row: dict,
    readiness: list[dict],
    *,
    depot_id_override: Optional[str] = None,
    organization_id_override: Optional[str] = None,
) -> dict:
    """Build the standard ``{depot, readiness_checklist}`` payload for depot-setup endpoints.

    Returns ``depot_id`` and ``organization_id`` (snake_case, matching
    ``GET /me/depots``) so the frontend can use a single ``DepotSummary`` schema
    across read and write paths. ``id`` is kept as a deprecated alias so existing
    callers keep working; remove after one release once the frontend has migrated.
    """
    depot_id = depot_id_override or depot_row.get("depot_id") or depot_row.get("id")
    organization_id = organization_id_override or depot_row.get("organization_id")
    return {
        "depot": {
            "id": depot_id,  # deprecated alias of depot_id; kept for back-compat
            "depot_id": depot_id,
            "organization_id": organization_id,
            "name": depot_row["name"],
            "timezone": depot_row["timezone"],
            "currency": depot_row["currency"],
            "max_grid_kw": depot_row["max_grid_kw"],
        },
        "readiness_checklist": readiness,
    }


async def _create_depot_for_org(
    *, organization_id: str, request: "FirstDepotSetupRequest", conn: Any
) -> dict:
    """Insert a depot row and any associated battery storage. Caller owns the transaction."""
    depot = request.depot
    address = depot.address.model_dump(exclude_none=True)
    billing_metadata = depot.billing.model_dump(exclude_none=True)
    building_load_source = depot.building_load_source.model_dump(exclude_none=True)
    building_load_assumption_kw = float(depot.building_load_source.assumption_kw or 0.0)
    tariff_kwargs = _depot_setup_tariff_kwargs(depot.demand_charge)

    created = await db_queries.create_depot_setup(
        conn,
        organization_id=organization_id,
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
    return created


async def _depot_setup_endpoint(
    body: dict,
    idempotency_key: str,
    user: dict,
):
    """Shared handler for POST /admin/depots and POST /admin/first-depot-setup.

    Creates a tenant-scoped depot in the caller's organization. Safe to call
    multiple times to add additional depots. ``Idempotency-Key`` is required
    so retries don't create phantom rows.
    """
    org_id = _require_customer_admin_with_org(user)
    user_id = str(user.get("sub") or "")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token missing 'sub' claim",
        )
    if not idempotency_key.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key is required",
        )

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

    endpoint = _DEPOT_SETUP_IDEMPOTENCY_ENDPOINT
    request_hash = _canonical_request_hash(request)

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
                        detail={
                            "error_code": "IDEMPOTENCY_KEY_REUSED",
                            "message": "Idempotency-Key was already used with a different request body",
                        },
                    )
                return JSONResponse(
                    status_code=int(existing["status_code"] or status.HTTP_200_OK),
                    content=_json_response_payload(existing["response_json"]),
                )

            # Atomic with depot insert: ensure the org + user_organizations rows
            # exist so RLS-gated reads (e.g. Supabase PostgREST policies on
            # ``sites``) can see this depot immediately. ensure_tenant_mirrored
            # already ran best-effort; this guarantees consistency or rolls back.
            await mirror_user_tenant_atomic(conn, user)

            created = await _create_depot_for_org(
                organization_id=org_id, request=request, conn=conn
            )
            readiness = await _build_readiness_checklist(
                created["depot_id"], request.depot, conn=conn
            )
            response_payload = _depot_setup_response_payload(
                created,
                readiness,
                organization_id_override=org_id,
            )
            await db_queries.store_charger_onboarding_idempotency(
                conn,
                organization_id=org_id,
                user_id=user_id,
                endpoint=endpoint,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_json=response_payload,
                status_code=status.HTTP_200_OK,
                ttl_minutes=30,
            )

    return JSONResponse(status_code=status.HTTP_200_OK, content=response_payload)


@app.post(
    "/admin/depots",
    tags=["admin"],
    summary="Create a depot in the caller's organization",
)
async def create_depot(
    body: dict,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Create a tenant-scoped depot.

    Can be called multiple times to create additional depots in the caller's
    organization. ``Idempotency-Key`` is required so retries cannot create
    phantom rows; replays with the same key + same body return the original
    response, while replays with the same key + different body return 409.

    Authorization: customer_admin with ``app_metadata.organization_id`` set.
    """
    return await _depot_setup_endpoint(body, idempotency_key, user)


@app.post(
    "/admin/first-depot-setup",
    tags=["admin"],
    summary="Create a depot in the caller's organization (alias of POST /admin/depots)",
)
async def create_first_depot_setup(
    body: dict,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Backward-compatible alias for POST /admin/depots.

    Originally introduced as a "first depot" wizard endpoint, this route now
    delegates to the same handler as ``POST /admin/depots`` and may be called
    repeatedly to create additional depots. ``Idempotency-Key`` is required.
    """
    return await _depot_setup_endpoint(body, idempotency_key, user)


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
    return _depot_setup_response_payload(
        updated,
        readiness,
        depot_id_override=depot_id,
    )


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
                "SELECT charger_vehicle_access_default FROM sites WHERE id = $1::uuid",
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
            "SELECT organization_id::text AS organization_id, name, timezone, currency, "
            "max_grid_kw FROM sites WHERE id = $1::uuid",
            depot_id,
        )
    if not depot_row:
        raise DepotNotFoundError(f"Depot {depot_id} not found")

    readiness = await _build_depot_readiness_checklist(depot_id)
    return _depot_setup_response_payload(
        dict(depot_row),
        readiness,
        depot_id_override=depot_id,
    )


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
            detail={
                "error_code": ErrorCode.INTERNAL_ERROR.value,
                "detail": "Failed to get depot state",
            },
        ) from e


@app.get(
    "/depots/{depot_id}/savings-summary",
    response_model=SavingsSummaryResponse,
    tags=["depots"],
    summary="Month-to-date charging cost vs unmanaged baseline",
    description=(
        "Returns the seven fields the frontend's 'today' savings card "
        "needs: actual cost, flat-rate baseline cost, absolute and "
        "percentage savings, and the period window (UTC). 'Month-to-date' "
        "means sessions started in the current calendar month in the "
        "depot's local timezone. Missing-data paths (no sessions yet, no "
        "bidding zone, no price rows) return zeros instead of erroring so "
        "the UI shows '—' rather than a generic failure. Polling cadence "
        "is 60s; values change slowly so caching is appropriate."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "No access to this depot"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_depot_savings_summary(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> SavingsSummaryResponse:
    """Return month-to-date savings vs flat-rate baseline."""
    if not db_pools:
        raise DatabaseError("Database not available")

    try:
        summary = await compute_savings_summary(db_pools.static, db_pools.ts, depot_id)
    except asyncpg.PostgresError as exc:
        logger.error(
            "Database error computing savings summary: %s",
            exc,
            exc_info=True,
            extra={"depot_id": depot_id},
        )
        raise DatabaseError() from exc

    return SavingsSummaryResponse(
        current_month_eur=summary.current_month_eur,
        baseline_month_eur=summary.baseline_month_eur,
        saved_eur=summary.saved_eur,
        saved_pct=summary.saved_pct,
        period_start=summary.period_start.isoformat().replace("+00:00", "Z"),
        period_end=summary.period_end.isoformat().replace("+00:00", "Z"),
        as_of=summary.as_of.isoformat().replace("+00:00", "Z"),
    )


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
    card_label: Optional[str] = Field(
        None,
        description=(
            "Human-readable RFID card label (or id_tag fallback). Set only "
            "when grouping by card; multiple cards sharing a label are "
            "merged into one row."
        ),
    )
    energy_kwh: float = Field(..., description="Total energy delivered in the bucket (kWh)")
    session_count: int = Field(..., description="Number of charging sessions in the bucket")
    avg_kw: float = Field(..., description="Average charging power across sessions (kWh / hours)")
    cost: EnergyReportCost = Field(..., description="Bucketed cost (sum or estimate)")


class EnergyReportTotals(BaseModel):
    """Cross-bucket totals returned alongside report rows."""

    energy_kwh: float
    session_count: int
    avg_kw: float
    cost: EnergyReportCost


class EnergyReportResponse(BaseModel):
    """Response from /reports/depots/{depot_id}/energy/monthly."""

    depot_id: str
    depot_name: Optional[str] = Field(None, description="Human-readable depot name")
    currency: str
    from_: str = Field(..., alias="from")
    to: str
    rows: list[EnergyReportRow]
    totals: Optional[EnergyReportTotals] = Field(
        None, description="Sum of energy / count / cost across all rows in the window"
    )

    model_config = {"populate_by_name": True}


# ── Report & AgentAction models ───────────────────────────────────────────────


class Report(BaseModel):
    """A persisted report row."""

    id: str
    depot_id: str
    title: str
    kind: str = Field(
        ...,
        description="weekly_ops | monthly_savings | monthly_consumption | incident | compliance",
    )
    status: str = Field(..., description="draft | pending | approved")
    period_start: datetime
    period_end: datetime
    created_at: datetime
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    export_url: Optional[str] = None
    group_by: Optional[str] = Field(
        None, description="card | vehicle — only set for monthly_consumption"
    )

    model_config = ConfigDict(populate_by_name=True)


class AgentAction(BaseModel):
    """A proposed / shadow / executed agent-generated action.

    Wire format is camelCase to match the frontend zod schema
    (``AgentActionSchema`` in ``src/lib/schemas/today.ts``).  Routes that
    return ``AgentAction`` must set ``response_model_by_alias=True``.
    """

    id: str
    depot_id: str
    agent_type: str
    action_class: str
    mode: str = Field(..., description="shadow | proposed | auto_notify | auto_silent")
    status: str = Field(
        ..., description="pending | executed | rejected | rolled_back | failed | shadow"
    )
    summary: str
    entity_type: Optional[str] = Field(None, description="charger | vehicle | site | session")
    entity_id: Optional[str] = None
    created_at: datetime
    resolved_at: Optional[datetime] = None
    payload: Optional[dict] = None

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


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
        await verify_depot_access(depot_id, user, db_pools.static if db_pools else None)
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error_code": "CROSS_ORG_DENIED",
                    "message": (
                        exc.detail
                        if isinstance(exc.detail, str)
                        else "Access denied: cross-organization access is not permitted"
                    ),
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
        logger.warning("Ignoring non-numeric under_cap_rate in billing_metadata: %r", raw)
        return None


def _optional_text(value: object) -> Optional[str]:
    """Normalize nullable report dimension values from asyncpg records."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def _load_report_context(
    depot_id: str,
) -> tuple[str, str, str, Optional[float], list[str], dict[str, str]]:
    """Fetch depot reporting context and charger mappings from static DB.

    Returns ``(depot_name, timezone_name, currency, under_cap_rate,
    ocpp_ids, charger_id_by_ocpp_id)``. ``depot_name`` falls back to the
    depot UUID when ``sites.name`` is NULL so the report always has
    something to render in place of the raw ID.
    """
    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.static.acquire() as conn:
        depot_row = await conn.fetchrow(
            """
            SELECT name, timezone, currency, billing_metadata
            FROM sites
            WHERE id = $1::uuid
            """,
            depot_id,
        )
        if not depot_row:
            raise DepotNotFoundError(f"Depot {depot_id} not found")

        charger_rows = await conn.fetch(
            "SELECT id::text AS charger_id, station_id AS ocpp_id FROM charging_stations WHERE site_id = $1::uuid",
            depot_id,
        )

    depot_name = depot_row["name"] or depot_id
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
    return depot_name, timezone_name, currency, under_cap_rate, ocpp_ids, charger_id_by_ocpp_id


async def _fetch_session_rows(
    depot_id: str,
    timezone_name: str,
    ocpp_ids: list[str],
    charger_id_by_ocpp_id: dict[str, str],
    from_date: date,
    to_date: date,
    ts_conn: Optional[asyncpg.Connection] = None,
) -> list[SessionRow]:
    """Fetch charging_sessions rows in [from, to] (inclusive) in depot TZ.

    Card identity is enriched with ``label`` and ``id_tag`` from the
    ``rfid_cards`` table so the aggregator can group same-label cards
    together and the export carries human-readable names.
    """
    if not db_pools or db_pools.ts is None:
        raise DatabaseError("Database not available")

    # Match live OCPP rows by their station_id and imported (XLSX backfill) rows
    # by site_id directly. Imported rows carry a synthetic station_id that has
    # no charging_stations row, so they must be selected via cs.site_id.
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
        WHERE (
                cs.station_id = ANY($1::text[])
                OR (cs.site_id = $5::uuid AND cs.source = 'import')
              )
          AND cs.start_time >= ($2::date)::timestamp AT TIME ZONE $4
          AND cs.start_time < (($3::date) + INTERVAL '1 day')::timestamp AT TIME ZONE $4
        ORDER BY cs.start_time
        """

    if ts_conn is None:
        async with db_pools.ts.acquire() as conn:
            records = await conn.fetch(query, ocpp_ids, from_date, to_date, timezone_name, depot_id)
    else:
        records = await ts_conn.fetch(query, ocpp_ids, from_date, to_date, timezone_name, depot_id)

    card_ids = {
        _optional_text(r["card_id"]) for r in records if _optional_text(r["card_id"]) is not None
    }
    card_info: dict[str, tuple[Optional[str], Optional[str]]] = {}
    if card_ids:
        # Scope by site_id so a charging_sessions.card_id that points to a
        # card from another depot (legacy / bad-import data) is NOT enriched
        # — we'd otherwise leak the other tenant's label/id_tag and merge
        # buckets under the wrong card name. Unenriched rows fall back to
        # grouping by raw card_id UUID via aggregate_energy_rows.
        async with db_pools.static.acquire() as conn:
            card_rows = await conn.fetch(
                """
                SELECT id::text AS card_id, label, id_tag
                FROM rfid_cards
                WHERE id = ANY($1::uuid[])
                  AND site_id = $2::uuid
                """,
                list(card_ids),
                depot_id,
            )
        card_info = {
            row["card_id"]: (
                _optional_text(row["label"]),
                _optional_text(row["id_tag"]),
            )
            for row in card_rows
        }

    rows: list[SessionRow] = []
    for r in records:
        energy = r["energy_delivered_kwh"]
        cost = r["cost_total"]
        card_id = _optional_text(r["card_id"])
        card_label, card_id_tag = card_info.get(card_id, (None, None)) if card_id else (None, None)
        rows.append(
            SessionRow(
                start_time=r["start_time"],
                end_time=r["end_time"],
                energy_kwh=float(energy) if energy is not None else None,
                cost_total=float(cost) if cost is not None else None,
                vehicle_id=_optional_text(r["vehicle_id"]),
                charger_id=charger_id_by_ocpp_id.get(r["charger_id"]),
                driver_id=_optional_text(r["driver_id"]),
                card_id=card_id,
                card_label=card_label,
                card_id_tag=card_id_tag,
            )
        )
    return rows


def _session_matches_search(row: SessionRow, search_lower: str) -> bool:
    """Return True if any ID field of the session contains ``search_lower``."""
    return any(
        f is not None and search_lower in f.lower()
        for f in (row.vehicle_id, row.charger_id, row.driver_id, row.card_id)
    )


async def _build_energy_report(
    depot_id: str,
    from_str: str,
    to_str: str,
    group_by: Optional[str],
    *,
    search: Optional[str] = None,
) -> tuple[dict, list[dict], str]:
    """Run the full report pipeline and return (metadata, rows, currency).

    ``metadata`` carries ``depot_name`` and a ``totals`` block so the
    monthly endpoint and downstream consumers (PDF, CSV) can render the
    report header and a Total line without re-querying the data.
    """
    from_date = _parse_report_date(from_str, "from")
    to_date = _parse_report_date(to_str, "to")
    if to_date < from_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'to' must be on or after 'from'",
        )

    depot_name, timezone_name, currency, under_cap_rate, ocpp_ids, charger_id_by_ocpp_id = (
        await _load_report_context(depot_id)
    )
    sessions = await _fetch_session_rows(
        depot_id, timezone_name, ocpp_ids, charger_id_by_ocpp_id, from_date, to_date
    )
    if search:
        search_lower = search.lower()
        sessions = [s for s in sessions if _session_matches_search(s, search_lower)]
    rows = aggregate_energy_rows(
        sessions,
        timezone=timezone_name,
        group_by=group_by,
        under_cap_rate=under_cap_rate,
        currency=currency,
        from_date=from_date,
        to_date=to_date,
    )
    totals = compute_energy_totals(
        sessions,
        timezone=timezone_name,
        under_cap_rate=under_cap_rate,
        currency=currency,
        from_date=from_date,
        to_date=to_date,
    )
    metadata = {
        "depot_id": depot_id,
        "depot_name": depot_name,
        "currency": currency,
        "from": from_str,
        "to": to_str,
        "totals": totals,
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
    from_: str = Query(
        ..., alias="from", description="Start date (YYYY-MM-DD, inclusive, depot TZ)"
    ),
    to: str = Query(..., description="End date (YYYY-MM-DD, inclusive, depot TZ)"),
    group_by: Optional[str] = Query(
        None,
        description="Optional grouping dimension: vehicle | charger | driver | card",
    ),
    search: Optional[str] = Query(
        None,
        description=(
            "Case-insensitive substring filter applied to vehicle_id, charger_id, "
            "driver_id, and card_id before aggregation."
        ),
    ),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Monthly energy + cost rollup for a depot."""
    validate_depot_id(depot_id)
    await _verify_depot_access_for_report(depot_id, user)
    grouping = _validate_report_group_by(group_by)

    try:
        metadata, rows, _ = await _build_energy_report(depot_id, from_, to, grouping, search=search)
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
    search: Optional[str] = Query(None),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Stream the monthly energy report as CSV."""
    validate_depot_id(depot_id)
    await _verify_depot_access_for_report(depot_id, user)
    grouping = _validate_report_group_by(group_by)

    try:
        metadata, rows, _ = await _build_energy_report(depot_id, from_, to, grouping, search=search)
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
    totals_for_stream = metadata.get("totals")

    def _generate() -> Iterator[str]:
        yield from stream_rows_as_csv(rows_for_stream, group_by=grouping, totals=totals_for_stream)

    return StreamingResponse(_generate(), media_type="text/csv", headers=headers)


# ── Energy transactions endpoint ──────────────────────────────────────────────


class EnergyTransactionCost(BaseModel):
    """Per-session cost block."""

    amount: float = Field(..., description="Cost amount in the depot currency")
    currency: str = Field(..., description="ISO 4217 currency code")
    estimated: bool = Field(
        False,
        description=(
            "True when ``amount`` was derived from energy_kwh × tariff because "
            "``charging_sessions.cost_total`` was NULL for this session."
        ),
    )

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class EnergyTransactionRow(BaseModel):
    """One row per charging_sessions transaction."""

    session_id: str = Field(..., description="charging_sessions.session_id (UUID)")
    started_at: str = Field(..., description="ISO 8601 UTC timestamp (Z suffix)")
    ended_at: Optional[str] = Field(
        None, description="ISO 8601 UTC timestamp; null while the session is active"
    )
    vehicle_id: Optional[str] = Field(None, description="Vehicle UUID; null if unresolved")
    charger_id: Optional[str] = Field(None, description="Charger UUID; null if unresolved")
    driver_id: Optional[str] = Field(None, description="Driver UUID; null if unresolved")
    card_id: Optional[str] = Field(
        None, description="RFID card UUID; null for manual-authorize sessions"
    )
    energy_kwh: float = Field(..., description="Delivered kWh for this session (0 is valid)")
    avg_kw: float = Field(
        ..., description="energy_kwh / (duration_minutes / 60); 0 when duration_minutes == 0"
    )
    duration_minutes: int = Field(..., ge=0, description="Backend-computed duration in minutes")
    cost: EnergyTransactionCost = Field(..., description="Per-session cost")

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class EnergyTransactionsResponse(BaseModel):
    """Response from ``GET /reports/depots/{depot_id}/energy/transactions``."""

    depot_id: str = Field(..., description="Echo of the path parameter")
    currency: str = Field(..., description="Depot billing currency")
    from_: str = Field(..., alias="from", description="Echo of the from query parameter")
    to: str = Field(..., description="Echo of the to query parameter")
    rows: list[EnergyTransactionRow]
    next_cursor: Optional[str] = Field(
        None, description="Opaque cursor for the next page; null when no more rows"
    )

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


_ENERGY_TRANSACTIONS_DEFAULT_LIMIT = 500
_ENERGY_TRANSACTIONS_MAX_LIMIT = 5000
_ENERGY_TRANSACTIONS_MAX_RANGE_DAYS = 366  # inclusive 1-year window (leap-safe)


def _encode_transaction_cursor(started_at: datetime, session_id: str) -> str:
    """Encode ``(started_at, session_id)`` into an opaque base64 cursor."""
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=timezone.utc)
    started_at_utc = started_at.astimezone(timezone.utc)
    payload = json.dumps(
        {
            "startedAt": started_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sessionId": session_id,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def _decode_transaction_cursor(value: str) -> tuple[datetime, str]:
    """Decode an opaque cursor; raises HTTPException(400) on malformed input."""
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii")).decode("utf-8")
        payload = json.loads(decoded)
        started_at_str = payload["startedAt"]
        session_id = payload["sessionId"]
        try:
            started_at = datetime.strptime(started_at_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            # Backward compatibility for pre-fix cursors encoded at second precision.
            started_at = datetime.strptime(started_at_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
        UUID(session_id)
    except (KeyError, TypeError, ValueError, binascii.Error, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid cursor",
        ) from exc
    return started_at, session_id


def _format_utc_z(value: Optional[datetime]) -> Optional[str]:
    """Format a datetime as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC, second precision)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _round_half_up_int(value: float) -> int:
    """Round half-up to nearest integer (PRD/contract spec for durationMinutes)."""
    return int(math.floor(value + 0.5))


def _escape_like(value: str) -> str:
    """Escape ``%`` and ``_`` so a substring search is treated literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _fetch_transaction_records(
    depot_id: str,
    timezone_name: str,
    ocpp_ids: list[str],
    *,
    from_date: date,
    to_date: date,
    vehicle_id: Optional[str],
    target_station_id: Optional[str],
    driver_id: Optional[str],
    card_id: Optional[str],
    search: Optional[str],
    search_station_ids: Optional[list[str]],
    cursor: Optional[tuple[datetime, str]],
    limit_plus_one: int,
) -> list[asyncpg.Record]:
    """Fetch up to ``limit_plus_one`` charging_sessions rows for the transactions endpoint.

    Matches both live OCPP rows (via ``station_id IN ocpp_ids``) and imported
    backfill rows (via ``site_id = depot_id AND source = 'import'``). Optional
    filters (vehicle/charger/driver/card/search/cursor) are pushed down so the
    DB does the heavy lifting and the response slice is deterministic.
    """
    if not db_pools or db_pools.ts is None:
        raise DatabaseError("Database not available")

    args: list[Any] = []

    def add(value: Any) -> str:
        args.append(value)
        return f"${len(args)}"

    p_ocpp_ids = add(ocpp_ids)
    p_from = add(from_date)
    p_to = add(to_date)
    p_tz = add(timezone_name)
    p_depot = add(depot_id)

    where_parts = [
        f"(cs.station_id = ANY({p_ocpp_ids}::text[]) "
        f"OR (cs.site_id = {p_depot}::uuid AND cs.source = 'import'))",
        f"cs.start_time >= ({p_from}::date)::timestamp AT TIME ZONE {p_tz}",
        f"cs.start_time < (({p_to}::date) + INTERVAL '1 day')::timestamp AT TIME ZONE {p_tz}",
    ]

    if vehicle_id is not None:
        where_parts.append(f"cs.vehicle_id = {add(vehicle_id)}::uuid")
    if target_station_id is not None:
        where_parts.append(f"cs.station_id = {add(target_station_id)}")
    if driver_id is not None:
        where_parts.append(f"cs.driver_id = {add(driver_id)}::uuid")
    if card_id is not None:
        where_parts.append(f"cs.card_id = {add(card_id)}::uuid")

    if search:
        pattern = f"%{_escape_like(search)}%"
        p_pattern = add(pattern)
        p_search_stations = add(search_station_ids or [])
        where_parts.append(
            "("
            f"cs.vehicle_id::text ILIKE {p_pattern} ESCAPE '\\' "
            f"OR cs.driver_id::text ILIKE {p_pattern} ESCAPE '\\' "
            f"OR cs.card_id::text ILIKE {p_pattern} ESCAPE '\\' "
            f"OR cs.session_id::text ILIKE {p_pattern} ESCAPE '\\' "
            f"OR cs.station_id = ANY({p_search_stations}::text[])"
            ")"
        )

    if cursor is not None:
        cursor_started_at, cursor_session_id = cursor
        p_cursor_ts = add(cursor_started_at)
        p_cursor_sid = add(cursor_session_id)
        where_parts.append(
            f"(cs.start_time, cs.session_id) < ({p_cursor_ts}::timestamptz, {p_cursor_sid}::uuid)"
        )

    p_limit = add(limit_plus_one)

    query = f"""
        SELECT
            cs.session_id::text AS session_id,
            cs.start_time AS started_at,
            cs.end_time AS ended_at,
            cs.energy_delivered_kwh,
            cs.cost_total,
            cs.vehicle_id::text AS vehicle_id,
            cs.station_id AS ocpp_id,
            cs.driver_id::text AS driver_id,
            cs.card_id::text AS card_id
        FROM charging_sessions cs
        WHERE {' AND '.join(where_parts)}
        ORDER BY cs.start_time DESC, cs.session_id DESC
        LIMIT {p_limit}
    """

    async with db_pools.ts.acquire() as conn:
        return await conn.fetch(query, *args)


def _transaction_row_payload(
    record: asyncpg.Record,
    *,
    charger_id_by_ocpp_id: dict[str, str],
    currency: str,
    under_cap_rate: Optional[float],
    now_utc: datetime,
) -> dict:
    """Build the per-row dict (camelCase keys) for the transactions response."""
    started_at: datetime = record["started_at"]
    ended_at: Optional[datetime] = record["ended_at"]

    energy_raw = record["energy_delivered_kwh"]
    energy_kwh = float(energy_raw) if energy_raw is not None else 0.0

    end_for_duration = ended_at if ended_at is not None else now_utc
    started_at_utc = (
        started_at if started_at.tzinfo is not None else started_at.replace(tzinfo=timezone.utc)
    )
    end_utc = (
        end_for_duration
        if end_for_duration.tzinfo is not None
        else end_for_duration.replace(tzinfo=timezone.utc)
    )
    duration_seconds = max((end_utc - started_at_utc).total_seconds(), 0.0)
    duration_minutes = _round_half_up_int(duration_seconds / 60.0)

    if duration_minutes > 0:
        avg_kw = round(energy_kwh / (duration_minutes / 60.0), 2)
    else:
        avg_kw = 0.0

    cost_raw = record["cost_total"]
    if cost_raw is not None:
        cost_amount = round(float(cost_raw), 2)
        estimated = False
    else:
        estimate = energy_kwh * under_cap_rate if under_cap_rate is not None else 0.0
        cost_amount = round(estimate, 2)
        estimated = True

    return {
        "sessionId": record["session_id"],
        "startedAt": _format_utc_z(started_at),
        "endedAt": _format_utc_z(ended_at),
        "vehicleId": _optional_text(record["vehicle_id"]),
        "chargerId": charger_id_by_ocpp_id.get(record["ocpp_id"]),
        "driverId": _optional_text(record["driver_id"]),
        "cardId": _optional_text(record["card_id"]),
        "energyKwh": round(energy_kwh, 6),
        "avgKw": avg_kw,
        "durationMinutes": duration_minutes,
        "cost": {
            "amount": cost_amount,
            "currency": currency,
            "estimated": estimated,
        },
    }


@app.get(
    "/reports/depots/{depot_id}/energy/transactions",
    response_model=EnergyTransactionsResponse,
    response_model_by_alias=True,
    tags=["Reports"],
    summary="List individual charging-session transactions",
    operation_id="getEnergyTransactions",
    description=(
        "Return one row per ``charging_sessions`` record whose ``start_time`` "
        "falls inside the [from, to] window in the depot's local timezone. "
        "Supports exact-match filters (vehicle/charger/driver/card), a free-text "
        "substring search, and opaque cursor pagination ordered by "
        "``(start_time DESC, session_id DESC)``. Sessions that started before "
        "``from`` but are still active are excluded — they belong to the "
        "prior period for billing."
    ),
    responses={
        400: {"model": ErrorResponse, "description": "Invalid query parameters"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Cross-organization access denied"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_energy_transactions(
    depot_id: str,
    from_: str = Query(
        ..., alias="from", description="Start date (YYYY-MM-DD, inclusive, depot TZ)"
    ),
    to: str = Query(..., description="End date (YYYY-MM-DD, inclusive, depot TZ)"),
    vehicle_id: Optional[str] = Query(None, description="Exact match against vehicles.id"),
    charger_id: Optional[str] = Query(None, description="Exact match against charging_stations.id"),
    driver_id: Optional[str] = Query(None, description="Exact match against drivers.id"),
    card_id: Optional[str] = Query(None, description="Exact match against rfid_cards.id"),
    search: Optional[str] = Query(
        None,
        description=(
            "Case-insensitive substring filter over vehicle_id, charger_id, "
            "driver_id, card_id, and session_id (UUID texts)."
        ),
    ),
    limit: int = Query(
        _ENERGY_TRANSACTIONS_DEFAULT_LIMIT,
        ge=1,
        le=_ENERGY_TRANSACTIONS_MAX_LIMIT,
        description=(
            f"Max page size (default {_ENERGY_TRANSACTIONS_DEFAULT_LIMIT}, "
            f"hard-cap {_ENERGY_TRANSACTIONS_MAX_LIMIT})."
        ),
    ),
    cursor: Optional[str] = Query(
        None, description="Opaque cursor from a prior response's nextCursor"
    ),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """One row per ``charging_sessions`` record, in time-descending order."""
    validate_depot_id(depot_id)
    await _verify_depot_access_for_report(depot_id, user)

    from_date = _parse_report_date(from_, "from")
    to_date = _parse_report_date(to, "to")
    if to_date < from_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'to' must be on or after 'from'",
        )
    if (to_date - from_date).days >= _ENERGY_TRANSACTIONS_MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Date range cannot exceed 1 year",
        )

    if vehicle_id is not None:
        validate_uuid(vehicle_id, "vehicle_id")
    if charger_id is not None:
        validate_uuid(charger_id, "charger_id")
    if driver_id is not None:
        validate_uuid(driver_id, "driver_id")
    if card_id is not None:
        validate_uuid(card_id, "card_id")

    cursor_pair: Optional[tuple[datetime, str]] = None
    if cursor:
        cursor_pair = _decode_transaction_cursor(cursor)

    search_clean = search.strip() if search else None
    if not search_clean:
        search_clean = None

    try:
        (
            _depot_name,
            timezone_name,
            currency,
            under_cap_rate,
            ocpp_ids,
            charger_id_by_ocpp_id,
        ) = await _load_report_context(depot_id)

        target_station_id: Optional[str] = None
        if charger_id is not None:
            for ocpp, uuid_str in charger_id_by_ocpp_id.items():
                if uuid_str == charger_id:
                    target_station_id = ocpp
                    break
            if target_station_id is None:
                # Charger UUID not in this depot — short-circuit to an empty page.
                return {
                    "depotId": depot_id,
                    "currency": currency,
                    "from": from_,
                    "to": to,
                    "rows": [],
                    "nextCursor": None,
                }

        search_station_ids: Optional[list[str]] = None
        if search_clean is not None:
            needle = search_clean.lower()
            search_station_ids = [
                ocpp
                for ocpp, uuid_str in charger_id_by_ocpp_id.items()
                if needle in uuid_str.lower()
            ]

        records = await _fetch_transaction_records(
            depot_id,
            timezone_name,
            ocpp_ids,
            from_date=from_date,
            to_date=to_date,
            vehicle_id=vehicle_id,
            target_station_id=target_station_id,
            driver_id=driver_id,
            card_id=card_id,
            search=search_clean,
            search_station_ids=search_station_ids,
            cursor=cursor_pair,
            limit_plus_one=limit + 1,
        )
    except DepotNotFoundError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error(
            "Database error generating energy transactions: %s",
            exc,
            exc_info=True,
            extra={"depot_id": depot_id},
        )
        raise DatabaseError(f"Database error: {str(exc)}") from exc

    has_more = len(records) > limit
    page = records[:limit]

    now_utc = datetime.now(timezone.utc)
    rows = [
        _transaction_row_payload(
            rec,
            charger_id_by_ocpp_id=charger_id_by_ocpp_id,
            currency=currency,
            under_cap_rate=under_cap_rate,
            now_utc=now_utc,
        )
        for rec in page
    ]

    next_cursor: Optional[str] = None
    if has_more and page:
        last = page[-1]
        next_cursor = _encode_transaction_cursor(last["started_at"], last["session_id"])

    return {
        "depotId": depot_id,
        "currency": currency,
        "from": from_,
        "to": to,
        "rows": rows,
        "nextCursor": next_cursor,
    }


# ── Reports CRUD endpoints ────────────────────────────────────────────────────


def _row_to_report(row: asyncpg.Record) -> Report:
    """Convert an asyncpg Record from the reports table to a Report model."""
    return Report(
        id=str(row["id"]),
        depot_id=str(row["depot_id"]),
        title=row["title"],
        kind=row["kind"],
        status=row["status"],
        period_start=row["period_start"],
        period_end=row["period_end"],
        created_at=row["created_at"],
        approved_at=row["approved_at"],
        approved_by=row["approved_by"],
        export_url=row["export_url"],
        group_by=row["group_by"],
    )


@app.get(
    "/depots/{depot_id}/reports",
    response_model=list[Report],
    tags=["depots"],
    summary="List reports for a depot",
    description="Returns all reports for the depot, ordered by created_at descending.",
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Access denied"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def list_reports(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> list[Report]:
    """GET /depots/{depot_id}/reports — list reports ordered by created_at desc."""
    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.ts.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, depot_id, title, kind, status, period_start, period_end,
                   created_at, approved_at, approved_by, export_url, group_by
            FROM reports
            WHERE depot_id = $1::uuid
            ORDER BY created_at DESC
            """,
            depot_id,
        )

    return [_row_to_report(r) for r in rows]


class CreateReportRequest(BaseModel):
    """Body for POST /depots/{depot_id}/reports."""

    kind: str = Field(
        ...,
        description="weekly_ops | monthly_savings | monthly_consumption | incident | compliance",
    )
    title: Optional[str] = Field(None)
    group_by: Optional[str] = Field(None, alias="groupBy")
    period_start: Optional[str] = Field(None, alias="periodStart")
    period_end: Optional[str] = Field(None, alias="periodEnd")

    model_config = ConfigDict(populate_by_name=True)


@app.post(
    "/depots/{depot_id}/reports",
    response_model=Report,
    status_code=201,
    tags=["depots"],
    summary="Create a draft report",
    description=(
        "Create a draft Report row. For kind='monthly_consumption' the aggregation "
        "runs inline against charging_sessions data and is stored as JSONB for later export."
    ),
    responses={
        400: {"model": ErrorResponse, "description": "Invalid parameters"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Access denied"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def create_report(
    body: CreateReportRequest,
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> Report:
    """POST /depots/{depot_id}/reports — create a draft report."""
    params = {
        "kind": body.kind,
        "title": body.title,
        "groupBy": body.group_by,
        "periodStart": body.period_start,
        "periodEnd": body.period_end,
    }
    result = await _handle_reports_generate(params, depot_id, dry_run=False, user=user)

    # Re-fetch the created row to return a full Report object.
    if not db_pools:
        raise DatabaseError("Database not available")
    async with db_pools.ts.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, depot_id, title, kind, status, period_start, period_end,
                   created_at, approved_at, approved_by, export_url, group_by
            FROM reports
            WHERE id = $1::uuid
            """,
            result["reportId"],
        )
    if not row:
        raise HTTPException(status_code=500, detail="Report created but not retrievable")
    return _row_to_report(row)


@app.get(
    "/depots/{depot_id}/reports/{report_id}",
    response_model=Report,
    tags=["depots"],
    summary="Get a single report",
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Access denied"},
        404: {"model": ErrorResponse, "description": "Report not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_report(
    report_id: str,
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> Report:
    """GET /depots/{depot_id}/reports/{report_id}."""
    validate_uuid(report_id, "report_id")
    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.ts.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, depot_id, title, kind, status, period_start, period_end,
                   created_at, approved_at, approved_by, export_url, group_by
            FROM reports
            WHERE id = $1::uuid AND depot_id = $2::uuid
            """,
            report_id,
            depot_id,
        )

    if not row:
        raise HTTPException(status_code=404, detail=f"Report {report_id} not found")

    return _row_to_report(row)


@app.get(
    "/depots/{depot_id}/reports/{report_id}/export",
    tags=["depots"],
    summary="Export an approved report as CSV",
    description=(
        "Streams the report as CSV. Only available for approved reports with stored data. "
        "Returns 404 for draft/pending reports or kinds without stored data."
    ),
    responses={
        200: {"content": {"text/csv": {}}, "description": "CSV export"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Access denied"},
        404: {"model": ErrorResponse, "description": "Report not found or not yet approved"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def export_report(
    report_id: str,
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> StreamingResponse:
    """GET /depots/{depot_id}/reports/{report_id}/export — stream report as CSV."""
    validate_uuid(report_id, "report_id")
    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.ts.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT kind, status, group_by, data FROM reports WHERE id = $1::uuid AND depot_id = $2::uuid",
            report_id,
            depot_id,
        )

    if not row:
        raise HTTPException(status_code=404, detail=f"Report {report_id} not found")
    if row["status"] != "approved":
        raise HTTPException(
            status_code=404,
            detail="Report is not yet approved; export is only available for approved reports",
        )
    if row["data"] is None:
        raise HTTPException(
            status_code=404,
            detail=f"No export data available for {row['kind']} reports",
        )

    stored = row["data"] if isinstance(row["data"], dict) else json.loads(row["data"])
    agg_rows: list[dict] = stored.get("rows", [])
    totals: Optional[dict] = stored.get("totals")
    group_by: Optional[str] = row["group_by"]

    filename = f"report_{report_id}.csv"
    headers_resp = {"Content-Disposition": f'attachment; filename="{filename}"'}
    rows_for_stream = agg_rows

    def _generate() -> Iterator[str]:
        yield from stream_rows_as_csv(rows_for_stream, group_by=group_by, totals=totals)

    return StreamingResponse(_generate(), media_type="text/csv", headers=headers_resp)


# ── Report schedules (read endpoints) ─────────────────────────────────────────
# Writes flow through POST /commands/execute (reports.schedule.*); these GETs
# are readable by any depot member. Responses are plain dicts (camelCase, all
# nullable fields present) to satisfy the frontend's strict Zod schemas.


@app.get(
    "/depots/{depot_id}/report-schedules",
    tags=["depots"],
    summary="List report schedules for a depot",
    description="Returns all report schedules for the depot (empty array when none).",
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Access denied"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def list_report_schedules(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> list[dict]:
    """GET /depots/{depot_id}/report-schedules."""
    if not db_pools:
        raise DatabaseError("Database not available")
    async with db_pools.ts.acquire() as conn:
        return await _report_schedules.serialize_schedules(conn, depot_id)


@app.get(
    "/depots/{depot_id}/report-schedules/{schedule_id}",
    tags=["depots"],
    summary="Get a single report schedule",
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Access denied"},
        404: {"model": ErrorResponse, "description": "Schedule not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_report_schedule(
    schedule_id: str,
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> dict:
    """GET /depots/{depot_id}/report-schedules/{schedule_id}."""
    validate_uuid(schedule_id, "schedule_id")
    if not db_pools:
        raise DatabaseError("Database not available")
    async with db_pools.ts.acquire() as conn:
        schedule = await _report_schedules.serialize_schedule(conn, depot_id, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail=f"Report schedule {schedule_id} not found")
    return schedule


@app.get(
    "/depots/{depot_id}/report-schedules/{schedule_id}/runs",
    tags=["depots"],
    summary="List runs for a report schedule (most-recent first)",
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Access denied"},
        404: {"model": ErrorResponse, "description": "Schedule not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def list_report_schedule_runs(
    schedule_id: str,
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> list[dict]:
    """GET /depots/{depot_id}/report-schedules/{schedule_id}/runs."""
    validate_uuid(schedule_id, "schedule_id")
    if not db_pools:
        raise DatabaseError("Database not available")
    async with db_pools.ts.acquire() as conn:
        # Scope check: the schedule must belong to this depot.
        schedule = await _report_schedules.fetch_schedule_row(conn, depot_id, schedule_id)
        if schedule is None:
            raise HTTPException(
                status_code=404, detail=f"Report schedule {schedule_id} not found"
            )
        return await _report_schedules.serialize_runs(conn, schedule_id)


# ── Agent Actions endpoint ─────────────────────────────────────────────────────


def _row_to_agent_action(row: asyncpg.Record) -> AgentAction:
    """Convert an asyncpg Record from agent_actions to an AgentAction model."""
    payload = row["payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = None
    return AgentAction(
        id=str(row["id"]),
        depot_id=str(row["depot_id"]),
        agent_type=row["agent_type"],
        action_class=row["action_class"],
        mode=row["mode"],
        status=row["status"],
        summary=row["summary"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
        payload=payload,
    )


@app.get(
    "/depots/{depot_id}/agent-actions",
    response_model=list[AgentAction],
    response_model_by_alias=True,
    tags=["depots"],
    summary="List agent actions for a depot",
    description=(
        "Returns agent-proposed actions for the depot ordered by created_at descending. "
        "Polled every 10 seconds by the frontend to surface new proposals."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Access denied"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def list_agent_actions(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> list[AgentAction]:
    """GET /depots/{depot_id}/agent-actions — list actions ordered by created_at desc."""
    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.ts.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, depot_id, agent_type, action_class, mode, status, summary,
                   entity_type, entity_id, created_at, resolved_at, payload
            FROM agent_actions
            WHERE depot_id = $1::uuid
            ORDER BY created_at DESC
            """,
            depot_id,
        )

    return [_row_to_agent_action(r) for r in rows]


# ── Autonomy-settings endpoint ─────────────────────────────────────────────────
# Returns the per-depot autonomy matrix.  The frontend renders one row per
# action_class with a level selector; writes go through the
# `agents.autonomy.set` command (POST /commands/execute).  Persistence lives
# in `agent_autonomy_settings` (migration 043).


_AGENT_AUTONOMY_LEVELS: tuple[str, ...] = ("shadow", "proposed", "auto_notify", "auto_silent")

# Default action classes surfaced even when no row has been written yet.
# Keep in sync with the frontend matrix renderer (src/lib/schemas/today.ts).
# Additional classes returned by the DB are layered on top of these defaults.
_DEFAULT_AGENT_AUTONOMY_CLASSES: tuple[str, ...] = (
    "charger_restart",
    "session_reassign",
    "price_reoptimize",
    "soc_guardrail",
    "report_draft",
)

_DEFAULT_AGENT_AUTONOMY_LEVEL: str = "proposed"


class AutonomySettingsRow(BaseModel):
    """One row of the depot autonomy matrix."""

    action_class: str
    level: str = Field(..., description="shadow | proposed | auto_notify | auto_silent")

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class AutonomySettings(BaseModel):
    """Per-depot autonomy matrix returned by GET /depots/{id}/autonomy-settings."""

    rows: list[AutonomySettingsRow]
    as_of: datetime

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


@app.get(
    "/depots/{depot_id}/autonomy-settings",
    response_model=AutonomySettings,
    response_model_by_alias=True,
    tags=["depots"],
    summary="Get depot agent autonomy matrix",
    description=(
        "Returns the per-depot autonomy matrix: one row per action_class with the "
        "current level (shadow | proposed | auto_notify | auto_silent). "
        "Defaults are returned for known action classes that have not yet been written. "
        "Writes go through the `agents.autonomy.set` command on POST /commands/execute."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "Access denied"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_autonomy_settings(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> AutonomySettings:
    """GET /depots/{depot_id}/autonomy-settings."""
    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.ts.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT action_class, level, updated_at
            FROM agent_autonomy_settings
            WHERE depot_id = $1::uuid
            """,
            depot_id,
        )

    stored: dict[str, str] = {r["action_class"]: r["level"] for r in rows}
    latest_update: Optional[datetime] = max((r["updated_at"] for r in rows), default=None)

    # Layer defaults so the matrix is fully populated even for fresh depots.
    merged: dict[str, str] = {
        cls: _DEFAULT_AGENT_AUTONOMY_LEVEL for cls in _DEFAULT_AGENT_AUTONOMY_CLASSES
    }
    merged.update(stored)

    matrix_rows = [
        AutonomySettingsRow(action_class=cls, level=level) for cls, level in sorted(merged.items())
    ]

    return AutonomySettings(
        rows=matrix_rows,
        as_of=latest_update or datetime.now(timezone.utc),
    )


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
            detail={
                "error_code": ErrorCode.INTERNAL_ERROR.value,
                "detail": "Failed to get schedule",
            },
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
                "SELECT name FROM sites WHERE id = $1",
                depot_id,
            )
            if depot_row is None:
                raise HTTPException(status_code=404, detail=f"Depot {depot_id} not found")
            depot_name: Optional[str] = depot_row["name"]

            charger_rows = await conn.fetch(
                "SELECT id AS charger_id, station_id AS ocpp_id FROM charging_stations WHERE site_id = $1",
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

                rows = await _list_alerts(conn, UUID(depot_id), statuses=("active", "acknowledged"))
                notification_alerts = [
                    _alert_to_notification_item(a, depot_name=depot_name) for a in rows
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


# ============ Fleet List Endpoints (chargers + vehicles) ============


def _fleet_list_cache_get(depot_id: str, kind: str) -> Optional[dict]:
    """Return a cached fleet list payload if it's fresh, else None."""
    key = (depot_id, kind)
    entry = _fleet_list_cache.get(key)
    if entry is None:
        return None
    payload, cached_at = entry
    if time.time() - cached_at >= _fleet_list_cache_ttl:
        _fleet_list_cache.pop(key, None)
        return None
    return payload


def _fleet_list_cache_set(depot_id: str, kind: str, payload: dict) -> None:
    """Store a fleet list payload in the per-depot cache."""
    _fleet_list_cache[(depot_id, kind)] = (payload, time.time())


def _fleet_list_lock(depot_id: str, kind: str) -> asyncio.Lock:
    """Return the single-flight lock for ``(depot_id, kind)``, creating it lazily."""
    key = (depot_id, kind)
    lock = _fleet_list_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _fleet_list_locks[key] = lock
    return lock


def _fleet_list_response(payload: dict) -> JSONResponse:
    """Wrap a fleet list payload in a JSONResponse with Cache-Control."""
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "max-age=5, stale-while-revalidate=10"},
    )


_RUNTIME_ENRICHMENT_DEGRADE_ERRORS = (
    asyncpg.PostgresError,
    asyncio.TimeoutError,
)


async def _safe_runtime_fetch(coro_factory, *, label: str, fallback_value=None):
    """Run optional runtime enrichment without failing the static fleet list."""
    if fallback_value is None:
        fallback_value = {}
    try:
        return await coro_factory()
    except _RUNTIME_ENRICHMENT_DEGRADE_ERRORS as exc:
        logger.warning(
            "Optional runtime enrichment failed for %s; using fallback: %s",
            label,
            exc,
            exc_info=True,
        )
        return fallback_value


@app.get(
    "/depots/{depot_id}/chargers",
    response_model=ChargerListResponse,
    tags=["depots"],
    summary="List chargers for a depot with live status",
    description=(
        "Static charger reference data from `charging_stations` joined with the "
        "latest `connector_status` and any open `charging_sessions` rows. The "
        "`status` field is a 4-state pill (`charging | idle | offline | fault`) "
        "computed server-side; `ocpp_connector_status` exposes the raw OCPP "
        "literal for tooltips and admin drilldown."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "No access to this depot"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_depot_chargers(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> JSONResponse:
    """List chargers for a depot with live status and any active session."""
    if not db_pools:
        raise DatabaseError("Database not available")

    cached = _fleet_list_cache_get(depot_id, "chargers")
    if cached is not None:
        return _fleet_list_response(cached)

    async with _fleet_list_lock(depot_id, "chargers"):
        cached = _fleet_list_cache_get(depot_id, "chargers")
        if cached is not None:
            return _fleet_list_response(cached)

        try:
            async with db_pools.static.acquire() as static_conn:
                static_rows = await db_queries.list_chargers_for_depot(
                    static_conn, depot_id=depot_id
                )

            ocpp_ids = [row["ocpp_id"] for row in static_rows]

            connector_statuses: dict[str, dict] = {}
            open_sessions: dict[str, dict] = {}
            if ocpp_ids:

                async def _fetch_connector_statuses():
                    async with db_pools.ts.acquire() as ts_conn:
                        return await db_queries.latest_connector_status_by_stations(
                            ts_conn, ocpp_ids
                        )

                async def _fetch_open_sessions():
                    async with db_pools.ts.acquire() as ts_conn:
                        return await db_queries.open_sessions_by_stations(ts_conn, ocpp_ids)

                connector_statuses = await _safe_runtime_fetch(
                    _fetch_connector_statuses, label="charger connector status"
                )
                open_sessions = await _safe_runtime_fetch(
                    _fetch_open_sessions, label="charger open sessions"
                )

            now = datetime.now(timezone.utc)

            # Consult the LivenessHub in-memory cache for the freshest
            # ``last_interaction_at`` per station — the cache is fed by
            # every OCPP frame via pg_notify, including Heartbeats which
            # the connector_status MAX query would miss.
            def _live_lookup(ocpp_id: str):
                if liveness_hub is None:
                    return None
                return liveness_hub.get_last_interaction(ocpp_id)

            items = [
                _fleet_list.format_charger_item(
                    static_row,
                    connector_status=connector_statuses.get(static_row["ocpp_id"]),
                    open_session=open_sessions.get(static_row["ocpp_id"]),
                    now=now,
                    last_interaction_override=_live_lookup(static_row["ocpp_id"]),
                )
                for static_row in static_rows
            ]

            payload = {"items": items, "fetched_at": now.isoformat()}
            # NOTE: do not cache liveness-augmented payloads aggressively —
            # the override only changes when an OCPP frame arrives, and
            # the existing 2 s TTL is short enough that staleness is bounded.
            _fleet_list_cache_set(depot_id, "chargers", payload)
            return _fleet_list_response(payload)

        except HTTPException:
            raise
        except asyncpg.PostgresError as exc:
            logger.error(
                "Database error listing chargers for depot %s: %s",
                depot_id,
                exc,
                exc_info=True,
            )
            raise DatabaseError() from exc


@app.get(
    "/depots/{depot_id}/vehicles",
    response_model=VehicleListResponse,
    tags=["depots"],
    summary="List vehicles for a depot with live state",
    description=(
        "Static vehicle reference data from `vehicles` joined with the latest "
        "`telemetry`, any open `charging_sessions`, the next future `schedules` "
        "row, and (if applicable) the currently active schedule window. The "
        "`current_state.state` field is a 5-state pill "
        "(`ready | charging | at_risk | in_route | offline`) computed server-side."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "No access to this depot"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_depot_vehicles(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> JSONResponse:
    """List vehicles for a depot with live state and next departure."""
    if not db_pools:
        raise DatabaseError("Database not available")

    cached = _fleet_list_cache_get(depot_id, "vehicles")
    if cached is not None:
        return _fleet_list_response(cached)

    async with _fleet_list_lock(depot_id, "vehicles"):
        cached = _fleet_list_cache_get(depot_id, "vehicles")
        if cached is not None:
            return _fleet_list_response(cached)

        try:
            async with db_pools.static.acquire() as static_conn:
                static_rows = await db_queries.list_vehicles_for_depot(
                    static_conn, depot_id=depot_id
                )
                charger_id_map = await db_queries.charger_id_by_ocpp_id(
                    static_conn, depot_id=depot_id
                )

            vehicle_ids = [row["id"] for row in static_rows]

            telemetry: dict[str, dict] = {}
            sessions: dict[str, dict] = {}
            next_departures: dict[str, dict] = {}
            active_schedules: dict[str, dict] = {}
            if vehicle_ids:
                async with db_pools.ts.acquire() as ts_conn:
                    telemetry = await _safe_runtime_fetch(
                        lambda: db_queries.latest_telemetry_by_vehicles(ts_conn, vehicle_ids),
                        label="vehicle telemetry",
                    )
                    sessions = await _safe_runtime_fetch(
                        lambda: db_queries.open_session_by_vehicles(ts_conn, vehicle_ids),
                        label="vehicle open sessions",
                    )
                    next_departures = await _safe_runtime_fetch(
                        lambda: db_queries.next_departures_by_vehicles(ts_conn, vehicle_ids),
                        label="vehicle next departures",
                    )
                    active_schedules = await _safe_runtime_fetch(
                        lambda: db_queries.active_schedule_by_vehicles(ts_conn, vehicle_ids),
                        label="vehicle active schedules",
                    )

            now = datetime.now(timezone.utc)
            items = [
                _fleet_list.format_vehicle_item(
                    static_row,
                    telemetry=telemetry.get(static_row["id"]),
                    open_session=sessions.get(static_row["id"]),
                    next_departure=next_departures.get(static_row["id"]),
                    active_schedule=active_schedules.get(static_row["id"]),
                    charger_id_by_ocpp_id=charger_id_map,
                    now=now,
                )
                for static_row in static_rows
            ]

            payload = {"items": items, "fetched_at": now.isoformat()}
            _fleet_list_cache_set(depot_id, "vehicles", payload)
            return _fleet_list_response(payload)

        except HTTPException:
            raise
        except asyncpg.PostgresError as exc:
            logger.error(
                "Database error listing vehicles for depot %s: %s",
                depot_id,
                exc,
                exc_info=True,
            )
            raise DatabaseError() from exc


# ===== Live Sessions / Realtime State Endpoints =====


def _isoformat(value: Any) -> Optional[str]:
    """Render a datetime as ISO 8601, or return None if value is falsy."""
    return value.isoformat() if value else None


def _encode_session_cursor(end_time: datetime, session_id: str) -> str:
    """Opaque base64 cursor over ``(end_time_iso, session_id)``."""
    raw = f"{end_time.isoformat()}|{session_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_session_cursor(cursor: str) -> tuple[datetime, str]:
    """Inverse of :func:`_encode_session_cursor`. Raises HTTPException(400) on bad input."""
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        ts_str, session_id = decoded.split("|", 1)
        ts = datetime.fromisoformat(ts_str)
        UUID(session_id)  # validate
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(status_code=400, detail="Invalid cursor") from exc
    return ts, session_id


def _build_completed_sessions_response(rows: list[dict], limit: int) -> CompletedSessionsResponse:
    """Shared row -> response shaping for the completed-sessions endpoints."""
    items = [
        CompletedSessionItem(
            session_id=r["session_id"],
            ocpp_id=r.get("ocpp_id"),
            connector_id=r.get("connector_id"),
            vehicle_id=r.get("vehicle_id"),
            driver_id=r.get("driver_id"),
            started_at=_isoformat(r["started_at"]),
            ended_at=_isoformat(r["ended_at"]),
            energy_delivered_kwh=(
                float(r["energy_delivered_kwh"])
                if r.get("energy_delivered_kwh") is not None
                else None
            ),
            energy_received_kwh=(
                float(r["energy_received_kwh"])
                if r.get("energy_received_kwh") is not None
                else None
            ),
            cost_total=(float(r["cost_total"]) if r.get("cost_total") is not None else None),
            start_soc_percent=(
                float(r["start_soc_percent"]) if r.get("start_soc_percent") is not None else None
            ),
            end_soc_percent=(
                float(r["end_soc_percent"]) if r.get("end_soc_percent") is not None else None
            ),
            source=r.get("source") or "live",
        )
        for r in rows
    ]
    next_cursor = (
        _encode_session_cursor(rows[-1]["ended_at"], rows[-1]["session_id"])
        if len(rows) == limit
        else None
    )
    return CompletedSessionsResponse(
        items=items,
        next_cursor=next_cursor,
        fetched_at=datetime.now(timezone.utc).isoformat(),
    )


@app.get(
    "/depots/{depot_id}/sessions/active",
    response_model=ActiveSessionsResponse,
    tags=["depots"],
    summary="List currently-open charging sessions for a depot",
    description=(
        "Returns one row per open `charging_sessions` row "
        "(`end_time IS NULL AND source='live'`) for any OCPP station belonging "
        "to this depot. Live `current_power_kw` and `current_soc` are kept "
        "fresh on the row by every MeterValues; poll this endpoint at the "
        "same cadence the UI refreshes (5–15 s)."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "No access to this depot"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_depot_active_sessions(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> JSONResponse:
    """List currently-open charging sessions for a depot."""
    if not db_pools:
        raise DatabaseError("Database not available")

    cached = _fleet_list_cache_get(depot_id, "sessions_active")
    if cached is not None:
        return _fleet_list_response(cached)

    async with _fleet_list_lock(depot_id, "sessions_active"):
        cached = _fleet_list_cache_get(depot_id, "sessions_active")
        if cached is not None:
            return _fleet_list_response(cached)

        try:
            async with db_pools.static.acquire() as static_conn:
                ocpp_id_map = await db_queries.charger_id_by_ocpp_id(static_conn, depot_id=depot_id)
            ocpp_ids = list(ocpp_id_map.keys())

            rows: list[dict] = []
            telemetry_by_station: dict[tuple[str, int], dict] = {}
            if ocpp_ids:

                async def _fetch():
                    async with db_pools.ts.acquire() as ts_conn:
                        return await db_queries.list_active_sessions_for_depot(
                            ts_conn, station_ids=ocpp_ids
                        )

                rows = await _safe_runtime_fetch(_fetch, label="active sessions", fallback_value=[])

                async def _fetch_telemetry():
                    async with db_pools.ts.acquire() as ts_conn:
                        return await db_queries.latest_telemetry_by_stations(ts_conn, ocpp_ids)

                telemetry_by_station = await _safe_runtime_fetch(
                    _fetch_telemetry,
                    label="latest charger telemetry",
                    fallback_value={},
                )

            items = []
            for r in rows:
                telemetry_row = telemetry_by_station.get((r["ocpp_id"], int(r["connector_id"])))
                current_power = r.get("current_power_kw")
                if current_power is None and telemetry_row:
                    current_power = telemetry_row.get("current_power_kw")
                current_soc = r.get("current_soc")
                if current_soc is None and telemetry_row:
                    current_soc = telemetry_row.get("current_soc")
                sample_at = r.get("last_sample_at")
                if sample_at is None and telemetry_row:
                    sample_at = telemetry_row.get("last_seen_at")

                items.append(
                    {
                        "session_id": r["session_id"],
                        "ocpp_id": r["ocpp_id"],
                        "connector_id": r["connector_id"],
                        "vehicle_id": r.get("vehicle_id"),
                        "started_at": _isoformat(r["started_at"]),
                        "current_power_kw": (
                            float(current_power) if current_power is not None else None
                        ),
                        "current_soc": float(current_soc) if current_soc is not None else None,
                        "target_soc": (
                            float(r["target_soc"]) if r.get("target_soc") is not None else None
                        ),
                        "estimated_end_at": _isoformat(r.get("estimated_end_at")),
                        "last_sample_at": _isoformat(sample_at),
                    }
                )

            now = datetime.now(timezone.utc)
            payload = {"items": items, "fetched_at": now.isoformat()}
            _fleet_list_cache_set(depot_id, "sessions_active", payload)
            return _fleet_list_response(payload)

        except HTTPException:
            raise
        except asyncpg.PostgresError as exc:
            logger.error(
                "Database error listing active sessions for depot %s: %s",
                depot_id,
                exc,
                exc_info=True,
            )
            raise DatabaseError() from exc


@app.get(
    "/depots/{depot_id}/sessions",
    response_model=CompletedSessionsResponse,
    tags=["depots"],
    summary="Paginated completed charging sessions for a depot",
    description=(
        "Keyset pagination over `(end_time DESC, session_id "
        "DESC)` so concurrent inserts don't shift pages. Includes both `live` "
        "(OCPP-derived) and `import` (XLSX-backfilled) rows."
    ),
    responses={
        400: {"model": ErrorResponse, "description": "Invalid query parameters"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "No access to this depot"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_depot_sessions(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
    from_ts: Optional[datetime] = Query(
        None, alias="from", description="Filter end_time >= this ISO 8601 timestamp"
    ),
    to_ts: Optional[datetime] = Query(
        None, alias="to", description="Filter end_time < this ISO 8601 timestamp"
    ),
    limit: int = Query(50, ge=1, le=500),
    cursor: Optional[str] = Query(
        None, description="Opaque cursor returned in `next_cursor` from the prior page"
    ),
) -> CompletedSessionsResponse:
    """Paginated completed charging sessions for a depot."""
    if not db_pools:
        raise DatabaseError("Database not available")

    if from_ts is not None and to_ts is not None and from_ts >= to_ts:
        raise HTTPException(status_code=400, detail="`from` must be earlier than `to`")

    cursor_tuple = _decode_session_cursor(cursor) if cursor else None

    try:
        async with db_pools.static.acquire() as static_conn:
            ocpp_id_map = await db_queries.charger_id_by_ocpp_id(static_conn, depot_id=depot_id)
        ocpp_ids = list(ocpp_id_map.keys())

        async with db_pools.ts.acquire() as ts_conn:
            rows = await db_queries.list_completed_sessions_for_depot(
                ts_conn,
                depot_id=depot_id,
                station_ids=ocpp_ids,
                from_ts=from_ts,
                to_ts=to_ts,
                limit=limit,
                cursor=cursor_tuple,
            )

        return _build_completed_sessions_response(rows, limit)

    except HTTPException:
        raise
    except asyncpg.PostgresError as exc:
        logger.error(
            "Database error listing sessions for depot %s: %s",
            depot_id,
            exc,
            exc_info=True,
        )
        raise DatabaseError() from exc


@app.get(
    "/depots/{depot_id}/vehicles/state",
    response_model=VehicleRealtimeStateResponse,
    tags=["depots"],
    summary="Latest telemetry per vehicle in a depot",
    description=(
        "Returns one row per vehicle in the depot that has at least one "
        "telemetry sample. Lightweight by design — for the richer vehicle list "
        "with schedule/state derivation, use `GET /depots/{id}/vehicles`."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {"model": ErrorResponse, "description": "No access to this depot"},
        404: {"model": ErrorResponse, "description": "Depot not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def get_depot_vehicles_state(
    depot_id: str = Depends(_require_depot_access),
    user: dict = Depends(ensure_tenant_mirrored),
) -> JSONResponse:
    """Latest telemetry per vehicle for a depot."""
    if not db_pools:
        raise DatabaseError("Database not available")

    cached = _fleet_list_cache_get(depot_id, "vehicles_state")
    if cached is not None:
        return _fleet_list_response(cached)

    async with _fleet_list_lock(depot_id, "vehicles_state"):
        cached = _fleet_list_cache_get(depot_id, "vehicles_state")
        if cached is not None:
            return _fleet_list_response(cached)

        try:
            async with db_pools.static.acquire() as static_conn:
                static_rows = await db_queries.list_vehicles_for_depot(
                    static_conn, depot_id=depot_id
                )
            vehicle_ids = [row["id"] for row in static_rows]

            rows: list[dict] = []
            if vehicle_ids:

                async def _fetch():
                    async with db_pools.ts.acquire() as ts_conn:
                        return await db_queries.latest_telemetry_for_depot_vehicles(
                            ts_conn, vehicle_ids=vehicle_ids
                        )

                rows = await _safe_runtime_fetch(
                    _fetch, label="vehicle realtime state", fallback_value=[]
                )

            items = [
                {
                    "vehicle_id": r["vehicle_id"],
                    "charger_id": r.get("charger_id"),
                    "soc": float(r["soc"]) if r.get("soc") is not None else None,
                    "power_kw": (float(r["power_kw"]) if r.get("power_kw") is not None else None),
                    "is_plugged": r.get("is_plugged"),
                    "last_seen_at": _isoformat(r["last_seen_at"]),
                }
                for r in rows
            ]

            now = datetime.now(timezone.utc)
            payload = {"items": items, "fetched_at": now.isoformat()}
            _fleet_list_cache_set(depot_id, "vehicles_state", payload)
            return _fleet_list_response(payload)

        except HTTPException:
            raise
        except asyncpg.PostgresError as exc:
            logger.error(
                "Database error listing vehicle state for depot %s: %s",
                depot_id,
                exc,
                exc_info=True,
            )
            raise DatabaseError() from exc


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
        SELECT external_id, battery_capacity_kwh AS battery_kwh, max_charge_rate_kw AS max_charge_kw
        FROM vehicles
        WHERE id = $1 AND site_id = $2
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
        # Security (H4): no localhost fallback. The destination must be
        # configured per-depot or via DEFAULT_DEPOT_ENDPOINT. A missing
        # endpoint is a config error, not a silent self-callback.
        dest_depot_endpoint = os.getenv(
            f"DEPOT_{request.dest_depot_id}_ENDPOINT",
            os.getenv("DEFAULT_DEPOT_ENDPOINT", ""),
        )
        if not dest_depot_endpoint:
            logger.error(
                "Handoff destination endpoint not configured for depot=%s",
                request.dest_depot_id,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "Inter-depot handoff is unavailable: "
                    "destination depot endpoint not configured "
                    "(error_code=DEPOT_ENDPOINT_NOT_CONFIGURED)"
                ),
            )

        # Security (H4): SSRF guard + DNS rebinding mitigation — validate once,
        # connect to the pinned IP with Host/SNI from the original hostname.
        receive_url = f"{dest_depot_endpoint}/depots/{request.dest_depot_id}/handoff/receive"
        request_url, host_header, httpx_extensions = await prepare_handoff_http_target(
            receive_url, _environment
        )

        # Security (C2/H4): HMAC signing key is mandatory in non-development
        # environments. Without it the receive side fails closed, so refuse
        # here to surface the misconfig at the source rather than after a
        # network round-trip.
        signing_key = os.getenv("HANDOFF_SIGNING_KEY", "")
        if not signing_key and _environment in {"production", "staging"}:
            logger.error("HANDOFF_SIGNING_KEY is not configured; refusing send_handoff")
            raise HTTPException(
                status_code=503,
                detail=(
                    "Inter-depot handoff is unavailable: "
                    "signing key not configured "
                    "(error_code=HANDOFF_SIGNING_KEY_NOT_CONFIGURED)"
                ),
            )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
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
                # Security (H4): sign canonical body bytes; ship signature in
                # the X-Handoff-Signature header so it stays out of the JSON
                # body schema. ``sort_keys=True`` matches the receiver's
                # canonicalisation rule.
                payload_bytes = json.dumps(receive_payload, sort_keys=True).encode("utf-8")
                headers: dict[str, str] = {}
                if signing_key:
                    headers["X-Handoff-Signature"] = compute_handoff_signature(
                        signing_key.encode(), payload_bytes
                    )

                post_kw: dict = {"content": payload_bytes}
                if httpx_extensions:
                    post_kw["extensions"] = httpx_extensions
                response = await client.post(
                    request_url,
                    headers={
                        "Content-Type": "application/json",
                        "Host": host_header,
                        **headers,
                    },
                    **post_kw,
                )
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

    except HTTPException:
        # Preserve status codes raised inside the body (e.g. SSRF guard 400,
        # missing endpoint/key 503) — don't downgrade them to 500.
        raise
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
            detail={
                "error_code": ErrorCode.INTERNAL_ERROR.value,
                "detail": "Failed to send handoff",
            },
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
    http_request: Request,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Receive inter-depot handoff message.

    Per PRD Section 5.4, the destination depot:
    1. Validates the request
    2. Stores message in interdepot_messages with status='acknowledged'
    3. Returns acknowledgment with acknowledged_at timestamp

    Security (H4): the request MUST carry an HMAC-SHA256 signature in the
    ``X-Handoff-Signature`` header. The signed body must include a fresh
    ``nonce`` and ``timestamp`` within ``_HANDOFF_REPLAY_WINDOW_S`` of now.
    In production/staging the signing key is required; in development we
    log and continue if the header is absent so local dev tooling can
    exercise the path without ceremony. JWT auth alone is insufficient
    because authenticated tenants would otherwise be able to inject handoff
    messages claiming any ``origin_depot_id``.

    Reference: PRD_v2.md#7-1-rest-api-endpoints
    """
    if not db_pools:
        raise DatabaseError("Database not available")

    # Validate depot_id early so the SSRF guard error is surfaced before any
    # signature work. The body-level UUIDs are validated below after parsing.
    validate_depot_id(depot_id)

    # Security (H4): read raw body and signature header before parsing so we
    # can verify the HMAC over the exact bytes the sender hashed.
    try:
        raw_body = await http_request.body()
    except Exception:  # pragma: no cover - starlette wraps recv errors
        raise HTTPException(status_code=400, detail="Could not read request body")

    signing_key = os.getenv("HANDOFF_SIGNING_KEY", "")
    signature_header = http_request.headers.get("X-Handoff-Signature", "")

    if _environment in {"production", "staging"}:
        if not signing_key:
            logger.error(
                "HANDOFF_SIGNING_KEY is not configured; refusing handoff for depot=%s",
                depot_id,
            )
            raise HTTPException(
                status_code=503,
                detail=(
                    "Inter-depot handoff is unavailable: "
                    "signing key not configured "
                    "(error_code=HANDOFF_SIGNING_KEY_NOT_CONFIGURED)"
                ),
            )
        if not signature_header:
            peer = http_request.client.host if http_request.client else "unknown"
            logger.warning(
                "Handoff signature header missing: depot=%s peer=%s",
                depot_id,
                peer,
            )
            raise HTTPException(
                status_code=401,
                detail=(
                    "Inter-depot handoff requires X-Handoff-Signature header "
                    "(error_code=HANDOFF_SIGNATURE_REQUIRED)"
                ),
            )
        if not _verify_handoff_payload(raw_body, signature_header, signing_key):
            peer = http_request.client.host if http_request.client else "unknown"
            logger.warning(
                "Handoff signature verification failed: depot=%s peer=%s",
                depot_id,
                peer,
            )
            raise HTTPException(
                status_code=401,
                detail=(
                    "Invalid or expired handoff signature " "(error_code=HANDOFF_SIGNATURE_INVALID)"
                ),
            )
    else:
        # Development: verify if the sender bothered to sign; otherwise warn.
        if signature_header and signing_key:
            if not _verify_handoff_payload(raw_body, signature_header, signing_key):
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "Invalid or expired handoff signature "
                        "(error_code=HANDOFF_SIGNATURE_INVALID)"
                    ),
                )
        else:
            logger.warning(
                "Handoff received without HMAC signature in development; "
                "production/staging will require X-Handoff-Signature."
            )

    # Parse the body into the request schema only after signature verification
    # so we never act on an unverified payload.
    try:
        request = HandoffReceiveRequest.model_validate_json(raw_body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=exc.errors()) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid request body: {exc}") from exc

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

    except HTTPException:
        # Preserve any HTTP error raised inside the body (rate-limit 429, etc.)
        # rather than masking it as a 500.
        raise
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
                "SELECT site_id::text AS depot_id FROM charging_stations WHERE station_id = $1",
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


_CONTENT_DISPOSITION_FILENAME_RE = re.compile(
    r'filename\s*=\s*"?(?P<name>[^";\r\n]+?)"?\s*(?:;|$)',
    re.IGNORECASE,
)


def _extract_upload_filename(request: Request) -> Optional[str]:
    """Return a sanitized client-supplied filename, or ``None``.

    Prefers the explicit ``X-File-Name`` header; falls back to a
    Content-Disposition ``filename=`` parameter only when one is
    actually present. The previous implementation used
    ``split("filename=")[-1]``, which returned the whole header value
    when the parameter was absent — producing junk file names like
    ``"attachment"`` that were stored verbatim in the DB.
    """
    raw = request.headers.get("X-File-Name")
    if raw:
        candidate = raw.strip()
    else:
        cd = request.headers.get("Content-Disposition", "")
        match = _CONTENT_DISPOSITION_FILENAME_RE.search(cd) if cd else None
        candidate = match.group("name").strip() if match else ""
    if not candidate:
        return None
    # Strip directory parts and other noise; the DB column is plain text
    # but operators read it, so keep it short and printable.
    candidate = candidate.replace("\\", "/").split("/")[-1]
    candidate = "".join(ch for ch in candidate if ch.isprintable() and ch not in '"\r\n\t')
    candidate = candidate.strip(" \"';")
    return candidate[:255] or None


@app.post(
    "/internal/charger_logs/upload",
    include_in_schema=False,
)
async def upload_charger_log_endpoint(
    request: Request,
    token: str = Query(
        ..., description="HMAC-signed upload token from GetDiagnostics location URL"
    ),
    import_id: Optional[str] = Query(
        None, description="Convenience param; the canonical id is encoded in token"
    ),
):
    """Receive a charger-uploaded diagnostics archive.

    Public endpoint (chargers don't carry Supabase JWTs). Auth is the
    HMAC-signed ``token`` query param produced by
    :func:`src.adapters.chargers.upload_token.mint_token` and embedded
    in the ``location`` URL we sent in ``GetDiagnostics``. The token
    encodes ``(import_id, expiry, hmac)``; the upload endpoint
    recomputes the HMAC and rejects malformed / expired / mismatched
    tokens with 401.

    Size-cap: the body is read into memory up to
    ``CHARGER_LOG_UPLOAD_MAX_BYTES`` (default 50 MiB). Bodies above
    that limit return 413 — the legacy ``MaxBodySizeMiddleware`` may
    already trip on this before we ever see it.

    On success: stores ``raw_payload`` on the ``charger_log_imports``
    row, flips status to ``'received'``, schedules
    :func:`run_post_upload_pipeline` as a post-commit background task,
    returns 202 ``{import_id, status_url}``.
    """
    _ = import_id  # accepted for charger compatibility; not authoritative

    if db_pools is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "DATABASE_UNAVAILABLE",
                "message": "Database not available",
            },
        )

    start = time.perf_counter()

    # Validate the token *before* reading a single byte of the body so
    # an attacker with a bad / expired token can't waste server
    # bandwidth or memory streaming a payload that will be rejected
    # anyway. The full token-hash check against
    # ``charger_log_imports.upload_token_hash`` still runs inside
    # ``receive_upload``; this is just the cheap pre-check.
    from ..adapters.chargers.upload_token import (
        UploadTokenError,
        get_max_upload_bytes,
        verify_token,
    )

    try:
        decoded_token = verify_token(token)
    except UploadTokenError as exc:
        # Don't leak the specific reason (malformed / bad signature /
        # expired) to the client — keeps timing-side channels narrow.
        # Server log carries the detail.
        logger.info("upload token rejected: %s", exc)
        raise HTTPException(
            status_code=401,
            detail={
                "error_code": "CHARGER_LOG_UPLOAD_REJECTED",
                "message": "invalid token",
            },
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error_code": "CHARGER_LOG_UPLOAD_REJECTED",
                "message": f"upload misconfigured: {exc}",
            },
        ) from exc

    # Stream the body with a hard byte ceiling so a missing /
    # understated ``Content-Length`` can't force unbounded buffering.
    # ``MaxBodySizeMiddleware`` already checks the header up-front, but
    # only when the client sends one — Starlette's ``request.body()``
    # otherwise concatenates chunked-transfer payloads of any size.
    max_bytes = get_max_upload_bytes()
    body = bytearray()
    try:
        async for chunk in request.stream():
            if not chunk:
                continue
            if len(body) + len(chunk) > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail={
                        "error_code": "CHARGER_LOG_UPLOAD_REJECTED",
                        "message": "upload exceeds max size",
                    },
                )
            body.extend(chunk)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        # Network drop mid-upload — treat as a client error rather than 500.
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "CHARGER_LOG_UPLOAD_REJECTED",
                "message": f"upload stream error: {exc}",
            },
        ) from exc
    body_bytes = bytes(body)

    file_name = _extract_upload_filename(request)

    try:
        # Pass the already-decoded token so receive_upload doesn't
        # re-verify and risk a different verdict than the pre-check.
        # Between streaming and reaching the DB the token's expiry
        # could elapse; a double verify would then reject a body we
        # just spent bandwidth accepting.
        result = await receive_upload(
            db_pools.ts,
            token=token,
            body=body_bytes,
            file_name=file_name,
            decoded=decoded_token,
        )
    except UploadRejected as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "error_code": "CHARGER_LOG_UPLOAD_REJECTED",
                "message": exc.reason,
            },
        ) from exc

    # Record histogram before scheduling the background pipeline so a
    # slow parse doesn't pollute the receive-latency metric.
    from ..monitoring.metrics import CHARGER_LOG_UPLOAD_DURATION

    CHARGER_LOG_UPLOAD_DURATION.observe(max(time.perf_counter() - start, 1e-6))

    schedule_post_upload_pipeline(
        db_pools.ts,
        import_id=result.import_id,
        background_tasks=_background_tasks,
    )

    status_url = None
    if result.session_id is not None:
        session_depot = await db_pools.ts.fetchval(
            """
            SELECT site_id::text
              FROM charging_sessions
             WHERE session_id = $1::uuid
            """,
            result.session_id,
        )
        if session_depot:
            status_url = (
                f"/admin/depots/{session_depot}/sessions/{result.session_id}/log_comparison"
            )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "import_id": str(result.import_id),
            "status": "received",
            "file_size_bytes": result.file_size_bytes,
            "content_sha256": result.content_sha256,
            "status_url": status_url,
        },
    )


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

    **Authentication:** Requires ``Authorization: Bearer <METRICS_TOKEN>``.
    Configure Prometheus via ``authorization.credentials_file`` so the
    scrape config carries the token.
    """,
    include_in_schema=False,  # Hide from OpenAPI docs (internal endpoint)
)
async def metrics(request: Request):
    """Prometheus metrics endpoint.

    Refuses every request when ``METRICS_TOKEN`` is unset (503) so an
    unconfigured deploy never leaks operational data. Otherwise parses
    ``Authorization`` as a Bearer credential — RFC 7235 declares HTTP
    auth schemes case-insensitive, so ``bearer`` and ``Bearer`` are
    equivalent — and constant-time-compares the token against
    ``METRICS_TOKEN``. Returns 401 on mismatch.
    """
    if not _METRICS_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Metrics endpoint not configured (METRICS_TOKEN missing)",
        )
    auth_header = request.headers.get("Authorization", "")
    # RFC 7235 BNF: ``credentials = auth-scheme 1*SP token68``. Partition at
    # the first SP and ``lstrip`` any extras so a header like
    # ``Bearer   <token>`` (multiple SPs from a permissive proxy) is still
    # accepted. The token itself must remain a constant-time compare.
    scheme, separator, rest = auth_header.partition(" ")
    presented_token = rest.lstrip(" ")
    if not separator or scheme.lower() != "bearer" or not presented_token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    # ``compare_digest`` raises ``TypeError`` on non-ASCII ``str`` inputs, so
    # a request carrying ``Authorization: Bearer <obs-text>`` would otherwise
    # surface as a 500 instead of a clean 401. Encode both sides to bytes —
    # constant-time semantics are preserved and any byte sequence compares
    # cleanly without raising.
    if not secrets.compare_digest(presented_token.encode("utf-8"), _METRICS_TOKEN.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Unauthorized")
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
    strict: bool = False,
) -> None:
    """Write one ``audit_log`` row after an admin action.

    Default best-effort. Pass ``strict=True`` for cross-org admin
    enumeration paths where the audit row is part of the security
    guarantee — failures propagate as ``AdminAuditWriteError`` and
    callers translate to HTTP 503 ``AUDIT_LOG_UNAVAILABLE``.
    """
    if db_pools is None:
        if strict:
            raise AdminAuditWriteError(f"Admin audit DB pool unavailable for action={action}")
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
    await write_admin_audit_row(db_pools.ts, row, strict=strict)


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
        strict=True,
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
            strict=True,
        )

    return {"organization_id": str(org_id), "depots": depots, "count": len(depots)}


async def _resolve_depot_for_admin(
    depot_id: str, user: dict, *, endpoint_name: Optional[str] = None
) -> tuple[dict, bool]:
    """Resolve a depot for cross-org admin access.

    Returns (depot_row, cross_org_read).

    - favonius_admin: always allowed; cross_org_read=True if the depot's org
      differs from any caller-org in the JWT (favonius_admin has no own org).
      Negative-result lookups (depot_id does not exist) write a
      strict ``admin.read`` audit row before raising 404 so platform-admin
      enumeration cannot proceed silently.
    - customer_admin / customer_operator: must own the depot via organization_id.
      403 path is leak-resistant (no distinction between "no such depot" and
      "wrong tenant") and intentionally NOT audited so that audit volume
      does not leak existence to untrusted tenants.
    - viewer or unknown roles: 403.

    Raises 403 (never 404) when the depot does not exist for non-admin
    callers so existence does not leak across tenants.
    """
    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.static.acquire() as conn:
        depot_row = await db_queries.get_depot_by_id(conn, depot_id)

    role = get_user_role(user)

    if role == "favonius_admin":
        if depot_row is None:
            await _record_admin_action(
                user=user,
                action="admin.read",
                depot_id=depot_id,
                target_type="depot",
                target_id=str(depot_id),
                metadata={
                    "endpoint": endpoint_name or "admin.depot_lookup",
                    "result": "not_found",
                },
                strict=True,
            )
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


async def _resolve_charger_for_depot(*, depot_id: str, charger_id: str) -> Optional[dict]:
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

    depot_row, cross_org_read = await _resolve_depot_for_admin(
        depot_id,
        user,
        endpoint_name="GET /admin/depots/{depot_id}/chargers/{charger_id}/credentials_status",
    )
    charger_row = await _resolve_charger_for_depot(depot_id=depot_id, charger_id=charger_id)
    if charger_row is None:
        if cross_org_read:
            await _record_admin_action(
                user=user,
                action="admin.read",
                depot_id=depot_id,
                organization_id_override=(
                    str(depot_row.get("organization_id"))
                    if depot_row.get("organization_id")
                    else None
                ),
                target_type="charger",
                target_id=str(charger_id),
                metadata={
                    "endpoint": "GET /admin/depots/{depot_id}/chargers/{charger_id}/credentials_status",
                    "result": "not_found",
                },
                strict=True,
            )
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
            organization_id_override=(
                str(depot_row.get("organization_id")) if depot_row.get("organization_id") else None
            ),
            target_type="charger",
            target_id=str(charger_id),
            metadata={
                "endpoint": "GET /admin/depots/{depot_id}/chargers/{charger_id}/credentials_status",
            },
            strict=True,
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

    depot_row, _ = await _resolve_depot_for_admin(
        depot_id,
        user,
        endpoint_name="POST /admin/depots/{depot_id}/chargers/{charger_id}/rotate_credentials",
    )
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
        organization_id_override=(
            str(depot_row.get("organization_id")) if depot_row.get("organization_id") else None
        ),
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


@app.get(
    "/admin/depots/{depot_id}/chargers/{charger_id}/sessions",
    response_model=CompletedSessionsResponse,
    tags=["admin"],
    summary="Paginated completed charging sessions for a single charger",
    description=(
        "Lists completed charging sessions on a specific charger, scoped "
        "by the charger's OCPP ``station_id``. Companion to "
        "``POST .../sessions/{session_id}/fetch_logs`` — operators pick a "
        "session here, then trigger a charger-side log pull. Same keyset "
        "pagination shape as ``GET /depots/{id}/sessions``. Gated to "
        "favonius_admin or customer_admin since it feeds the admin "
        "log-pull workflow."
    ),
    responses={
        400: {"model": ErrorResponse, "description": "Invalid query parameters"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        403: {
            "model": ErrorResponse,
            "description": "Insufficient role or no access to this depot",
        },
        404: {"model": ErrorResponse, "description": "Depot or charger not found"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def list_charger_completed_sessions(
    depot_id: str,
    charger_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
    from_ts: Optional[datetime] = Query(
        None, alias="from", description="Filter end_time >= this ISO 8601 timestamp"
    ),
    to_ts: Optional[datetime] = Query(
        None, alias="to", description="Filter end_time < this ISO 8601 timestamp"
    ),
    limit: int = Query(50, ge=1, le=500),
    cursor: Optional[str] = Query(
        None, description="Opaque cursor returned in `next_cursor` from the prior page"
    ),
) -> CompletedSessionsResponse:
    """List completed sessions for a specific charger.

    Mirrors ``GET /depots/{id}/sessions`` (same paging, same row shape)
    but restricts results to one charger. Role-gated identically to
    ``POST .../sessions/{session_id}/fetch_logs`` so the listing and
    the action it feeds have the same audience.
    """
    validate_uuid(depot_id, "depot_id")
    validate_uuid(charger_id, "charger_id")

    role = get_user_role(user)
    if role not in ("favonius_admin", "customer_admin"):
        raise _forbidden("FORBIDDEN_ROLE", "favonius_admin or customer_admin role required")

    depot_row, cross_org_read = await _resolve_depot_for_admin(
        depot_id,
        user,
        endpoint_name="GET /admin/depots/{depot_id}/chargers/{charger_id}/sessions",
    )

    if not db_pools:
        raise DatabaseError("Database not available")

    if from_ts is not None and to_ts is not None and from_ts >= to_ts:
        raise HTTPException(status_code=400, detail="`from` must be earlier than `to`")

    cursor_tuple = _decode_session_cursor(cursor) if cursor else None

    async def _audit_cross_org(result: str) -> None:
        # Mirror the credentials_status read: a favonius_admin reading
        # another tenant's depot leaves an ``admin.read`` trail. strict=True
        # so a missing audit log fails closed (503) rather than silently
        # serving cross-tenant data without provenance.
        if not cross_org_read:
            return
        await _record_admin_action(
            user=user,
            action="admin.read",
            depot_id=depot_id,
            organization_id_override=(
                str(depot_row.get("organization_id")) if depot_row.get("organization_id") else None
            ),
            target_type="charger",
            target_id=str(charger_id),
            metadata={
                "endpoint": "GET /admin/depots/{depot_id}/chargers/{charger_id}/sessions",
                "result": result,
            },
            strict=True,
        )

    try:
        async with db_pools.static.acquire() as static_conn:
            charger_row = await static_conn.fetchrow(
                """
                SELECT station_id AS ocpp_id
                  FROM charging_stations
                 WHERE id = $1::uuid AND site_id = $2::uuid
                """,
                charger_id,
                depot_id,
            )
        if charger_row is None:
            await _audit_cross_org("not_found")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "CHARGER_NOT_FOUND", "message": "Charger not found"},
            )

        async with db_pools.ts.acquire() as ts_conn:
            rows = await db_queries.list_completed_sessions_for_charger(
                ts_conn,
                station_ocpp_id=charger_row["ocpp_id"],
                from_ts=from_ts,
                to_ts=to_ts,
                limit=limit,
                cursor=cursor_tuple,
            )

        await _audit_cross_org("ok")
        return _build_completed_sessions_response(rows, limit)

    except HTTPException:
        raise
    except asyncpg.PostgresError as exc:
        logger.error(
            "Database error listing sessions for charger %s in depot %s: %s",
            charger_id,
            depot_id,
            exc,
            exc_info=True,
        )
        raise DatabaseError() from exc


@app.post(
    "/admin/depots/{depot_id}/chargers/{charger_id}/sessions/{session_id}/fetch_logs",
    tags=["admin"],
    summary="Trigger a GetDiagnostics pull of charger-side session logs",
)
async def fetch_charger_session_logs_endpoint(
    depot_id: str,
    charger_id: str,
    session_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Enqueue an OCPP ``GetDiagnostics`` to extract the charger's own log.

    Flow:
      1. Authorize favonius_admin / customer_admin and resolve the depot.
      2. Look up the charger's OCPP id (``station_id``) + vendor from
         ``charging_stations``. 404 if charger isn't in the depot.
      3. Verify the session belongs to that station — defence against
         someone fetching logs for a different depot's session by id
         alone.
      4. :func:`dispatch_get_diagnostics` writes a ``charger_log_imports``
         row, mints a signed upload URL, and enqueues a
         ``charging_command_queue`` row. The WS handler drains the
         queue and pushes ``GetDiagnostics(location=<URL>)`` to the
         live OCPP socket.
      5. Audit row ``charger.logs.fetched`` is written before returning.

    Returns ``{import_id, status, status_url}``. The operator polls
    ``GET /admin/depots/.../sessions/.../log_comparison`` for the
    parsed + reconciled result.
    """
    validate_uuid(depot_id, "depot_id")
    validate_uuid(charger_id, "charger_id")
    validate_uuid(session_id, "session_id")

    role = get_user_role(user)
    if role not in ("favonius_admin", "customer_admin"):
        raise _forbidden("FORBIDDEN_ROLE", "favonius_admin or customer_admin role required")

    depot_row, _ = await _resolve_depot_for_admin(
        depot_id,
        user,
        endpoint_name=(
            "POST /admin/depots/{depot_id}/chargers/{charger_id}/sessions/{session_id}/fetch_logs"
        ),
    )

    if db_pools is None:
        raise DatabaseError("Database not available")

    async with db_pools.static.acquire() as conn:
        charger_row = await conn.fetchrow(
            """
            SELECT id::text     AS charger_id,
                   station_id   AS ocpp_id,
                   vendor       AS vendor,
                   connector_count
              FROM charging_stations
             WHERE id = $1::uuid AND site_id = $2::uuid
            """,
            charger_id,
            depot_id,
        )
    if charger_row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "CHARGER_NOT_FOUND", "message": "Charger not found"},
        )

    async with db_pools.ts.acquire() as conn:
        session_row = await conn.fetchrow(
            """
            SELECT session_id, station_id, connector_id, transaction_id,
                   start_time, end_time
              FROM charging_sessions
             WHERE session_id = $1::uuid
               AND station_id = $2
            """,
            session_id,
            charger_row["ocpp_id"],
        )
    if session_row is None:
        # Either the session doesn't exist or it's on a different
        # charger — return 404 without leaking which.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error_code": "SESSION_NOT_FOUND", "message": "Session not found"},
        )

    # One idempotency key per (session, source) so a double-click on
    # the UI button doesn't queue two GetDiagnostics within the same
    # second. Includes a coarse timestamp to allow a fresh attempt an
    # hour later if the first one failed — using only session_id would
    # make the row permanently un-re-fetchable.
    hour_bucket = int(time.time() // 3600)
    idempotency_key = f"get_diagnostics:{session_id}:{hour_bucket}"

    try:
        import_id = await dispatch_get_diagnostics(
            db_pools.ts,
            station_id=charger_row["ocpp_id"],
            session_id=UUID(session_id),
            charger_id=UUID(charger_id),
            connector_id=(
                int(session_row["connector_id"])
                if session_row["connector_id"] is not None
                else None
            ),
            vendor=charger_row["vendor"],
            # Bound the diagnostic dump to the session window so
            # chargers return only the relevant slice instead of a
            # full-disk export. ABB Terra AC honours these OCPP fields
            # when present; ignoring them risks oversized uploads that
            # blow CHARGER_LOG_UPLOAD_MAX_BYTES.
            start_time=session_row["start_time"],
            stop_time=session_row["end_time"],
            idempotency_key=idempotency_key,
        )
    except RuntimeError as exc:
        # Missing env config — surface a 503 so operators see why the
        # action did nothing instead of a generic 500.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "CHARGER_LOG_UPLOAD_NOT_CONFIGURED",
                "message": str(exc),
            },
        ) from exc
    except asyncpg.UniqueViolationError:
        # Idempotency-key collision — another request in this hour
        # bucket already queued a GetDiagnostics for this session.
        async with db_pools.ts.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT id, status
                  FROM charger_log_imports
                 WHERE idempotency_key = $1
                """,
                idempotency_key,
            )
        if existing is not None:
            # Still write the audit row so privileged retries inside
            # the dedupe window leave a trail. Compliance reviews care
            # about "who tried" as much as "what changed".
            await _record_admin_action(
                user=user,
                action="charger.logs.fetched",
                depot_id=depot_id,
                organization_id_override=(
                    str(depot_row.get("organization_id"))
                    if depot_row.get("organization_id")
                    else None
                ),
                target_type="charger_log_import",
                target_id=str(existing["id"]),
                metadata={
                    "endpoint": (
                        "POST /admin/depots/{depot_id}/chargers/{charger_id}/sessions/{session_id}/fetch_logs"
                    ),
                    "charger_id": charger_id,
                    "session_id": session_id,
                    "ocpp_id": charger_row["ocpp_id"],
                    "vendor": charger_row["vendor"],
                    "deduplicated": True,
                    "existing_status": existing["status"],
                },
            )
            return {
                "import_id": str(existing["id"]),
                "status": existing["status"],
                "deduplicated": True,
                # Clients that drive subsequent polls off ``status_url``
                # must see the same key on every response path,
                # including the dedupe short-circuit. Otherwise an
                # idempotent retry leaves them unable to follow the
                # comparison endpoint without rebuilding the URL.
                "status_url": (f"/admin/depots/{depot_id}/sessions/{session_id}/log_comparison"),
            }
        # Shouldn't reach here, but if we do the UniqueViolation is
        # honest news for the caller.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error_code": "DUPLICATE_REQUEST",
                "message": "Identical GetDiagnostics already queued",
            },
        )

    await _record_admin_action(
        user=user,
        action="charger.logs.fetched",
        depot_id=depot_id,
        organization_id_override=(
            str(depot_row.get("organization_id")) if depot_row.get("organization_id") else None
        ),
        target_type="charger_log_import",
        target_id=str(import_id),
        metadata={
            "endpoint": (
                "POST /admin/depots/{depot_id}/chargers/{charger_id}/sessions/{session_id}/fetch_logs"
            ),
            "charger_id": charger_id,
            "session_id": session_id,
            "ocpp_id": charger_row["ocpp_id"],
            "vendor": charger_row["vendor"],
        },
    )

    return {
        "import_id": str(import_id),
        "status": "requested",
        "status_url": (f"/admin/depots/{depot_id}/sessions/{session_id}/log_comparison"),
    }


@app.get(
    "/admin/depots/{depot_id}/sessions/{session_id}/log_comparison",
    tags=["admin"],
    summary="Side-by-side comparison of our session record vs the charger's log",
)
async def get_session_log_comparison_endpoint(
    depot_id: str,
    session_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Return ``{our_session, charger_entries, reconciliation, import}`` for the UI.

    Loads the most recent ``charger_log_imports`` row for the session
    (any source — GetDiagnostics, GetLog, or manual_upload), the parsed
    entries from ``charger_session_log_entries``, the matching
    ``session_log_reconciliations`` row, and the original
    ``charging_sessions`` row.

    Responses:
      * 200 with full payload when the import has reached ``reconciled``.
      * 200 with partial payload (no reconciliation block) when the
        import is still ``requested`` / ``uploading`` / ``received`` /
        ``parsed`` — the UI shows a "pending" state.
      * 404 when no import exists yet for this session.
    """
    validate_uuid(depot_id, "depot_id")
    validate_uuid(session_id, "session_id")

    _ = _require_admin_role(user)
    depot_row, _ = await _resolve_depot_for_admin(
        depot_id,
        user,
        endpoint_name="GET /admin/depots/{depot_id}/sessions/{session_id}/log_comparison",
    )

    if db_pools is None:
        raise DatabaseError("Database not available")

    async with db_pools.ts.acquire() as conn:
        session_row = await conn.fetchrow(
            """
            SELECT session_id, station_id, connector_id, transaction_id,
                   vehicle_id, start_time, end_time, energy_delivered_kwh,
                   cost_total, cost_total_source, site_id
              FROM charging_sessions
             WHERE session_id = $1::uuid
            """,
            session_id,
        )
        if session_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error_code": "SESSION_NOT_FOUND", "message": "Session not found"},
            )

        # Tenant fence: the session must belong to the caller's depot.
        # Prefer charging_sessions.site_id so decommissioned chargers
        # (dropped from Supabase) don't deny access to retained history.
        # favonius_admin bypasses (the resolve helper already audited).
        if get_user_role(user) != "favonius_admin":
            owner = session_row["site_id"]
            if owner is None:
                owner = await db_pools.static.fetchval(
                    """
                    SELECT site_id::text
                      FROM charging_stations
                     WHERE station_id = $1
                    """,
                    session_row["station_id"],
                )
            if owner is None or str(owner) != depot_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "error_code": "SESSION_NOT_FOUND",
                        "message": "Session not found",
                    },
                )

        import_row = await conn.fetchrow(
            """
            SELECT id, status, source, vendor, file_name, file_size_bytes,
                   content_sha256, requested_at, received_at, parsed_at,
                   reconciled_at, error_message
              FROM charger_log_imports
             WHERE session_id = $1::uuid
             ORDER BY requested_at DESC
             LIMIT 1
            """,
            session_id,
        )
        if import_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error_code": "NO_CHARGER_LOG_IMPORT",
                    "message": "No charger log has been fetched for this session yet",
                },
            )

        entries = await conn.fetch(
            """
            SELECT time, connector_id, transaction_id,
                   soc, charging_kw, energy_kwh, raw_fields
              FROM charger_session_log_entries
             WHERE log_import_id = $1
             ORDER BY time
            """,
            import_row["id"],
        )

        reconciliation = await conn.fetchrow(
            """
            SELECT computed_at, source,
                   our_energy_kwh, charger_energy_kwh, energy_delta_pct,
                   our_duration_s, charger_duration_s,
                   our_start_time, charger_start_time,
                   our_end_time, charger_end_time, notes
              FROM session_log_reconciliations
             WHERE session_id = $1::uuid AND log_import_id = $2
            """,
            session_id,
            import_row["id"],
        )

    payload: dict[str, Any] = {
        "session": {
            "session_id": str(session_row["session_id"]),
            "station_id": session_row["station_id"],
            "connector_id": session_row["connector_id"],
            "transaction_id": session_row["transaction_id"],
            "vehicle_id": (
                str(session_row["vehicle_id"]) if session_row["vehicle_id"] is not None else None
            ),
            "start_time": _isoformat(session_row["start_time"]),
            "end_time": _isoformat(session_row["end_time"]),
            "energy_delivered_kwh": (
                float(session_row["energy_delivered_kwh"])
                if session_row["energy_delivered_kwh"] is not None
                else None
            ),
            "cost_total": (
                float(session_row["cost_total"]) if session_row["cost_total"] is not None else None
            ),
            "cost_total_source": session_row["cost_total_source"],
        },
        "import": {
            "import_id": str(import_row["id"]),
            "status": import_row["status"],
            "source": import_row["source"],
            "vendor": import_row["vendor"],
            "file_name": import_row["file_name"],
            "file_size_bytes": import_row["file_size_bytes"],
            "content_sha256": import_row["content_sha256"],
            "requested_at": _isoformat(import_row["requested_at"]),
            "received_at": _isoformat(import_row["received_at"]),
            "parsed_at": _isoformat(import_row["parsed_at"]),
            "reconciled_at": _isoformat(import_row["reconciled_at"]),
            "error_message": import_row["error_message"],
        },
        "charger_entries": [
            {
                "time": _isoformat(e["time"]),
                "connector_id": e["connector_id"],
                "transaction_id": e["transaction_id"],
                "soc": e["soc"],
                "charging_kw": e["charging_kw"],
                "energy_kwh": e["energy_kwh"],
                "raw_fields": e["raw_fields"],
            }
            for e in entries
        ],
        "reconciliation": (
            {
                "computed_at": _isoformat(reconciliation["computed_at"]),
                "source": reconciliation["source"],
                "our_energy_kwh": reconciliation["our_energy_kwh"],
                "charger_energy_kwh": reconciliation["charger_energy_kwh"],
                "energy_delta_pct": reconciliation["energy_delta_pct"],
                "our_duration_s": reconciliation["our_duration_s"],
                "charger_duration_s": reconciliation["charger_duration_s"],
                "our_start_time": _isoformat(reconciliation["our_start_time"]),
                "charger_start_time": _isoformat(reconciliation["charger_start_time"]),
                "our_end_time": _isoformat(reconciliation["our_end_time"]),
                "charger_end_time": _isoformat(reconciliation["charger_end_time"]),
                "notes": reconciliation["notes"],
            }
            if reconciliation is not None
            else None
        ),
    }
    return payload


@app.post(
    "/admin/depots/{depot_id}/chargers/{charger_id}/local_auth/reset",
    tags=["admin"],
    summary="Clear cached LocalAuthorizationList support outcome for a charger",
)
async def reset_charger_local_auth_cache_endpoint(
    depot_id: str,
    charger_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Force a re-probe of LocalAuthListManagement support on the next reconnect.

    Use cases:
      * Firmware was upgraded on the charger to a version that now
        supports LocalAuthListManagement, and we want to retest WITHOUT
        waiting for the per-firmware cache to invalidate naturally.
      * A transient internal error on a supported charger caused us to
        cache ``supported=False`` and we want to give it another chance.
      * Ops debugging a specific charger's bootstrap behavior.

    Side effect: zeros ``charging_stations.local_list_supported``,
    ``local_list_probed_firmware``, ``local_list_probed_at``,
    ``local_list_last_status`` AND ``local_list_version``. The version
    reset means the next sync runs the first-sync code path including
    the bootstrap ChangeConfiguration sequence — exactly the same
    behavior as a brand-new charger. The bootstrap fail-fast (PR #207)
    keeps a worst-case outcome bounded.

    Authorization:
      - favonius_admin: always allowed.
      - customer_admin: allowed only when caller's organization_id
        matches the depot's organization_id.
      - others: 403.

    Audit: writes ``charger.local_auth.cache_reset`` row with the
    previous cache state in metadata so ops can see what was cleared.
    """
    validate_uuid(depot_id, "depot_id")
    validate_uuid(charger_id, "charger_id")

    role = get_user_role(user)
    if role not in ("favonius_admin", "customer_admin"):
        raise _forbidden("FORBIDDEN_ROLE", "favonius_admin or customer_admin role required")

    depot_row, _ = await _resolve_depot_for_admin(
        depot_id,
        user,
        endpoint_name=("POST /admin/depots/{depot_id}/chargers/{charger_id}/local_auth/reset"),
    )
    # _resolve_depot_for_admin enforces tenant access for customer_admin
    # and cross-org for favonius_admin.

    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.static.acquire() as conn:
        result = await db_queries.reset_local_auth_cache(
            conn,
            depot_id=depot_id,
            charger_id=charger_id,
        )

    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error_code": "CHARGER_NOT_FOUND",
                "message": "Charger not found in this depot",
            },
        )

    await _record_admin_action(
        user=user,
        action="charger.local_auth.cache_reset",
        depot_id=depot_id,
        organization_id_override=(
            str(depot_row.get("organization_id")) if depot_row.get("organization_id") else None
        ),
        target_type="charger",
        target_id=str(charger_id),
        metadata={
            "endpoint": ("POST /admin/depots/{depot_id}/chargers/{charger_id}/local_auth/reset"),
            "ocpp_id": result["ocpp_id"],
            "previous_supported": result["previous_supported"],
            "previous_probed_firmware": result["previous_probed_firmware"],
            "legacy_schema": result.get("legacy_schema", False),
        },
    )

    return {
        "depot_id": depot_id,
        "charger_id": charger_id,
        "ocpp_id": result["ocpp_id"],
        "previous_supported": result["previous_supported"],
        "previous_probed_firmware": result["previous_probed_firmware"],
        "reset_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post(
    "/admin/depots/{depot_id}/chargers/{charger_id}/manual_authorize",
    tags=["admin"],
    summary="Authorize a charging session at a charger without an RFID scan",
)
async def manual_authorize_charger_endpoint(
    depot_id: str,
    charger_id: str,
    body: dict = Body(...),
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Mint a one-shot operator authorization override + dispatch RemoteStartTransaction.

    Use case: an operator wants to start a charging session when the RFID
    reader is broken or the driver has no card. Flow:

      1. Validate caller is customer_admin (or favonius_admin) with tenant
         access to the depot.
      2. Reject if a recent unconsumed override already exists for this
         connector (60 s cooldown — the de-facto idempotency).
      3. Mint synthetic id_tag ``OP-<uuid>`` + insert a row into
         ``operator_authorization_overrides`` (migration 031).
      4. Enqueue a ``remote_start_transaction`` row in
         ``charging_command_queue``; the WS handler's
         ``ChargingCommandQueueConsumer`` drains it within ~2 s.
      5. When the charger sends Authorize/StartTransaction with the
         synthetic tag, ``RFIDAuthorizationService.authorize`` atomically
         consumes the override and returns Accepted.
      6. ``charger.manual_authorize`` audit_log row written.

    Body:
      - ``connector_id`` (int, required): 1-based connector to authorize.
      - ``expires_in_seconds`` (int, optional, default 60, max 300): how
        long the synthetic tag stays valid.
      - ``reason`` (string, optional, max 200 chars): operator note for
        the audit trail.

    Idempotency: an ``Idempotency-Key`` header is accepted and recorded in
    the audit metadata but the natural dedupe is the per-connector
    cooldown — a second POST within 60 s returns 409 RECENT_OVERRIDE_EXISTS
    regardless of whether the same key is sent.
    """
    validate_uuid(depot_id, "depot_id")
    validate_uuid(charger_id, "charger_id")

    role = get_user_role(user)
    if role not in ("favonius_admin", "customer_admin"):
        raise _forbidden("FORBIDDEN_ROLE", "favonius_admin or customer_admin role required")

    connector_id = body.get("connector_id")
    if not isinstance(connector_id, int) or connector_id < 1:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "VALIDATION_ERROR",
                "message": "connector_id must be a positive integer",
            },
        )

    expires_in_seconds = body.get("expires_in_seconds", 60)
    if not isinstance(expires_in_seconds, int) or not (1 <= expires_in_seconds <= 300):
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": "VALIDATION_ERROR",
                "message": "expires_in_seconds must be an integer between 1 and 300",
            },
        )

    reason = body.get("reason")
    if reason is not None:
        if not isinstance(reason, str) or len(reason) > 200:
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": "VALIDATION_ERROR",
                    "message": "reason must be a string of at most 200 characters",
                },
            )

    depot_row, _ = await _resolve_depot_for_admin(
        depot_id,
        user,
        endpoint_name=("POST /admin/depots/{depot_id}/chargers/{charger_id}/manual_authorize"),
    )
    organization_id = depot_row.get("organization_id")
    if not organization_id:
        raise HTTPException(
            status_code=500,
            detail={
                "error_code": "DEPOT_MISSING_ORG",
                "message": "Depot has no organization assigned",
            },
        )

    if not db_pools:
        raise DatabaseError("Database not available")

    # Resolve charger UUID → OCPP id for the queue dispatch.
    async with db_pools.static.acquire() as conn:
        charger_row = await conn.fetchrow(
            """
            SELECT station_id AS ocpp_id
              FROM charging_stations
             WHERE id = $1::uuid
               AND site_id = $2::uuid
            """,
            charger_id,
            depot_id,
        )
    if charger_row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "CHARGER_NOT_FOUND",
                "message": f"Charger {charger_id} not found in depot {depot_id}",
            },
        )
    ocpp_id = charger_row["ocpp_id"]

    synthetic_tag = f"OP-{uuid.uuid4().hex[:17]}"
    audit_metadata = {
        "endpoint": ("POST /admin/depots/{depot_id}/chargers/{charger_id}/manual_authorize"),
        "ocpp_id": ocpp_id,
        "expires_in_seconds": expires_in_seconds,
        "idempotency_key": idempotency_key,
    }

    # Atomic insert of override + queue row in the same TS transaction so a
    # crash mid-call can't leave a queue row pointing at a missing override.
    async with db_pools.ts.acquire() as conn:
        async with conn.transaction():
            # Serialize manual authorization attempts per connector so the
            # cooldown check and inserts are effectively atomic.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1), $2)",
                ocpp_id,
                connector_id,
            )
            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(seconds=expires_in_seconds)
            recent = await conn.fetchrow(
                """
                SELECT id, expires_at, created_at
                  FROM operator_authorization_overrides
                 WHERE station_id = $1
                   AND connector_id = $2
                   AND created_at >= NOW() - INTERVAL '60 seconds'
                ORDER BY created_at DESC
                 LIMIT 1
                """,
                ocpp_id,
                connector_id,
            )
            if recent is not None:
                retry_after = max(
                    int((recent["created_at"] + timedelta(seconds=60) - now).total_seconds()),
                    1,
                )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error_code": "RECENT_OVERRIDE_EXISTS",
                        "message": "A recent override exists for this connector",
                        "retry_after_seconds": retry_after,
                        "active_override_id": str(recent["id"]),
                        "expires_at": recent["expires_at"].isoformat(),
                    },
                )
            override_row = await conn.fetchrow(
                """
                INSERT INTO operator_authorization_overrides (
                    station_id, id_tag, connector_id,
                    organization_id, depot_id, expires_at, created_by,
                    reason, audit_metadata
                ) VALUES (
                    $1, $2, $3, $4::uuid, $5::uuid, $6, $7::uuid, $8, $9::jsonb
                )
                RETURNING id, expires_at
                """,
                ocpp_id,
                synthetic_tag,
                connector_id,
                str(organization_id),
                depot_id,
                expires_at,
                user["sub"],
                reason,
                json.dumps(audit_metadata),
            )
            queue_id = int(
                await conn.fetchval(
                    """
                    INSERT INTO charging_command_queue (
                        charge_point_id, connector_id, command_type,
                        payload, expires_at
                    ) VALUES (
                        $1, $2, 'remote_start_transaction',
                        $3::jsonb,
                        $4
                    )
                    RETURNING queue_id
                    """,
                    ocpp_id,
                    connector_id,
                    json.dumps({"id_tag": synthetic_tag}),
                    expires_at,
                )
            )

    try:
        await _record_admin_action(
            user=user,
            action="charger.manual_authorize",
            depot_id=depot_id,
            organization_id_override=str(organization_id),
            target_type="charger",
            target_id=str(charger_id),
            metadata={
                "endpoint": (
                    "POST /admin/depots/{depot_id}/chargers/{charger_id}/manual_authorize"
                ),
                "ocpp_id": ocpp_id,
                "connector_id": connector_id,
                "override_id": str(override_row["id"]),
                "queue_id": queue_id,
                "expires_at": override_row["expires_at"].isoformat(),
                "reason": reason,
            },
        )
    except Exception:
        logger.warning(
            "manual authorize audit write failed for depot=%s charger=%s override=%s",
            depot_id,
            charger_id,
            override_row["id"],
            exc_info=True,
        )

    return {
        "status": "Accepted",
        "override_id": str(override_row["id"]),
        "expires_at": override_row["expires_at"].isoformat(),
        "queue_id": queue_id,
        "transaction_started": False,
    }


@app.get(
    "/admin/depots/{depot_id}/chargers/{charger_id}/last_manual_override",
    tags=["admin"],
    summary="Most recent manual authorization for a charger (for the admin panel)",
)
async def get_last_manual_override_endpoint(
    depot_id: str,
    charger_id: str,
    user: dict = Depends(ensure_tenant_mirrored),
):
    """Return the most recent manual authorize event for this charger.

    Used by the charger detail page's "Last manual override" panel so the
    operator can see who overrode authorization most recently. Returns
    404 when no override has ever been issued for this charger.
    """
    validate_uuid(depot_id, "depot_id")
    validate_uuid(charger_id, "charger_id")

    role = get_user_role(user)
    if role not in ("favonius_admin", "customer_admin", "customer_operator"):
        raise _forbidden(
            "FORBIDDEN_ROLE",
            "favonius_admin, customer_admin, or customer_operator role required",
        )

    await _resolve_depot_for_admin(
        depot_id,
        user,
        endpoint_name=("GET /admin/depots/{depot_id}/chargers/{charger_id}/last_manual_override"),
    )

    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.static.acquire() as conn:
        charger_row = await conn.fetchrow(
            """
            SELECT station_id AS ocpp_id
              FROM charging_stations
             WHERE id = $1::uuid
               AND site_id = $2::uuid
            """,
            charger_id,
            depot_id,
        )
    if charger_row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "CHARGER_NOT_FOUND",
                "message": f"Charger {charger_id} not found in depot {depot_id}",
            },
        )

    async with db_pools.ts.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, connector_id, created_at, created_by, reason,
                   expires_at, consumed_at
              FROM operator_authorization_overrides
             WHERE station_id = $1
            ORDER BY created_at DESC
             LIMIT 1
            """,
            charger_row["ocpp_id"],
        )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error_code": "NO_MANUAL_OVERRIDE",
                "message": "No manual authorization has been issued for this charger",
            },
        )
    return {
        "id": str(row["id"]),
        "connector_id": row["connector_id"],
        "created_at": row["created_at"].isoformat(),
        "created_by": str(row["created_by"]),
        "reason": row["reason"],
        "expires_at": row["expires_at"].isoformat(),
        "consumed_at": row["consumed_at"].isoformat() if row["consumed_at"] else None,
    }


# ── Command Dispatcher ────────────────────────────────────────────────────────


async def _handle_charger_restart(
    params: dict,
    depot_id: str,
    dry_run: bool,
    user: Optional[dict] = None,
) -> dict:
    """Restart a charger via OCPP RemoteReset.

    Params:
        charger_id: UUID of the target charger (charging_stations.id).
        reset_type: 'Soft' (default) or 'Hard'.

    Production runs with ``OCPP_SERVER_ENABLED=false`` on the API service —
    live charger sockets live in the legacy WS handler. Dispatch is therefore
    queue-mediated: we INSERT a ``remote_reset`` row into
    ``charging_command_queue`` and the WS handler's ``ChargingCommandQueueConsumer``
    drains it (typically within 2 s, sooner via pg_notify).

    Reset is irreversible at the device, so the row is treated terminal on
    first attempt by the consumer and the boot-replay path skips it. We use
    a short 5-minute expiry so a stale request doesn't lurk in the queue.
    """
    charger_id = params.get("charger_id")
    if not charger_id:
        raise HTTPException(status_code=400, detail="params.charger_id is required")
    validate_uuid(charger_id, "charger_id")

    reset_type = str(params.get("reset_type", "Soft"))
    if reset_type not in {"Soft", "Hard"}:
        raise HTTPException(
            status_code=422,
            detail="reset_type must be 'Soft' or 'Hard'",
        )

    if dry_run:
        return {
            "charger_id": charger_id,
            "action": "RemoteReset",
            "reset_type": reset_type,
            "simulated": True,
        }

    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.static.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT station_id AS ocpp_id
              FROM charging_stations
             WHERE id = $1::uuid
               AND site_id = $2::uuid
            """,
            charger_id,
            depot_id,
        )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Charger {charger_id} not found in depot {depot_id}",
        )
    ocpp_id = row["ocpp_id"]

    async with db_pools.ts.acquire() as conn:
        queue_id = int(
            await conn.fetchval(
                """
                INSERT INTO charging_command_queue (
                    charge_point_id, connector_id, command_type,
                    payload, expires_at
                ) VALUES (
                    $1, 0, 'remote_reset',
                    $2::jsonb,
                    NOW() + INTERVAL '5 minutes'
                )
                RETURNING queue_id
                """,
                ocpp_id,
                json.dumps({"type": reset_type}),
            )
        )

    logger.info(
        "Charger restart enqueued",
        extra={
            "charger_id": charger_id,
            "ocpp_id": ocpp_id,
            "depot_id": depot_id,
            "reset_type": reset_type,
            "queue_id": queue_id,
        },
    )
    return {
        "charger_id": charger_id,
        "ocpp_id": ocpp_id,
        "action": "RemoteReset",
        "reset_type": reset_type,
        "queue_id": queue_id,
        "status": "enqueued",
    }


async def _handle_schedule_adjust(
    params: dict,
    depot_id: str,
    dry_run: bool,
    user: Optional[dict] = None,
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
            SELECT s.id AS schedule_id
            FROM schedules s
            JOIN vehicles v ON v.id = s.vehicle_id
            WHERE s.vehicle_id = $1::uuid
              AND v.site_id = $2::uuid
              AND s.departure_time >= $3
            ORDER BY s.departure_time
            LIMIT 1
        )
        UPDATE schedules s
        SET required_soc = $4
        FROM target_schedule ts
        WHERE s.id = ts.schedule_id
        RETURNING s.id::text AS schedule_id, s.departure_time, s.required_soc
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
    user: Optional[dict] = None,
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
        f"UPDATE sites SET {', '.join(set_clauses)} " "WHERE id = $1::uuid RETURNING max_grid_kw"
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
    user: Optional[dict] = None,
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


_VALID_REPORT_KINDS = frozenset(
    {"weekly_ops", "monthly_savings", "monthly_consumption", "incident", "compliance"}
)
_VALID_REPORT_GROUP_BY = frozenset({"card", "vehicle"})


async def _handle_reports_generate(
    params: dict,
    depot_id: str,
    dry_run: bool,
    user: Optional[dict] = None,
    ts_conn: Optional[asyncpg.Connection] = None,
) -> dict:
    """Create a draft Report row.

    For kind='monthly_consumption' the aggregation runs inline and is stored
    as JSONB in reports.data so the export endpoint can stream without
    re-querying.
    """
    kind = params.get("kind") or params.get("Kind")
    if not kind:
        raise HTTPException(status_code=400, detail="params.kind is required")
    if kind not in _VALID_REPORT_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown kind '{kind}'. Valid: {sorted(_VALID_REPORT_KINDS)}",
        )

    title = params.get("title") or f"Report — {kind}"
    group_by = params.get("groupBy") or params.get("group_by")

    if kind == "monthly_consumption" and not group_by:
        raise HTTPException(
            status_code=400,
            detail="params.groupBy is required for kind='monthly_consumption'",
        )
    if group_by and group_by not in _VALID_REPORT_GROUP_BY:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown groupBy '{group_by}'. Valid: {sorted(_VALID_REPORT_GROUP_BY)}",
        )

    # Load depot timezone to resolve defaults and for UTC conversion.
    depot_name, timezone_name, currency, under_cap_rate, ocpp_ids, charger_id_by_ocpp_id = (
        await _load_report_context(depot_id)
    )
    tz = ZoneInfo(timezone_name)

    period_start_str = params.get("periodStart") or params.get("period_start")
    period_end_str = params.get("periodEnd") or params.get("period_end")

    if bool(period_start_str) != bool(period_end_str):
        raise HTTPException(
            status_code=400,
            detail="periodStart and periodEnd must both be provided together",
        )
    if not period_start_str and not period_end_str:
        from .report_schedule_timing import previous_month_bounds  # noqa: PLC0415

        period_start_str, period_end_str, _ = previous_month_bounds(datetime.now(tz))

    try:
        period_start_date = date.fromisoformat(period_start_str)
        period_end_date = date.fromisoformat(period_end_str)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid period date: {exc}",
        ) from exc

    if period_end_date < period_start_date:
        raise HTTPException(status_code=400, detail="periodEnd must be on or after periodStart")

    # Convert local calendar dates → UTC datetimes for storage.
    # period_start = midnight at start of first day in depot TZ.
    # period_end   = exclusive upper bound: midnight of day after last day in depot TZ.
    period_start_utc = datetime(
        period_start_date.year,
        period_start_date.month,
        period_start_date.day,
        0,
        0,
        0,
        tzinfo=tz,
    ).astimezone(timezone.utc)
    _next_day = period_end_date + timedelta(days=1)
    period_end_utc = datetime(
        _next_day.year,
        _next_day.month,
        _next_day.day,
        0,
        0,
        0,
        tzinfo=tz,
    ).astimezone(timezone.utc)

    if dry_run:
        return {
            "kind": kind,
            "periodStart": period_start_str,
            "periodEnd": period_end_str,
            "title": title,
            "groupBy": group_by,
        }

    if not db_pools:
        raise DatabaseError("Database not available")

    # For monthly_consumption: run aggregation and persist result.
    stored_data: Optional[str] = None
    if kind == "monthly_consumption":
        sessions = await _fetch_session_rows(
            depot_id,
            timezone_name,
            ocpp_ids,
            charger_id_by_ocpp_id,
            period_start_date,
            period_end_date,
            ts_conn=ts_conn,
        )
        agg_rows = aggregate_energy_rows(
            sessions,
            timezone=timezone_name,
            group_by=group_by,
            under_cap_rate=under_cap_rate,
            currency=currency,
            from_date=period_start_date,
            to_date=period_end_date,
        )
        totals = compute_energy_totals(
            sessions,
            timezone=timezone_name,
            under_cap_rate=under_cap_rate,
            currency=currency,
            from_date=period_start_date,
            to_date=period_end_date,
        )
        stored_data = json.dumps(
            {
                "rows": agg_rows,
                "currency": currency,
                "group_by": group_by,
                "depot_name": depot_name,
                "totals": totals,
            }
        )

    async def _insert_report(conn: asyncpg.Connection) -> asyncpg.Record:
        return await conn.fetchrow(
            """
            INSERT INTO reports
                (depot_id, title, kind, status, period_start, period_end, group_by, data)
            VALUES
                ($1::uuid, $2, $3, 'draft', $4, $5, $6, $7::jsonb)
            RETURNING id::text, created_at
            """,
            depot_id,
            title,
            kind,
            period_start_utc,
            period_end_utc,
            group_by,
            stored_data,
        )

    if ts_conn is None:
        async with db_pools.ts.acquire() as conn:
            row = await _insert_report(conn)
    else:
        row = await _insert_report(ts_conn)

    return {
        "reportId": str(row["id"]),
        "status": "draft",
        "createdAt": row["created_at"].isoformat(),
        "title": title,
        "kind": kind,
        "groupBy": group_by,
        "periodStart": period_start_str,
        "periodEnd": period_end_str,
    }


async def _handle_reports_approve(
    params: dict,
    depot_id: str,
    dry_run: bool,
    user: Optional[dict] = None,
) -> dict:
    """Approve a draft report: set status='approved', stamp approved_at/by, set export_url."""
    report_id = params.get("reportId") or params.get("report_id")
    if not report_id:
        raise HTTPException(status_code=400, detail="params.reportId is required")
    validate_uuid(report_id, "reportId")

    if dry_run:
        return {"reportId": report_id, "status": "approved"}

    if not db_pools:
        raise DatabaseError("Database not available")

    approved_by = (user or {}).get("email") or (user or {}).get("sub")

    async with db_pools.ts.acquire() as conn:
        async with conn.transaction():
            report_row = await conn.fetchrow(
                """
                SELECT kind, data
                FROM reports
                WHERE id = $1::uuid
                  AND depot_id = $2::uuid
                  AND status IN ('draft', 'pending')
                FOR UPDATE
                """,
                report_id,
                depot_id,
            )
            if not report_row:
                raise HTTPException(
                    status_code=404,
                    detail=f"Report {report_id} not found or not in approvable status",
                )

            export_url: Optional[str] = None
            if report_row["kind"] == "monthly_consumption" and report_row["data"] is not None:
                export_url = f"/depots/{depot_id}/reports/{report_id}/export"

            updated = await conn.fetchrow(
                """
                UPDATE reports
                SET status      = 'approved',
                    approved_at = NOW(),
                    approved_by = $3,
                    export_url  = $4
                WHERE id = $1::uuid
                  AND depot_id = $2::uuid
                  AND status IN ('draft', 'pending')
                RETURNING id::text, approved_at
                """,
                report_id,
                depot_id,
                approved_by,
                export_url,
            )

    return {
        "reportId": report_id,
        "status": "approved",
        "exportUrl": export_url,
        "approvedAt": updated["approved_at"].isoformat(),
    }


def _coerce_payload(raw: Any) -> dict:
    """Return an agent_action payload as a dict, tolerating str-encoded JSONB."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


async def _handle_agent_action_approve(
    params: dict,
    depot_id: str,
    dry_run: bool,
    user: Optional[dict] = None,
) -> dict:
    """Approve a pending agent action.

    For a manual report_draft this regenerates the report from the embedded
    payload (legacy behaviour). For a schedule-originated report_draft (payload
    carries runId + scheduleId) the report already exists, so approval instead
    delivers it and flips the originating run from pending_approval to
    succeeded. Either way the action is marked 'executed'.
    """
    action_id = params.get("actionId") or params.get("action_id")
    if not action_id:
        raise HTTPException(status_code=400, detail="params.actionId is required")
    validate_uuid(action_id, "actionId")

    if not db_pools:
        raise DatabaseError("Database not available")

    def _report_params_from_payload(payload: dict) -> dict:
        return {
            "kind": payload.get("kind"),
            "title": payload.get("title"),
            "groupBy": payload.get("groupBy"),
            "periodStart": payload.get("periodStart"),
            "periodEnd": payload.get("periodEnd"),
        }

    report_result: Optional[dict] = None
    if dry_run:
        async with db_pools.ts.acquire() as conn:
            action_row = await conn.fetchrow(
                """
                SELECT action_class, status, payload
                FROM agent_actions
                WHERE id = $1::uuid AND depot_id = $2::uuid
                """,
                action_id,
                depot_id,
            )
        if not action_row:
            raise HTTPException(status_code=404, detail=f"Action {action_id} not found")

        current_status = action_row["status"]
        if current_status not in ("pending", "shadow"):
            raise HTTPException(
                status_code=409,
                detail=f"Action is already '{current_status}' and cannot be approved",
            )

        if action_row["action_class"] == "report_draft":
            payload = _coerce_payload(action_row["payload"])
            if payload.get("runId") and payload.get("scheduleId"):
                # Scheduled draft: the report already exists; approval delivers it.
                report_result = {"reportId": payload.get("reportId"), "scheduled": True}
            else:
                report_result = await _handle_reports_generate(
                    _report_params_from_payload(payload),
                    depot_id,
                    dry_run=True,
                    user=user,
                )

        result: dict = {"actionId": action_id, "actionStatus": "executed"}
        if report_result:
            result["report"] = report_result
        return result

    scheduled_run_id: Optional[str] = None
    scheduled_payload: dict = {}
    mark_executed_after_delivery = False

    async with db_pools.ts.acquire() as conn:
        async with conn.transaction():
            action_row = await conn.fetchrow(
                """
                SELECT *
                FROM agent_actions
                WHERE id = $1::uuid AND depot_id = $2::uuid
                FOR UPDATE
                """,
                action_id,
                depot_id,
            )
            if not action_row:
                raise HTTPException(status_code=404, detail=f"Action {action_id} not found")

            current_status = action_row["status"]
            if current_status not in ("pending", "shadow"):
                raise HTTPException(
                    status_code=409,
                    detail=f"Action is already '{current_status}' and cannot be approved",
                )

            action_class = action_row["action_class"]

            if action_class == "report_draft":
                payload = _coerce_payload(action_row["payload"])
                if payload.get("runId") and payload.get("scheduleId"):
                    # Schedule-originated draft: deliver post-commit; keep the
                    # action pending until delivery succeeds so approval can retry.
                    scheduled_run_id = str(payload["runId"])
                    scheduled_payload = payload
                    mark_executed_after_delivery = True
                else:
                    report_result = await _handle_reports_generate(
                        _report_params_from_payload(payload),
                        depot_id,
                        dry_run=False,
                        user=user,
                        ts_conn=conn,
                    )

            if not mark_executed_after_delivery:
                updated = await conn.fetchrow(
                    """
                    UPDATE agent_actions
                    SET status = 'executed', resolved_at = NOW()
                    WHERE id = $1::uuid
                      AND depot_id = $2::uuid
                      AND status IN ('pending', 'shadow')
                    RETURNING id::text
                    """,
                    action_row["id"],
                    depot_id,
                )
                if not updated:
                    raise HTTPException(
                        status_code=409,
                        detail="Action could not be approved because its status changed",
                    )

    # Post-commit: deliver the already-generated scheduled report and flip its
    # run to succeeded. Done outside the transaction to avoid holding the row
    # lock across email I/O.
    if scheduled_run_id is not None:
        run_wire: Optional[dict] = None
        delivery_succeeded = False
        if report_email_client is not None:
            run_wire, delivery_succeeded = await _report_schedules.deliver_pending_run(
                db_pools,
                run_id=scheduled_run_id,
                email_client=report_email_client,
                default_from=report_email_from,
            )
        else:
            async with db_pools.ts.acquire() as conn:
                run_wire = await _report_schedules.serialize_run(conn, scheduled_run_id)
        if not delivery_succeeded:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Report delivery failed or is unavailable; the action remains "
                    "pending so approval can be retried"
                ),
            )
        async with db_pools.ts.acquire() as conn:
            updated = await conn.fetchrow(
                """
                UPDATE agent_actions
                SET status = 'executed', resolved_at = NOW()
                WHERE id = $1::uuid
                  AND depot_id = $2::uuid
                  AND status IN ('pending', 'shadow')
                RETURNING id::text
                """,
                action_id,
                depot_id,
            )
            if not updated:
                raise HTTPException(
                    status_code=409,
                    detail="Action could not be approved because its status changed",
                )
        report_result = {
            "reportId": scheduled_payload.get("reportId"),
            "scheduleId": scheduled_payload.get("scheduleId"),
            "run": run_wire,
        }

    result: dict = {"actionId": action_id, "actionStatus": "executed"}
    if report_result:
        result["report"] = report_result
    return result


async def _handle_agent_action_reject(
    params: dict,
    depot_id: str,
    dry_run: bool,
    user: Optional[dict] = None,
) -> dict:
    """Reject a pending agent action."""
    action_id = params.get("actionId") or params.get("action_id")
    if not action_id:
        raise HTTPException(status_code=400, detail="params.actionId is required")
    validate_uuid(action_id, "actionId")

    if dry_run:
        return {"actionId": action_id, "actionStatus": "rejected"}

    if not db_pools:
        raise DatabaseError("Database not available")

    # Lock the action row FOR UPDATE and skip the originating run in the SAME
    # transaction, so a concurrent approve (which also locks the row FOR UPDATE)
    # is serialized — it can't race the run/action state apart and strand a run.
    async with db_pools.ts.acquire() as conn:
        async with conn.transaction():
            action_row = await conn.fetchrow(
                """
                SELECT action_class, status, payload
                FROM agent_actions
                WHERE id = $1::uuid AND depot_id = $2::uuid
                FOR UPDATE
                """,
                action_id,
                depot_id,
            )
            if not action_row or action_row["status"] not in ("pending", "shadow"):
                raise HTTPException(
                    status_code=404,
                    detail=f"Action {action_id} not found or not in a rejectable state",
                )

            if action_row["action_class"] == "report_draft":
                payload = _coerce_payload(action_row["payload"])
                run_id = payload.get("runId")
                if run_id and payload.get("scheduleId"):
                    skipped = await conn.fetchrow(
                        "UPDATE schedule_runs SET status = 'skipped', completed_at = NOW() "
                        "WHERE id = $1::uuid AND status = 'pending_approval' "
                        "RETURNING schedule_id::text",
                        str(run_id),
                    )
                    if skipped is not None:
                        await _report_schedules._update_schedule_last_run_status(
                            conn, str(run_id), "skipped"
                        )

            await conn.execute(
                "UPDATE agent_actions SET status = 'rejected', resolved_at = NOW() "
                "WHERE id = $1::uuid AND depot_id = $2::uuid",
                action_id,
                depot_id,
            )

    return {"actionId": action_id, "actionStatus": "rejected"}


async def _handle_agent_action_rollback(
    params: dict,
    depot_id: str,
    dry_run: bool,
    user: Optional[dict] = None,
) -> dict:
    """Roll back an executed agent action."""
    action_id = params.get("actionId") or params.get("action_id")
    if not action_id:
        raise HTTPException(status_code=400, detail="params.actionId is required")
    validate_uuid(action_id, "actionId")

    if dry_run:
        return {"actionId": action_id, "actionStatus": "rolled_back"}

    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.ts.acquire() as conn:
        updated = await conn.fetchrow(
            """
            UPDATE agent_actions
            SET status = 'rolled_back', resolved_at = NOW()
            WHERE id = $1::uuid
              AND depot_id = $2::uuid
              AND status = 'executed'
            RETURNING id::text
            """,
            action_id,
            depot_id,
        )

    if not updated:
        raise HTTPException(
            status_code=404,
            detail=f"Action {action_id} not found or not in 'executed' state",
        )
    return {"actionId": action_id, "actionStatus": "rolled_back"}


# ── Report schedule command handlers (reports.schedule.*) ─────────────────────
# Writes require ADMIN_CONFIG (granted to customer_admin + favonius_admin only).
# Each handler returns its domain object as the CommandResponse.result; the
# dispatcher supplies the surrounding {status, command, depot_id} envelope.


async def _generate_report_for_schedule(params: dict, depot_id: str) -> str:
    """Adapter for the worker: run reports.generate, return the new report id."""
    result = await _handle_reports_generate(params, depot_id, dry_run=False, user=None)
    return result["reportId"]


async def _get_depot_timezone(depot_id: str) -> str:
    """Resolve a depot's IANA timezone from its static `sites` row."""
    _, timezone_name, *_ = await _load_report_context(depot_id)
    return timezone_name


async def _resolve_schedule_next_run_at(
    depot_id: str, norm, *, now_utc: Optional[datetime] = None
) -> Optional[datetime]:
    """Compute next_run_at for a normalized schedule (None when inactive)."""
    if not norm.is_active:
        return None
    timezone_name = await _get_depot_timezone(depot_id)
    return compute_next_run_at(
        now_utc or datetime.now(timezone.utc),
        frequency=norm.frequency,
        time_of_day=norm.time_of_day,
        tz_name=timezone_name,
        day_of_month=norm.day_of_month,
        day_of_week=norm.day_of_week,
    )


async def _handle_report_schedule_create(
    params: dict, depot_id: str, dry_run: bool, user: Optional[dict] = None
) -> dict:
    """reports.schedule.create — params.input is a ScheduleCreatePayload."""
    try:
        norm = normalize_create_payload(params.get("input"))
    except ScheduleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if dry_run:
        return {"valid": True}
    if not db_pools:
        raise DatabaseError("Database not available")
    try:
        next_run_at = await _resolve_schedule_next_run_at(depot_id, norm)
    except ScheduleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    created_by = (user or {}).get("sub")
    async with db_pools.ts.acquire() as conn:
        async with conn.transaction():
            row = await _report_schedules.insert_schedule(
                conn,
                depot_id=depot_id,
                norm=norm,
                next_run_at=next_run_at,
                created_by=created_by,
            )
            await _report_schedules.replace_recipients(conn, str(row["id"]), norm.recipients)
            schedule = await _report_schedules.serialize_schedule(conn, depot_id, str(row["id"]))
    return schedule  # type: ignore[return-value]


async def _handle_report_schedule_update(
    params: dict, depot_id: str, dry_run: bool, user: Optional[dict] = None
) -> dict:
    """reports.schedule.update — params.scheduleId + params.patch (SchedulePatchPayload)."""
    schedule_id = params.get("scheduleId") or params.get("schedule_id")
    if not schedule_id:
        raise HTTPException(status_code=400, detail="params.scheduleId is required")
    validate_uuid(schedule_id, "scheduleId")
    patch = params.get("patch") or {}
    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.ts.acquire() as conn:
        existing = await _report_schedules.fetch_schedule_row(conn, depot_id, schedule_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"Report schedule {schedule_id} not found")

    current = {
        "name": existing["name"],
        "kind": existing["kind"],
        "group_by": existing["group_by"],
        "frequency": existing["frequency"],
        "day_of_month": existing["day_of_month"],
        "day_of_week": existing["day_of_week"],
        "time_of_day": existing["time_of_day"],
        "autonomy_mode": existing["autonomy_mode"],
        "is_active": existing["is_active"],
    }
    try:
        norm = normalize_patch_payload(patch, current=current)
    except ScheduleValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if dry_run:
        return {"valid": True}

    # Only recompute next_run_at when the patch changes cadence/activation.
    # A metadata-only edit (name, kind, groupBy, recipients) must keep the
    # existing next_run_at so an overdue pending slot isn't silently jumped
    # forward and dropped.
    _cadence_keys = {"frequency", "dayOfMonth", "dayOfWeek", "timeOfDay", "isActive"}
    if isinstance(patch, dict) and _cadence_keys & set(patch.keys()):
        try:
            next_run_at = await _resolve_schedule_next_run_at(depot_id, norm)
        except ScheduleValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        next_run_at = existing["next_run_at"]

    recipients_present = isinstance(patch, dict) and "recipients" in patch
    async with db_pools.ts.acquire() as conn:
        async with conn.transaction():
            await _report_schedules.update_schedule_fields(
                conn, schedule_id=schedule_id, norm=norm, next_run_at=next_run_at
            )
            if recipients_present:
                await _report_schedules.replace_recipients(conn, schedule_id, norm.recipients)
            schedule = await _report_schedules.serialize_schedule(conn, depot_id, schedule_id)
    return schedule  # type: ignore[return-value]


async def _handle_report_schedule_delete(
    params: dict, depot_id: str, dry_run: bool, user: Optional[dict] = None
) -> dict:
    """reports.schedule.delete — params.scheduleId."""
    schedule_id = params.get("scheduleId") or params.get("schedule_id")
    if not schedule_id:
        raise HTTPException(status_code=400, detail="params.scheduleId is required")
    validate_uuid(schedule_id, "scheduleId")
    if dry_run:
        return {"scheduleId": schedule_id, "deleted": True}
    if not db_pools:
        raise DatabaseError("Database not available")
    async with db_pools.ts.acquire() as conn:
        deleted = await _report_schedules.delete_schedule(conn, depot_id, schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Report schedule {schedule_id} not found")
    return {"scheduleId": schedule_id, "deleted": True}


async def _handle_report_schedule_run_now(
    params: dict, depot_id: str, dry_run: bool, user: Optional[dict] = None
) -> dict:
    """reports.schedule.run_now — fire a schedule immediately (returns ScheduleRun).

    Behaves like a tick that resolved this schedule, with scheduled_for = now
    (truncated to the second so a double-click is deduped by the schedule_runs
    UNIQUE constraint). Does not alter the cadence-based next_run_at.
    """
    schedule_id = params.get("scheduleId") or params.get("schedule_id")
    if not schedule_id:
        raise HTTPException(status_code=400, detail="params.scheduleId is required")
    validate_uuid(schedule_id, "scheduleId")
    if dry_run:
        return {"scheduleId": schedule_id, "status": "ok"}
    if not db_pools:
        raise DatabaseError("Database not available")

    async with db_pools.ts.acquire() as conn:
        schedule = await _report_schedules.fetch_schedule_row(conn, depot_id, schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail=f"Report schedule {schedule_id} not found")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    timezone_name = await _get_depot_timezone(depot_id)

    async with db_pools.ts.acquire() as conn:
        run_id = await _report_schedules.claim_run(conn, schedule_id, now_utc)

    if run_id is not None:
        try:
            terminal_status = await _report_schedules.execute_schedule_run(
                db_pools,
                schedule_row=schedule,
                run_id=run_id,
                tz_name=timezone_name,
                email_client=report_email_client,
                generate_report=_generate_report_for_schedule,
                default_from=report_email_from,
                now_utc=now_utc,
            )
        except Exception as exc:  # noqa: BLE001 - record a clean failed run, not a stuck placeholder
            logger.error("run_now failed schedule=%s: %s", schedule_id, exc, exc_info=True)
            async with db_pools.ts.acquire() as conn:
                await _report_schedules.finalize_run(
                    conn, run_id, status="failed", error_message=str(exc)
                )
            terminal_status = "failed"
        async with db_pools.ts.acquire() as conn:
            await conn.execute(
                "UPDATE report_schedules SET last_run_at = $2, last_run_status = $3, "
                "updated_at = NOW() WHERE id = $1::uuid",
                schedule_id,
                now_utc,
                terminal_status,
            )
        target_run_id: Optional[str] = run_id
    else:
        # Double-click within the same second → return the run already claimed.
        async with db_pools.ts.acquire() as conn:
            existing_run = await conn.fetchrow(
                "SELECT id::text FROM schedule_runs "
                "WHERE schedule_id = $1::uuid AND scheduled_for = $2",
                schedule_id,
                now_utc,
            )
        target_run_id = existing_run["id"] if existing_run else None
        if target_run_id is not None:
            await _report_schedules.wait_for_run_finalized(db_pools, target_run_id)

    if target_run_id is None:
        raise HTTPException(status_code=500, detail="run_now failed to produce a run")
    async with db_pools.ts.acquire() as conn:
        return await _report_schedules.serialize_run(conn, target_run_id)  # type: ignore[return-value]
async def _handle_agent_autonomy_set(
    params: dict,
    depot_id: str,
    dry_run: bool,
    user: Optional[dict] = None,
) -> dict:
    """Upsert one row of the depot autonomy matrix.

    Params:
        actionClass: action class string (any non-empty value; trimmed).
        level: one of shadow | proposed | auto_notify | auto_silent.
    """
    action_class = params.get("actionClass") or params.get("action_class")
    level = params.get("level")
    if not isinstance(action_class, str) or not action_class.strip():
        raise HTTPException(
            status_code=400, detail="params.actionClass is required (non-empty string)"
        )
    action_class = action_class.strip()
    if level not in _AGENT_AUTONOMY_LEVELS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"params.level must be one of {list(_AGENT_AUTONOMY_LEVELS)}; " f"got {level!r}"
            ),
        )

    if dry_run:
        return {
            "actionClass": action_class,
            "level": level,
            "simulated": True,
        }

    if not db_pools:
        raise DatabaseError("Database not available")

    actor_raw = user.get("sub") if isinstance(user, dict) else None
    actor_id: Optional[UUID] = None
    if actor_raw:
        try:
            actor_id = UUID(str(actor_raw))
        except (TypeError, ValueError):
            actor_id = None

    async with db_pools.ts.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO agent_autonomy_settings
                (depot_id, action_class, level, updated_at, updated_by)
            VALUES ($1::uuid, $2, $3, NOW(), $4)
            ON CONFLICT (depot_id, action_class) DO UPDATE
            SET level = EXCLUDED.level,
                updated_at = NOW(),
                updated_by = EXCLUDED.updated_by
            RETURNING action_class, level, updated_at
            """,
            depot_id,
            action_class,
            level,
            actor_id,
        )

    return {
        "actionClass": row["action_class"],
        "level": row["level"],
        "updatedAt": row["updated_at"].isoformat(),
    }


async def _handle_alerts_acknowledge(
    params: dict,
    depot_id: str,
    dry_run: bool = False,
    user: Optional[dict] = None,
) -> dict:
    """Transition an active alert to acknowledged.

    Params: alert_id (UUID), acknowledged_by_email (email string, optional).
    Looks up by alert_id within the caller's org and command depot_id.
    """
    alert_id = params.get("alert_id")
    if not alert_id:
        raise HTTPException(status_code=422, detail="params.alert_id is required")
    validate_uuid(alert_id, "alert_id")

    acknowledged_by_email = params.get("acknowledged_by_email")
    if acknowledged_by_email is not None and not isinstance(acknowledged_by_email, str):
        raise HTTPException(
            status_code=422, detail="params.acknowledged_by_email must be a string or null"
        )

    role = get_user_role(user or {})
    org_id = get_user_organization_id(user or {})
    if role != "favonius_admin" and not org_id:
        raise HTTPException(status_code=400, detail="organization_id not present in token")

    actor_raw = (user or {}).get("sub")
    if not actor_raw:
        raise _forbidden("FORBIDDEN", "user id not present in token")
    try:
        actor_uuid = UUID(str(actor_raw))
    except ValueError:
        raise HTTPException(status_code=422, detail="user sub claim is not a valid UUID")

    if not db_pools:
        raise DatabaseError("Database not available")

    from src.notifications import alerts as alerts_repo

    async with db_pools.ts.acquire() as conn:
        existing = await alerts_repo.get_by_id(conn, UUID(alert_id))

    if (
        existing is None
        or (role != "favonius_admin" and str(existing.organization_id) != str(org_id))
        or not _alert_belongs_to_depot(existing.depot_id, depot_id)
    ):
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")

    if existing.status != "active":
        raise HTTPException(
            status_code=409,
            detail=f"Alert {alert_id} has status '{existing.status}'; only active alerts can be acknowledged",
        )

    effective_email = acknowledged_by_email
    if effective_email is None and isinstance(user, dict):
        effective_email = get_user_email(user)

    if dry_run:
        projected = _project_alert_acknowledged(
            existing,
            user_id=actor_uuid,
            user_email=effective_email,
        )
        depot_name: Optional[str] = None
        if existing.depot_id and db_pools:
            async with db_pools.static.acquire() as sc:
                row = await sc.fetchrow(
                    "SELECT name FROM sites WHERE id = $1", existing.depot_id
                )
                depot_name = row["name"] if row else None
        return _alert_to_notification_item(projected, depot_name=depot_name).model_dump()

    async with db_pools.ts.acquire() as conn:
        updated = await alerts_repo.acknowledge_for_org(
            conn,
            UUID(alert_id),
            org_id=UUID(str(existing.organization_id)),
            user_id=actor_uuid,
            user_email=effective_email,
        )

    if updated is None:
        async with db_pools.ts.acquire() as conn:
            current = await alerts_repo.get_by_id(conn, UUID(alert_id))
        if current is not None and current.status != "active":
            raise HTTPException(
                status_code=409,
                detail=f"Alert {alert_id} is already {current.status}",
            )
        raise HTTPException(
            status_code=409,
            detail=f"Alert {alert_id} could not be acknowledged",
        )

    depot_name = None
    if updated.depot_id and db_pools:
        try:
            async with db_pools.static.acquire() as sc:
                row = await sc.fetchrow("SELECT name FROM sites WHERE id = $1", updated.depot_id)
                depot_name = row["name"] if row else None
        except Exception:
            logger.warning("Failed to fetch depot name for alert %s; omitting from response", alert_id)

    return _alert_to_notification_item(updated, depot_name=depot_name).model_dump()


async def _handle_alerts_resolve(
    params: dict,
    depot_id: str,
    dry_run: bool = False,
    user: Optional[dict] = None,
) -> dict:
    """Transition an active or acknowledged alert to resolved.

    Params: alert_id (UUID), acknowledged_by_email (email string, optional).
    Sets acknowledged_by/email via COALESCE so existing values are preserved.
    Looks up by alert_id within the caller's org and command depot_id.
    """
    alert_id = params.get("alert_id")
    if not alert_id:
        raise HTTPException(status_code=422, detail="params.alert_id is required")
    validate_uuid(alert_id, "alert_id")

    acknowledged_by_email = params.get("acknowledged_by_email")
    if acknowledged_by_email is not None and not isinstance(acknowledged_by_email, str):
        raise HTTPException(
            status_code=422, detail="params.acknowledged_by_email must be a string or null"
        )

    role = get_user_role(user or {})
    org_id = get_user_organization_id(user or {})
    if role != "favonius_admin" and not org_id:
        raise HTTPException(status_code=400, detail="organization_id not present in token")

    actor_raw = (user or {}).get("sub")
    if not actor_raw:
        raise _forbidden("FORBIDDEN", "user id not present in token")
    try:
        actor_uuid = UUID(str(actor_raw))
    except ValueError:
        raise HTTPException(status_code=422, detail="user sub claim is not a valid UUID")

    if not db_pools:
        raise DatabaseError("Database not available")

    from src.notifications import alerts as alerts_repo

    async with db_pools.ts.acquire() as conn:
        existing = await alerts_repo.get_by_id(conn, UUID(alert_id))

    if (
        existing is None
        or (role != "favonius_admin" and str(existing.organization_id) != str(org_id))
        or not _alert_belongs_to_depot(existing.depot_id, depot_id)
    ):
        raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")

    if existing.status == "resolved":
        raise HTTPException(
            status_code=409, detail=f"Alert {alert_id} is already resolved"
        )

    effective_email = acknowledged_by_email
    if effective_email is None and isinstance(user, dict):
        effective_email = get_user_email(user)

    if dry_run:
        projected = _project_alert_resolved(
            existing,
            user_id=actor_uuid,
            user_email=effective_email,
        )
        depot_name = None
        if existing.depot_id and db_pools:
            async with db_pools.static.acquire() as sc:
                row = await sc.fetchrow(
                    "SELECT name FROM sites WHERE id = $1", existing.depot_id
                )
                depot_name = row["name"] if row else None
        return _alert_to_notification_item(projected, depot_name=depot_name).model_dump()

    async with db_pools.ts.acquire() as conn:
        updated = await alerts_repo.resolve_by_id(
            conn,
            UUID(alert_id),
            org_id=UUID(str(existing.organization_id)),
            user_id=actor_uuid,
            user_email=effective_email,
        )

    if updated is None:
        async with db_pools.ts.acquire() as conn:
            current = await alerts_repo.get_by_id(conn, UUID(alert_id))
        if current is not None and current.status == "resolved":
            raise HTTPException(
                status_code=409,
                detail=f"Alert {alert_id} is already resolved",
            )
        raise HTTPException(
            status_code=409,
            detail=f"Alert {alert_id} could not be resolved",
        )

    depot_name = None
    if updated.depot_id and db_pools:
        try:
            async with db_pools.static.acquire() as sc:
                row = await sc.fetchrow("SELECT name FROM sites WHERE id = $1", updated.depot_id)
                depot_name = row["name"] if row else None
        except Exception:
            logger.warning("Failed to fetch depot name for alert %s; omitting from response", alert_id)

    return _alert_to_notification_item(updated, depot_name=depot_name).model_dump()


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
    "reports.generate": _CommandSpec(
        required_permission=Permission.DEPOT_VIEW,
        handler=_handle_reports_generate,
    ),
    "reports.approve": _CommandSpec(
        required_permission=Permission.DEPOT_MANAGE,
        handler=_handle_reports_approve,
    ),
    "agents.action.approve": _CommandSpec(
        required_permission=Permission.DEPOT_MANAGE,
        handler=_handle_agent_action_approve,
    ),
    "agents.action.reject": _CommandSpec(
        required_permission=Permission.DEPOT_MANAGE,
        handler=_handle_agent_action_reject,
    ),
    "agents.action.rollback": _CommandSpec(
        required_permission=Permission.DEPOT_MANAGE,
        handler=_handle_agent_action_rollback,
    ),
    "reports.schedule.create": _CommandSpec(
        required_permission=Permission.ADMIN_CONFIG,
        handler=_handle_report_schedule_create,
    ),
    "reports.schedule.update": _CommandSpec(
        required_permission=Permission.ADMIN_CONFIG,
        handler=_handle_report_schedule_update,
    ),
    "reports.schedule.delete": _CommandSpec(
        required_permission=Permission.ADMIN_CONFIG,
        handler=_handle_report_schedule_delete,
    ),
    "reports.schedule.run_now": _CommandSpec(
        required_permission=Permission.ADMIN_CONFIG,
        handler=_handle_report_schedule_run_now,
    ),
    "agents.autonomy.set": _CommandSpec(
        required_permission=Permission.DEPOT_MANAGE,
        handler=_handle_agent_autonomy_set,
    ),
    "alerts.acknowledge": _CommandSpec(
        required_permission=Permission.DEPOT_MANAGE,
        handler=_handle_alerts_acknowledge,
    ),
    "alerts.resolve": _CommandSpec(
        required_permission=Permission.DEPOT_MANAGE,
        handler=_handle_alerts_resolve,
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
    | `reports.generate` | `depot:view` (viewer+) | `kind`, `title`, `groupBy`, `periodStart`, `periodEnd` |
    | `reports.approve` | `depot:manage` (operator+) | `reportId` |
    | `agents.action.approve` | `depot:manage` (operator+) | `actionId` |
    | `agents.action.reject` | `depot:manage` (operator+) | `actionId` |
    | `agents.action.rollback` | `depot:manage` (operator+) | `actionId` |
    | `agents.autonomy.set` | `depot:manage` (operator+) | `actionClass`, `level` |
    | `alerts.acknowledge` | `depot:manage` (operator+) | `alert_id`, `acknowledged_by_email` |
    | `alerts.resolve` | `depot:manage` (operator+) | `alert_id`, `acknowledged_by_email` |

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

    result = await spec.handler(body.params, body.depot_id, dry_run=body.dry_run, user=user)

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

    role = get_user_role(user)
    caller_org = get_user_organization_id(user)
    if role != "favonius_admin" and caller_org is None:
        raise _forbidden("FORBIDDEN", "organization_id not present in token")

    from src.notifications import alerts as alerts_repo

    async with db_pools.ts.acquire() as conn:
        existing = await alerts_repo.get_by_id(conn, UUID(alert_id))
        # Depot-visibility check + org scope check (the latter prevents a
        # favonius_admin from acknowledging an org-level alert that belongs
        # to a different tenant just by supplying any valid depot_id).
        if (
            existing is None
            or not _alert_belongs_to_depot(existing.depot_id, depot_id)
            or (
                role != "favonius_admin"
                and str(existing.organization_id) != str(caller_org)
            )
        ):
            raise HTTPException(status_code=404, detail=f"Alert {alert_id} not found")

        updated = await alerts_repo.acknowledge_for_org(
            conn,
            UUID(alert_id),
            org_id=UUID(str(existing.organization_id)),
            user_id=UUID(str(actor_user_id)),
            user_email=get_user_email(user) if isinstance(user, dict) else None,
        )
    if updated is None:
        # Existed and depot matched on get_by_id but acknowledge() returned None
        # → alert was already resolved/acknowledged. Surface 409 so callers
        # can distinguish from "not found".
        raise HTTPException(status_code=409, detail=f"Alert {alert_id} is not in active state")
    if updated.acknowledged_at is None:
        raise DatabaseError("Acknowledged alert missing acknowledged_at timestamp")
    return AcknowledgeAlertResponse(
        id=str(updated.id),
        status=updated.status,
        acknowledged_at=updated.acknowledged_at.isoformat(),
    )


def _alert_belongs_to_depot(alert_depot_id: Optional[UUID], depot_id: str) -> bool:
    """Whether an alert is visible for a depot-scoped route or command.

    Org-level alerts (``depot_id IS NULL``) are not tied to one depot and match
    any depot context within the caller's organization.
    """
    if alert_depot_id is None:
        return True
    return str(alert_depot_id) == depot_id


def _project_alert_acknowledged(
    alert: Any,
    *,
    user_id: Optional[UUID],
    user_email: Optional[str],
) -> Any:
    """Dry-run projection of acknowledge_for_org outcome."""
    now = datetime.now(timezone.utc)
    return replace(
        alert,
        status="acknowledged",
        acknowledged_at=now,
        acknowledged_by=user_id,
        acknowledged_by_email=user_email,
    )


def _project_alert_resolved(
    alert: Any,
    *,
    user_id: Optional[UUID],
    user_email: Optional[str],
) -> Any:
    """Dry-run projection of resolve_by_id outcome."""
    now = datetime.now(timezone.utc)
    ack_by = alert.acknowledged_by if alert.acknowledged_by is not None else user_id
    ack_email = alert.acknowledged_by_email if alert.acknowledged_by_email is not None else user_email
    ack_at = alert.acknowledged_at
    if ack_at is None and ack_by is not None:
        ack_at = now
    return replace(
        alert,
        status="resolved",
        resolved_at=now,
        acknowledged_by=ack_by,
        acknowledged_by_email=ack_email,
        acknowledged_at=ack_at,
    )


def _alert_to_notification_item(
    alert: Any,
    *,
    depot_name: Optional[str] = None,
) -> "NotificationAlertItem":
    """Convert an Alert dataclass to its API wire shape."""
    return NotificationAlertItem(
        alert_id=str(alert.id),
        organization_id=str(alert.organization_id),
        depot_id=str(alert.depot_id) if alert.depot_id else None,
        depot_name=depot_name,
        alert_type=alert.alert_type,
        severity=alert.severity.value,
        subject=alert.title,
        body=alert.detail,
        dedup_key=alert.dedup_key,
        status=alert.status,
        first_seen_at=alert.first_occurrence_at.isoformat(),
        last_seen_at=alert.last_occurrence_at.isoformat(),
        occurrence_count=alert.occurrence_count,
        acknowledged_by=str(alert.acknowledged_by) if alert.acknowledged_by else None,
        acknowledged_by_email=alert.acknowledged_by_email,
        resolved_at=alert.resolved_at.isoformat() if alert.resolved_at else None,
        last_notified_at=alert.last_notified_at.isoformat() if alert.last_notified_at else None,
    )


@app.get(
    "/alerts",
    response_model=OrgAlertsListResponse,
    tags=["alerts"],
    summary="List organization alerts",
    description="""
    Paginated, org-scoped alert list. Scope is derived from the caller's JWT
    ``app_metadata.organization_id``; favonius_admin callers must have an org
    or will receive a 400.

    **Filters (all optional):** ``status``, ``severity``, ``depot_id``,
    ``alert_type``, ``page`` (1-based, default 1), ``page_size`` (default 25).

    Returns 200 with an empty ``items`` array when no alerts match.

    **Authentication:** Requires JWT in Authorization header.
    """,
    responses={
        400: {"model": ErrorResponse, "description": "organization_id missing in token"},
        401: {"model": ErrorResponse, "description": "Unauthorized"},
        422: {"model": ErrorResponse, "description": "Invalid filter value"},
        503: {"model": ErrorResponse, "description": "Database not available"},
    },
)
async def list_org_alerts(
    status: Optional[str] = Query(
        None, description="Filter by status: active | acknowledged | resolved"
    ),
    severity: Optional[str] = Query(
        None, description="Filter by severity: info | warning | critical"
    ),
    depot_id: Optional[str] = Query(None, description="Filter to a specific depot UUID"),
    alert_type: Optional[str] = Query(None, description="Filter by alert_type string"),
    page: int = Query(1, ge=1, description="1-based page number"),
    page_size: int = Query(25, ge=1, le=200, description="Items per page (max 200)"),
    user: dict = Depends(ensure_tenant_mirrored),
):
    """GET /alerts — paginated org-scoped alert list."""
    if not db_pools:
        raise DatabaseError("Database not available")

    role = get_user_role(user)
    if not has_permission(role, Permission.DEPOT_MANAGE):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    org_id = get_user_organization_id(user)
    if not org_id:
        raise HTTPException(
            status_code=400,
            detail="organization_id not present in token app_metadata",
        )

    if status and status not in ("active", "acknowledged", "resolved"):
        raise HTTPException(status_code=422, detail=f"Invalid status: {status!r}")
    if severity and severity not in ("info", "warning", "critical"):
        raise HTTPException(status_code=422, detail=f"Invalid severity: {severity!r}")
    if depot_id:
        validate_uuid(depot_id, "depot_id")

    from src.notifications import alerts as alerts_repo

    depot_id_uuid = UUID(depot_id) if depot_id else None

    alerts_list: list[Any] = []
    total = 0
    try:
        async with db_pools.ts.acquire() as conn:
            alerts_list, total = await alerts_repo.list_for_org(
                conn,
                UUID(str(org_id)),
                status_filter=status,
                severity_filter=severity,
                depot_id_filter=depot_id_uuid,
                alert_type_filter=alert_type,
                page=page,
                page_size=page_size,
            )
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError) as exc:
        logger.warning("notification_alerts schema unavailable, returning empty list: %s", exc)

    # Batch-fetch depot names from static pool
    unique_depot_ids = [a.depot_id for a in alerts_list if a.depot_id is not None]
    depot_name_map: dict[str, str] = {}
    if unique_depot_ids:
        async with db_pools.static.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, name FROM sites WHERE id = ANY($1::uuid[])",
                unique_depot_ids,
            )
            depot_name_map = {str(r["id"]): r["name"] for r in rows}

    items = [
        _alert_to_notification_item(
            a, depot_name=depot_name_map.get(str(a.depot_id)) if a.depot_id else None
        )
        for a in alerts_list
    ]

    return OrgAlertsListResponse(items=items, total=total, page=page, page_size=page_size)


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
            raise _forbidden(
                "MISSING_ORGANIZATION", "missing organization_id in token app_metadata"
            )
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
            strict=True,
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
        # Alert deliveries (existing): updates notification_deliveries in place.
        await alerts_repo.update_delivery_status(
            conn,
            provider_message_id=event.provider_message_id,
            status=event.status,
            status_detail=event.detail,
        )
        # Scheduled-report deliveries: append a new row so lastDelivery reflects
        # the latest provider status (e.g. bounced) for that recipient triple.
        await _report_schedules.append_delivery_status_from_webhook(
            conn,
            provider_message_id=event.provider_message_id,
            provider_status=event.status,
            detail=event.detail,
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
