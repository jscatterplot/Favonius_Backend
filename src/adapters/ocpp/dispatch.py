"""Charging-profile dispatch from optimization results.

Production ships ``OCPP_SERVER_ENABLED=false`` on the FastAPI service —
the OCPP WebSocket server only runs in the *legacy* websocket_handler
process — so calling ``server.get_charge_point(...).set_charging_profile(...)``
in-process always returned ``None`` and the schedule was dropped on the
floor.

Session 3 makes the queue the *primary* dispatch path:

  Optimizer → INSERT charging_command_queue (status='pending')
            → pg_notify('charging_command_queue', '<queue_id>:<cp_id>')
            → ChargingCommandQueueConsumer (in websocket_handler) drains
              and pushes SetChargingProfile to the connected charger.

Reference: PRD §9.1 (OCPP), §10.5 (observability), session 3 brief.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Optional
from uuid import UUID, uuid4

import asyncpg

from src.core.models import OptimizationResult

from ...db.pools import DatabasePools
from ..chargers.upload_token import (
    build_upload_url,
    get_upload_base_url,
    mint_token,
)
from .charge_point import convert_schedule_to_ocpp_profile
from .mapping import get_vehicle_to_charger_map

logger = logging.getLogger(__name__)


async def dispatch_charging_profiles(
    optimization_result: OptimizationResult,
    *,
    pools: DatabasePools,
    depot_id: str | UUID,
    vehicle_to_charger_map: Optional[dict[str, tuple[str, int]]] = None,
    delta_t: float = 0.25,
    server: Any = None,  # noqa: ARG001  retained for signature compat with callers
    expires_in_min: int = 60,
) -> dict[str, bool]:
    """Convert optimization output to OCPP profiles and enqueue them.

    Each ``OptimizationResult.schedule`` entry becomes one
    ``charging_command_queue`` row. The legacy WebSocket handler's
    ``ChargingCommandQueueConsumer`` (or, on charger reconnect, the
    BootNotification replay path) drains the queue and pushes
    ``SetChargingProfile`` to the charger.

    Args:
        optimization_result: MILP solver output.
        pools: Both DB pools (static for the cp_id mapping, ts for the queue).
        depot_id: Depot UUID — needed to resolve the vehicle→charger map
            unless an explicit map is supplied (tests / unit calls).
        vehicle_to_charger_map: Optional explicit map. When omitted we look
            it up from Supabase (5-min cache).
        delta_t: Scheduling timestep in hours (default 0.25 = 15 min).
        server: Unused; accepted so existing callers don't have to change
            signature in lockstep. Will be removed in a future cleanup.
        expires_in_min: How long an enqueued row stays eligible for delivery
            before transitioning to ``expired``.

    Returns:
        ``{vehicle_id: True}`` if the row was enqueued, ``False`` if it was
        skipped (no mapping, empty schedule, conversion error). 'True' is
        an *enqueue* success, not a delivery success — the consumer
        publishes the delivery outcome via ``profile_push_latency_seconds``
        and the row's terminal status (``sent`` / ``failed``).
    """
    if vehicle_to_charger_map is None:
        try:
            vehicle_to_charger_map = await get_vehicle_to_charger_map(
                pools.static, depot_id, use_cache=True
            )
        except Exception as exc:
            logger.error(
                "Could not resolve vehicle→charger map for depot %s: %s",
                depot_id,
                exc,
            )
            raise

    results: dict[str, bool] = {}
    logger.info(
        "Enqueuing charging profiles for %d vehicles (depot=%s)",
        len(optimization_result.schedule),
        depot_id,
    )

    for vehicle_id, schedule_data in optimization_result.schedule.items():
        charger_info = vehicle_to_charger_map.get(vehicle_id)
        if not charger_info:
            logger.warning(
                "No charger mapping for vehicle %s — schedule period dropped",
                vehicle_id,
            )
            results[vehicle_id] = False
            continue

        charge_point_id, connector_id = charger_info

        charging_power = schedule_data.get("charging_power", []) or []
        schedule = [
            (t, power)
            for t, power in enumerate(charging_power)
            if power is not None and power > 0
        ]
        if not schedule:
            logger.debug(
                "Empty / zero schedule for vehicle %s — nothing to enqueue",
                vehicle_id,
            )
            results[vehicle_id] = False
            continue

        try:
            ocpp_profile = convert_schedule_to_ocpp_profile(
                schedule,
                delta_t=delta_t,
                charging_rate_unit="A",
            )
        except Exception as exc:
            logger.error("Profile conversion failed for vehicle %s: %s", vehicle_id, exc)
            results[vehicle_id] = False
            continue

        # The legacy enqueue helper expects the profile dict (not a list of
        # periods); wrap if convert_schedule_to_ocpp_profile returned the
        # flat period list.
        if isinstance(ocpp_profile, list):
            payload: dict[str, Any] = {
                "chargingProfilePurpose": "TxDefaultProfile",
                "chargingProfileKind": "Absolute",
                "stackLevel": 0,
                "chargingSchedule": {
                    "chargingRateUnit": "A",
                    "chargingSchedulePeriod": ocpp_profile,
                },
            }
        else:
            payload = ocpp_profile
            charging_schedule = payload.get("chargingSchedule")
            if isinstance(charging_schedule, dict):
                charging_schedule["chargingRateUnit"] = "A"

        try:
            queue_id = await _enqueue(
                pools.ts,
                charge_point_id=charge_point_id,
                connector_id=connector_id,
                payload=payload,
                expires_in_min=expires_in_min,
            )
        except Exception as exc:
            logger.error(
                "Failed to enqueue profile for vehicle %s (cp=%s): %s",
                vehicle_id,
                charge_point_id,
                exc,
            )
            results[vehicle_id] = False
            continue

        results[vehicle_id] = True
        logger.info(
            "Enqueued profile queue_id=%s vehicle=%s cp=%s connector=%s",
            queue_id,
            vehicle_id,
            charge_point_id,
            connector_id,
        )

        # Best-effort audit row — keeps charging_commands as the per-run
        # ledger that data-analysts already read.
        try:
            await _store_charging_command(
                pools.ts,
                vehicle_id=vehicle_id,
                charge_point_id=charge_point_id,
                connector_id=connector_id,
                charging_profile=payload,
                optimization_result=optimization_result,
            )
        except Exception as exc:
            logger.warning("Audit insert failed for vehicle %s: %s", vehicle_id, exc)

    success_count = sum(1 for v in results.values() if v)
    logger.info(
        "Charging-profile enqueue complete: %d/%d enqueued",
        success_count,
        len(results),
    )
    return results


async def _enqueue(
    pool: asyncpg.Pool,
    *,
    charge_point_id: str,
    connector_id: int,
    payload: dict[str, Any],
    expires_in_min: int,
) -> int:
    """Insert one row into ``charging_command_queue`` and return queue_id.

    The AFTER INSERT trigger from migration 014 fires
    ``pg_notify('charging_command_queue', '<queue_id>:<cp_id>')`` so a
    LISTEN-based consumer can pick the row up immediately. The polling
    consumer in the legacy handler also catches it on its next tick.
    """
    async with pool.acquire() as conn:
        return int(
            await conn.fetchval(
                """
                INSERT INTO charging_command_queue (
                    charge_point_id, connector_id, command_type,
                    payload, expires_at
                ) VALUES (
                    $1, $2, 'set_charging_profile',
                    $3::jsonb,
                    NOW() + ($4 || ' minutes')::interval
                )
                RETURNING queue_id
                """,
                charge_point_id,
                connector_id,
                json.dumps(payload),
                str(expires_in_min),
            )
        )


async def dispatch_get_diagnostics(
    ts_pool: asyncpg.Pool,
    *,
    station_id: str,
    session_id: Optional[UUID] = None,
    charger_id: Optional[UUID] = None,
    connector_id: Optional[int] = None,
    vendor: Optional[str] = None,
    expires_in_min: Optional[int] = None,
    retries: Optional[int] = None,
    retry_interval: Optional[int] = None,
    start_time: Optional[datetime] = None,
    stop_time: Optional[datetime] = None,
    idempotency_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> UUID:
    """Enqueue a ``GetDiagnostics`` command and create its import row.

    The function:
      1. Inserts a ``charger_log_imports`` row in status ``'requested'``.
      2. Mints an HMAC-signed upload URL embedding the new row's id.
      3. Hashes the token and stores it in ``upload_token_hash`` so
         the upload endpoint can reject mismatched tokens.
      4. Enqueues a ``charging_command_queue`` row with
         ``command_type='get_diagnostics'`` and a payload carrying the
         signed URL, retries/retryInterval, and optional time window.

    The legacy WS handler's ``ChargingCommandQueueConsumer`` drains the
    queue, calls ``FleetChargePoint.get_diagnostics(location=…)`` with
    the signed URL, and the charger uploads to the upload endpoint.

    Args:
        ts_pool: TimescaleDB pool.
        station_id: OCPP charge-point id (matches
            ``charging_command_queue.charge_point_id`` /
            ``charging_sessions.station_id``).
        session_id: ``charging_sessions.session_id`` if pulling logs for
            a specific session. Optional for future depot-wide pulls.
        charger_id: Supabase UUID of the charger (for the imports row).
        connector_id: 1-based connector id, if known.
        vendor: Vendor string (drives parser dispatch at receive time).
            When unset, the receive path looks it up from
            ``charging_stations``.
        expires_in_min: How long the queue row stays eligible for delivery.
        retries / retry_interval: Forwarded to OCPP ``GetDiagnostics``.
        start_time / stop_time: Optional OCPP window for the diagnostic
            dump (passed through to the charger).
        idempotency_key: When set, the row's ``UNIQUE`` constraint on
            ``idempotency_key`` blocks a second request from creating
            a duplicate row.
        base_url: Optional fully-qualified upload URL (scheme + host +
            ``/internal/charger_logs/upload``) derived from the
            triggering request's ``Host``. Used only when
            ``CHARGER_LOG_UPLOAD_BASE_URL`` is unset; the env var takes
            precedence when both are present.

    Returns:
        The new ``charger_log_imports.id``.

    Raises:
        RuntimeError: When neither ``CHARGER_LOG_UPLOAD_BASE_URL`` nor a
            derived ``base_url`` is available (the charger could not
            reach a usable URL), or when ``CHARGER_LOG_UPLOAD_SIGNING_KEY``
            is unset (no secret to sign the token). Dispatch refuses
            up-front rather than emit a broken request.
        asyncpg.UniqueViolationError: When ``idempotency_key`` collides
            with an existing row. Callers translate this to HTTP 409.
    """
    # Resolve the upload URL the charger will POST to. Precedence:
    #   1. CHARGER_LOG_UPLOAD_BASE_URL env — explicit operator override,
    #      required when the charger-reachable host differs from the host
    #      an operator's browser used to trigger this.
    #   2. base_url derived from the triggering request's Host — the
    #      zero-config path for single public-ingress deploys.
    # Fail fast if neither is available, rather than mint a token the
    # charger could never use.
    resolved_base_url = get_upload_base_url() or base_url
    if resolved_base_url is None:
        raise RuntimeError(
            "Charger log upload URL is not configured and could not be derived "
            "from the request: set CHARGER_LOG_UPLOAD_BASE_URL "
            "(and CHARGER_LOG_UPLOAD_SIGNING_KEY)."
        )

    # Anchor the queue row's expiry to the token TTL so a charger that
    # only reconnects late won't be handed a command whose upload URL
    # has already expired. ``get_token_ttl_seconds()`` enforces a 60 s
    # floor; the queue ceiling is at least that, capped at the caller's
    # explicit ``expires_in_min`` when one is supplied.
    from ..chargers.upload_token import get_token_ttl_seconds

    token_ttl_s = get_token_ttl_seconds()
    if expires_in_min is None:
        queue_expires_in_min = max(1, (token_ttl_s + 59) // 60)
    else:
        queue_expires_in_min = min(expires_in_min, max(1, (token_ttl_s + 59) // 60))

    import_id = uuid4()
    # Mint exactly once so the token embedded in the URL is the same
    # one whose sha256 we persist in ``upload_token_hash``. A second
    # mint inside build_upload_url would re-sample ``time.time()`` and
    # could straddle a second boundary, producing a different signature
    # and a 401 "token mismatch" on upload.
    token = mint_token(import_id)
    token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
    location = build_upload_url(import_id, base_url=resolved_base_url, token=token)

    payload: dict[str, Any] = {
        "location": location,
        "import_id": str(import_id),
    }
    if retries is not None:
        payload["retries"] = retries
    if retry_interval is not None:
        payload["retry_interval"] = retry_interval
    if start_time is not None:
        payload["start_time"] = start_time.isoformat()
    if stop_time is not None:
        payload["stop_time"] = stop_time.isoformat()

    async with ts_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO charger_log_imports (
                    id, station_id, charger_id, connector_id, session_id,
                    vendor, source, status, requested_at,
                    upload_token_hash, idempotency_key
                ) VALUES (
                    $1, $2, $3, $4, $5,
                    $6, 'get_diagnostics', 'requested', NOW(),
                    $7, $8
                )
                """,
                import_id,
                station_id,
                charger_id,
                connector_id,
                session_id,
                vendor,
                token_sha256,
                idempotency_key,
            )
            await conn.execute(
                """
                INSERT INTO charging_command_queue (
                    charge_point_id, connector_id, command_type,
                    payload, expires_at
                ) VALUES (
                    $1, $2, 'get_diagnostics',
                    $3::jsonb,
                    NOW() + ($4 || ' minutes')::interval
                )
                """,
                station_id,
                connector_id if connector_id is not None else 0,
                json.dumps(payload),
                str(queue_expires_in_min),
            )

    logger.info(
        "Enqueued GetDiagnostics import_id=%s station=%s session=%s",
        import_id,
        station_id,
        session_id,
    )
    return import_id


async def _store_charging_command(
    pool: asyncpg.Pool,
    *,
    vehicle_id: str,
    charge_point_id: str,
    connector_id: int,
    charging_profile: dict,
    optimization_result: OptimizationResult,
) -> None:
    """Per-run audit insert into ``charging_commands``.

    Distinct from ``charging_command_queue``: this is the immutable record
    of *what we asked for*, scoped to the optimization run, and FK-linked
    to ``chargers.charger_id``. The queue tracks *delivery state*.
    """
    query = """
    INSERT INTO charging_commands (
        charger_id, vehicle_id, issued_at, profile_json, status
    )
    VALUES ($1, $2, $3, $4::jsonb, $5)
    ON CONFLICT DO NOTHING
    """

    profile_json = json.dumps(
        {
            "connector_id": connector_id,
            "profile": charging_profile,
            "run_id": str(getattr(optimization_result, "run_id", "")) or None,
        }
    )

    async with pool.acquire() as conn:
        await conn.execute(
            query,
            charge_point_id,
            vehicle_id,
            datetime.utcnow(),
            profile_json,
            "queued",
        )
