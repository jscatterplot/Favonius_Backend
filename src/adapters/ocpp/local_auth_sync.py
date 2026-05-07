"""Push the approved idTag list to OCPP 1.6 chargers so they can authorize
RFID tags while offline.

Strategy (v1):

* On every BootNotification, push the full approved list via SendLocalList.
* The list is the strict subset of idTags the central Authorize path would
  accept at this charger today (see ``list_authorized_id_tags`` in
  ``src/db/queries.py``) — there is never a tag that works offline but not
  online.
* On the first successful push for a charger (when ``charging_stations.
  local_list_version`` was 0), also flip the OCPP config keys that make the
  charger consult the list and the cache: ``LocalAuthListEnabled``,
  ``LocalPreAuthorize``, ``AuthorizationCacheEnabled``. These are
  best-effort; chargers that don't expose the keys return ``NotSupported``
  and we log + carry on.
* Vendor-specific caps (notably ABB's 16-entry limit) are enforced inside
  ``FleetChargePoint.send_local_list`` — when the cap is hit the charger
  falls back to central Authorize and we record the rejection in
  ``charging_stations.local_list_last_status`` for ops visibility.

Delta updates and DB-trigger-driven mid-session resyncs are deliberately
out of scope for v1; reconnect events are the trigger and a Full update
keeps reconciliation simple.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)


# Configuration keys to enable on the first successful sync per charger.
# OCPP 1.6 §9.1 makes these standard config keys; `NotSupported` from a
# specific charger model just means we silently fall through to whatever
# default behavior that vendor ships with.
_BOOTSTRAP_CONFIG_KEYS: tuple[tuple[str, str], ...] = (
    ("LocalAuthListEnabled", "true"),
    ("LocalPreAuthorize", "true"),
    ("AuthorizationCacheEnabled", "true"),
)


@dataclass(frozen=True)
class SyncResult:
    """Outcome of a single ``sync_charger`` call.

    ``status`` is the OCPP 1.6 SendLocalList response (``Accepted`` |
    ``Failed`` | ``NotSupported`` | ``VersionMismatch``) for real attempts,
    or one of: ``skipped`` (env gate / unknown station / db_error) and
    ``no_change`` (reserved for future delta-mode short-circuits).
    """

    status: str
    version: int
    entries: int
    reason: Optional[str] = None


class _ChargePointProto(Protocol):
    """Minimal duck-typed interface required from FleetChargePoint.

    Declared here so the sync module is decoupled from the concrete
    ``src.adapters.ocpp.charge_point.FleetChargePoint`` class — this
    matters for unit tests, which inject a fake.
    """

    vendor: Optional[str]

    async def send_local_list(
        self,
        list_version: int,
        update_type: str = ...,
        local_authorization_list: Optional[list[dict]] = ...,
    ) -> str: ...

    async def change_configuration(self, key: str, value: str) -> str: ...


def _format_entries(id_tag_rows: list[dict]) -> list[dict]:
    """Turn DB rows into OCPP 1.6 AuthorizationData wire entries.

    All entries are pushed with ``status='Accepted'``. Expiry dates and
    ``parentIdTag`` grouping are not modelled in v1 — revocations are
    handled by the next sync omitting the tag.
    """
    return [
        {
            "id_tag": row["id_tag"],
            "id_tag_info": {"status": "Accepted"},
        }
        for row in id_tag_rows
    ]


async def _bootstrap_local_auth_config(cp: _ChargePointProto, station_id: str) -> None:
    """Best-effort: enable LocalAuthList + cache + pre-authorize on first sync.

    Errors are swallowed: the per-call status is logged but never raised, so
    a single charger that doesn't honor a config key cannot prevent the
    SendLocalList push that follows.
    """
    for key, value in _BOOTSTRAP_CONFIG_KEYS:
        try:
            status = await cp.change_configuration(key, value)
        except Exception as exc:  # defence in depth; change_configuration shouldn't raise
            logger.warning(
                "local_auth_bootstrap_config station=%s key=%s error=%s",
                station_id,
                key,
                exc,
            )
            continue
        if status not in {"Accepted", "RebootRequired"}:
            logger.info(
                "local_auth_bootstrap_config station=%s key=%s value=%s status=%s",
                station_id,
                key,
                value,
                status,
            )


async def sync_charger(
    cp: _ChargePointProto,
    db: Any,
    station_id: str,
) -> SyncResult:
    """Push the current approved idTag list to a charger via SendLocalList.

    Always sends a Full update; the new list version is one greater than
    whatever the backend last recorded in ``charging_stations.local_list_version``.
    On Accepted we bump the version and stamp ``local_list_synced_at``; on
    any other response we still record ``local_list_last_status`` for ops.

    The push itself is idempotent — replaying the same list with a new
    version number is safe per OCPP 1.6 §5.16.

    The ``OCPP_DISABLE_LOCAL_AUTH_LIST`` env switch short-circuits the whole
    function so an operator can fall back to pure central Authorize without
    a code change. Vendor caps are enforced inside ``send_local_list`` —
    we just record the resulting status.
    """
    # Local import to avoid a circular dependency at module load time:
    # src.db.queries imports nothing from this package today, but routing
    # the call lazily keeps that boundary explicit.
    from src.db.queries import list_authorized_id_tags

    if os.getenv("OCPP_DISABLE_LOCAL_AUTH_LIST", "false").lower() == "true":
        logger.info(
            "local_auth_sync station=%s skipped: OCPP_DISABLE_LOCAL_AUTH_LIST=true",
            station_id,
        )
        return SyncResult(status="skipped", version=0, entries=0, reason="env_disabled")

    try:
        station_row = await db.fetchrow(
            """
            SELECT id, local_list_version
            FROM charging_stations
            WHERE station_id = $1
            """,
            station_id,
        )
    except Exception as exc:
        logger.error(
            "local_auth_sync station=%s db_error fetching station row: %s",
            station_id,
            exc,
        )
        return SyncResult(status="skipped", version=0, entries=0, reason="db_error")

    if station_row is None:
        logger.warning(
            "local_auth_sync station=%s skipped: station not found in charging_stations",
            station_id,
        )
        return SyncResult(status="skipped", version=0, entries=0, reason="unknown_station")

    current_version = int(station_row["local_list_version"] or 0)
    is_first_sync = current_version == 0
    new_version = current_version + 1

    try:
        rows = await list_authorized_id_tags(db, station_id)
    except Exception as exc:
        logger.error(
            "local_auth_sync station=%s db_error building tag list: %s",
            station_id,
            exc,
        )
        return SyncResult(status="skipped", version=current_version, entries=0, reason="db_error")

    entries = _format_entries(rows)

    if is_first_sync:
        await _bootstrap_local_auth_config(cp, station_id)

    try:
        status = await cp.send_local_list(
            list_version=new_version,
            update_type="Full",
            local_authorization_list=entries,
        )
    except Exception as exc:
        # FleetChargePoint.send_local_list catches its own exceptions, but
        # a duck-typed fake might not — keep the orchestration safe.
        logger.error(
            "local_auth_sync station=%s send_local_list raised: %s",
            station_id,
            exc,
        )
        status = "Failed"

    try:
        if status == "Accepted":
            await db.execute(
                """
                UPDATE charging_stations
                SET local_list_version = $1,
                    local_list_synced_at = NOW(),
                    local_list_last_status = $2
                WHERE id = $3
                """,
                new_version,
                status,
                station_row["id"],
            )
        else:
            await db.execute(
                """
                UPDATE charging_stations
                SET local_list_synced_at = NOW(),
                    local_list_last_status = $1
                WHERE id = $2
                """,
                status,
                station_row["id"],
            )
    except Exception as exc:
        logger.error(
            "local_auth_sync station=%s db_error updating sync state (status=%s): %s",
            station_id,
            status,
            exc,
        )

    logger.info(
        "local_auth_sync station=%s status=%s entries=%d version=%d first_sync=%s",
        station_id,
        status,
        len(entries),
        new_version if status == "Accepted" else current_version,
        is_first_sync,
    )

    return SyncResult(
        status=status,
        version=new_version if status == "Accepted" else current_version,
        entries=len(entries),
    )
