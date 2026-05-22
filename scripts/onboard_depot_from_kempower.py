#!/usr/bin/env python3
"""Attach a Kempower ChargEye depot's inventory + history to an existing Favonius depot.

This is the second half of a **two-step onboarding** for Kempower customers:

1. The operator creates the Favonius depot first via the dashboard
   (``POST /admin/depots``). The depot row carries the commercial /
   regulatory context Kempower doesn't expose — utility, tariff, currency,
   timezone, building-load source, stationary battery.
2. This CLI then pulls the inventory and history from Kempower and
   attaches it to that existing depot: chargers, vehicles,
   charger↔vehicle access matrix, and historical charging sessions.

Idempotent. Re-running adds zero new rows when nothing has changed in
Kempower:

* Chargers dedup on ``(charging_stations.site_id, station_id)``.
* Vehicles dedup on ``(vehicles.site_id, external_id)``, with Kempower
  ids namespaced as ``kempower:<vehicleId>`` so two tenants can't
  collide on the global ``vehicles.external_id`` UNIQUE.
* Sessions dedup on ``charging_sessions_import_dedup_idx`` (migration
  030): ``(site_id, sha256(depot_id|start_time_utc|id_tag))`` for
  ``source='import'``.
* Access matrix uses ``ON CONFLICT … DO UPDATE`` (existing helper).

Usage::

    python scripts/onboard_depot_from_kempower.py \\
        --depot-id <our_favonius_depot_uuid> \\
        --kempower-location-id <kempower_loc_id> \\
        --backfill-since 2025-01-01 \\
        [--dry-run | --execute] \\
        [--apply-site-suggestions]

``--dry-run`` (default) reports the planned writes — including which
existing rows would be touched — and exits without writing. ``--execute``
commits. ``--apply-site-suggestions`` prompts the operator per-field to
PATCH the depot's ``name``, ``address``, ``latitude``, ``longitude``,
or ``max_grid_kw`` from the Kempower-side values; without it, the diff
is printed read-only.

Per-charger Basic Auth credentials are emitted to stdout exactly once
on first creation — capture them before re-running. Use the existing
``POST /admin/depots/{id}/chargers/{cid}/rotate_credentials`` endpoint
to rotate later.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4

import asyncpg
import bcrypt

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Imports below intentionally after sys.path tweak.
from src.adapters.kempower import (  # noqa: E402
    KempowerClient,
    KempowerClientError,
    UnsupportedConnectorError,
    kempower_location_to_site_suggestions,
    kempower_station_to_charger_request,
    kempower_transaction_to_session_row,
    kempower_vehicle_to_identity,
)
from src.db import queries as db_queries  # noqa: E402
from src.db.postgres_url import prepare_asyncpg_url_and_ssl  # noqa: E402

logger = logging.getLogger("onboard_depot_from_kempower")


# ----------------------------------------------------------------------
# OCPP credential helpers (mirror of ``src/api/main.py``)
# ----------------------------------------------------------------------
#
# Re-implemented inline so the CLI stays standalone — pulling them from
# ``src.api.main`` would drag the entire FastAPI app (and the OCPP
# library) into the import tree of a tool that needs neither.
#
# If the live admin endpoint's password policy tightens, mirror it here.

_OCPP_BASIC_PASSWORD_ALPHABET = (
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)
_OCPP_BASIC_PASSWORD_LENGTH = 10


def _generate_ocpp_basic_password() -> str:
    """ABB-compatible high-entropy one-time Basic Auth password."""
    return "".join(
        secrets.choice(_OCPP_BASIC_PASSWORD_ALPHABET)
        for _ in range(_OCPP_BASIC_PASSWORD_LENGTH)
    )


async def _hash_ocpp_basic_password(password: str) -> str:
    """Hash a Basic Auth password for ``station_credentials``."""
    hashed = await asyncio.to_thread(
        bcrypt.hashpw, password.encode("utf-8"), bcrypt.gensalt()
    )
    return hashed.decode("utf-8")


# ----------------------------------------------------------------------
# Summary accumulator
# ----------------------------------------------------------------------


@dataclass
class _Counts:
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
# Pool wiring
# ----------------------------------------------------------------------


def _resolve_ts_url() -> str:
    url = (
        os.getenv("DATABASE_URL")
        or os.getenv("TIMESCALE_SERVICE_URL")
    )
    if not url:
        raise RuntimeError(
            "Set DATABASE_URL (or TIMESCALE_SERVICE_URL) to point at the "
            "TimescaleDB instance (charging_sessions lives there)."
        )
    return url


def _resolve_static_url() -> str:
    url = (
        os.getenv("STATIC_DATABASE_URL")
        or os.getenv("SUPABASE_DB_URL")
        or os.getenv("DATABASE_URL")
    )
    if not url:
        raise RuntimeError(
            "Set STATIC_DATABASE_URL (or SUPABASE_DB_URL, or DATABASE_URL) "
            "to point at the Supabase static schema."
        )
    return url


async def _open_pool(url: str, *, label: str) -> asyncpg.Pool:
    clean_url, ssl_config = prepare_asyncpg_url_and_ssl(url)
    connect_kw: dict[str, Any] = {}
    if ssl_config is not None:
        connect_kw["ssl"] = ssl_config
    logger.info("Opening %s pool", label)
    return await asyncpg.create_pool(clean_url, min_size=1, max_size=5, **connect_kw)


# ----------------------------------------------------------------------
# Stages
# ----------------------------------------------------------------------


async def _load_depot(static_pool: asyncpg.Pool, depot_id: str) -> dict[str, Any]:
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


async def _import_chargers(
    static_pool: asyncpg.Pool,
    kempower: KempowerClient,
    *,
    depot_id: str,
    kempower_location_id: str,
    dry_run: bool,
    counts: _Counts,
) -> dict[str, str]:
    """Return ``{kempower_station_id → our_charger_id (UUID text)}``.

    For each Kempower station, look up an existing row in
    ``charging_stations`` keyed by ``(site_id, station_id)``. When absent
    and the run is not dry, insert via
    :func:`db_queries.create_charger_with_credentials` and surface the
    plaintext credential on stdout exactly once.
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
            logger.info(
                "Charger %s already linked → %s", kempower_station_id, existing["id"]
            )
            continue

        if dry_run:
            counts.chargers_created += 1
            station_map[kempower_station_id] = f"<would-create:{kempower_station_id}>"
            logger.info("DRY would create charger %s (rated=%skW)",
                        kempower_station_id, charger_request.rated_kw)
            continue

        password = _generate_ocpp_basic_password()
        password_hash = await _hash_ocpp_basic_password(password)
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
        counts.credentials_emitted.append(
            (kempower_station_id, kempower_station_id, password)
        )
        logger.info("Created charger %s → %s", kempower_station_id, created["id"])

    return station_map


