"""OCPP 1.6 session adapter for the legacy WebSocket handler.

Routes chargers that negotiate the ``ocpp1.6`` subprotocol through the
correct ocpp.v16 library instead of the 2.0.1 handler.  This eliminates
the schema-validation log storm caused by passing 1.6 messages through
``EnhancedOCPPChargePoint`` (which uses ocpp.v201).

Each ``OCPP16Session`` wraps ``FleetChargePoint`` (adapters/ocpp/charge_point.py)
and wires its callbacks to:
  - ``timescale_client.insert_telemetry_batch``  — telemetry persistence
  - ``message_handler._push_to_main_api``        — real-time event push
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from datetime import datetime, timezone
from itertools import count
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

from ocpp.v16.enums import AuthorizationStatus

from .meter_value_utils import normalize_energy_to_wh

from src.adapters.ocpp.charge_point import FleetChargePoint
from src.adapters.ocpp.local_auth_sync import sync_charger as sync_local_auth_list

from .monitoring import ACTIVE_TRANSACTIONS, PROFILE_PUSH_LATENCY
from .rfid_authorization import RFIDAuthStatus

# Replay window after a charger reconnects: pending commands enqueued while
# the charger was offline are flushed within this many seconds of boot.
REPLAY_BACKOFF_SECONDS = 1.0

# How long to wait for a charger to send its own BootNotification before
# nudging it via TriggerMessage. Some ABB Terra AC firmwares (and other
# OCPP 1.6 implementations) skip BootNotification on WebSocket reconnect,
# leaving the heartbeat interval un-negotiated and the session stuck.
BOOT_TRIGGER_GRACE_SECONDS = 5

# Minimum seconds between tenant-context resolution attempts on the frame
# path. BootNotification resolves it eagerly; this cooldown bounds the
# lazy retry so an un-onboarded station (or a transient Supabase outage)
# can't trigger a lookup on every OCPP frame.
_TENANT_CONTEXT_RETRY_COOLDOWN_S = 60.0

if TYPE_CHECKING:
    from .connection_manager import ConnectionManager
    from .liveness_notifier import LivenessNotifier
    from .message_handler import MessageHandler
    from .supabase_client import SupabaseClient
    from .timescale_client import TimescaleClient

logger = logging.getLogger(__name__)
_PROFILE_ID_FALLBACK_START = 9_000_000_000_000_000_000
_PROFILE_ID_FALLBACK_SPAN = 100_000_000


def _new_profile_id_fallback_counter() -> count:
    """Create a non-constant fallback chargingProfileId stream.

    The DB sequence is the durable source of truth. This fallback only runs
    during sequence outages, so seed it from process-start entropy instead of
    a fixed constant to avoid deterministic reuse after a crash/restart.
    """
    seed_offset = (time.time_ns() + secrets.randbelow(_PROFILE_ID_FALLBACK_SPAN)) % (
        _PROFILE_ID_FALLBACK_SPAN
    )
    return count(seed_offset)


_profile_id_fallback_counter = _new_profile_id_fallback_counter()


def _resolve_meter_stop(
    meter_stop: Optional[int],
    transaction_data: Optional[List[Dict[str, Any]]],
) -> Optional[int]:
    """Return the best available meterStop value in Wh for StopTransaction.

    OCPP 1.6 declares ``meterStop`` required on ``StopTransaction``, but
    chargers in the wild send ``None`` or ``0`` whenever the meter register
    is unavailable (charger crash before final read, EVDisconnected with
    no graceful stop, meter fault). When ``StopTxnSampledData`` is
    configured (Issue 3A pushes this in BootNotification),
    ``transactionData`` carries the final
    ``Energy.Active.Import.Register`` sample — which is what we actually
    want for billing.

    Resolution order:

      1. ``meter_stop`` (Wh int from the top-level field) when it is
         non-None and non-zero — the spec-compliant happy path.
      2. The maximum ``Energy.Active.Import.Register`` sample from
         ``transaction_data`` — the fallback the recovery research
         identified as the highest-confidence value when (1) fails.
      3. The original ``meter_stop`` value (which may be ``None`` or ``0``)
         when ``transaction_data`` has nothing usable; the close path
         will detect the anomaly and leave ``energy_delivered_kwh = NULL``.

    Args:
        meter_stop: Top-level ``meterStop`` from the OCPP message (Wh).
        transaction_data: Optional ``transactionData`` array from
            ``StopTransaction.req``. Each entry has ``sampledValue`` /
            ``sampled_value`` (both spellings observed in the wild) carrying
            measurand readings.

    Returns:
        Best available ``meter_stop_wh`` value, or ``None`` if no usable
        source exists.
    """
    if meter_stop is not None and meter_stop > 0:
        return int(meter_stop)

    if not transaction_data:
        return meter_stop  # propagate None / 0 unchanged

    best_wh: Optional[float] = None
    for entry in transaction_data:
        samples = entry.get("sampledValue") or entry.get("sampled_value") or []
        for sample in samples:
            if sample.get("measurand") != "Energy.Active.Import.Register":
                continue
            value = sample.get("value")
            if value is None:
                continue
            try:
                wh = normalize_energy_to_wh(
                    float(value),
                    sample.get("unit"),
                    sample.get("multiplier"),
                )
            except (TypeError, ValueError):
                continue
            if wh < 0:
                continue
            if best_wh is None or wh > best_wh:
                best_wh = wh

    if best_wh is not None:
        return int(best_wh)
    return meter_stop


_OCPP16_AUTH_FROM_STATUS: dict[RFIDAuthStatus, AuthorizationStatus] = {
    RFIDAuthStatus.ACCEPTED: AuthorizationStatus.accepted,
    RFIDAuthStatus.EXPIRED: AuthorizationStatus.expired,
    RFIDAuthStatus.BLOCKED: AuthorizationStatus.blocked,
    RFIDAuthStatus.CONCURRENT_TX: AuthorizationStatus.concurrent_tx,
    RFIDAuthStatus.INVALID: AuthorizationStatus.invalid,
}


class OCPP16Session:
    """Manages a single OCPP 1.6 charger connection.

    Provides the same external interface as ``EnhancedOCPPChargePoint``
    (``start``, ``send_charging_profile``, ``send_der_control``,
    ``clear_der_control``) so ``OCPPWebSocketServer`` can store both types
    in the same ``charge_points`` dict without special-casing.
    """

    def __init__(
        self,
        station_id: str,
        websocket: Any,
        timescale_client: "TimescaleClient",
        message_handler: "MessageHandler",
        connection_manager: Optional["ConnectionManager"] = None,
        supabase_client: Optional["SupabaseClient"] = None,
        liveness_notifier: Optional["LivenessNotifier"] = None,
    ) -> None:
        """Initialise the session and wire all FleetChargePoint callbacks.

        ``connection_manager`` is optional only so that targeted unit tests
        can construct a session without spinning up the full handler stack.
        Production wiring (``OCPPWebSocketServer``) always passes one — it
        is required for the stale-connection sweeper to see incoming OCPP
        traffic on this socket.

        ``supabase_client`` resolves tenant context (organization_id / depot_id)
        on BootNotification — and lazily on the frame path when a charger
        reconnects without one (see ``_ensure_tenant_context``) — so
        ``insert_connector_status`` can label rows for the alerts trigger
        (post-migration 029) and the liveness pg_notify carries an org scope.
        Optional for the same reason as above; when None the connector_status
        writes carry NULL context and the trigger bails silently.
        """
        self._station_id = station_id
        self._timescale = timescale_client
        self._message_handler = message_handler
        self._connection_manager = connection_manager
        self._authz = message_handler.rfid_authorization
        self._supabase_client = supabase_client
        self._liveness_notifier = liveness_notifier
        # Resolved on BootNotification (and lazily on the frame path for
        # chargers that reconnect without one) and reused for the lifetime of
        # the WS connection — values don't change while the charger is online.
        self._tenant_context: Optional[Dict[str, Optional[str]]] = None
        # Single-flight + cooldown guards for lazy resolution off the frame
        # path. ``_last_attempt`` is monotonic seconds; 0.0 means "never tried".
        self._tenant_context_lock = asyncio.Lock()
        self._tenant_context_last_attempt: float = 0.0
        # Single-slot stash for the most recent accepted StartTransaction so
        # ``_next_transaction_id`` can persist the open ``charging_sessions``
        # row alongside the generated tx_id. Safe because FleetChargePoint
        # serialises message handling per charger socket.
        self._pending_start: Optional[Dict[str, Any]] = None
        # tx_ids accepted by the in-memory gate but not yet durable in DB
        # because insert_open_session failed. These must continue to block
        # sibling StartTransaction retries on the same connector.
        self._in_memory_only_tx_ids: Set[int] = set()
        # Per-session inbound frame counter. Set by ``_on_message_received``
        # for every OCPP frame the charger sends. ``_force_boot_notification``
        # reads this after the grace period to decide whether the charger is
        # silent (trigger needed) or already chatting (skip — re-bootstrapping
        # a clearly-alive charger on every reconnect is what caused the HRX
        # Vilnius reconfig loop).
        self._inbound_frame_count: int = 0
        self._replay_task: Optional[asyncio.Task[None]] = None
        self._local_auth_sync_task: Optional[asyncio.Task[None]] = None
        self._metering_config_task: Optional[asyncio.Task[None]] = None
        self._boot_trigger_task: Optional[asyncio.Task[None]] = None
        self._background_tasks: Set[asyncio.Task[Any]] = set()
        self._telemetry_queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue(maxsize=1024)
        self._telemetry_flush_task: Optional[asyncio.Task[None]] = None
        self._stop_telemetry_flush = asyncio.Event()

        self._cp = FleetChargePoint(
            id=station_id,
            connection=websocket,
            on_boot=self._on_boot,
            on_meter_values=self._on_meter_values,
            on_status_change=self._on_status_change,
            on_transaction_start=self._on_transaction_start,
            on_transaction_stop=self._on_transaction_stop,
            on_authorize=self._on_authorize,
            on_security_event=self._on_security_event,
            on_message_received=self._on_message_received,
            tx_id_provider=self._next_transaction_id,
        )

    async def _on_message_received(self, message_size: int = 0) -> None:
        """Refresh the connection-manager liveness clock on every OCPP frame
        and broadcast a rate-limited liveness signal to API replicas.

        OCPP 1.6 chargers vary widely in Heartbeat cadence (the spec only
        requires "at least every Heartbeat interval", which BootNotification
        sets to 300s). Without the connection-manager update the WS handler's
        stale sweeper kills the socket after ~90s even though MeterValues
        and StatusNotification are flowing.

        The ``LivenessNotifier`` fan-out goes to API replicas via pg_notify
        (``charger_liveness`` channel) so the frontend's SSE subscribers
        get a "last interaction" event per ~10s of frames per charger.
        Fire-and-forget — a slow notify must not backpressure OCPP
        message processing.

        ``message_size`` is the raw frame length in bytes; it feeds the
        ``connection_stats.messages_received`` / ``bytes_received`` counters
        so the ``Unregistered connection ... Messages: X/Y`` log line shows
        real traffic instead of always ``0/0`` (previously the OCPP 1.6
        path never called ``record_message_received``).
        """
        # Used by ``_force_boot_notification`` to skip the synthetic
        # TriggerMessage(BootNotification) when the charger is clearly alive
        # (sending StatusNotification, Heartbeat, etc.) but has not yet sent
        # its own BootNotification. A bounded counter is enough — we only
        # check "> 0" — and overflow is impossible in practice.
        self._inbound_frame_count += 1
        if self._connection_manager is not None:
            try:
                await self._connection_manager.update_heartbeat(self._station_id)
            except Exception:
                logger.exception("update_heartbeat failed for station=%s", self._station_id)
            try:
                await self._connection_manager.record_message_received(
                    self._station_id, message_size
                )
            except Exception:
                logger.exception("record_message_received failed for station=%s", self._station_id)

        if self._liveness_notifier is not None:
            task = asyncio.create_task(
                self._publish_liveness(),
                name=f"liveness_notify:{self._station_id}",
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start processing messages from the charger (blocks until disconnect)."""
        self._stop_telemetry_flush.clear()
        # Reset per-session inbound frame counter so the boot-trigger gate
        # makes its decision against frames received in *this* connection,
        # not anything stale from a previous adapter instance (which won't
        # happen with the current lifecycle but defends against future
        # reuse).
        self._inbound_frame_count = 0
        self._telemetry_flush_task = asyncio.create_task(self._flush_telemetry_queue())
        self._boot_trigger_task = asyncio.create_task(self._force_boot_notification())
        try:
            await self._cp.start()
        finally:
            if self._boot_trigger_task is not None and not self._boot_trigger_task.done():
                self._boot_trigger_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._boot_trigger_task
            if self._telemetry_flush_task is not None:
                self._stop_telemetry_flush.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._telemetry_flush_task
            if self._background_tasks:
                pending = tuple(self._background_tasks)
                for task in pending:
                    task.cancel()
                with contextlib.suppress(Exception):
                    await asyncio.gather(*pending, return_exceptions=True)
                self._background_tasks.clear()

    async def _force_boot_notification(self) -> None:
        """Nudge silent chargers that skip BootNotification on reconnect.

        OCPP 1.6 §4.2 requires the charger to send BootNotification on connect,
        and the central system's response carries the negotiated heartbeat
        interval. Some ABB Terra AC firmwares (1.8.x) skip BootNotification on
        WebSocket reconnects after the initial cold boot, leaving the session
        with no heartbeat cadence. TriggerMessage(BootNotification) is the
        spec-sanctioned way to wake them up (OCPP 1.6 §4.18).

        Three conditions are checked after the grace period elapses:

          1. ``last_boot_at`` is set — the charger volunteered a
             BootNotification within the grace, no trigger needed.
          2. Still no boot after grace (even if other frames arrived) —
             Trigger BootNotification so ``_on_boot`` remains reachable
             for this session (queued command replay, local auth sync,
             metering bootstrap).
        """
        try:
            await asyncio.sleep(BOOT_TRIGGER_GRACE_SECONDS)
            if self._cp.last_boot_at is not None:
                return
            if self._inbound_frame_count > 0:
                logger.info(
                    "force_boot_notification station=%s proceeding after grace: "
                    "charger sent %d frame(s) without BootNotification",
                    self._station_id,
                    self._inbound_frame_count,
                )
            status = await self._cp.trigger_message("BootNotification")
            logger.info(
                "force_boot_notification station=%s status=%s",
                self._station_id,
                status,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "force_boot_notification failed for station=%s",
                self._station_id,
            )

    # ------------------------------------------------------------------
    # Outgoing commands (matches EnhancedOCPPChargePoint's interface)
    # ------------------------------------------------------------------

    async def send_charging_profile(
        self,
        evse_id: int,
        charging_profile: Dict,
        *,
        allow_enqueue: bool = True,
    ) -> bool:
        """Send SetChargingProfile to the charger using OCPP 1.6 semantics.

        ``evse_id`` is treated as OCPP 1.6 ``connector_id`` (equivalent concept).
        The ``charging_profile`` dict is expected to carry the standard OCPP
        fields; the schedule periods are forwarded verbatim.

        If the caller does not pass ``chargingProfileId``, we draw a unique
        id from the ``ocpp_charging_profile_id`` sequence so concurrent
        pushes do not stack-collide on the charger.

        When the underlying WebSocket is closed (the charger went offline
        between the optimizer dispatch and this call), we persist the profile
        to ``charging_command_queue`` and return ``False`` so the caller can
        record the deferred state. ``allow_enqueue=False`` disables this
        fallback — used by the replay path itself to avoid loops.
        """
        schedule_periods: List[Dict] = []
        cp_schedule = charging_profile.get("chargingSchedule", {})
        if cp_schedule:
            schedule_periods = cp_schedule.get("chargingSchedulePeriod", [])
        else:
            # Flat format: list of period dicts at top level
            schedule_periods = charging_profile.get("chargingSchedulePeriod", [])

        profile_id = charging_profile.get("chargingProfileId")
        if profile_id is None:
            try:
                profile_id = await self._timescale.next_charging_profile_id()
            except Exception as exc:
                profile_id = _PROFILE_ID_FALLBACK_START + (
                    next(_profile_id_fallback_counter) % _PROFILE_ID_FALLBACK_SPAN
                )
                logger.warning(
                    "next_charging_profile_id failed for station=%s; using local fallback id=%s: %s",
                    self._station_id,
                    profile_id,
                    exc,
                )

        charging_rate_unit = (cp_schedule or {}).get(
            "chargingRateUnit", charging_profile.get("chargingRateUnit", "A")
        )

        push_start = time.monotonic()
        try:
            accepted = await self._cp.set_charging_profile(
                connector_id=evse_id,
                charging_schedule=schedule_periods,
                profile_purpose=charging_profile.get("chargingProfilePurpose", "TxDefaultProfile"),
                profile_kind=charging_profile.get("chargingProfileKind", "Absolute"),
                charging_rate_unit=charging_rate_unit,
                stack_level=charging_profile.get("stackLevel", 0),
                profile_id=profile_id,
            )
        except Exception as exc:
            try:
                PROFILE_PUSH_LATENCY.labels(station_id=self._station_id, outcome="raised").observe(
                    max(time.monotonic() - push_start, 1e-6)
                )
            except Exception:
                pass
            if not allow_enqueue:
                raise
            accepted = False
            if self._is_connection_open():
                logger.warning(
                    "set_charging_profile raised for station=%s connector=%s while online: %s",
                    self._station_id,
                    evse_id,
                    exc,
                )
                return False
            logger.warning(
                "set_charging_profile push failed for station=%s connector=%s: %s — enqueuing",
                self._station_id,
                evse_id,
                exc,
            )
        else:
            try:
                PROFILE_PUSH_LATENCY.labels(
                    station_id=self._station_id,
                    outcome="sent" if accepted else "rejected",
                ).observe(max(time.monotonic() - push_start, 1e-6))
            except Exception:
                pass
        if accepted:
            return True
        if not allow_enqueue:
            return False
        if self._is_connection_open():
            logger.info(
                "SetChargingProfile rejected by online charger station=%s connector=%s; not enqueuing",
                self._station_id,
                evse_id,
            )
            return False
        try:
            await self._timescale.enqueue_charging_command(
                charge_point_id=self._station_id,
                connector_id=evse_id,
                payload=charging_profile,
            )
        except Exception as enq_exc:
            logger.error(
                "Failed to enqueue charging profile for station=%s: %s",
                self._station_id,
                enq_exc,
            )
        return False

    async def send_reset(self, reset_type: str = "Soft") -> bool:
        """Send OCPP 1.6 Reset to the charger.

        Returns True if the charger responded ``Accepted``. Reset is irreversible
        on the device side, so the queue consumer marks the row terminal on the
        first attempt regardless of outcome — we do NOT enqueue on failure here
        and the boot-replay path explicitly skips ``remote_reset`` rows.
        """
        try:
            status = await self._cp.reset(reset_type=reset_type)
        except Exception as exc:
            logger.warning(
                "Reset raised for station=%s type=%s: %s",
                self._station_id,
                reset_type,
                exc,
            )
            return False
        return status == "Accepted"

    async def replay_queued_commands(self) -> int:
        """Flush ``charging_command_queue`` rows for this station.

        Called from ``_on_boot`` after BootNotification has been answered.
        Each pending row is pushed via ``send_charging_profile`` with
        ``allow_enqueue=False`` so a transient failure during replay does not
        re-enqueue an already-queued row. Returns the number of rows that
        were marked ``acked``.

        Only ``set_charging_profile`` rows are replayed. Other command types
        (e.g. ``remote_reset``) are intentionally skipped — replaying a reset
        on every reconnect would loop a stuck charger.
        """
        try:
            rows = await self._timescale.fetch_pending_commands(self._station_id)
        except Exception as exc:
            logger.error(
                "fetch_pending_commands failed for station=%s: %s",
                self._station_id,
                exc,
            )
            return 0

        sent = 0
        for row in rows:
            if row.get("command_type", "set_charging_profile") != "set_charging_profile":
                continue
            queue_id = row["queue_id"]
            payload = row["payload"]
            if isinstance(payload, str):
                import json as _json

                payload = _json.loads(payload)
            connector_id = row["connector_id"]
            try:
                ok = await self.send_charging_profile(connector_id, payload, allow_enqueue=False)
            except Exception as exc:
                try:
                    await self._timescale.mark_command_failed(queue_id, str(exc))
                except Exception as mark_exc:
                    logger.error(
                        "mark_command_failed raised for station=%s queue_id=%s: %s",
                        self._station_id,
                        queue_id,
                        mark_exc,
                    )
                continue
            if ok:
                try:
                    await self._timescale.mark_command_acked(queue_id)
                except Exception as exc:
                    logger.error(
                        "mark_command_acked raised for station=%s queue_id=%s: %s",
                        self._station_id,
                        queue_id,
                        exc,
                    )
                    continue
                sent += 1
            else:
                try:
                    await self._timescale.mark_command_failed(
                        queue_id, "charger rejected SetChargingProfile"
                    )
                except Exception as exc:
                    logger.error(
                        "mark_command_failed raised for station=%s queue_id=%s: %s",
                        self._station_id,
                        queue_id,
                        exc,
                    )
        if sent:
            logger.info("Replayed %d queued command(s) to station=%s", sent, self._station_id)
        return sent

    # ------------------------------------------------------------------
    # Database-backed providers wired into FleetChargePoint
    # ------------------------------------------------------------------

    async def _next_transaction_id(self) -> int:
        """Provide a restart-safe transactionId from the DB sequence.

        Also persists an open ``charging_sessions`` row using context stashed
        by the most recent ``_on_transaction_start`` so the boot-reload path
        can find the session if the handler restarts mid-transaction.
        """
        pending = self._pending_start
        self._pending_start = None
        try:
            tx_id = await self._timescale.next_transaction_id()
        except Exception:
            if pending is not None:
                fallback_tx_id = self._cp.transactions.get(pending["connector_id"])
                if fallback_tx_id is not None:
                    try:
                        self._in_memory_only_tx_ids.add(int(fallback_tx_id))
                    except (TypeError, ValueError):
                        pass
            raise
        if pending is not None:
            try:
                await self._timescale.insert_open_session(
                    station_id=self._station_id,
                    transaction_id=tx_id,
                    evse_id=pending["evse_id"],
                    connector_id=pending["connector_id"],
                    id_token=pending.get("id_tag"),
                    start_time=pending["start_time"],
                    vehicle_id=pending.get("vehicle_id"),
                    driver_id=pending.get("driver_id"),
                    card_id=pending.get("card_id"),
                    meter_start_wh=pending.get("meter_start_wh"),
                )
                self._in_memory_only_tx_ids.discard(int(tx_id))
            except Exception as exc:
                self._in_memory_only_tx_ids.add(int(tx_id))
                logger.warning(
                    "insert_open_session failed for station=%s tx_id=%s: %s",
                    self._station_id,
                    tx_id,
                    exc,
                )
        return tx_id

    async def _validate_id_tag(self, cp_id: str, id_tag: str, source: str) -> AuthorizationStatus:
        """Validate an OCPP idTag against ``vehicles.id_tag``.

        Returns ``Accepted`` for tags registered in the fleet, ``Invalid``
        for unknown tags. We deliberately do not use ``Blocked`` /
        ``Expired`` here — those require richer tag metadata which is out
        of scope for the pilot.
        """
        decision = await self._authz.authorize(cp_id, id_tag, source)
        return self._map_auth_status(decision.status)

    @staticmethod
    def _map_auth_status(status: RFIDAuthStatus) -> AuthorizationStatus:
        """Map RFID authorization status to OCPP 1.6 AuthorizationStatus."""
        return _OCPP16_AUTH_FROM_STATUS[status]

    async def _on_authorize(self, cp_id: str, id_tag: str) -> AuthorizationStatus:
        """Handle Authorize by failing closed on unknown fleet idTags."""
        try:
            return await self._validate_id_tag(cp_id, id_tag, "Authorize")
        except Exception as exc:
            logger.error(
                "Authorize validation raised unexpectedly for station=%s id_tag=%s: %s",
                cp_id,
                id_tag,
                exc,
            )
            return AuthorizationStatus.invalid

    async def send_remote_start_transaction(self, connector_id: int, id_tag: str) -> bool:
        """Send OCPP 1.6 RemoteStartTransaction with a synthetic id_tag.

        Used by the operator-override flow (manual_authorize endpoint): the
        caller has already minted an entry in
        ``operator_authorization_overrides`` so when the charger sends
        Authorize/StartTransaction with this tag,
        ``RFIDAuthorizationService.authorize`` will atomically consume the
        override and return Accepted. Returns ``True`` if the charger
        accepted the RemoteStart request.
        """
        try:
            return await self._cp.remote_start_transaction(
                connector_id=connector_id,
                id_tag=id_tag,
            )
        except Exception as exc:
            logger.warning(
                "send_remote_start_transaction failed for station=%s connector=%s: %s",
                self._station_id,
                connector_id,
                exc,
            )
            return False

    async def send_der_control(self, der_control: Dict) -> bool:  # noqa: ARG002
        """DER control is an OCPP 2.x feature; no-op for OCPP 1.6 chargers."""
        logger.debug("send_der_control called on OCPP 1.6 session — skipping")
        return False

    async def clear_der_control(self) -> bool:
        """DER control is an OCPP 2.x feature; no-op for OCPP 1.6 chargers."""
        logger.debug("clear_der_control called on OCPP 1.6 session — skipping")
        return False

    async def get_diagnostics(
        self,
        location: str,
        *,
        retries: Optional[int] = None,
        retry_interval: Optional[int] = None,
        start_time: Any = None,
        stop_time: Any = None,
    ) -> Optional[str]:
        """Send OCPP 1.6 ``GetDiagnostics`` to the connected charger.

        The queue consumer dispatches charger-log fetches by calling
        ``cp.get_diagnostics(location=…)`` on whatever the in-memory
        session exposes. Without this wrapper, the ``getattr`` lookup
        on an ``OCPP16Session`` returned ``None`` and the row was
        terminally marked failed — GetDiagnostics never reached the
        charger. Delegates to ``FleetChargePoint.get_diagnostics`` and
        returns the upload filename the charger reported (``None`` on
        any failure — the underlying call swallows exceptions).

        ``start_time`` / ``stop_time`` are accepted as datetime or
        ISO-8601 strings; they're forwarded as ISO-8601 strings because
        the underlying ``call.GetDiagnostics`` expects that shape.
        """
        if not self._is_connection_open():
            logger.info(
                "get_diagnostics requested for station=%s while offline — skipping push",
                self._station_id,
            )
            return None

        def _coerce(ts: Any) -> Optional[str]:
            if ts is None or ts == "":
                return None
            if isinstance(ts, str):
                return ts
            try:
                return ts.isoformat()
            except AttributeError:
                return str(ts)

        try:
            return await self._cp.get_diagnostics(
                location=location,
                start_time=_coerce(start_time),
                stop_time=_coerce(stop_time),
                retries=retries,
                retry_interval=retry_interval,
            )
        except Exception as exc:
            logger.warning(
                "send_get_diagnostics failed for station=%s: %s",
                self._station_id,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # FleetChargePoint callbacks
    # ------------------------------------------------------------------

    async def _publish_liveness(self) -> None:
        """Resolve tenant context if needed, then publish a liveness signal.

        Runs as a fire-and-forget task per OCPP frame, off the
        message-processing path. BootNotification is the primary trigger for
        tenant-context resolution, but some ABB Terra firmwares skip
        BootNotification on quick reconnects; without a lazy resolve here the
        whole connection would publish ``org_id=None`` and
        ``LivenessNotifier.maybe_notify`` would no-op, so the charger shows
        ``offline`` on the dashboard even while frames flow. The notifier
        rate-limits to one publish per ~10 s per station, so this only does
        real work on the first frame(s) of such a connection.
        """
        # Skip if resolution is already in-flight (another task holds the
        # lock) — that task will publish once it resolves, so accumulating
        # waiting tasks behind a slow/failing lookup is unnecessary churn.
        if self._tenant_context is None and self._tenant_context_lock.locked():
            return
        await self._ensure_tenant_context()
        org_id = (self._tenant_context or {}).get("organization_id")
        await self._liveness_notifier.maybe_notify(self._station_id, org_id)

    async def _ensure_tenant_context(self, *, eager: bool = False) -> None:
        """Lazily resolve tenant context off the BootNotification path.

        Single-flight (an ``asyncio.Lock`` so concurrent per-frame tasks issue
        at most one Supabase lookup) and cooldown-guarded
        (``_TENANT_CONTEXT_RETRY_COOLDOWN_S``) so a station that isn't
        onboarded — or a transient Supabase outage — doesn't trigger a lookup
        on every frame. Once resolved, the fast path returns immediately for
        the rest of the connection.

        When ``eager`` is True (BootNotification), the cooldown is bypassed so
        boot always retries after transient frame-path failures.
        """
        if self._tenant_context is not None:
            return
        # Cheap pre-check outside the lock to avoid serialising every frame's
        # task on the lock once we're inside a cooldown window.
        if not eager:
            last = self._tenant_context_last_attempt
            if last and (time.monotonic() - last) < _TENANT_CONTEXT_RETRY_COOLDOWN_S:
                return
        async with self._tenant_context_lock:
            # Re-check under the lock: another frame's task may have resolved
            # the context or refreshed the attempt clock while we waited.
            if self._tenant_context is not None:
                return
            if not eager:
                last = self._tenant_context_last_attempt
                if last and (time.monotonic() - last) < _TENANT_CONTEXT_RETRY_COOLDOWN_S:
                    return
            await self._resolve_tenant_context()

    async def _resolve_tenant_context(self) -> None:
        """Look up (organization_id, depot_id) for this WS connection.

        Fired eagerly from ``_on_boot`` and lazily from
        ``_ensure_tenant_context`` on the frame path. The result is cached on
        the session so every subsequent ``_on_status_change`` /
        ``_publish_liveness`` reuses it without an extra DB roundtrip. If the
        lookup fails (or no supabase_client is wired) we leave the cache as
        ``None`` and the insert proceeds with NULL context — the alerts
        trigger added by migration 029 bails silently in that case, matching
        the legacy behavior. The attempt timestamp is always recorded so the
        frame-path cooldown applies regardless of caller.
        """
        # Record the attempt up-front so the lazy retry cooldown holds even
        # when this was invoked from _on_boot and resolves to None.
        self._tenant_context_last_attempt = time.monotonic()
        if self._supabase_client is None:
            return
        try:
            self._tenant_context = await self._supabase_client.lookup_tenant_context(
                self._station_id
            )
        except Exception as exc:
            logger.warning(
                "tenant_context_lookup_failed station=%s error=%s",
                self._station_id,
                exc,
            )
            # Don't wipe a valid cache on transient failure — the existing
            # value (None or a prior successful lazy resolve) is preserved.
        if self._tenant_context is None:
            logger.info(
                "tenant_context_not_found station=%s "
                "(connector_status writes will be NULL-labelled; alerts trigger will bail)",
                self._station_id,
            )

    async def _on_boot(
        self,
        cp_id: str,
        vendor: str,
        model: str,
        serial_number: Optional[str],
        firmware_version: Optional[str],
        **kwargs: Any,
    ) -> None:
        logger.info(
            "OCPP 1.6 boot: station=%s vendor=%s model=%s serial=%s fw=%s",
            cp_id,
            vendor,
            model,
            serial_number,
            firmware_version,
        )
        # Resolve tenant context for the lifetime of this WS connection.
        # Routes through _ensure_tenant_context (single-flight lock) so a
        # frame-path lazy resolve that already cached a valid context is
        # reused rather than overwritten by a concurrent boot-time lookup.
        # eager=True bypasses the frame-path cooldown so every boot retries.
        await self._ensure_tenant_context(eager=True)
        # Persist vendor metadata so vendor-keyed dispatch (parser
        # selection for GetDiagnostics, ABB-safe measurand guard at
        # endpoint boundaries) can read it from the DB without holding
        # an in-memory FleetChargePoint reference.
        await self._persist_station_vendor(vendor, model, firmware_version)
        # Cross-restart safety:
        #   1. Reload still-open transactions into FleetChargePoint.transactions
        #      so an incoming StopTransaction from the rebooted charger is
        #      recognised instead of being treated as an unknown txn.
        #   2. Schedule the queued-command replay to run shortly after we
        #      return the BootNotification response. Doing it synchronously
        #      here would delay the boot ack and could trip the charger's
        #      response timeout.
        try:
            await self._timescale.clear_sessions_seen(cp_id)
        except Exception as exc:
            logger.error("clear_sessions_seen failed for station=%s: %s", cp_id, exc)

        try:
            open_rows = await self._timescale.fetch_open_sessions(cp_id)
        except Exception as exc:
            open_rows = []
            logger.error("fetch_open_sessions failed for station=%s: %s", cp_id, exc)

        def _session_start(row: Dict[str, Any]) -> datetime:
            start_time = row.get("start_time")
            if isinstance(start_time, datetime):
                return start_time
            return datetime.min.replace(tzinfo=timezone.utc)

        for row in sorted(open_rows, key=_session_start):
            connector_id = row["connector_id"]
            tx_id = row["transaction_id"]
            if connector_id is None or tx_id is None:
                continue
            self._cp.transactions[connector_id] = int(tx_id)
            self._cp.current_transaction_id = int(tx_id)
        if open_rows:
            logger.info(
                "Reloaded %d open session(s) for station=%s on boot",
                len(open_rows),
                cp_id,
            )

        if self._replay_task is not None and not self._replay_task.done():
            self._replay_task.cancel()
        self._replay_task = asyncio.create_task(self._delayed_replay())

        # Push the approved idTag list to the charger so it can authorize
        # RFID tags while offline. Runs after the queued-command replay so
        # SetChargingProfile and SendLocalList don't race on the same socket
        # for vendors that mishandle interleaved request/response cycles.
        if self._local_auth_sync_task is not None and not self._local_auth_sync_task.done():
            self._local_auth_sync_task.cancel()
        self._local_auth_sync_task = asyncio.create_task(self._delayed_local_auth_sync())

        # Push metering configuration so every connected charger emits the
        # measurands we depend on for energy tracking (Issue 3A). This
        # neutralises factory-default measurand sets — notably ABB Terra AC
        # firmware versions that ship with Power-only sampling and never
        # produce Energy.Active.Import.Register without an explicit
        # ChangeConfiguration. Fire-and-forget so the boot ack is not
        # blocked by a slow or unresponsive charger.
        if self._metering_config_task is not None and not self._metering_config_task.done():
            self._metering_config_task.cancel()
        self._metering_config_task = asyncio.create_task(self._push_metering_config())

    async def _on_security_event(
        self,
        cp_id: str,
        event_type: str,
        timestamp: str,
        tech_info: Optional[str],
    ) -> None:
        """Persist OCPP 1.6 SecurityEventNotification to ``security_events``.

        Charger-side timestamps arrive as ISO 8601 with a ``Z`` suffix; we
        convert to a timezone-aware datetime before handing to asyncpg.
        Exceptions propagate to the wrapper in
        ``FleetChargePoint.on_security_event_notification`` which logs them
        — the OCPP ack still goes back to the charger.
        """
        try:
            event_ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            event_ts = datetime.now(timezone.utc)
            logger.warning(
                "SecurityEventNotification timestamp unparseable for station=%s: %r",
                cp_id,
                timestamp,
            )
        await self._timescale.store_security_event(
            {
                "station_id": cp_id,
                "event_type": event_type,
                "timestamp": event_ts,
                "tech_info": tech_info,
                "additional_info": {"source": "ocpp1.6.SecurityEventNotification"},
            }
        )

    async def _delayed_replay(self) -> None:
        """Run the queued-command replay shortly after BootNotification.

        Sleeps ``REPLAY_BACKOFF_SECONDS`` so the BootNotification reply is
        flushed and the charger has accepted us before we start sending
        SetChargingProfile messages on the same socket.
        """
        try:
            await asyncio.sleep(REPLAY_BACKOFF_SECONDS)
            await self.replay_queued_commands()
        except asyncio.CancelledError:
            logger.debug("Queued-command replay cancelled for station=%s", self._station_id)
            raise
        except Exception as exc:
            logger.error(
                "Queued-command replay failed for station=%s: %s",
                self._station_id,
                exc,
            )
        finally:
            current = asyncio.current_task()
            if current is not None and self._replay_task is current:
                self._replay_task = None

    async def _delayed_local_auth_sync(self) -> None:
        """Push the approved idTag list to the charger after BootNotification.

        Awaits the queued-command replay (which itself waits for
        ``REPLAY_BACKOFF_SECONDS``) so SendLocalList doesn't interleave with
        SetChargingProfile pushes on chargers that mishandle concurrent
        request/response cycles. If replay has already finished or was
        cancelled, the sync runs straight away.

        Errors are caught and logged — a charger that rejects SendLocalList
        falls back to central Authorize while online and simply has no
        offline auth coverage. That degradation is acceptable; raising here
        would tear the WebSocket handler.
        """
        try:
            replay_task = self._replay_task
            if replay_task is not None:
                try:
                    await replay_task
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                except Exception:
                    pass
            pool = self._resolve_static_pool()
            if pool is None:
                logger.debug(
                    "local_auth_sync station=%s skipped: no static or timescale pool available",
                    self._station_id,
                )
                return
            await sync_local_auth_list(self._cp, pool, self._station_id)
        except asyncio.CancelledError:
            logger.debug(
                "local_auth_sync cancelled for station=%s",
                self._station_id,
            )
            raise
        except Exception as exc:
            logger.error(
                "local_auth_sync failed for station=%s: %s",
                self._station_id,
                exc,
            )
        finally:
            current = asyncio.current_task()
            if current is not None and self._local_auth_sync_task is current:
                self._local_auth_sync_task = None

    # OCPP configuration keys pushed after every BootNotification (Issue 3A).
    # Values are conservative defaults that cover Energy.Active.Import.Register
    # (billing source of truth) plus a few measurands we use for live UI
    # without exceeding the ABB-safe set in adapters/ocpp/charge_point.py.
    _METERING_CONFIG_KEYS: List[tuple[str, str]] = [
        (
            "MeterValuesSampledData",
            "Energy.Active.Import.Register,Power.Active.Import,Current.Import",
        ),
        (
            "StopTxnSampledData",
            "Energy.Active.Import.Register",
        ),
        (
            "MeterValueSampleInterval",
            "60",
        ),
    ]

    # OCPP statuses that mean "the key is at the desired value as far as the
    # charger is concerned". ``Accepted`` is the obvious one. ``RebootRequired``
    # means the charger acknowledged the new value but will only apply it on
    # next restart — for idempotency that still counts as "applied" because
    # re-pushing on the very next reconnect will just produce the same
    # response. Everything else (``Rejected``, ``NotSupported``, timeout)
    # is treated as a non-success and disables the cache stamp for this
    # firmware so we re-attempt next time.
    _METERING_CONFIG_SUCCESS_STATUSES: frozenset[str] = frozenset({"Accepted", "RebootRequired"})

    async def _metering_config_already_applied(self) -> bool:
        """Return True iff the metering config was successfully pushed to
        this station on its current firmware version.

        Reads ``charging_stations.metering_config_applied_firmware`` (added
        by migration 014) and compares against the firmware string from the
        current BootNotification. A match means every key in
        ``_METERING_CONFIG_KEYS`` was confirmed by the charger on this
        firmware — re-pushing on reconnect is wasted work and exactly the
        round-trip storm that caused the HRX Vilnius reconfig loop.

        Returns ``False`` in three cases:
          1. The current firmware is unknown (no BootNotification yet, or
             the charger omitted ``firmware_version``). We can't scope a
             cache lookup without it.
          2. The probe column is missing — migration 014 not applied yet.
             Falling back to the always-push behaviour preserves the
             pre-migration semantics while keeping the new code safe on
             older DB schemas.
          3. The cached firmware doesn't match the live firmware — the
             charger updated and we have to re-verify the keys still take.

        DB errors are caught and the function returns ``False`` so the push
        runs (failure-open for a non-critical optimisation).
        """
        current_fw = getattr(self._cp, "firmware_version", None)
        if not current_fw:
            return False
        pool = self._resolve_static_pool()
        if pool is None:
            return False
        try:
            row = await pool.fetchrow(
                """
                SELECT metering_config_applied_firmware
                FROM charging_stations
                WHERE station_id = $1
                """,
                self._station_id,
            )
        except Exception as exc:
            # ``42703`` (undefined_column) — migration 014 not applied.
            # Anything else (DB blip, pool exhausted) — fall back to push.
            if getattr(exc, "sqlstate", None) == "42703":
                logger.warning(
                    "metering_config_cache station=%s schema missing applied_firmware "
                    "column (apply migrations/supabase/014_metering_config_cache.sql) — "
                    "will re-push metering config every reconnect",
                    self._station_id,
                )
            else:
                logger.warning(
                    "metering_config_cache station=%s lookup failed (%s); "
                    "will push metering config",
                    self._station_id,
                    exc,
                )
            return False
        if row is None:
            return False
        applied_fw = row.get("metering_config_applied_firmware")
        return applied_fw == current_fw

    async def _record_metering_config_applied(self) -> None:
        """Stamp the metering-config cache columns after a clean push.

        Errors (including missing schema) are logged and swallowed: the
        cache is an optimisation, not a correctness requirement. The next
        reconnect will simply re-push.
        """
        current_fw = getattr(self._cp, "firmware_version", None)
        if not current_fw:
            return
        pool = self._resolve_static_pool()
        if pool is None:
            return
        try:
            await pool.execute(
                """
                UPDATE charging_stations
                SET metering_config_applied_firmware = $1,
                    metering_config_applied_at = NOW()
                WHERE station_id = $2
                """,
                current_fw,
                self._station_id,
            )
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "42703":
                # Migration 014 not applied — silent fail. The legacy log
                # in ``_metering_config_already_applied`` already warned on
                # the read path; no need to spam again on the write.
                return
            logger.warning(
                "metering_config_cache station=%s update failed (%s); "
                "next reconnect will re-push",
                self._station_id,
                exc,
            )

    async def _persist_station_vendor(
        self,
        vendor: Optional[str],
        model: Optional[str],  # noqa: ARG002  reserved for a future model column
        firmware_version: Optional[str],  # noqa: ARG002  tracked via metering_config_applied_firmware
    ) -> None:
        """Best-effort UPDATE of ``charging_stations.vendor``.

        Drives the per-vendor parser dispatch in
        :func:`src.adapters.chargers.get_parser_for_vendor`. The column
        was added by Supabase mig 006 but never populated until now —
        existing rows pick up the value on first reconnect after this
        ships. Only writes when the stored vendor differs to avoid
        churning ``updated_at`` on every reconnect.

        Firmware tracking already lives in
        ``charging_stations.metering_config_applied_firmware`` (Supabase
        mig 014), so this method intentionally does not touch it — one
        column, one writer.
        """
        if not vendor:
            return
        pool = self._resolve_static_pool()
        if pool is None:
            return
        try:
            await pool.execute(
                """
                UPDATE charging_stations
                   SET vendor = $1
                 WHERE station_id = $2
                   AND vendor IS DISTINCT FROM $1
                """,
                vendor,
                self._station_id,
            )
        except Exception as exc:
            sqlstate = getattr(exc, "sqlstate", None)
            if sqlstate == "42703":
                # Column missing on this DB — Supabase mig 006 not applied.
                return
            logger.warning(
                "Could not persist vendor for station=%s: %s",
                self._station_id,
                exc,
            )

    def _resolve_static_pool(self) -> Any:
        """Return the static asyncpg pool used for charging_stations writes,
        falling back to the timescale pool if the static one is not wired.

        Mirrors the discovery logic in ``_delayed_local_auth_sync`` so both
        paths read the same DB regardless of how the WS handler is
        configured (single-pool vs split static / timescale pools).
        """
        pool = None
        static_pool_fn = getattr(self._timescale, "_static_pool", None)
        if callable(static_pool_fn):
            try:
                pool = static_pool_fn()
            except Exception:
                pool = None
        if pool is None:
            pool = getattr(self._timescale, "pg_pool", None)
        return pool

    async def _push_metering_config(self) -> None:
        """Push the metering configuration keys to the charger after boot.

        Fire-and-forget: failures must NOT block the BootNotification ack or
        the heartbeat loop. Three failure modes are tolerated explicitly:

          * ``Rejected`` — charger acknowledged but refused the key (e.g.
            Terra AC pre-1.8.32 on certain measurand combinations). Logged
            at WARN level; the rest of the keys still attempt.
          * ``Timeout`` — charger never responded (network drop mid-call).
            Logged at WARN; the next BootNotification will retry.
          * Vendor-safe refusal — ``change_configuration`` returns
            ``NotSupported`` when the requested measurand set would exit the
            ``_ABB_SAFE_MEASURANDS`` allowlist; that defends against the
            Terra AC ≤1.8.21 reboot-loop bug and is logged at INFO.

        Per-firmware idempotency: once every key in
        ``_METERING_CONFIG_KEYS`` was accepted by the charger on its
        current firmware, the bootstrap is short-circuited on subsequent
        reconnects until the charger reports a new ``firmware_version``.
        This is the fix for the HRX Vilnius reconfig loop where ABB Terra
        AC chargers reconnected every ~60 s and the full ~15 s bootstrap
        re-ran on every cycle. See migration 014.

        Errors are swallowed locally; raising here would tear down the
        OCPP session for a non-fatal configuration mismatch.
        """
        try:
            if await self._metering_config_already_applied():
                logger.info(
                    "metering_config_cache_hit station=%s firmware=%s — "
                    "skipping ChangeConfiguration sequence",
                    self._station_id,
                    getattr(self._cp, "firmware_version", None),
                )
                return

            all_keys_applied = True
            for key, value in self._METERING_CONFIG_KEYS:
                try:
                    status = await asyncio.wait_for(
                        self._cp.change_configuration(key=key, value=value),
                        timeout=10.0,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "ChangeConfiguration timed out for station=%s key=%s; "
                        "will retry on next BootNotification",
                        self._station_id,
                        key,
                    )
                    all_keys_applied = False
                    continue
                except Exception as exc:
                    logger.warning(
                        "ChangeConfiguration raised for station=%s key=%s: %s",
                        self._station_id,
                        key,
                        exc,
                    )
                    all_keys_applied = False
                    continue
                if status in self._METERING_CONFIG_SUCCESS_STATUSES:
                    logger.info(
                        "ChargingMetering config %s on station=%s: %s=%s",
                        status,
                        self._station_id,
                        key,
                        value,
                    )
                else:
                    logger.warning(
                        "ChargingMetering config %s on station=%s: %s=%s",
                        status,
                        self._station_id,
                        key,
                        value,
                    )
                    all_keys_applied = False
            if all_keys_applied:
                await self._record_metering_config_applied()
        except asyncio.CancelledError:
            logger.debug("metering_config push cancelled for station=%s", self._station_id)
            raise
        finally:
            current = asyncio.current_task()
            if current is not None and self._metering_config_task is current:
                self._metering_config_task = None

    def _is_connection_open(self) -> bool:
        """Best-effort check whether the charger socket is still open."""
        connection = getattr(self._cp, "_connection", None)
        if connection is None:
            connection = getattr(self._cp, "connection", None)
        if connection is None:
            return False
        closed = getattr(connection, "closed", None)
        if isinstance(closed, bool):
            return not closed
        state = getattr(connection, "state", None)
        if state is not None:
            state_name = str(getattr(state, "name", state)).upper()
            if "OPEN" in state_name:
                return True
            if any(token in state_name for token in ("CLOSED", "CLOSING")):
                return False
        client_state = getattr(connection, "client_state", None)
        if client_state is not None:
            state_name = str(getattr(client_state, "name", client_state)).upper()
            if state_name == "CONNECTED":
                return True
            if state_name in {"DISCONNECTED", "CLOSED"}:
                return False
        application_state = getattr(connection, "application_state", None)
        if application_state is not None:
            state_name = str(getattr(application_state, "name", application_state)).upper()
            if state_name == "CONNECTED":
                return True
            if state_name in {"DISCONNECTED", "CLOSED"}:
                return False
        return False

    async def _on_meter_values(
        self,
        cp_id: str,
        connector_id: int,
        soc: Optional[float],  # 0.0–1.0 fraction from FleetChargePoint; None when charger doesn't report it
        power_kw: Optional[float],
        energy_kwh: Optional[float],
        timestamp: datetime,
        transaction_id: Optional[int],
        max_charge_kw: Optional[float],
        raw_samples: list,  # noqa: ARG002
    ) -> None:
        """Write telemetry row and push a meter_values event to the main API."""
        # TimescaleClient expects soc_percent (0–100)
        soc_percent = soc * 100.0 if soc is not None else None

        base_row = {
            "time": timestamp,
            "station_id": cp_id,
            "connector_id": connector_id,
            "session_id": str(transaction_id) if transaction_id else None,
            "power_kw": power_kw,
            "energy_kwh": energy_kwh,
            "soc_percent": soc_percent,
            "max_charge_power_kw": max_charge_kw,
        }
        for sample_index, sample in enumerate(raw_samples or []):
            row = dict(base_row)
            row["raw_sample"] = sample
            # Preserve per-sample ordering for batches where multiple sampled
            # values share the same OCPP timestamp.
            row["sample_index"] = sample_index
            self._enqueue_telemetry_row(row)
        if not raw_samples:
            self._enqueue_telemetry_row(base_row)

        asyncio.create_task(
            self._message_handler._push_to_main_api(
                cp_id,
                "meter_values",
                {"soc_percent": soc_percent, "power_kw": power_kw},
            )
        )

    def _enqueue_telemetry_row(self, row: Dict[str, Any]) -> None:
        """Push telemetry row into bounded queue; drop oldest on overflow."""
        if self._stop_telemetry_flush.is_set():
            logger.warning(
                "Dropping telemetry row while session is shutting down: station=%s",
                self._station_id,
            )
            return
        try:
            self._telemetry_queue.put_nowait(row)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._telemetry_queue.get_nowait()
            try:
                self._telemetry_queue.put_nowait(row)
            except asyncio.QueueFull:
                logger.warning("Telemetry queue overflow for station=%s", self._station_id)

    async def _flush_telemetry_queue(self) -> None:
        """Drain queued telemetry rows in short batches."""
        stop_deadline: Optional[float] = None
        while True:
            if self._stop_telemetry_flush.is_set() and self._telemetry_queue.empty():
                break
            if self._stop_telemetry_flush.is_set() and stop_deadline is None:
                stop_deadline = time.monotonic() + 5.0
            if stop_deadline is not None and time.monotonic() >= stop_deadline:
                dropped = self._telemetry_queue.qsize()
                if dropped:
                    logger.warning(
                        "Telemetry flush timed out during shutdown: station=%s dropped=%s",
                        self._station_id,
                        dropped,
                    )
                break
            try:
                row = await asyncio.wait_for(self._telemetry_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            batch = [row]
            batch_started = time.monotonic()
            while len(batch) < 50 and (time.monotonic() - batch_started) < 0.25:
                try:
                    batch.append(self._telemetry_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._timescale.insert_telemetry_batch(batch)
            except Exception as exc:
                logger.warning(
                    "Telemetry write failed: station=%s batch_size=%s error=%s",
                    self._station_id,
                    len(batch),
                    exc,
                )

    async def _on_status_change(
        self,
        cp_id: str,
        connector_id: int,
        status: str,
        error_code: str,
        timestamp: Optional[Any] = None,
        vendor_id: Optional[str] = None,  # noqa: ARG002
        vendor_error_code: Optional[str] = None,  # noqa: ARG002
    ) -> None:
        """Persist StatusNotification to ``connector_status`` and forward it.

        The new (FastAPI-mounted) adapter writes status to DB inside
        FleetChargePoint, but this legacy path does not — without this row
        ``GET /depots/{id}/alerts`` returns nothing for OCPP 1.6 chargers
        (PRD §7.1).
        """
        ts = timestamp
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                ts = datetime.now(timezone.utc)
        elif not isinstance(ts, datetime):
            ts = datetime.now(timezone.utc)

        # Tenant context was resolved once on BootNotification and stashed
        # on the session. Passing it through lets the alerts trigger fire on
        # Faulted/Unavailable transitions (migration 029). When None — e.g.
        # the charger booted before we could resolve, or it isn't onboarded
        # in Supabase — the trigger bails silently per its own design.
        tenant = self._tenant_context or {}
        try:
            await self._timescale.insert_connector_status(
                {
                    "station_id": cp_id,
                    "connector_id": connector_id,
                    "status": status,
                    "error_code": error_code,
                    "organization_id": tenant.get("organization_id"),
                    "depot_id": tenant.get("depot_id"),
                    "timestamp": ts,
                }
            )
        except Exception as exc:
            logger.warning(
                "insert_connector_status failed: station=%s connector=%s error=%s",
                cp_id,
                connector_id,
                exc,
            )

        asyncio.create_task(
            self._message_handler._push_to_main_api(
                cp_id,
                "status_notification",
                {"status": status, "evse_id": connector_id, "connector_id": connector_id},
            )
        )

    async def _on_transaction_start(
        self,
        cp_id: str,
        connector_id: int,
        id_tag: str,
        meter_start: int,
        timestamp: str,
    ) -> AuthorizationStatus:
        # In-memory ``_cp.transactions`` is a cache populated by the boot
        # reload (line 714) and cleared by StopTransaction. Orphan recovery
        # can close the DB row without notifying this layer, so a hit here
        # may point at a closed session. DB is the source of truth — verify
        # before rejecting, and self-heal the cache when stale.
        if connector_id in self._cp.transactions:
            stale_tx_id = self._cp.transactions[connector_id]
            try:
                still_open = await self._timescale.is_transaction_open(cp_id, int(stale_tx_id))
            except Exception as exc:
                # Fail closed: a DB blip must never let two open sessions
                # exist on the same connector.
                logger.warning(
                    "is_transaction_open failed for station=%s connector=%s tx_id=%s: %s; "
                    "rejecting StartTransaction with ConcurrentTx",
                    cp_id,
                    connector_id,
                    stale_tx_id,
                    exc,
                )
                return AuthorizationStatus.concurrent_tx
            if still_open:
                logger.warning(
                    "Rejecting StartTransaction for station=%s connector=%s: "
                    "active transaction already exists (tx_id=%s)",
                    cp_id,
                    connector_id,
                    stale_tx_id,
                )
                return AuthorizationStatus.concurrent_tx
            if int(stale_tx_id) in self._in_memory_only_tx_ids:
                logger.warning(
                    "Rejecting StartTransaction for station=%s connector=%s: "
                    "tx_id=%s is active in-memory while DB row is missing",
                    cp_id,
                    connector_id,
                    stale_tx_id,
                )
                return AuthorizationStatus.concurrent_tx
            logger.warning(
                "Self-healing stale in-memory transaction for station=%s "
                "connector=%s tx_id=%s (DB says closed)",
                cp_id,
                connector_id,
                stale_tx_id,
            )
            if self._cp.transactions.get(connector_id) == stale_tx_id:
                self._cp.transactions.pop(connector_id, None)
            if self._cp.current_transaction_id == stale_tx_id:
                self._cp.current_transaction_id = None
            self._in_memory_only_tx_ids.discard(int(stale_tx_id))

        decision = await self._authz.authorize(cp_id, id_tag, "StartTransaction")
        auth_status = self._map_auth_status(decision.status)
        if auth_status != AuthorizationStatus.accepted:
            return auth_status

        # Concurrency re-check: between the gate above and here, a sibling
        # StartTransaction on the same connector (a charger retry storm)
        # could have raced through and stashed its own _pending_start.
        # The in-memory dict is the only signal we have until the DB
        # insert happens in ``_next_transaction_id``, so re-check it now.
        if connector_id in self._cp.transactions:
            logger.warning(
                "concurrent_start_race_blocked station=%s connector=%s: "
                "sibling StartTransaction beat us to the gate",
                cp_id,
                connector_id,
            )
            return AuthorizationStatus.concurrent_tx

        try:
            start_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            start_time = datetime.now(timezone.utc)
        # Stash for ``_next_transaction_id`` (FleetChargePoint will call it
        # next, only because we returned ``accepted``). evse_id == connector_id
        # in OCPP 1.6. meter_start_wh is persisted in the DB so a restart
        # between Start and Stop still yields a billing-grade kWh delta on
        # close (migration 036).
        #
        # meterStart=0 (or negative) is treated as "deferred" — some chargers
        # (ABB Terra AC firmwares are the documented case) emit 0 at
        # StartTransaction when the meter register isn't ready, then report
        # real values on the first MeterValues frame. Persisting NULL lets
        # ``_update_session_live_metrics`` backfill meter_start_wh from the
        # first positive Energy.Active.Import.Register sample. Without this
        # the row keeps a poison 0 that ``compute_energy_kwh`` rejects forever,
        # voiding the entire session's energy delta.
        if meter_start is not None and meter_start > 0:
            meter_start_wh: Optional[int] = meter_start
        else:
            meter_start_wh = None
            logger.info(
                "StartTransaction meter_start=%s on station=%s connector=%s; "
                "deferring meter_start_wh — will backfill from first MeterValues "
                "Energy.Active.Import.Register sample",
                meter_start,
                cp_id,
                connector_id,
            )
        self._pending_start = {
            "connector_id": connector_id,
            "evse_id": connector_id,
            "id_tag": id_tag,
            "start_time": start_time,
            "meter_start_wh": meter_start_wh,
        }
        self._pending_start.update(
            {
                "vehicle_id": decision.vehicle_id,
                "driver_id": decision.driver_id,
                "card_id": decision.card_id,
            }
        )

        # Inline gauge bump; reconciler in main.py corrects drift every 30 s.
        try:
            ACTIVE_TRANSACTIONS.labels(station_id=cp_id).inc()
        except Exception:
            pass

        asyncio.create_task(
            self._message_handler._push_to_main_api(
                cp_id,
                "transaction_start",
                {
                    "connector_id": connector_id,
                    "id_tag": id_tag,
                    "meter_start": meter_start,
                    "timestamp": timestamp,
                    "vehicle_id": self._pending_start.get("vehicle_id"),
                    "driver_id": self._pending_start.get("driver_id"),
                    "card_id": self._pending_start.get("card_id"),
                },
            )
        )
        return auth_status

    async def _on_transaction_stop(
        self,
        cp_id: str,
        transaction_id: int,
        id_tag: str,
        meter_stop: int,
        timestamp: str,
        reason: str,
        transaction_data: Optional[list] = None,
    ) -> None:
        connector_id: Optional[int] = None
        try:
            connector_id = await self._timescale.lookup_session_connector(
                station_id=cp_id,
                transaction_id=int(transaction_id),
            )
        except Exception as exc:
            logger.warning(
                "lookup_session_connector failed for station=%s tx_id=%s: %s",
                cp_id,
                transaction_id,
                exc,
            )

        try:
            end_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            end_time = datetime.now(timezone.utc)

        # OCPP 1.6 spec marks meterStop as required on StopTransaction, but
        # in practice chargers (notably ABB Terra AC ≤1.8.x and several
        # budget wallboxes) send None / 0 when the meter register is
        # unavailable or the EV disconnected before the charger could read
        # it. transactionData carries the final Energy.Active.Import.Register
        # sample when StopTxnSampledData is configured — use it as a
        # fallback so the close path produces a real meter_stop_wh.
        meter_stop_wh = _resolve_meter_stop(meter_stop, transaction_data)

        try:
            close_result = await self._timescale.close_open_session(
                cp_id,
                int(transaction_id),
                end_time,
                meter_stop_wh=meter_stop_wh,
                stop_reason=reason,
            )
        except Exception as exc:
            close_result = None
            logger.warning(
                "close_open_session failed for station=%s tx_id=%s: %s",
                cp_id,
                transaction_id,
                exc,
            )
        self._in_memory_only_tx_ids.discard(int(transaction_id))
        # Surface meter-delta anomalies so operators can reconcile the row
        # from the raw Wh values rather than from a NULL billing kWh. The
        # close already matched a row; we just couldn't compute the delta.
        # `close_result is None` covers the idempotent retry / no-match
        # case, which is silent on purpose.
        if close_result is not None:
            if close_result.get("synthesized"):
                logger.info(
                    "StopTransaction closed with synthesized_delta energy (station=%s tx_id=%s)",
                    cp_id,
                    transaction_id,
                )
            meter_start_db = close_result.get("meter_start_wh")
            last_meter_db = close_result.get("last_meter_wh")
            energy_kwh = close_result.get("energy_delivered_kwh")
            if energy_kwh is None:
                if meter_stop_wh is None:
                    anomaly_reason = "missing_meter_stop"
                elif meter_start_db is None:
                    # Two scenarios reach NULL meter_start_wh: a legacy row
                    # inserted before migration 036, or a "deferred" row from
                    # the meterStart=0 path that never saw a positive
                    # Energy.Active.Import.Register sample to backfill from.
                    # last_meter_wh tells them apart — if it's NULL too, the
                    # charger never sent register samples (the configuration
                    # push that switches Terra AC into the right measurand
                    # set failed, or the firmware truly lacks it). Operators
                    # need to investigate measurand configuration rather than
                    # reconcile from a legacy backfill.
                    if last_meter_db is None:
                        anomaly_reason = "deferred_meter_start_never_backfilled"
                    else:
                        anomaly_reason = "missing_meter_start"
                elif meter_start_db == 0:
                    # Defensive: post-Phase-1 we coerce meterStart=0 to NULL
                    # at the OCPP boundary so this branch should be dead in
                    # production. Kept to catch rows inserted by external
                    # tooling or tests that bypass the adapter.
                    anomaly_reason = "meter_start_is_zero"
                elif meter_stop_wh < int(meter_start_db):
                    anomaly_reason = "meter_stop_lt_meter_start"
                else:
                    anomaly_reason = "unknown"
                logger.warning(
                    "Anomalous meter delta on station=%s tx_id=%s: %s "
                    "(meter_start_wh=%s, last_meter_wh=%s, meter_stop_wh=%s, "
                    "raw_meter_stop=%s). Leaving energy_delivered_kwh NULL.",
                    cp_id,
                    transaction_id,
                    anomaly_reason,
                    meter_start_db,
                    last_meter_db,
                    meter_stop_wh,
                    meter_stop,
                )

        # Persist optional StopTransaction.transactionData samples using the
        # same telemetry pipeline so vendors that only emit end-of-session
        # samples still feed analytics/debug views.
        if transaction_data and connector_id is None:
            logger.warning(
                "Skipping StopTransaction.transactionData persistence due to unknown connector: "
                "station=%s tx_id=%s samples=%s",
                cp_id,
                transaction_id,
                len(transaction_data),
            )
        elif transaction_data:
            for meter_value in transaction_data:
                ts = meter_value.get("timestamp", timestamp)
                parsed_ts = datetime.now(timezone.utc)
                try:
                    parsed_ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    pass
                for sampled in meter_value.get(
                    "sampledValue", meter_value.get("sampled_value", [])
                ):
                    try:
                        parsed_value = float(sampled.get("value", "0"))
                    except (TypeError, ValueError):
                        continue
                    payload = {
                        "measurand": sampled.get("measurand", "Energy.Active.Import.Register"),
                        "value": parsed_value,
                        "unit": sampled.get("unit", "Wh"),
                        "context": sampled.get("context", "Transaction.End"),
                        "phase": sampled.get("phase"),
                        "location": sampled.get("location", "Outlet"),
                        "format": sampled.get("format", "Raw"),
                        "timestamp": parsed_ts,
                    }
                    try:
                        await self._timescale.insert_telemetry_batch(
                            [
                                {
                                    "time": parsed_ts,
                                    "station_id": cp_id,
                                    "connector_id": connector_id,
                                    "session_id": str(transaction_id),
                                    "power_kw": None,
                                    "energy_kwh": None,
                                    "soc_percent": None,
                                    "max_charge_power_kw": None,
                                    "raw_sample": payload,
                                }
                            ]
                        )
                    except Exception as exc:
                        logger.warning(
                            "transactionData write failed for station=%s tx_id=%s: %s",
                            cp_id,
                            transaction_id,
                            exc,
                        )

        try:
            ACTIVE_TRANSACTIONS.labels(station_id=cp_id).dec()
        except Exception:
            pass

        asyncio.create_task(
            self._message_handler._push_to_main_api(
                cp_id,
                "transaction_stop",
                {
                    "transaction_id": transaction_id,
                    "id_tag": id_tag,
                    "meter_stop": meter_stop,
                    "timestamp": timestamp,
                    "reason": reason,
                },
            )
        )
