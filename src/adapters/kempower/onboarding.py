"""Reusable Kempower onboarding stages (shared by the CLI and the service).

These async functions perform the actual DB writes for a Kempower import —
chargers, vehicles, the charger↔vehicle access matrix, and historical
charging-session backfill. They take asyncpg pools and an
:class:`OnboardingCounts` accumulator and contain **no** I/O shell concerns
(argparse, getpass, stdout/stderr, dry-run printing) so both
``scripts/onboard_depot_from_kempower.py`` (operator CLI) and
``src.core.data_sources.kempower_provider`` (website-triggered ingestion) call
the same code path.

The ``dry_run`` flag is preserved so the CLI keeps its plan-only mode; the
service always passes ``dry_run=False``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import asyncpg
import bcrypt

from src.adapters.kempower import (
    KempowerClient,
    UnsupportedConnectorError,
    kempower_station_to_charger_request,
    kempower_transaction_to_session_row,
    kempower_vehicle_to_identity,
)
from src.db import queries as db_queries

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# OCPP credential helpers (mirror of ``src/api/main.py``)
# ----------------------------------------------------------------------
#
# Re-implemented inline so the import tree stays light — pulling them from
# ``src.api.main`` would drag the entire FastAPI app (and the OCPP library)
# into a module the standalone CLI imports.
#
# If the live admin endpoint's password policy tightens, mirror it here.

_OCPP_BASIC_PASSWORD_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_OCPP_BASIC_PASSWORD_LENGTH = 10


def generate_ocpp_basic_password() -> str:
    """Return an ABB-compatible high-entropy one-time Basic Auth password."""
    return "".join(
        secrets.choice(_OCPP_BASIC_PASSWORD_ALPHABET) for _ in range(_OCPP_BASIC_PASSWORD_LENGTH)
    )


async def hash_ocpp_basic_password(password: str) -> str:
    """Hash a Basic Auth password for ``station_credentials``."""
    hashed = await asyncio.to_thread(bcrypt.hashpw, password.encode("utf-8"), bcrypt.gensalt())
    decoded: str = hashed.decode("utf-8")
    return decoded


# ----------------------------------------------------------------------
# Summary accumulator
# ----------------------------------------------------------------------


@dataclass
class OnboardingCounts:
    """Mutable tally threaded through the stages and rendered into a summary."""

    chargers_created: int = 0
    chargers_already_linked: int = 0
    chargers_skipped: list[str] = field(default_factory=list)
    vehicles_created: int = 0
    vehicles_already_linked: int = 0
    vehicles_skipped: list[str] = field(default_factory=list)
    access_rows_upserted: int = 0
    sessions_inserted: int = 0
    sessions_already_present: int = 0
    sessions_skipped: list[str] = field(default_factory=list)
    credentials_emitted: list[tuple[str, str, str]] = field(default_factory=list)
    site_suggestions: dict[str, Any] = field(default_factory=dict)
    site_patch_applied: bool = False


# ----------------------------------------------------------------------
# Stages
# ----------------------------------------------------------------------


async def load_depot(static_pool: asyncpg.Pool, depot_id: str) -> dict[str, Any]:
    """Return the depot row, raising if it doesn't exist."""
    async with static_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id::text AS depot_id,
                   organization_id::text AS organization_id,
                   name,
                   timezone,
                   currency,
                   max_grid_kw,
                   utility_id,
                   demand_charge_rate_kw,
                   demand_charge_billing_period,
                   address,
                   billing_metadata,
                   building_load_source,
                   building_load_assumption_kw,
                   charger_vehicle_access_default,
                   tariff_type,
                   energy_cap_kwh,
                   under_cap_rate_per_kwh,
                   over_cap_penalty_per_kwh,
                   cap_billing_period,
                   latitude,
                   longitude
            FROM sites
            WHERE id = $1::uuid
            """,
            depot_id,
        )
    if not row:
        raise RuntimeError(
            f"Depot {depot_id!r} not found. Create it via the dashboard "
            "first (POST /admin/depots), then re-run with its UUID."
        )
    return dict(row)


async def import_chargers(
    static_pool: asyncpg.Pool,
    kempower: KempowerClient,
    *,
    depot_id: str,
    kempower_location_id: str,
    dry_run: bool,
    counts: OnboardingCounts,
) -> dict[str, str]:
    """Return ``{kempower_station_id → our_charger_id (UUID text)}``.

    For each Kempower station, look up an existing row in ``charging_stations``
    keyed by ``(site_id, station_id)``. When absent and the run is not dry,
    insert via :func:`db_queries.create_charger_with_credentials` and record the
    one-time plaintext credential on ``counts.credentials_emitted``.
    """
    station_map: dict[str, str] = {}
    async for kempower_station in kempower.iter_charging_stations(kempower_location_id):
        kempower_station_id = kempower_station.get("stationId")
        if not kempower_station_id:
            counts.chargers_skipped.append("(missing stationId)")
            continue
        try:
            charger_request = kempower_station_to_charger_request(kempower_station)
        except UnsupportedConnectorError as exc:
            counts.chargers_skipped.append(f"{kempower_station_id}: {exc}")
            logger.warning("Skipping charger %s: %s", kempower_station_id, exc)
            continue
        except ValueError as exc:
            counts.chargers_skipped.append(f"{kempower_station_id}: {exc}")
            logger.warning("Skipping charger %s: %s", kempower_station_id, exc)
            continue

        async with static_pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id::text AS id FROM charging_stations "
                "WHERE site_id = $1::uuid AND station_id = $2",
                depot_id,
                kempower_station_id,
            )
        if existing:
            station_map[kempower_station_id] = existing["id"]
            counts.chargers_already_linked += 1
            logger.info("Charger %s already linked → %s", kempower_station_id, existing["id"])
            continue

        if dry_run:
            counts.chargers_created += 1
            station_map[kempower_station_id] = f"<would-create:{kempower_station_id}>"
            logger.info(
                "DRY would create charger %s (rated=%skW)",
                kempower_station_id,
                charger_request.rated_kw,
            )
            continue

        password = generate_ocpp_basic_password()
        password_hash = await hash_ocpp_basic_password(password)
        connector_ids = charger_request.connector_ids or list(
            range(1, charger_request.connector_count + 1)
        )
        async with static_pool.acquire() as conn:
            async with conn.transaction():
                created = await db_queries.create_charger_with_credentials(
                    conn,
                    depot_id=depot_id,
                    ocpp_id=kempower_station_id,
                    display_name=charger_request.display_name,
                    vendor=charger_request.vendor,
                    model=charger_request.model,
                    serial_number=charger_request.serial_number,
                    firmware=charger_request.firmware,
                    rated_kw=charger_request.rated_kw,
                    connector_type=charger_request.connector_type,
                    connector_count=charger_request.connector_count,
                    connector_ids=connector_ids,
                    network_notes=charger_request.network_notes,
                    password_hash=password_hash,
                )
        station_map[kempower_station_id] = created["id"]
        counts.chargers_created += 1
        counts.credentials_emitted.append((kempower_station_id, kempower_station_id, password))
        logger.info("Created charger %s → %s", kempower_station_id, created["id"])

    return station_map


async def import_vehicles(
    static_pool: asyncpg.Pool,
    kempower: KempowerClient,
    *,
    depot_id: str,
    organization_id: str,
    kempower_location_id: str,
    dry_run: bool,
    counts: OnboardingCounts,
) -> dict[str, str]:
    """Return ``{kempower_vehicle_id → our_vehicle_id (UUID text)}``."""
    vehicle_map: dict[str, str] = {}
    async for kempower_vehicle in kempower.iter_vehicles(kempower_location_id):
        kempower_vehicle_id = kempower_vehicle.get("id")
        if not kempower_vehicle_id:
            counts.vehicles_skipped.append("(missing id)")
            continue
        try:
            identity = kempower_vehicle_to_identity(kempower_vehicle)
        except ValueError as exc:
            counts.vehicles_skipped.append(f"{kempower_vehicle_id}: {exc}")
            logger.warning("Skipping vehicle %s: %s", kempower_vehicle_id, exc)
            continue

        async with static_pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id::text AS id FROM vehicles "
                "WHERE site_id = $1::uuid AND external_id = $2",
                depot_id,
                identity.external_id,
            )
        if existing:
            vehicle_map[str(kempower_vehicle_id)] = existing["id"]
            counts.vehicles_already_linked += 1
            logger.info(
                "Vehicle %s already linked → %s",
                kempower_vehicle_id,
                existing["id"],
            )
            continue

        if dry_run:
            counts.vehicles_created += 1
            vehicle_map[str(kempower_vehicle_id)] = f"<would-create:{kempower_vehicle_id}>"
            logger.info(
                "DRY would create vehicle %s (battery=%skWh, max_charge=%skW)",
                kempower_vehicle_id,
                identity.battery_kwh,
                identity.max_charge_kw,
            )
            continue

        async with static_pool.acquire() as conn:
            created = await db_queries.create_vehicle_identity(
                conn,
                depot_id=depot_id,
                organization_id=organization_id,
                external_id=identity.external_id,
                vehicle_type=identity.vehicle_type,
                battery_kwh=identity.battery_kwh,
                max_charge_kw=identity.max_charge_kw,
                display_name=identity.display_name,
                id_tag=identity.id_tag,
                vin=identity.vin,
                license_plate=identity.license_plate,
                vehicle_status=identity.status,
            )
        if created is None:
            counts.vehicles_skipped.append(
                f"{kempower_vehicle_id}: create_vehicle_identity returned None"
            )
            continue
        vehicle_map[str(kempower_vehicle_id)] = created["vehicle_id"]
        counts.vehicles_created += 1
        logger.info("Created vehicle %s → %s", kempower_vehicle_id, created["vehicle_id"])

    return vehicle_map


async def upsert_access_matrix(
    static_pool: asyncpg.Pool,
    *,
    depot_id: str,
    station_map: dict[str, str],
    vehicle_map: dict[str, str],
    dry_run: bool,
    counts: OnboardingCounts,
) -> None:
    """All-to-all access for the depot's charger × vehicle product."""
    entries = [
        {
            "charger_id": charger_uuid,
            "vehicle_id": vehicle_uuid,
            "is_accessible": True,
        }
        for charger_uuid in station_map.values()
        for vehicle_uuid in vehicle_map.values()
        if not charger_uuid.startswith("<would-create")
        and not vehicle_uuid.startswith("<would-create")
    ]
    counts.access_rows_upserted = len(entries)
    if not entries:
        return
    if dry_run:
        logger.info("DRY would upsert %d access matrix rows", len(entries))
        return
    async with static_pool.acquire() as conn:
        async with conn.transaction():
            result = await db_queries.upsert_charger_vehicle_access(
                conn, depot_id=depot_id, entries=entries
            )
    if result["invalid_chargers"] or result["invalid_vehicles"]:
        # Should never happen because we just inserted these rows ourselves
        # — but if it does, surface it loudly rather than swallowing.
        raise RuntimeError(
            "upsert_charger_vehicle_access rejected freshly-created IDs: "
            f"invalid_chargers={result['invalid_chargers']} "
            f"invalid_vehicles={result['invalid_vehicles']}"
        )
    logger.info("Upserted %d access matrix rows", len(entries))


