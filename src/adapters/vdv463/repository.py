"""VDV 463 persistence: charging requests and depot updates.

Per PRD Section 9.6 and dev plan Phase 5: store ProvideChargingRequests
in vdv463_charging_requests, track depot updates for trigger monitor.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from .messages import ChargingRequest


async def upsert_charging_request(
    pool: Any,
    depot_id: str,
    presystem_id: str,
    req: ChargingRequest,
    vehicle_id: str,
    charging_point_id: Optional[str],
    message_id: str,
) -> None:
    """Insert or update one charging request. ON CONFLICT (depot_id, charging_request_id, presystem_id) DO UPDATE."""
    precond_type = (
        "manual"
        if req.manual_preconditioning
        else ("automatic" if req.automatic_preconditioning else None)
    )
    precond_start = None
    ambient_temp = None
    req_start = None
    req_finish = None
    hvac_aux = None
    system_aux = None
    if req.manual_preconditioning:
        precond_start = req.manual_preconditioning.get("hvacPreconditioningStartTime")
        hvac_aux = req.manual_preconditioning.get("hvacAuxiliaryConsumerPower")
        system_aux = req.manual_preconditioning.get("systemAuxiliaryConsumerPower")
    if req.automatic_preconditioning:
        ambient_temp = req.automatic_preconditioning.get("ambientTemperature")
        req_start = req.automatic_preconditioning.get("requestedStartTime")
        req_finish = req.automatic_preconditioning.get("requestedFinishTime")

    arrival_ts = _parse_iso(req.arrival_time) if req.arrival_time else None
    departure_ts = _parse_iso(req.departure_time) if req.departure_time else None
    precond_start_ts = _parse_iso(precond_start) if isinstance(precond_start, str) else None
    req_start_ts = _parse_iso(req_start) if isinstance(req_start, str) else None
    req_finish_ts = _parse_iso(req_finish) if isinstance(req_finish, str) else None

    status = "terminated" if req.charging_instruction == "Terminate" else "active"
    await pool.execute(
        """
        INSERT INTO vdv463_charging_requests (
            depot_id, charging_request_id, presystem_id, vehicle_id, charging_point_id,
            priority, charging_instruction, expected_arrival, expected_soc_at_arrival,
            min_target_soc, max_target_soc, requested_departure,
            preconditioning_type, preconditioning_start, ambient_temperature,
            requested_start_time, requested_finish_time, hvac_aux_power, system_aux_power,
            message_id, status, validation_status
        ) VALUES (
            $1::uuid, $2, $3, $4::uuid, $5::uuid,
            $6, $7, $8, $9, $10, $11, $12,
            $13, $14, $15, $16, $17, $18, $19,
            $20, $21, $22
        )
        ON CONFLICT (depot_id, charging_request_id, presystem_id) DO UPDATE SET
            vehicle_id = EXCLUDED.vehicle_id,
            charging_point_id = EXCLUDED.charging_point_id,
            priority = EXCLUDED.priority,
            charging_instruction = EXCLUDED.charging_instruction,
            expected_arrival = EXCLUDED.expected_arrival,
            expected_soc_at_arrival = EXCLUDED.expected_soc_at_arrival,
            min_target_soc = EXCLUDED.min_target_soc,
            max_target_soc = EXCLUDED.max_target_soc,
            requested_departure = EXCLUDED.requested_departure,
            preconditioning_type = EXCLUDED.preconditioning_type,
            preconditioning_start = EXCLUDED.preconditioning_start,
            ambient_temperature = EXCLUDED.ambient_temperature,
            requested_start_time = EXCLUDED.requested_start_time,
            requested_finish_time = EXCLUDED.requested_finish_time,
            hvac_aux_power = EXCLUDED.hvac_aux_power,
            system_aux_power = EXCLUDED.system_aux_power,
            message_id = EXCLUDED.message_id,
            status = EXCLUDED.status,
            validation_status = EXCLUDED.validation_status,
            received_at = NOW()
        """,
        depot_id,
        req.charging_request_id,
        presystem_id,
        vehicle_id,
        charging_point_id,
        req.priority or 1,
        req.charging_instruction,
        arrival_ts,
        req.expected_soc_at_arrival,
        req.min_target_soc,
        req.max_target_soc,
        departure_ts,
        precond_type,
        precond_start_ts,
        ambient_temp,
        req_start_ts,
        req_finish_ts,
        hvac_aux,
        system_aux,
        message_id,
        status,
        req.validation_status or "ok",
    )


async def set_charging_request_terminated(
    pool: Any,
    depot_id: str,
    presystem_id: str,
    charging_request_id: str,
) -> None:
    """Mark a single charging request as terminated."""
    await pool.execute(
        """
        UPDATE vdv463_charging_requests
        SET status = 'terminated'
        WHERE depot_id = $1::uuid AND presystem_id = $2 AND charging_request_id = $3
        """,
        depot_id,
        presystem_id,
        charging_request_id,
    )


async def terminate_requests_not_in_list(
    pool: Any,
    depot_id: str,
    presystem_id: str,
    keep_charging_request_ids: List[str],
) -> None:
    """Mark as terminated all active requests for (depot_id, presystem_id) whose charging_request_id is not in the list."""
    if not keep_charging_request_ids:
        await pool.execute(
            """
            UPDATE vdv463_charging_requests
            SET status = 'terminated'
            WHERE depot_id = $1::uuid AND presystem_id = $2 AND status = 'active'
            """,
            depot_id,
            presystem_id,
        )
        return
    await pool.execute(
        """
        UPDATE vdv463_charging_requests
        SET status = 'terminated'
        WHERE depot_id = $1::uuid AND presystem_id = $2 AND status = 'active'
          AND NOT (charging_request_id = ANY($3::text[]))
        """,
        depot_id,
        presystem_id,
        keep_charging_request_ids,
    )


async def log_vdv463_connection(
    pool: Any,
    depot_id: str,
    presystem_id: str,
    system_type: str = "BMS",
) -> None:
    """Log VDV 463 connection (call on BootNotification)."""
    await pool.execute(
        """
        INSERT INTO vdv463_connections (depot_id, presystem_id, system_type)
        VALUES ($1::uuid, $2, $3)
        """,
        depot_id,
        presystem_id,
        system_type,
    )


async def update_vdv463_connection_disconnect(
    pool: Any,
    depot_id: str,
    presystem_id: str,
    disconnect_reason: Optional[str] = None,
) -> None:
    """Mark the latest VDV 463 connection for (depot_id, presystem_id) as disconnected (call on cleanup)."""
    await pool.execute(
        """
        UPDATE vdv463_connections
        SET disconnected_at = NOW(), disconnect_reason = $3
        WHERE id = (
            SELECT id FROM vdv463_connections
            WHERE depot_id = $1::uuid AND presystem_id = $2 AND disconnected_at IS NULL
            ORDER BY connected_at DESC
            LIMIT 1
        )
        """,
        depot_id,
        presystem_id,
        disconnect_reason,
    )


async def update_depot_vdv463_timestamp(pool: Any, depot_id: str) -> None:
    """Update last VDV 463 update timestamp for trigger monitor."""
    await pool.execute(
        """
        INSERT INTO vdv463_depot_updates (depot_id, last_update_at)
        VALUES ($1::uuid, NOW())
        ON CONFLICT (depot_id) DO UPDATE SET last_update_at = NOW()
        """,
        depot_id,
    )


async def log_vdv463_error(
    pool: Any,
    depot_id: str,
    presystem_id: str,
    error_code: str,
    description: str,
    charging_request_id: Optional[str] = None,
) -> None:
    """Insert an error record for operator diagnostics."""
    await pool.execute(
        """
        INSERT INTO vdv463_errors (depot_id, presystem_id, charging_request_id, error_code, description)
        VALUES ($1::uuid, $2, $3, $4, $5)
        """,
        depot_id,
        presystem_id,
        charging_request_id,
        error_code,
        description,
    )


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse ISO 8601 string to datetime. Returns None on failure."""
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
