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
* Before each ``SendLocalList`` we probe the charger with
  ``GetConfiguration(SupportedFeatureProfiles, LocalAuthListMaxLength)``
  and cache the answer (``local_list_supported``, scoped to the current
  ``local_list_probed_firmware``). Chargers that report missing
  ``LocalAuthListManagement`` get short-circuited on every subsequent
  reconnect until firmware changes — this is the fix for ABB Terra AC
  V1.8.x firmware where the config keys are accepted but ``SendLocalList``
  itself returns ``NotSupported`` (HRX Vilnius pilot).

Delta updates and DB-trigger-driven mid-session resyncs are deliberately
out of scope for v1; reconnect events are the trigger and a Full update
keeps reconciliation simple.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)


# Per-call timeout for ``ChangeConfiguration`` in the bootstrap. Mirrors the
# ``_push_metering_config`` timeout in ``ocpp16_adapter._push_metering_config``.
# The OCPP library's default call timeout is 30 s, so without this wrapper a
# single dead key wedges the sync for 30 s × 4 keys = 2 min. The fail-fast
# logic relies on this — a charger that drops the WebSocket mid-RPC raises
# inside ``cp.call`` long before 30 s anyway, but ABB Terra AC V1.8.x
# sometimes ACKs an HTTP-level keepalive without ever returning the
# ChangeConfiguration result, and only this wall-clock timeout breaks that.
_BOOTSTRAP_CHANGECONFIG_TIMEOUT_S = 10.0


class BootstrapOutcome(str, Enum):
    """Outcome of the post-boot ``ChangeConfiguration`` bootstrap.

    Used by ``sync_charger`` to decide whether to attempt ``SendLocalList``
    or to short-circuit and cache the firmware as unsupported.

    * ``SUCCESS`` — the critical key (``LocalAuthListEnabled``) was Accepted
      or returned ``RebootRequired``. Other keys may have failed; those are
      logged as warnings but do not block the push.
    * ``UNSUPPORTED`` — the critical key returned ``Rejected`` or
      ``NotSupported``, raised, or timed out. Strong evidence the charger
      does not implement LocalAuthListManagement on its current firmware.
      Caller MUST skip SendLocalList and record the negative outcome so
      the next reconnect short-circuits via the probe cache.
    * ``UNKNOWN`` — defensive default, currently unused. Kept as an
      explicit state so future contributors can extend the branch logic
      (e.g. a "retry once with backoff" path) without changing the enum
      contract.
    """

    SUCCESS = "success"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


def _normalize_firmware(firmware: Optional[str]) -> Optional[str]:
    """Strip wrapping whitespace from a firmware string before cache compare.

    Some ABB Terra AC firmwares occasionally pad the ``firmware_version``
    field with trailing whitespace; without this normalisation the cache
    miss/hit cycle churns: ``"V1.8.36"`` written this boot ≠ ``"V1.8.36 "``
    read next boot → re-probe → re-write. ``None`` is returned unchanged so
    upstream callers can short-circuit on a missing firmware string.
    """
    if firmware is None:
        return None
    return firmware.strip()

# Local-cap enforced inside ``FleetChargePoint.send_local_list``. Mirrored
# here so we can tell apart a ``NotSupported`` that came back from the
# charger (definitive evidence about firmware capability) from one our
# own code returned because the entry count exceeded the ABB cap
# (transient — depends on the tag list, not the firmware).
_LOCAL_LIST_MAX_ENTRIES = 16