async def backfill_sessions(
    ts_pool: asyncpg.Pool,
    kempower: KempowerClient,
    *,
    depot_id: str,
    station_map: dict[str, str],
    vehicle_map: dict[str, str],
    backfill_since: datetime,
    batch_id: UUID,
    dry_run: bool,
    counts: OnboardingCounts,
) -> None:
    """Backfill ``charging_sessions`` rows for every charger in the location."""
    if not station_map:
        logger.info("No stations resolved; skipping transaction backfill")
        return
    end_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    start_iso = backfill_since.isoformat().replace("+00:00", "Z")

    for kempower_station_id, _station_uuid in station_map.items():
        # In dry-run, we still iterate Kempower so the summary reflects the
        # actual transaction count — but we don't write.
        rows_to_insert: list[dict[str, Any]] = []
        async for tx in kempower.iter_transactions(
            station_id=kempower_station_id,
            start_iso=start_iso,
            end_iso=end_iso,
        ):
            # TxInfo has no top-level vehicle id; the link is on schedulePlan.evId
            # (per ChargEye Transactions OpenAPI). Falls back to None for guest
            # / unscheduled charges, in which case the session lands with no
            # vehicle linkage — acceptable for the historical import.
            kempower_vehicle_id = (tx.get("schedulePlan") or {}).get("evId")
            our_vehicle_id: Optional[str] = None
            if kempower_vehicle_id:
                mapped = vehicle_map.get(str(kempower_vehicle_id))
                if mapped and not mapped.startswith("<would-create"):
                    our_vehicle_id = mapped
            try:
                row = kempower_transaction_to_session_row(
                    tx,
                    site_id=depot_id,
                    station_id=kempower_station_id,
                    vehicle_id=our_vehicle_id,
                    batch_id=batch_id,
                )
            except ValueError as exc:
                counts.sessions_skipped.append(f"{tx.get('txId')}: {exc}")
                logger.warning("Skipping transaction %s: %s", tx.get("txId"), exc)
                continue
            rows_to_insert.append(row)

        if not rows_to_insert:
            continue
        if dry_run:
            counts.sessions_inserted += len(rows_to_insert)
            logger.info(
                "DRY would insert %d sessions for station %s",
                len(rows_to_insert),
                kempower_station_id,
            )
            continue

        # Bulk insert in chunks of 500 — matches the migration 030 import path's
        # behaviour and keeps lock holding times tight.
        async with ts_pool.acquire() as conn:
            async with conn.transaction():
                for chunk in chunked(rows_to_insert, 500):
                    inserted = await insert_session_chunk(conn, chunk)
                    counts.sessions_inserted += inserted
                    counts.sessions_already_present += len(chunk) - inserted

        logger.info(
            "Inserted/refreshed %d sessions for station %s",
            len(rows_to_insert),
            kempower_station_id,
        )


