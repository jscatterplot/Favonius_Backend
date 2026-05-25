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
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4

import asyncpg

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Imports below intentionally after sys.path tweak. The DB-writing stages live
# in ``src.adapters.kempower.onboarding`` so the website-triggered ingestion
# service shares one code path with this CLI; they're aliased to their original
# private names here so ``_run`` and the existing tests are unchanged.
from src.adapters.kempower import (  # noqa: E402
    KempowerClient,
    KempowerClientError,
    kempower_location_to_site_suggestions,
)
from src.adapters.kempower import onboarding as _onboarding  # noqa: E402
from src.db import queries as db_queries  # noqa: E402
from src.db.postgres_url import prepare_asyncpg_url_and_ssl  # noqa: E402

# Backwards-compatible private aliases: ``_run`` and the existing tests call the
# stages by their old names. The implementations now live in the shared
# ``onboarding`` module so the website ingestion service uses the same code path.
_Counts = _onboarding.OnboardingCounts
_load_depot = _onboarding.load_depot
_import_chargers = _onboarding.import_chargers
_import_vehicles = _onboarding.import_vehicles
_upsert_access_matrix = _onboarding.upsert_access_matrix
_backfill_sessions = _onboarding.backfill_sessions

logger = logging.getLogger("onboard_depot_from_kempower")


# ----------------------------------------------------------------------
# Pool wiring
# ----------------------------------------------------------------------


def _resolve_ts_url() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("TIMESCALE_SERVICE_URL")
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
    root_group_id = location.get("rootPowerGroupId") or location.get("powerGroupId")
    if root_group_id:
        try:
            power_group = await kempower.get_power_group(root_group_id)
        except KempowerClientError as exc:
            logger.warning("Could not fetch power group %s: %s", root_group_id, exc)

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
    logger.info("Applied site suggestions: %s", ", ".join(sorted(chosen.keys())))


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
            if args.backfill_since is not None:
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
        help="Override KEMPOWER_PASSWORD. If absent and username is set, " "prompt interactively.",
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