# Configuration keys to push on the first successful sync per charger.
# The first three are OCPP 1.6 §9.1 standard keys that turn on the local
# authorization list + cache + pre-authorize path. `FreevendEnabled` is
# an ABB Terra AC vendor-specific key (FW ≥ 1.6.6) that defaults to TRUE
# and silently bypasses Authorize by sending StartTransaction with the
# charger's serial number as the idTag — see denial pattern in HRX
# Vilnius logs where `id_tag=TACW1141622G1438` matched the boot serial.
# `NotSupported` from a non-ABB charger just means we silently fall
# through to whatever default behavior that vendor ships with.
_BOOTSTRAP_CONFIG_KEYS: tuple[tuple[str, str], ...] = (
    ("LocalAuthListEnabled", "true"),
    ("LocalPreAuthorize", "true"),
    ("AuthorizationCacheEnabled", "true"),
    ("FreevendEnabled", "false"),
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
    firmware_version: Optional[str]

    async def send_local_list(
        self,
        list_version: int,
        update_type: str = ...,
        local_authorization_list: Optional[list[dict]] = ...,
    ) -> str: ...
    async def send_local_list_with_error(
        self,
        list_version: int,
        update_type: str = ...,
        local_authorization_list: Optional[list[dict]] = ...,
    ) -> tuple[str, bool]: ...

    async def change_configuration(self, key: str, value: str) -> str: ...
    async def change_configuration_with_error(self, key: str, value: str) -> tuple[str, bool]: ...

    async def get_configuration(self, keys: Optional[list[str]] = ...) -> dict: ...


async def _probe_local_auth_support(cp: _ChargePointProto, station_id: str) -> Optional[bool]:
    """Ask the charger whether it supports LocalAuthorizationList management.

    Issues a single ``GetConfiguration`` for the two well-known OCPP 1.6
    keys: ``SupportedFeatureProfiles`` (the authoritative answer) and
    ``LocalAuthListMaxLength`` (a corroborating signal — present iff the
    feature is implemented).

    Returns:
        ``True``  — charger advertises ``LocalAuthListManagement`` in its
                    ``SupportedFeatureProfiles``.
        ``False`` — charger returned ``SupportedFeatureProfiles`` and the
                    profile is *not* listed (e.g. ABB Terra AC V1.8.x).
        ``None``  — neither key was returned (chargers that don't expose
                    GetConfiguration for these keys, or that put them in
                    ``unknown_key``). The caller should fall back to
                    attempting ``SendLocalList`` directly.
    """
    try:
        response = await cp.get_configuration(
            keys=["SupportedFeatureProfiles", "LocalAuthListMaxLength"]
        )
    except Exception as exc:
        logger.warning(
            "local_auth_probe station=%s get_configuration_error=%s — " "treating as ambiguous",
            station_id,
            exc,
        )
        return None

    configuration_key = response.get("configuration_key") or []
    profiles_entry: Optional[dict] = None
    max_length_entry: Optional[dict] = None
    for entry in configuration_key:
        key_name = (entry.get("key") or "").lower()
        if key_name == "supportedfeatureprofiles":
            profiles_entry = entry
        elif key_name == "localauthlistmaxlength":
            max_length_entry = entry

    if profiles_entry is None and max_length_entry is None:
        # Charger does not advertise either key — ambiguous, fall through.
        logger.info(
            "local_auth_probe station=%s ambiguous: neither "
            "SupportedFeatureProfiles nor LocalAuthListMaxLength returned",
            station_id,
        )
        return None

    if profiles_entry is not None:
        raw_value = profiles_entry.get("value") or ""
        # Per OCPP 1.6 §9.1.1.7 the value is a comma-separated list of
        # profile names. Whitespace tolerance + case-insensitive match
        # because some vendors emit "LocalAuthListManagement " or
        # "localauthlistmanagement".
        profiles = {p.strip().lower() for p in raw_value.split(",") if p.strip()}
        supported = "localauthlistmanagement" in profiles
        logger.info(
            "local_auth_probe station=%s SupportedFeatureProfiles=%r supported=%s",
            station_id,
            raw_value,
            supported,
        )
        return supported

    # No SupportedFeatureProfiles but we did get LocalAuthListMaxLength.
    # A value of "0" or missing is interpreted as unsupported; any positive
    # integer is supported.
    raw_max = (max_length_entry.get("value") if max_length_entry else "") or ""
    try:
        max_length = int(raw_max)
    except (TypeError, ValueError):
        logger.info(
            "local_auth_probe station=%s LocalAuthListMaxLength=%r unparseable — "
            "treating as ambiguous",
            station_id,
            raw_max,
        )
        return None
    logger.info(
        "local_auth_probe station=%s LocalAuthListMaxLength=%d",
        station_id,
        max_length,
    )
    return max_length > 0


async def _record_probe_outcome(
    db: Any,
    station_pk: Any,
    supported: bool,
    firmware: Optional[str],
    last_status: Optional[str] = None,
) -> None:
    """Persist the probe result so subsequent syncs can short-circuit.

    ``last_status`` is optional — when the negative came from a real
    ``SendLocalList`` reply (vs the probe alone) we stamp the wire status
    so ops can see "the charger said NotSupported on firmware X".
    """
    try:
        if last_status is not None:
            await db.execute(
                """
                UPDATE charging_stations
                SET local_list_supported = $1,
                    local_list_probed_firmware = $2,
                    local_list_probed_at = NOW(),
                    local_list_last_status = $3
                WHERE id = $4
                """,
                supported,
                firmware,
                last_status,
                station_pk,
            )
        else:
            await db.execute(
                """
                UPDATE charging_stations
                SET local_list_supported = $1,
                    local_list_probed_firmware = $2,
                    local_list_probed_at = NOW()
                WHERE id = $3
                """,
                supported,
                firmware,
                station_pk,
            )
    except Exception as exc:
        logger.error(
            "local_auth_probe db_error recording outcome (supported=%s, fw=%s): %s",
            supported,
            firmware,
            exc,
        )


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


async def _bootstrap_local_auth_config(
    cp: _ChargePointProto, station_id: str
) -> BootstrapOutcome:
    """Fail-fast: enable LocalAuthList + cache + pre-authorize on first sync.

    The first key in ``_BOOTSTRAP_CONFIG_KEYS`` (``LocalAuthListEnabled``) is
    the most discriminating signal: if it returns ``Rejected``,
    ``NotSupported``, raises, or times out, the charger almost certainly
    does NOT implement LocalAuthListManagement on its current firmware
    (ABB Terra AC V1.8.x is the documented case). In that case we return
    ``BootstrapOutcome.UNSUPPORTED`` immediately and the caller skips the
    remaining three ``ChangeConfiguration`` pushes AND the ``SendLocalList``
    that would follow — limiting blast radius to a single OCPP call rather
    than the historical 4 calls + SendLocalList that wedged the WS.

    The remaining three keys (``LocalPreAuthorize``,
    ``AuthorizationCacheEnabled``, ``FreevendEnabled``) are auxiliary; if
    any of them fails after ``LocalAuthListEnabled`` succeeded, the failure
    is logged as a warning but the bootstrap is still considered
    ``SUCCESS`` for the purpose of attempting ``SendLocalList``.

    Each ``change_configuration`` call is wrapped in ``asyncio.wait_for``
    with ``_BOOTSTRAP_CHANGECONFIG_TIMEOUT_S``. Without that wrapper the
    OCPP library's 30 s default timeout dwarfs the WS reconnect cadence
    we observed at HRX Vilnius (~10-60 s) and the bootstrap never gets
    to fail before the connection is gone.
    """
    async def _change_configuration_with_error(key: str, value: str) -> tuple[str, bool]:
        method = getattr(cp, "change_configuration_with_error", None)
        if method is not None and (
            "change_configuration_with_error" in vars(cp)
            or hasattr(type(cp), "change_configuration_with_error")
        ):
            return await method(key, value)
        status = await cp.change_configuration(key, value)
        return status, False

    if not _BOOTSTRAP_CONFIG_KEYS:
        return BootstrapOutcome.SUCCESS  # pragma: no cover — defensive

    critical_key, critical_value = _BOOTSTRAP_CONFIG_KEYS[0]
    auxiliary = _BOOTSTRAP_CONFIG_KEYS[1:]

    # --- Critical key: fail-fast ---
    critical_status: Optional[str] = None
    critical_transport_error = False
    try:
        critical_status, critical_transport_error = await asyncio.wait_for(
            _change_configuration_with_error(critical_key, critical_value),
            timeout=_BOOTSTRAP_CHANGECONFIG_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "local_auth_bootstrap_config station=%s key=%s timed_out_after=%.1fs — "
            "treating as firmware-permanent unsupported",
            station_id,
            critical_key,
            _BOOTSTRAP_CHANGECONFIG_TIMEOUT_S,
        )
        return BootstrapOutcome.UNSUPPORTED
    except asyncio.CancelledError:
        # WebSocket dropped mid-bootstrap; propagate so the sync task
        # terminates cleanly. The next reconnect retries the probe.
        raise
    except Exception as exc:  # defence in depth; change_configuration catches its own
        logger.warning(
            "local_auth_bootstrap_config station=%s key=%s error=%s — "
            "treating as firmware-permanent unsupported",
            station_id,
            critical_key,
            exc,
        )
        return BootstrapOutcome.UNSUPPORTED

    if critical_status not in {"Accepted", "RebootRequired"}:
        if critical_transport_error:
            logger.warning(
                "local_auth_bootstrap_config station=%s key=%s value=%s status=%s — "
                "transport fallback; treating as transient unknown",
                station_id,
                critical_key,
                critical_value,
                critical_status,
            )
            return BootstrapOutcome.UNKNOWN
        logger.info(
            "local_auth_bootstrap_config station=%s key=%s value=%s status=%s — "
            "treating as firmware-permanent unsupported",
            station_id,
            critical_key,
            critical_value,
            critical_status,
        )
        return BootstrapOutcome.UNSUPPORTED

    # --- Auxiliary keys: best-effort, do not gate SendLocalList ---
    for key, value in auxiliary:
        try:
            status, _ = await asyncio.wait_for(
                _change_configuration_with_error(key, value),
                timeout=_BOOTSTRAP_CHANGECONFIG_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "local_auth_bootstrap_config station=%s key=%s timed_out_after=%.1fs",
                station_id,
                key,
                _BOOTSTRAP_CHANGECONFIG_TIMEOUT_S,
            )
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:
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

    return BootstrapOutcome.SUCCESS


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

    station_row: Any = None
    legacy_schema = False
    try:
        station_row = await db.fetchrow(
            """
            SELECT id,
                   local_list_version,
                   local_list_supported,
                   local_list_probed_firmware
            FROM charging_stations
            WHERE station_id = $1
            """,
            station_id,
        )
    except Exception as exc:
        # Migration 012 (``migrations/supabase/012_local_list_support_probe.sql``)
        # adds the probe-cache columns. On a Supabase DB where ops hasn't
        # applied it yet, the SELECT errors with sqlstate 42703 and we MUST
        # fall back to the legacy push path — without that, ABB Terra AC
        # 1.8.x sees no post-boot OCPP traffic from the server (no
        # ChangeConfiguration, no SendLocalList) and reconnect-loops every
        # ~60 s waiting for the boot-completion exchange.
        if getattr(exc, "sqlstate", None) == "42703":
            logger.warning(
                "local_auth_sync station=%s schema missing probe columns "
                "(apply migrations/supabase/012_local_list_support_probe.sql) — "
                "falling back to legacy push without firmware-scoped probe cache",
                station_id,
            )
            legacy_schema = True
            try:
                station_row = await db.fetchrow(
                    """
                    SELECT id, local_list_version
                    FROM charging_stations
                    WHERE station_id = $1
                    """,
                    station_id,
                )
            except Exception as legacy_exc:
                logger.error(
                    "local_auth_sync station=%s db_error fetching station row "
                    "(legacy shape): %s",
                    station_id,
                    legacy_exc,
                )
                return SyncResult(
                    status="skipped", version=0, entries=0, reason="db_error"
                )
        else:
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

    # Probe-cache short-circuit: if a previous sync (or its probe) confirmed
    # this charger cannot accept SendLocalList on its current firmware, skip
    # the whole exchange. The cache is per firmware string so a firmware
    # upgrade automatically re-probes.
    # Normalise here so cache hits don't churn on whitespace-only differences
    # in the BootNotification firmware string (some ABB Terra AC firmwares
    # occasionally emit trailing whitespace).
    current_fw = _normalize_firmware(getattr(cp, "firmware_version", None))
    if legacy_schema:
        # Probe columns don't exist on this DB — bypass cache + probe and go
        # straight to the legacy bootstrap + SendLocalList path. This matches
        # the pre-PR-#167 behaviour: ABB returns NotSupported on
        # SendLocalList, we record it via ``local_list_last_status`` (which
        # exists via migration 011) and the charger gets the post-boot
        # exchange it expects.
        supported_cached = None
        probed_fw = None
    else:
        # ``.get()`` (vs ``[...]``) keeps the test fakes — which pass minimal
        # ``{"id": ..., "local_list_version": ...}`` dicts — working without
        # forcing every fixture to enumerate the new probe-state columns.
        # asyncpg ``Record`` also supports ``.get()``.
        supported_cached = station_row.get("local_list_supported")
        probed_fw = _normalize_firmware(station_row.get("local_list_probed_firmware"))
    if supported_cached is False and probed_fw == current_fw:
        logger.info(
            "local_auth_sync station=%s skipped: known_unsupported firmware=%s",
            station_id,
            current_fw,
        )
        return SyncResult(
            status="skipped",
            version=current_version,
            entries=0,
            reason="unsupported_cached",
        )

    # If we have no probe outcome yet, or the firmware has changed since we
    # probed, ask the charger directly before attempting the push. A negative
    # answer is recorded and short-circuits all future reconnects on this
    # firmware. A positive or ambiguous answer falls through to the push.
    # Skipped under legacy schema: the probe outcome has nowhere to be cached
    # so an extra GetConfiguration round-trip on every reconnect is wasted.
    #
    # ``probe_positive`` tracks whether we have STRONG evidence that this
    # charger supports LocalAuthListManagement on its current firmware.
    # It is consulted below when deciding whether a ``SendLocalList: Failed``
    # response is firmware-permanent (cache it) or transient (don't cache).
    # Conservative default: a True cache entry OR a positive probe result.
    probe_positive = supported_cached is True
    needs_probe = not legacy_schema and (
        supported_cached is None or probed_fw != current_fw
    )
    if needs_probe:
        probe_result = await _probe_local_auth_support(cp, station_id)
        if probe_result is False:
            if current_fw is not None:
                await _record_probe_outcome(
                    db,
                    station_row["id"],
                    supported=False,
                    firmware=current_fw,
                    last_status="UnsupportedFeatureProfile",
                )
            logger.info(
                "local_auth_sync station=%s status=UnsupportedFeatureProfile "
                "entries=0 version=%d first_sync=%s reason=probe_negative",
                station_id,
                current_version,
                is_first_sync,
            )
            return SyncResult(
                status="UnsupportedFeatureProfile",
                version=current_version,
                entries=0,
                reason="probe_negative",
            )
        if probe_result is True and supported_cached is not True:
            await _record_probe_outcome(
                db,
                station_row["id"],
                supported=True,
                firmware=current_fw,
            )
            probe_positive = True
        # probe_result is None → ambiguous; fall through to attempt the push.
        # probe_positive stays False — we do NOT have strong evidence of
        # support, so a subsequent ``SendLocalList: Failed`` should be
        # treated as firmware-permanent (see L4 below).

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
        bootstrap_outcome = await _bootstrap_local_auth_config(cp, station_id)
        # L3: if the critical ChangeConfiguration key failed (Rejected,
        # NotSupported, timeout, or exception), the charger does not honor
        # LocalAuthListManagement on its current firmware. Skip SendLocalList
        # entirely and persist the negative so subsequent reconnects
        # short-circuit via the probe cache. Without this branch, every
        # reconnect on a stuck-in-first-sync charger fires the full
        # ChangeConfiguration → SendLocalList sequence and the WebSocket
        # repeatedly dies mid-RPC (HRX Vilnius ABB Terra AC V1.8.x).
        if bootstrap_outcome is BootstrapOutcome.UNSUPPORTED:
            if (
                not legacy_schema
                and not probe_positive
                and current_fw is not None
            ):
                await _record_probe_outcome(
                    db,
                    station_row["id"],
                    supported=False,
                    firmware=current_fw,
                    last_status="UnsupportedFromBootstrap",
                )
            else:
                # Legacy schema: stamp last_status on the columns that DO exist
                # so ops can still see "we gave up at the bootstrap step".
                try:
                    await db.execute(
                        """
                        UPDATE charging_stations
                        SET local_list_last_status = $1
                        WHERE id = $2
                        """,
                        "UnsupportedFromBootstrap",
                        station_row["id"],
                    )
                except Exception as exc:
                    logger.error(
                        "local_auth_sync station=%s db_error stamping bootstrap "
                        "unsupported (legacy schema): %s",
                        station_id,
                        exc,
                    )
            logger.info(
                "local_auth_sync station=%s status=UnsupportedFromBootstrap "
                "entries=%d version=%d first_sync=%s reason=bootstrap_unsupported",
                station_id,
                len(entries),
                current_version,
                is_first_sync,
            )
            return SyncResult(
                status="UnsupportedFromBootstrap",
                version=current_version,
                entries=len(entries),
                reason="bootstrap_unsupported",
            )

    send_local_list_raised = False
    send_local_list_transport_error = False
    method = getattr(cp, "send_local_list_with_error", None)
    if callable(method):
        status, send_local_list_transport_error = await method(
            list_version=new_version,
            update_type="Full",
            local_authorization_list=entries,
        )
    else:
        try:
            status = await cp.send_local_list(
                list_version=new_version,
                update_type="Full",
                local_authorization_list=entries,
            )
        except Exception as exc:
            logger.error(
                "local_auth_sync station=%s send_local_list raised: %s",
                station_id,
                exc,
            )
            send_local_list_raised = True
            status = "Failed"

    # Fallback caching: record a firmware-scoped negative when the charger
    # itself reports the feature as broken so the next reconnect
    # short-circuits via the probe cache. Two distinct gating rules:
    #
    # * ``NotSupported`` — definitive per OCPP 1.6 §5.16. Cache whenever the
    #   refusal came from the charger (entries ≤ cap; the over-cap case is
    #   our own local refusal in ``FleetChargePoint.send_local_list`` and
    #   has nothing to do with firmware capability).
    #
    # * ``Failed`` — ambiguous in spec, but firmware-permanent in practice
    #   on ABB Terra AC V1.8.x where the ChangeConfiguration bootstrap was
    #   accepted but ``SendLocalList`` returned ``Failed`` rather than the
    #   spec-mandated ``NotSupported``. Cache ONLY for charger-returned
    #   ``Failed`` statuses (not transport/timeout exceptions) when we
    #   don't already have positive evidence of support
    #   (``probe_positive`` is False —
    #   probe returned False/None or no probe outcome ever recorded
    #   positive). This protects chargers that genuinely support the
    #   feature but returned ``Failed`` once due to a transient internal
    #   error: they keep their NULL cache and retry on the next reconnect.
    #
    # Skipped under legacy schema: the cache columns don't exist so we
    # fall through to the plain ``local_list_last_status`` UPDATE.
    cache_negative = False
    if not legacy_schema and current_fw is not None and len(entries) <= _LOCAL_LIST_MAX_ENTRIES:
        if status == "NotSupported":
            cache_negative = True
        elif (
            status == "Failed"
            and not probe_positive
            and not send_local_list_raised
            and not send_local_list_transport_error
        ):
            cache_negative = True
    if cache_negative:
        await _record_probe_outcome(
            db,
            station_row["id"],
            supported=False,
            firmware=current_fw,
            last_status=status,
        )
    else:
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
                    SET local_list_last_status = $1
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
