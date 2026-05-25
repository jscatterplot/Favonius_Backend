"""Kempower ChargEye provider — the first concrete data source.

Implements :class:`DataSourceProvider` by driving the shared onboarding stages
in ``src.adapters.kempower.onboarding`` (the same code path the operator CLI
uses). Site-suggestion application is intentionally CLI-only: an automated sync
must never silently overwrite the depot's operator-set commercial context.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from src.adapters.kempower import KempowerClient, KempowerClientError
from src.adapters.kempower.onboarding import (
    OnboardingCounts,
    backfill_sessions,
    import_chargers,
    import_vehicles,
    upsert_access_matrix,
)

from .base import (
    CredentialField,
    DataSourceProvider,
    IngestionContext,
    IngestionResult,
    IngestionStatus,
    ProviderCatalogueEntry,
)
from .errors import CredentialValidationError

logger = logging.getLogger(__name__)

_MAX_BACKFILL_DAYS = int(os.getenv("DATA_SOURCES_MAX_BACKFILL_DAYS", "730"))


def _parse_backfill_since(value: Optional[str]) -> Optional[datetime]:
    """Parse a ``backfillSince`` config value (YYYY-MM-DD or ISO-8601) to UTC.

    Returns None when unset (no transaction backfill). Clamps values older than
    ``DATA_SOURCES_MAX_BACKFILL_DAYS`` to bound first-run duration.
    """
    if not value:
        return None
    text = value.strip()
    if "T" not in text:
        text = f"{text}T00:00:00+00:00"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    floor = datetime.now(timezone.utc) - timedelta(days=_MAX_BACKFILL_DAYS)
    return max(dt, floor)


class KempowerProvider(DataSourceProvider):
    """Imports chargers, vehicles, access matrix, and sessions from ChargEye."""

    @property
    def provider_key(self) -> str:
        """Return the stable provider identifier."""
        return "kempower"

    def catalogue_entry(self) -> ProviderCatalogueEntry:
        """Return the credential-form schema for the Data Sources UI."""
        return ProviderCatalogueEntry(
            provider_key=self.provider_key,
            display_name="Kempower ChargEye",
            description=(
                "Import chargers, vehicles, the charger↔vehicle access "
                "matrix, and historical charging sessions from a Kempower "
                "ChargEye location."
            ),
            credential_fields=[
                CredentialField(
                    key="username",
                    label="ChargEye username",
                    type="string",
                    required=True,
                ),
                CredentialField(
                    key="password",
                    label="ChargEye password",
                    type="password",
                    required=True,
                    secret=True,
                ),
                CredentialField(
                    key="locationId",
                    label="ChargEye Location ID",
                    type="string",
                    required=True,
                    help_text="The Kempower Location to import from.",
                ),
                CredentialField(
                    key="baseUrl",
                    label="API base URL (optional)",
                    type="string",
                    required=False,
                    help_text="Override for sandbox / on-prem deployments.",
                ),
                CredentialField(
                    key="backfillSince",
                    label="Backfill sessions since (YYYY-MM-DD)",
                    type="string",
                    required=False,
                    help_text="Leave blank to skip historical session import.",
                ),
            ],
            supports_scheduled_sync=True,
            default_sync_interval_minutes=1440,
        )

    def _build_client(self, credentials: dict[str, Any], config: dict[str, Any]) -> KempowerClient:
        return KempowerClient(
            username=credentials.get("username"),
            password=credentials.get("password"),
            base_url=config.get("baseUrl") or os.getenv("KEMPOWER_API_BASE_URL"),
        )

    async def validate_credentials(
        self, credentials: dict[str, Any], config: dict[str, Any]
    ) -> None:
        """Cheap authenticated probe: fetch the configured location."""
        location_id = config.get("locationId")
        if not location_id:
            raise CredentialValidationError("locationId is required")
        if not credentials.get("username") or not credentials.get("password"):
            raise CredentialValidationError("username and password are required")
        try:
            async with self._build_client(credentials, config) as client:
                await client.get_location(location_id)
        except KempowerClientError as exc:
            raise CredentialValidationError(
                f"Kempower rejected the credentials or location: {exc}"
            ) from exc

    async def run_ingestion(self, ctx: IngestionContext) -> IngestionResult:
        """Drive the shared onboarding stages, reporting progress per stage."""
        location_id = ctx.config.get("locationId")
        if not location_id:
            return IngestionResult(
                status="failed", error_detail="locationId missing from connection config"
            )
        backfill_since = _parse_backfill_since(ctx.config.get("backfillSince"))
        counts = OnboardingCounts()
        error_detail: Optional[str] = None

        try:
            async with self._build_client(ctx.credentials, ctx.config) as client:
                station_map = await import_chargers(
                    ctx.static_pool,
                    client,
                    depot_id=ctx.depot_id,
                    kempower_location_id=location_id,
                    dry_run=False,
                    counts=counts,
                )
                await ctx.progress.update(stage="chargers", **_snapshot(counts))

                vehicle_map = await import_vehicles(
                    ctx.static_pool,
                    client,
                    depot_id=ctx.depot_id,
                    organization_id=ctx.organization_id,
                    kempower_location_id=location_id,
                    dry_run=False,
                    counts=counts,
                )
                await ctx.progress.update(stage="vehicles", **_snapshot(counts))

                await upsert_access_matrix(
                    ctx.static_pool,
                    depot_id=ctx.depot_id,
                    station_map=station_map,
                    vehicle_map=vehicle_map,
                    dry_run=False,
                    counts=counts,
                )
                await ctx.progress.update(stage="access", **_snapshot(counts))

                if backfill_since is not None:
                    await backfill_sessions(
                        ctx.ts_pool,
                        client,
                        depot_id=ctx.depot_id,
                        station_map=station_map,
                        vehicle_map=vehicle_map,
                        backfill_since=backfill_since,
                        batch_id=ctx.batch_id,
                        dry_run=False,
                        counts=counts,
                    )
                    await ctx.progress.update(stage="sessions", **_snapshot(counts))
        except KempowerClientError as exc:
            # Hard API failure mid-run: keep what landed, surface as partial/failed.
            error_detail = f"Kempower API error: {exc}"
            logger.warning("Kempower ingestion aborted: %s", exc)

        skipped = counts.chargers_skipped + counts.vehicles_skipped + counts.sessions_skipped
        landed = (
            counts.chargers_created
            + counts.vehicles_created
            + counts.sessions_inserted
            + counts.access_rows_upserted
        )
        status: IngestionStatus
        if error_detail is not None:
            status = "partial" if landed > 0 else "failed"
        elif skipped:
            status = "partial"
        else:
            status = "succeeded"

        return IngestionResult(
            status=status,
            chargers_created=counts.chargers_created,
            vehicles_created=counts.vehicles_created,
            sessions_inserted=counts.sessions_inserted,
            access_rows_upserted=counts.access_rows_upserted,
            skipped=skipped,
            error_detail=error_detail,
            credentials_emitted=[
                {"ocpp_id": ocpp_id, "username": username, "password": password}
                for ocpp_id, username, password in counts.credentials_emitted
            ],
        )


def _snapshot(counts: OnboardingCounts) -> dict[str, int]:
    """Cumulative counter snapshot for a progress update (overwrites by key)."""
    return {
        "chargers_created": counts.chargers_created,
        "vehicles_created": counts.vehicles_created,
        "sessions_inserted": counts.sessions_inserted,
        "access_rows_upserted": counts.access_rows_upserted,
        "skipped_count": (
            len(counts.chargers_skipped)
            + len(counts.vehicles_skipped)
            + len(counts.sessions_skipped)
        ),
    }