async def _import_vehicles(
    static_pool: asyncpg.Pool,
    kempower: KempowerClient,
    *,
    depot_id: str,
    organization_id: str,
    kempower_location_id: str,
    dry_run: bool,
    counts: _Counts,
) -> dict[str, str]:
    """Return ``{kempower_vehicle_id → our_vehicle_id (UUID text)}``."""
    vehicle_map: dict[str, str] = {}
    async for kempower_vehicle in kempower.iter_vehicles(kempower_location_id):
        kempower_vehicle_id = kempower_vehicle.get("vehicleId")
        if not kempower_vehicle_id:
            counts.vehicles_skipped.append("(missing vehicleId)")
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
                kempower_vehicle_id, existing["id"],
            )
            continue

        if dry_run:
            counts.vehicles_created += 1
            vehicle_map[str(kempower_vehicle_id)] = f"<would-create:{kempower_vehicle_id}>"
            logger.info(
                "DRY would create vehicle %s (battery=%skWh, max_charge=%skW)",
                kempower_vehicle_id, identity.battery_kwh, identity.max_charge_kw,
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
        logger.info(
            "Created vehicle %s → %s", kempower_vehicle_id, created["vehicle_id"]
        )

    return vehicle_map


async def _upsert_access_matrix(
    static_pool: asyncpg.Pool,
    *,
    depot_id: str,
    station_map: dict[str, str],
    vehicle_map: dict[str, str],
    dry_run: bool,
    counts: _Counts,
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


async def _backfill_sessions(
    ts_pool: asyncpg.Pool,
    kempower: KempowerClient,
    *,
    depot_id: str,
    station_map: dict[str, str],
    vehicle_map: dict[str, str],
    backfill_since: datetime,
    batch_id: UUID,
    dry_run: bool,
    counts: _Counts,
) -> None:
    """Backfill ``charging_sessions`` rows for every charger in the location."""
    if not station_map:
        logger.info("No stations resolved; skipping transaction backfill")
        return
    end_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    start_iso = backfill_since.isoformat().replace("+00:00", "Z")

    for kempower_station_id, _station_uuid in station_map.items():
        # In dry-run, we still iterate Kempower so the summary reflects
        # the actual transaction count — but we don't write.
        rows_to_insert: list[dict[str, Any]] = []
        async for tx in kempower.iter_transactions(
            station_id=kempower_station_id,
            start_iso=start_iso,
            end_iso=end_iso,
        ):
            kempower_vehicle_id = tx.get("vehicleId")
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
                len(rows_to_insert), kempower_station_id,
            )
            continue

        # Bulk insert in chunks of 500 — matches the migration 030 import
        # path's behaviour and keeps lock holding times tight.
        async with ts_pool.acquire() as conn:
            async with conn.transaction():
                for chunk in _chunked(rows_to_insert, 500):
                    inserted = await _insert_session_chunk(conn, chunk)
                    counts.sessions_inserted += inserted
                    counts.sessions_already_present += len(chunk) - inserted

        logger.info(
            "Inserted/refreshed %d sessions for station %s",
            len(rows_to_insert), kempower_station_id,
        )


def _chunked(items: list[Any], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


async def _insert_session_chunk(conn: asyncpg.Connection, rows: list[dict[str, Any]]) -> int:
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


async def _maybe_apply_site_suggestions(
    static_pool: asyncpg.Pool,
    kempower: KempowerClient,
    *,
    depot_row: dict[str, Any],
    kempower_location_id: str,
    apply: bool,
    dry_run: bool,
    counts: _Counts,
) -> None:
    """Pull Kempower Location + Power Group; render the diff; optionally PATCH."""
    location = await kempower.get_location(kempower_location_id)
    power_group: Optional[dict[str, Any]] = None
    root_group_id = (
        location.get("rootPowerGroupId") or location.get("powerGroupId")
    )
    if root_group_id:
        try:
            power_group = await kempower.get_power_group(root_group_id)
        except KempowerClientError as exc:
            logger.warning(
                "Could not fetch power group %s: %s", root_group_id, exc
            )

    suggestions = kempower_location_to_site_suggestions(location, power_group)
    diff: dict[str, Any] = {}
    for key, suggested in suggestions.items():
        current = depot_row.get(key)
        if _values_differ(current, suggested):
            diff[key] = {"current": current, "suggested": suggested}

    counts.site_suggestions = diff
    if not diff:
        logger.info("Site data matches Kempower — no suggestions")
        return

    print("\n=== Site data diff (Favonius vs Kempower) ===", file=sys.stderr)
    for key, vals in diff.items():
        print(
            f"  {key:14} current={vals['current']!r}  suggested={vals['suggested']!r}",
            file=sys.stderr,
        )

    if not apply:
        print(
            "Read-only diff (pass --apply-site-suggestions to interactively PATCH).",
            file=sys.stderr,
        )
        return
    if dry_run:
        print(
            "DRY: --apply-site-suggestions ignored under --dry-run.",
            file=sys.stderr,
        )
        return

    chosen: dict[str, Any] = {}
    for key, vals in diff.items():
        prompt = (
            f"Apply Kempower value for {key!r}? "
            f"(current={vals['current']!r} → suggested={vals['suggested']!r}) [y/N]: "
        )
        answer = input(prompt).strip().lower()
        if answer in ("y", "yes"):
            chosen[key] = vals["suggested"]

    if not chosen:
        print("No fields selected; depot left unchanged.", file=sys.stderr)
        return

    # Build the patch by overlaying chosen values onto the current row.
    patch_args = _depot_setup_kwargs_from_row(depot_row)
    patch_args.update(chosen)
    async with static_pool.acquire() as conn:
        result = await db_queries.update_depot_setup(
            conn,
            depot_id=depot_row["depot_id"],
            **patch_args,
        )
    counts.site_patch_applied = result is not None
    logger.info(
        "Applied site suggestions: %s", ", ".join(sorted(chosen.keys()))
    )


def _values_differ(a: Any, b: Any) -> bool:
    """Loose-equality compare for the diff payload."""
    if a is None and b is None:
        return False
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) > 1e-9
        except (TypeError, ValueError):
            return True
    return a != b


def _depot_setup_kwargs_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Translate a ``sites`` SELECT row into ``update_depot_setup`` kwargs."""
    return {
        "name": row["name"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "timezone": row["timezone"],
        "currency": row["currency"],
        "utility_id": row["utility_id"],
        "max_grid_kw": row["max_grid_kw"],
        "demand_charge_rate_kw": row["demand_charge_rate_kw"],
        "demand_charge_billing_period": row["demand_charge_billing_period"],
        "address": _jsonb_to_dict(row.get("address")),
        "billing_metadata": _jsonb_to_dict(row.get("billing_metadata")),
        "building_load_source": _jsonb_to_dict(row.get("building_load_source")),
        "building_load_assumption_kw": row.get("building_load_assumption_kw") or 0.0,
        "charger_vehicle_access_default": row.get(
            "charger_vehicle_access_default", "explicit_matrix"
        ),
        "tariff_type": row.get("tariff_type", "simple_demand"),
        "energy_cap_kwh": row.get("energy_cap_kwh"),
        "under_cap_rate_per_kwh": row.get("under_cap_rate_per_kwh"),
        "over_cap_penalty_per_kwh": row.get("over_cap_penalty_per_kwh"),
        "cap_billing_period": row.get("cap_billing_period", "monthly"),
    }


def _jsonb_to_dict(value: Any) -> dict[str, Any]:
    """Normalise an asyncpg JSONB value (str or dict) into a dict."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        import json
        return json.loads(value)
    return dict(value)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------


async def _run(args: argparse.Namespace) -> int:
    static_url = args.static_database_url or _resolve_static_url()
    ts_url = args.database_url or _resolve_ts_url()
    static_pool = await _open_pool(static_url, label="static")
    ts_pool = await _open_pool(ts_url, label="timescaledb")

    counts = _Counts()
    batch_id = uuid4()

    try:
        depot_row = await _load_depot(static_pool, args.depot_id)
        logger.info(
            "Resolved depot %s (org=%s, name=%r, tz=%s)",
            depot_row["depot_id"],
            depot_row["organization_id"],
            depot_row["name"],
            depot_row["timezone"],
        )

        username = args.kempower_username or os.getenv("KEMPOWER_USERNAME")
        password = args.kempower_password or os.getenv("KEMPOWER_PASSWORD")
        if not password and username:
            password = getpass.getpass(prompt=f"Kempower password for {username}: ")

        async with KempowerClient(
            username=username,
            password=password,
            base_url=args.kempower_base_url,
        ) as kempower:
            station_map = await _import_chargers(
                static_pool,
                kempower,
                depot_id=depot_row["depot_id"],
                kempower_location_id=args.kempower_location_id,
                dry_run=args.dry_run,
                counts=counts,
            )
            vehicle_map = await _import_vehicles(
                static_pool,
                kempower,
                depot_id=depot_row["depot_id"],
                organization_id=depot_row["organization_id"],
                kempower_location_id=args.kempower_location_id,
                dry_run=args.dry_run,
                counts=counts,
            )
            await _upsert_access_matrix(
                static_pool,
                depot_id=depot_row["depot_id"],
                station_map=station_map,
                vehicle_map=vehicle_map,
                dry_run=args.dry_run,
                counts=counts,
            )
            await _backfill_sessions(
                ts_pool,
                kempower,
                depot_id=depot_row["depot_id"],
                station_map=station_map,
                vehicle_map=vehicle_map,
                backfill_since=args.backfill_since,
                batch_id=batch_id,
                dry_run=args.dry_run,
                counts=counts,
            )
            await _maybe_apply_site_suggestions(
                static_pool,
                kempower,
                depot_row=depot_row,
                kempower_location_id=args.kempower_location_id,
                apply=args.apply_site_suggestions,
                dry_run=args.dry_run,
                counts=counts,
            )

        _print_summary(args, depot_row, counts, batch_id)
        return 0
    finally:
        await ts_pool.close()
        await static_pool.close()


def _print_summary(
    args: argparse.Namespace,
    depot_row: dict[str, Any],
    counts: _Counts,
    batch_id: UUID,
) -> None:
    """Emit a structured end-of-run report to stderr (so stdout stays parseable)."""
    mode = "DRY-RUN" if args.dry_run else "EXECUTED"
    print(f"\n=== Kempower onboarding {mode} ===", file=sys.stderr)
    print(f"Depot: {depot_row['name']} ({depot_row['depot_id']})", file=sys.stderr)
    print(f"Kempower location: {args.kempower_location_id}", file=sys.stderr)
    print(f"Import batch id: {batch_id}", file=sys.stderr)
    print(
        f"Chargers:  created={counts.chargers_created}  "
        f"already-linked={counts.chargers_already_linked}  "
        f"skipped={len(counts.chargers_skipped)}",
        file=sys.stderr,
    )
    print(
        f"Vehicles:  created={counts.vehicles_created}  "
        f"already-linked={counts.vehicles_already_linked}  "
        f"skipped={len(counts.vehicles_skipped)}",
        file=sys.stderr,
    )
    print(
        f"Access:    rows={counts.access_rows_upserted}",
        file=sys.stderr,
    )
    print(
        f"Sessions:  inserted={counts.sessions_inserted}  "
        f"already-present={counts.sessions_already_present}  "
        f"skipped={len(counts.sessions_skipped)}",
        file=sys.stderr,
    )
    if counts.chargers_skipped:
        print("\nSkipped chargers:", file=sys.stderr)
        for line in counts.chargers_skipped:
            print(f"  - {line}", file=sys.stderr)
    if counts.vehicles_skipped:
        print("\nSkipped vehicles:", file=sys.stderr)
        for line in counts.vehicles_skipped:
            print(f"  - {line}", file=sys.stderr)
    if counts.credentials_emitted:
        print(
            "\n=== One-time OCPP credentials (capture now — not stored in plaintext) ===",
            file=sys.stderr,
        )
        for ocpp_id, username, password in counts.credentials_emitted:
            print(
                f"  ocpp_id={ocpp_id}  username={username}  password={password}",
                file=sys.stderr,
            )
        print(
            "Rotate via POST /admin/depots/{id}/chargers/{cid}/rotate_credentials.",
            file=sys.stderr,
        )
    if counts.site_suggestions:
        print("\nSite-data diff present (see above).", file=sys.stderr)
        if counts.site_patch_applied:
            print("Applied selected suggestions via update_depot_setup.", file=sys.stderr)


# ----------------------------------------------------------------------
# Arg parsing
# ----------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument(
        "--depot-id",
        type=str,
        required=True,
        help="UUID of an existing Favonius depot to attach Kempower data to.",
    )
    p.add_argument(
        "--kempower-location-id",
        type=str,
        required=True,
        help="Kempower ChargEye Location id to import from.",
    )
    p.add_argument(
        "--backfill-since",
        type=_parse_date_arg,
        default=None,
        help="Backfill transactions on or after this date (YYYY-MM-DD or ISO-8601). "
        "Default: skip transaction backfill.",
    )

    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="(default) Plan writes and emit the summary, but commit nothing.",
    )
    mode.add_argument(
        "--execute",
        dest="dry_run",
        action="store_false",
        help="Actually write rows. The default is --dry-run.",
    )
    p.add_argument(
        "--apply-site-suggestions",
        action="store_true",
        help="Interactively PATCH the depot row from Kempower Location data. "
        "Off by default — diff is printed read-only.",
    )

    # Kempower API
    p.add_argument(
        "--kempower-base-url",
        type=str,
        default=None,
        help="Override KEMPOWER_API_BASE_URL.",
    )
    p.add_argument(
        "--kempower-username",
        type=str,
        default=None,
        help="Override KEMPOWER_USERNAME.",
    )
    p.add_argument(
        "--kempower-password",
        type=str,
        default=None,
        help="Override KEMPOWER_PASSWORD. If absent and username is set, "
        "prompt interactively.",
    )

    # DB URLs
    p.add_argument(
        "--database-url",
        type=str,
        default=None,
        help="TimescaleDB URL. Overrides DATABASE_URL / TIMESCALE_SERVICE_URL.",
    )
    p.add_argument(
        "--static-database-url",
        type=str,
        default=None,
        help="Supabase static-schema URL. Overrides "
        "STATIC_DATABASE_URL / SUPABASE_DB_URL / DATABASE_URL.",
    )
    p.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        help="Python logging level (default: INFO).",
    )

    args = p.parse_args()
    try:
        UUID(args.depot_id)
    except ValueError:
        p.error(f"--depot-id must be a UUID, got {args.depot_id!r}")
    if args.backfill_since is None:
        # Fall back to "everything from 1970" so the iterator pulls
        # whatever the operator's account has — same effective default
        # as passing 1970-01-01 explicitly.
        args.backfill_since = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return args


def _parse_date_arg(value: str) -> datetime:
    text = value.strip()
    if "T" not in text:
        text = f"{text}T00:00:00+00:00"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        logger.warning("Interrupted")
        return 130
    except Exception:
        logger.exception("Fatal error during onboarding")
        return 1


if __name__ == "__main__":
    sys.exit(main())