def chunked(items: list[Any], size: int):
    """Yield ``items`` in lists of at most ``size`` elements."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def insert_session_chunk(conn: asyncpg.Connection, rows: list[dict[str, Any]]) -> int:
    """INSERT one chunk, returning the number of actually-inserted rows.

    Uses ``ON CONFLICT (site_id, import_row_hash) WHERE source = 'import'
    DO NOTHING`` against the dedup index from migration 030.
    """
    query = """
        INSERT INTO charging_sessions (
            station_id, evse_id, connector_id,
            vehicle_id, id_token,
            start_time, end_time,
            energy_delivered_kwh,
            site_id, source,
            import_batch_id, import_row_hash,
            import_user_full_name, import_station_owner, import_status
        )
        VALUES (
            $1, 0, 0,
            $2::uuid, $3,
            $4, $5,
            $6,
            $7::uuid, 'import',
            $8::uuid, $9,
            $10, $11, $12
        )
        ON CONFLICT (site_id, import_row_hash) WHERE source = 'import'
        DO NOTHING
        RETURNING 1
    """
    inserted = 0
    for row in rows:
        rec = await conn.fetchrow(
            query,
            row["station_id"],
            row["vehicle_id"],
            row["id_token"],
            row["start_time"],
            row["end_time"],
            row["energy_delivered_kwh"],
            row["site_id"],
            row["import_batch_id"],
            row["import_row_hash"],
            row["import_user_full_name"],
            row["import_station_owner"],
            row["import_status"],
        )
        if rec is not None:
            inserted += 1
    return inserted
