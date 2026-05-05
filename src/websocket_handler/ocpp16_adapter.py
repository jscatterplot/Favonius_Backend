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
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ocpp.v16.enums import AuthorizationStatus

from src.adapters.ocpp.charge_point import FleetChargePoint

from .monitoring import ACTIVE_TRANSACTIONS, PROFILE_PUSH_LATENCY
from .rfid_authorization import RFIDAuthStatus

# Replay window after a charger reconnects: pending commands enqueued while
# the charger was offline are flushed within this many seconds of boot.
REPLAY_BACKOFF_SECONDS = 1.0

# How long to wait for a charger to send its own BootNotification before
# nudging it via TriggerMessage. Some ABB Terra AC firmwares (and other
# OCPP 1.6 implementations) skip BootNotification on WebSocket reconnect,
# leaving the heartbeat interval un-negotiated and the session stuck.
BOOT_TRIGGER_GRACE_SECONDS = 5.0

if TYPE_CHECKING:
    from .connection_manager import ConnectionManager
    from .message_handler import MessageHandler
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
    ) -> None:
        """Initialise the session and wire all FleetChargePoint callbacks.

        ``connection_manager`` is optional only so that targeted unit tests
        can construct a session without spinning up the full handler stack.
        Production wiring (``OCPPWebSocketServer``) always passes one — it
        is required for the stale-connection sweeper to see incoming OCPP
        traffic on this socket.
        """
        self._station_id = station_id
        self._timescale = timescale_client
        self._message_handler = message_handler
        self._connection_manager = connection_manager
        self._authz = message_handler.rfid_authorization
        # Single-slot stash for the most recent accepted StartTransaction so
        # ``_next_transaction_id`` can persist the open ``charging_sessions``
        # row alongside the generated tx_id. Safe because FleetChargePoint
        # serialises message handling per charger socket.
        self._pending_start: Optional[Dict[str, Any]] = None
        self._replay_task: Optional[asyncio.Task[None]] = None
        self._boot_trigger_task: Optional[asyncio.Task[None]] = None
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
            on_message_received=self._on_message_received,
            tx_id_provider=self._next_transaction_id,
        )

    async def _on_message_received(self) -> None:
        """Refresh the connection-manager liveness clock on every OCPP frame.

        OCPP 1.6 chargers vary widely in Heartbeat cadence (the spec only
        requires "at least every Heartbeat interval", which BootNotification
        sets to 300s). Without this hook the websocket handler's stale
        sweeper kills the socket after ~90s even though MeterValues and
        StatusNotification are flowing.
        """
        if self._connection_manager is None:
            return
        try:
            await self._connection_manager.update_heartbeat(self._station_id)
        except Exception:
            logger.exception("update_heartbeat failed for station=%s", self._station_id)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start processing messages from the charger (blocks until disconnect)."""
        self._stop_telemetry_flush.clear()
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

    async def _force_boot_notification(self) -> None:
        """Nudge spec-violating chargers that skip BootNotification on reconnect.

        OCPP 1.6 §4.2 requires the charger to send BootNotification on connect,
        and the central system's response carries the negotiated heartbeat
        interval. Some ABB Terra AC firmwares (1.8.x) skip BootNotification on
        WebSocket reconnects after the initial cold boot, leaving the session
        with no heartbeat cadence. TriggerMessage(BootNotification) is the
        spec-sanctioned way to wake them up (OCPP 1.6 §4.18).
        """
        try:
            await asyncio.sleep(BOOT_TRIGGER_GRACE_SECONDS)
            if self._cp.last_boot_at is not None:
                return
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
        tx_id = await self._timescale.next_transaction_id()
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
                )
            except Exception as exc:
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
        if status == RFIDAuthStatus.ACCEPTED:
            return AuthorizationStatus.accepted
        if status == RFIDAuthStatus.EXPIRED:
            return AuthorizationStatus.expired
        if status in {RFIDAuthStatus.BLOCKED, RFIDAuthStatus.CONCURRENT_TX}:
            return AuthorizationStatus.blocked
        return AuthorizationStatus.invalid

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

    async def send_der_control(self, der_control: Dict) -> bool:  # noqa: ARG002
        """DER control is an OCPP 2.x feature; no-op for OCPP 1.6 chargers."""
        logger.debug("send_der_control called on OCPP 1.6 session — skipping")
        return False

    async def clear_der_control(self) -> bool:
        """DER control is an OCPP 2.x feature; no-op for OCPP 1.6 chargers."""
        logger.debug("clear_der_control called on OCPP 1.6 session — skipping")
        return False

    # ------------------------------------------------------------------
    # FleetChargePoint callbacks
    # ------------------------------------------------------------------

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
        # Cross-restart safety:
        #   1. Reload still-open transactions into FleetChargePoint.transactions
        #      so an incoming StopTransaction from the rebooted charger is
        #      recognised instead of being treated as an unknown txn.
        #   2. Schedule the queued-command replay to run shortly after we
        #      return the BootNotification response. Doing it synchronously
        #      here would delay the boot ack and could trip the charger's
        #      response timeout.
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
        soc: float,  # 0.0–1.0 fraction from FleetChargePoint
        power_kw: float,
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
        for sample in raw_samples or []:
            row = dict(base_row)
            row["raw_sample"] = sample
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

        try:
            await self._timescale.insert_connector_status(
                {
                    "station_id": cp_id,
                    "connector_id": connector_id,
                    "status": status,
                    "error_code": error_code,
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
        if connector_id in self._cp.transactions:
            logger.warning(
                "Rejecting StartTransaction for station=%s connector=%s: active transaction already exists",
                cp_id,
                connector_id,
            )
            return AuthorizationStatus.blocked

        decision = await self._authz.authorize(cp_id, id_tag, "StartTransaction")
        auth_status = self._map_auth_status(decision.status)
        if auth_status != AuthorizationStatus.accepted:
            return auth_status

        try:
            start_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            start_time = datetime.now(timezone.utc)
        # Stash for ``_next_transaction_id`` (FleetChargePoint will call it
        # next, only because we returned ``accepted``). evse_id == connector_id
        # in OCPP 1.6.
        self._pending_start = {
            "connector_id": connector_id,
            "evse_id": connector_id,
            "id_tag": id_tag,
            "start_time": start_time,
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
        try:
            await self._timescale.close_open_session(cp_id, int(transaction_id), end_time)
        except Exception as exc:
            logger.warning(
                "close_open_session failed for station=%s tx_id=%s: %s",
                cp_id,
                transaction_id,
                exc,
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
