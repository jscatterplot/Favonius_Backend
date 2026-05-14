"""Regression + unit tests for the OCPP 1.6 session adapter.

These tests exist specifically to catch the class of bug where the
OCPP 1.6 adapter fails to import (OCPP16_AVAILABLE=False), causing
OCPP 1.6 chargers to be routed through the 2.0.1 handler and
producing schema-validation error storms.

Reference: src/websocket_handler/ocpp16_adapter.py
           src/websocket_handler/server.py (OCPP16_AVAILABLE guard)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import-path regression test
# ---------------------------------------------------------------------------


class TestOCPP16AdapterImport:
    """Verify the adapter is importable — catches broken import paths."""

    def test_ocpp16_session_importable(self) -> None:
        """OCPP16Session must import without error.

        This test will fail if the import path inside ocpp16_adapter.py is
        wrong (e.g. 'from adapters.ocpp...' instead of
        'from src.adapters.ocpp...'), which causes OCPP16_AVAILABLE=False
        and routes all OCPP 1.6 chargers through the 2.0.1 handler.
        """
        from src.websocket_handler.ocpp16_adapter import OCPP16Session  # noqa: F401

    def test_ocpp16_available_flag_is_true(self) -> None:
        """Server module must report OCPP16_AVAILABLE=True after import.

        If this is False the server silently misroutes every OCPP 1.6
        charger and floods logs with schema-validation dumps.

        Requires the full websocket_handler dependency stack (pydantic, etc.).
        Skipped in minimal environments that lack those packages.
        """
        pytest.importorskip("pydantic", reason="Full server stack not installed")
        import importlib

        # Re-import to get the module-level flag; use importlib so we pick up
        # any cached module from sys.modules without triggering side-effects.
        server_mod = importlib.import_module("src.websocket_handler.server")
        assert server_mod.OCPP16_AVAILABLE is True, (
            "OCPP16_AVAILABLE is False — the ocpp16_adapter import failed. "
            "Check the import path inside src/websocket_handler/ocpp16_adapter.py."
        )


# ---------------------------------------------------------------------------
# OCPP16Session unit tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_websocket() -> MagicMock:
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.recv = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.fixture()
def mock_timescale() -> MagicMock:
    tc = MagicMock()
    tc.insert_telemetry_batch = AsyncMock()
    tc.lookup_id_tag = AsyncMock(
        return_value={
            "vehicle_id": "vehicle-1",
            "depot_id": "depot-1",
            "driver_id": None,
            "card_id": None,
        }
    )
    tc.next_transaction_id = AsyncMock(return_value=4242)
    tc.next_charging_profile_id = AsyncMock(return_value=123456)
    tc.store_security_event = AsyncMock()
    # Default to "DB confirms open" so the existing ConcurrentTx-rejection
    # test path keeps its semantics. Tests that exercise the self-heal
    # branch override this per-test to return False.
    tc.is_transaction_open = AsyncMock(return_value=True)
    return tc


@pytest.fixture()
def mock_message_handler(mock_timescale) -> MagicMock:
    from src.websocket_handler.rfid_authorization import RFIDAuthorizationService

    mh = MagicMock()
    mh._push_to_main_api = AsyncMock()
    mh.rfid_authorization = RFIDAuthorizationService(mock_timescale, MagicMock())
    return mh


@pytest.fixture()
def session(mock_websocket, mock_timescale, mock_message_handler):
    from src.websocket_handler.ocpp16_adapter import OCPP16Session

    return OCPP16Session(
        station_id="test_station_001",
        websocket=mock_websocket,
        timescale_client=mock_timescale,
        message_handler=mock_message_handler,
    )


class TestOCPP16SessionInit:
    def test_session_holds_station_id(self, session) -> None:
        assert session._station_id == "test_station_001"

    def test_session_wraps_fleet_charge_point(self, session) -> None:
        from src.adapters.ocpp.charge_point import FleetChargePoint

        assert isinstance(session._cp, FleetChargePoint)

    def test_der_control_is_noop(self, session) -> None:
        """DER control is OCPP 2.x only; must return False without raising."""
        result = asyncio.run(session.send_der_control({"dummy": True}))
        assert result is False

    def test_clear_der_control_is_noop(self, session) -> None:
        result = asyncio.run(session.clear_der_control())
        assert result is False


class TestOCPP16SessionLiveness:
    """The session must refresh ``connection_manager.last_heartbeats`` on
    every received OCPP frame so the websocket-handler stale sweeper does
    not kill chargers that send only StatusNotification/MeterValues."""

    @pytest.mark.asyncio
    async def test_message_hook_calls_update_heartbeat(
        self, mock_websocket, mock_timescale, mock_message_handler
    ) -> None:
        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        cm = MagicMock()
        cm.update_heartbeat = AsyncMock()
        s = OCPP16Session(
            station_id="liveness_001",
            websocket=mock_websocket,
            timescale_client=mock_timescale,
            message_handler=mock_message_handler,
            connection_manager=cm,
        )
        await s._on_message_received()
        cm.update_heartbeat.assert_awaited_once_with("liveness_001")

    @pytest.mark.asyncio
    async def test_message_hook_swallows_connection_manager_failure(
        self, mock_websocket, mock_timescale, mock_message_handler
    ) -> None:
        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        cm = MagicMock()
        cm.update_heartbeat = AsyncMock(side_effect=RuntimeError("redis down"))
        s = OCPP16Session(
            station_id="liveness_002",
            websocket=mock_websocket,
            timescale_client=mock_timescale,
            message_handler=mock_message_handler,
            connection_manager=cm,
        )
        # Must not raise — message routing must keep working.
        await s._on_message_received()

    @pytest.mark.asyncio
    async def test_message_hook_is_noop_without_connection_manager(
        self, mock_websocket, mock_timescale, mock_message_handler
    ) -> None:
        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        s = OCPP16Session(
            station_id="liveness_003",
            websocket=mock_websocket,
            timescale_client=mock_timescale,
            message_handler=mock_message_handler,
        )
        # No connection_manager passed — must not raise.
        await s._on_message_received()

    @pytest.mark.asyncio
    async def test_session_passes_message_hook_to_fleet_charge_point(
        self, mock_websocket, mock_timescale, mock_message_handler
    ) -> None:
        """Calling the wired hook on FleetChargePoint must reach the
        connection_manager — proves the wiring without depending on
        bound-method identity."""
        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        cm = MagicMock()
        cm.update_heartbeat = AsyncMock()
        s = OCPP16Session(
            station_id="liveness_004",
            websocket=mock_websocket,
            timescale_client=mock_timescale,
            message_handler=mock_message_handler,
            connection_manager=cm,
        )

        assert s._cp._cb_message_received is not None
        await s._cp._cb_message_received()
        cm.update_heartbeat.assert_awaited_once_with("liveness_004")

    @pytest.mark.asyncio
    async def test_start_cancels_pending_liveness_background_tasks_on_teardown(
        self, mock_websocket, mock_timescale, mock_message_handler
    ) -> None:
        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        cancelled = asyncio.Event()

        class _BlockingNotifier:
            async def maybe_notify(self, *_args, **_kwargs):
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        s = OCPP16Session(
            station_id="liveness_005",
            websocket=mock_websocket,
            timescale_client=mock_timescale,
            message_handler=mock_message_handler,
            liveness_notifier=_BlockingNotifier(),
        )
        s._tenant_context = {"organization_id": "org-1"}

        async def _start_and_emit() -> None:
            await s._on_message_received()

        s._cp.start = AsyncMock(side_effect=_start_and_emit)

        await s.start()

        assert cancelled.is_set()
        assert not s._background_tasks


class TestOCPP16SessionForceBootNotification:
    """Workaround for ABB Terra AC firmware (1.8.x) and similar OCPP 1.6
    chargers that skip BootNotification on WebSocket reconnect."""

    @pytest.mark.asyncio
    async def test_no_op_when_charger_already_booted(self, session, monkeypatch) -> None:
        """If BootNotification already arrived organically, don't nudge."""
        from datetime import datetime, timezone

        session._cp.last_boot_at = datetime.now(timezone.utc)
        session._cp.trigger_message = AsyncMock()
        monkeypatch.setattr("src.websocket_handler.ocpp16_adapter.BOOT_TRIGGER_GRACE_SECONDS", 0)
        await session._force_boot_notification()
        session._cp.trigger_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_trigger_message_when_no_boot_seen(self, session, monkeypatch) -> None:
        session._cp.last_boot_at = None
        session._cp.trigger_message = AsyncMock(return_value="Accepted")
        monkeypatch.setattr("src.websocket_handler.ocpp16_adapter.BOOT_TRIGGER_GRACE_SECONDS", 0)
        await session._force_boot_notification()
        session._cp.trigger_message.assert_awaited_once_with("BootNotification")

    @pytest.mark.asyncio
    async def test_swallows_trigger_message_failure(self, session, monkeypatch) -> None:
        """A broken charger that ignores TriggerMessage must not crash the session."""
        session._cp.last_boot_at = None
        session._cp.trigger_message = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr("src.websocket_handler.ocpp16_adapter.BOOT_TRIGGER_GRACE_SECONDS", 0)
        await session._force_boot_notification()  # must not raise

    @pytest.mark.asyncio
    async def test_defers_then_triggers_when_inbound_frames_received(
        self, session, monkeypatch
    ) -> None:
        """If frames arrive but BootNotification does not, defer then trigger.

        This keeps `_on_boot` reachable for reconnect sessions that skip boot
        while still avoiding an immediate trigger on every short reconnect.
        """
        session._cp.last_boot_at = None
        session._cp.trigger_message = AsyncMock()
        # Simulate inbound frames arriving before the grace deadline.
        session._inbound_frame_count = 2
        monkeypatch.setattr("src.websocket_handler.ocpp16_adapter.BOOT_TRIGGER_GRACE_SECONDS", 0)
        monkeypatch.setattr("src.websocket_handler.ocpp16_adapter.BOOT_TRIGGER_FOLLOWUP_SECONDS", 0)
        await session._force_boot_notification()
        session._cp.trigger_message.assert_awaited_once_with("BootNotification")

    @pytest.mark.asyncio
    async def test_inbound_frame_counter_incremented_on_message_received(
        self, mock_websocket, mock_timescale, mock_message_handler
    ) -> None:
        """``_on_message_received`` must bump the per-session frame counter
        so the boot-trigger gate sees it; every OCPP frame counts, including
        the ones the handler doesn't act on (action=null responses, etc.)."""
        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        s = OCPP16Session(
            station_id="frame_count_001",
            websocket=mock_websocket,
            timescale_client=mock_timescale,
            message_handler=mock_message_handler,
        )
        assert s._inbound_frame_count == 0
        await s._on_message_received()
        await s._on_message_received()
        assert s._inbound_frame_count == 2


class TestOCPP16SessionMeteringConfigCache:
    """Per-firmware idempotency for ``_push_metering_config``.

    Without the cache the handler re-runs the full sequential
    ``ChangeConfiguration`` storm on every WebSocket reconnect. ABB Terra AC
    chargers that drop and reconnect every ~60 s never escape the bootstrap
    long enough to handle real OCPP traffic — this is the HRX Vilnius
    reconfig loop in production logs from 2026-05-14.
    """

    @pytest.fixture()
    def _session_with_pool(self, mock_websocket, mock_timescale, mock_message_handler):
        """Wires a fake asyncpg pool onto the session so ``fetchrow`` and
        ``execute`` calls in the cache helpers land somewhere predictable.

        The fake exposes ``fetchrow_return`` and ``executed`` attributes the
        tests can assert on. ``sqlstate`` can be set to simulate the
        "migration 014 not applied" path (column missing → sqlstate 42703).
        """
        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        class _FakePool:
            def __init__(self) -> None:
                self.fetchrow_return: Any = None
                self.fetchrow_raises: Optional[Exception] = None
                self.execute_raises: Optional[Exception] = None
                self.executed: list[tuple[str, tuple[Any, ...]]] = []

            async def fetchrow(self, _query: str, *args: Any) -> Any:
                if self.fetchrow_raises is not None:
                    raise self.fetchrow_raises
                return self.fetchrow_return

            async def execute(self, query: str, *args: Any) -> None:
                if self.execute_raises is not None:
                    raise self.execute_raises
                self.executed.append((query, args))

        s = OCPP16Session(
            station_id="metering_001",
            websocket=mock_websocket,
            timescale_client=mock_timescale,
            message_handler=mock_message_handler,
        )
        pool = _FakePool()
        # Mirror the production wiring: WS handler exposes the static pool
        # via a callable on the timescale client.
        mock_timescale._static_pool = lambda: pool
        return s, pool

    @pytest.mark.asyncio
    async def test_cache_hit_skips_change_configuration(self, _session_with_pool) -> None:
        """Same firmware as last successful push → no ChangeConfiguration
        calls. This is the core HRX Vilnius fix."""
        s, pool = _session_with_pool
        s._cp.firmware_version = "V1.8.36"
        s._cp.change_configuration = AsyncMock()
        pool.fetchrow_return = {
            "metering_config_applied_firmware": "V1.8.36",
        }

        await s._push_metering_config()

        s._cp.change_configuration.assert_not_called()
        # Cache hit doesn't write either — _record_metering_config_applied
        # is only called after a fresh push completes.
        assert pool.executed == []

    @pytest.mark.asyncio
    async def test_cache_miss_pushes_all_keys_and_stamps_firmware(self, _session_with_pool) -> None:
        """Different firmware → push every key, then stamp the cache row."""
        s, pool = _session_with_pool
        s._cp.firmware_version = "V1.8.37"
        s._cp.change_configuration = AsyncMock(return_value="Accepted")
        # Previously applied on an older firmware.
        pool.fetchrow_return = {
            "metering_config_applied_firmware": "V1.8.36",
        }

        await s._push_metering_config()

        # Every key in _METERING_CONFIG_KEYS should have been pushed.
        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        expected_calls = len(OCPP16Session._METERING_CONFIG_KEYS)
        assert s._cp.change_configuration.await_count == expected_calls
        # One UPDATE stamping metering_config_applied_firmware to V1.8.37.
        assert len(pool.executed) == 1
        update_sql, update_args = pool.executed[0]
        assert "metering_config_applied_firmware" in update_sql
        assert update_args[0] == "V1.8.37"
        assert update_args[1] == "metering_001"

    @pytest.mark.asyncio
    async def test_partial_failure_does_not_stamp_cache(self, _session_with_pool) -> None:
        """If even one key returns ``Rejected``/``NotSupported``/timeout, the
        cache is not stamped — so the next reconnect retries the full set
        rather than skipping based on a half-applied bootstrap."""
        s, pool = _session_with_pool
        s._cp.firmware_version = "V1.8.36"
        # First key accepted, second rejected, third accepted again.
        s._cp.change_configuration = AsyncMock(side_effect=["Accepted", "Rejected", "Accepted"])
        pool.fetchrow_return = None  # never cached

        await s._push_metering_config()

        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        assert s._cp.change_configuration.await_count == len(OCPP16Session._METERING_CONFIG_KEYS)
        # No UPDATE — partial success doesn't earn a cache stamp.
        assert pool.executed == []

    @pytest.mark.asyncio
    async def test_reboot_required_counts_as_applied(self, _session_with_pool) -> None:
        """``RebootRequired`` means the charger accepted the new value and
        will apply it on next restart. Re-pushing on every reconnect would
        produce the same response indefinitely — treat it as success for
        cache purposes."""
        s, pool = _session_with_pool
        s._cp.firmware_version = "V1.8.36"
        s._cp.change_configuration = AsyncMock(return_value="RebootRequired")
        pool.fetchrow_return = None

        await s._push_metering_config()

        # Cache stamped because every key returned a success status.
        assert len(pool.executed) == 1

    @pytest.mark.asyncio
    async def test_missing_column_falls_back_to_always_push(self, _session_with_pool) -> None:
        """Pre-migration-014 DB: SELECT raises sqlstate 42703. The cache
        check must return False (cache miss) so the bootstrap still runs."""
        s, pool = _session_with_pool
        s._cp.firmware_version = "V1.8.36"
        s._cp.change_configuration = AsyncMock(return_value="Accepted")

        # asyncpg.UndefinedColumnError carries ``sqlstate='42703'``; the
        # production code only reads that attribute, so a plain exception
        # with it monkey-patched on works just as well and keeps the test
        # independent of asyncpg's import surface.
        def _undefined_column_error() -> Exception:
            exc = RuntimeError('column "metering_config_applied_firmware" does not exist')
            exc.sqlstate = "42703"  # type: ignore[attr-defined]
            return exc

        pool.fetchrow_raises = _undefined_column_error()
        # Same exception class on the write side so the stamp is silently
        # skipped (no crash, no infinite retry).
        pool.execute_raises = _undefined_column_error()

        await s._push_metering_config()

        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        # All keys still pushed because the cache lookup failed open.
        assert s._cp.change_configuration.await_count == len(OCPP16Session._METERING_CONFIG_KEYS)

    @pytest.mark.asyncio
    async def test_unknown_firmware_does_not_query_cache(self, _session_with_pool) -> None:
        """Without ``firmware_version`` we cannot scope a cache lookup, so we
        always push and skip the stamp (nothing to key off)."""
        s, pool = _session_with_pool
        s._cp.firmware_version = None
        s._cp.change_configuration = AsyncMock(return_value="Accepted")

        # Sentinel: if fetchrow runs, the test should fail because the
        # cache check shouldn't happen without a firmware string.
        async def _explode(*_a: Any, **_kw: Any) -> Any:
            raise AssertionError("fetchrow called without firmware_version")

        pool.fetchrow = _explode  # type: ignore[assignment]

        await s._push_metering_config()

        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        assert s._cp.change_configuration.await_count == len(OCPP16Session._METERING_CONFIG_KEYS)
        # Stamp also skipped (no firmware to record).
        assert pool.executed == []

    @pytest.mark.asyncio
    async def test_change_configuration_timeout_blocks_cache_stamp(
        self, _session_with_pool
    ) -> None:
        """A ``ChangeConfiguration`` timeout (charger unresponsive mid-call)
        leaves the key un-applied; the cache must not be stamped or we
        would silently skip the retry on the next BootNotification."""
        s, pool = _session_with_pool
        s._cp.firmware_version = "V1.8.36"
        s._cp.change_configuration = AsyncMock(
            side_effect=[
                "Accepted",
                asyncio.TimeoutError(),
                "Accepted",
            ]
        )
        pool.fetchrow_return = None

        await s._push_metering_config()

        assert pool.executed == []


class TestOCPP16SessionSecurityEventPersistence:
    """Persist OCPP 1.6 SecurityEventNotification frames to ``security_events``."""

    @pytest.mark.asyncio
    async def test_persists_event_with_parsed_timestamp(self, session, mock_timescale) -> None:
        from datetime import datetime, timezone

        await session._on_security_event(
            cp_id="test_station_001",
            event_type="StartupOfTheDevice",
            timestamp="2026-05-04T15:43:41.000Z",
            tech_info=None,
        )
        mock_timescale.store_security_event.assert_awaited_once()
        payload = mock_timescale.store_security_event.await_args.args[0]
        assert payload["station_id"] == "test_station_001"
        assert payload["event_type"] == "StartupOfTheDevice"
        assert payload["tech_info"] is None
        assert payload["timestamp"] == datetime(2026, 5, 4, 15, 43, 41, tzinfo=timezone.utc)
        assert payload["additional_info"]["source"] == "ocpp1.6.SecurityEventNotification"

    @pytest.mark.asyncio
    async def test_unparseable_timestamp_falls_back_to_now(self, session, mock_timescale) -> None:
        from datetime import datetime, timezone

        before = datetime.now(timezone.utc)
        await session._on_security_event(
            cp_id="test_station_001",
            event_type="SettingSystemTime",
            timestamp="not-a-timestamp",
            tech_info="ocppBoot",
        )
        after = datetime.now(timezone.utc)
        mock_timescale.store_security_event.assert_awaited_once()
        payload = mock_timescale.store_security_event.await_args.args[0]
        assert before <= payload["timestamp"] <= after


class TestOCPP16SessionTenantContext:
    """Tenant context (organization_id, depot_id) is resolved once on boot
    and passed through to ``insert_connector_status`` so the alerts trigger
    can fire on Faulted/Unavailable transitions (migration 029)."""

    @pytest.mark.asyncio
    async def test_on_boot_resolves_and_caches_tenant_context(
        self, mock_websocket, mock_timescale, mock_message_handler
    ) -> None:
        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        supabase = MagicMock()
        supabase.lookup_tenant_context = AsyncMock(
            return_value={"organization_id": "org-1", "depot_id": "dep-1"}
        )
        mock_timescale.fetch_open_sessions = AsyncMock(return_value=[])
        mock_timescale.fetch_pending_commands = AsyncMock(return_value=[])
        s = OCPP16Session(
            station_id="ctx_001",
            websocket=mock_websocket,
            timescale_client=mock_timescale,
            message_handler=mock_message_handler,
            supabase_client=supabase,
        )

        await s._on_boot(
            cp_id="ctx_001",
            vendor="ABB",
            model="TerraAC",
            serial_number="SN-001",
            firmware_version="1.8.36",
        )

        supabase.lookup_tenant_context.assert_awaited_once_with("ctx_001")
        assert s._tenant_context == {"organization_id": "org-1", "depot_id": "dep-1"}

    @pytest.mark.asyncio
    async def test_status_change_propagates_cached_tenant_context(
        self, mock_websocket, mock_timescale, mock_message_handler
    ) -> None:
        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        mock_timescale.insert_connector_status = AsyncMock()
        s = OCPP16Session(
            station_id="ctx_002",
            websocket=mock_websocket,
            timescale_client=mock_timescale,
            message_handler=mock_message_handler,
        )
        s._tenant_context = {"organization_id": "org-X", "depot_id": "dep-X"}

        await s._on_status_change(
            cp_id="ctx_002",
            connector_id=1,
            status="Faulted",
            error_code="OverCurrentFailure",
            timestamp="2026-05-05T22:30:00.000Z",
        )

        mock_timescale.insert_connector_status.assert_awaited_once()
        payload = mock_timescale.insert_connector_status.await_args.args[0]
        assert payload["organization_id"] == "org-X"
        assert payload["depot_id"] == "dep-X"
        assert payload["status"] == "Faulted"

    @pytest.mark.asyncio
    async def test_status_change_passes_null_context_when_unresolved(
        self, mock_websocket, mock_timescale, mock_message_handler
    ) -> None:
        """Charger booted before resolution succeeded → write NULL context.

        The post-mig-029 alerts trigger bails silently on NULL org_id,
        so this matches the legacy behavior — but we still want to
        persist the connector_status row (for `GET /depots/{id}/alerts`).
        """
        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        mock_timescale.insert_connector_status = AsyncMock()
        s = OCPP16Session(
            station_id="ctx_003",
            websocket=mock_websocket,
            timescale_client=mock_timescale,
            message_handler=mock_message_handler,
        )
        # _tenant_context never resolved (still None)

        await s._on_status_change(
            cp_id="ctx_003",
            connector_id=0,
            status="Available",
            error_code="NoError",
            timestamp="2026-05-05T22:31:00.000Z",
        )

        payload = mock_timescale.insert_connector_status.await_args.args[0]
        assert payload["organization_id"] is None
        assert payload["depot_id"] is None

    @pytest.mark.asyncio
    async def test_resolve_falls_back_to_none_on_lookup_failure(
        self, mock_websocket, mock_timescale, mock_message_handler, caplog
    ) -> None:
        """Supabase lookup raises → log warning, leave cache None, don't crash boot."""
        import logging

        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        supabase = MagicMock()
        supabase.lookup_tenant_context = AsyncMock(side_effect=RuntimeError("supabase down"))
        mock_timescale.fetch_open_sessions = AsyncMock(return_value=[])
        mock_timescale.fetch_pending_commands = AsyncMock(return_value=[])
        s = OCPP16Session(
            station_id="ctx_004",
            websocket=mock_websocket,
            timescale_client=mock_timescale,
            message_handler=mock_message_handler,
            supabase_client=supabase,
        )

        with caplog.at_level(logging.WARNING):
            await s._on_boot(
                cp_id="ctx_004",
                vendor="ABB",
                model="TerraAC",
                serial_number="SN",
                firmware_version="1.8.36",
            )

        assert s._tenant_context is None
        assert any("tenant_context_lookup_failed" in r.getMessage() for r in caplog.records)


class TestOCPP16SessionCallbacks:
    @pytest.mark.asyncio
    async def test_on_boot_logs_without_error(self, session, caplog) -> None:
        import logging

        with caplog.at_level(logging.INFO):
            await session._on_boot(
                cp_id="test_station_001",
                vendor="FavoniusTest",
                model="SimulatedCharger",
                serial_number="SN-001",
                firmware_version="1.0.0",
            )
        assert "test_station_001" in caplog.text

    @pytest.mark.asyncio
    async def test_on_meter_values_writes_telemetry(self, session, mock_timescale) -> None:
        ts = datetime.now(tz=timezone.utc)
        await session._on_meter_values(
            cp_id="test_station_001",
            connector_id=1,
            soc=0.75,
            power_kw=22.0,
            energy_kwh=5.0,
            timestamp=ts,
            transaction_id=42,
            max_charge_kw=50.0,
            raw_samples=[],
        )
        assert session._telemetry_queue.qsize() == 1
        call_args = session._telemetry_queue.get_nowait()
        assert abs(call_args["soc_percent"] - 75.0) < 0.01
        assert call_args["power_kw"] == 22.0
        assert call_args["station_id"] == "test_station_001"

    @pytest.mark.asyncio
    async def test_on_meter_values_tolerates_telemetry_failure(
        self, session, mock_timescale
    ) -> None:
        """A DB write failure must not propagate — session must stay alive."""
        mock_timescale.insert_telemetry_batch.side_effect = Exception("DB down")
        ts = datetime.now(tz=timezone.utc)
        # Should not raise
        await session._on_meter_values(
            cp_id="test_station_001",
            connector_id=1,
            soc=0.5,
            power_kw=10.0,
            energy_kwh=None,
            timestamp=ts,
            transaction_id=None,
            max_charge_kw=None,
            raw_samples=[],
        )

    @pytest.mark.asyncio
    async def test_on_status_change_pushes_event(self, session, mock_message_handler) -> None:
        await session._on_status_change(
            cp_id="test_station_001",
            connector_id=1,
            status="Charging",
            error_code="NoError",
        )
        await asyncio.sleep(0)  # let create_task run
        mock_message_handler._push_to_main_api.assert_awaited()

    @pytest.mark.asyncio
    async def test_on_transaction_start_pushes_event(
        self, session, mock_timescale, mock_message_handler
    ) -> None:
        result = await session._on_transaction_start(
            cp_id="test_station_001",
            connector_id=1,
            id_tag="TAG-001",
            meter_start=0,
            timestamp="2026-01-01T00:00:00Z",
        )
        await asyncio.sleep(0)
        assert result.value == "Accepted"
        mock_timescale.lookup_id_tag.assert_any_await("TAG-001", station_id="test_station_001")
        payload = mock_message_handler._push_to_main_api.await_args.args[2]
        assert payload["vehicle_id"] == "vehicle-1"
        assert payload["driver_id"] is None
        assert payload["card_id"] is None
        mock_message_handler._push_to_main_api.assert_awaited()

    @pytest.mark.asyncio
    async def test_on_transaction_start_preserves_driver_card_identity(
        self, session, mock_timescale, mock_message_handler
    ) -> None:
        mock_timescale.lookup_id_tag.return_value = {
            "vehicle_id": "vehicle-2",
            "depot_id": "depot-1",
            "driver_id": "driver-1",
            "card_id": "card-1",
        }

        result = await session._on_transaction_start(
            cp_id="test_station_001",
            connector_id=1,
            id_tag="CARD-001",
            meter_start=0,
            timestamp="2026-01-01T00:00:00Z",
        )
        await asyncio.sleep(0)

        assert result.value == "Accepted"
        assert session._pending_start["vehicle_id"] == "vehicle-2"
        assert session._pending_start["driver_id"] == "driver-1"
        assert session._pending_start["card_id"] == "card-1"
        payload = mock_message_handler._push_to_main_api.await_args.args[2]
        assert payload["driver_id"] == "driver-1"
        assert payload["card_id"] == "card-1"

    @pytest.mark.asyncio
    async def test_on_transaction_start_rejects_active_connector_with_concurrent_tx(
        self, session, mock_timescale, mock_message_handler
    ) -> None:
        """OCPP 1.6 ConcurrentTx: EVSE already in transaction (not card Blocked).

        DB confirms the in-memory cache: tx is genuinely open. Authorize
        is skipped and the rejection is returned without touching the
        message handler.
        """
        from ocpp.v16.enums import AuthorizationStatus

        session._cp.transactions[1] = 999
        result = await session._on_transaction_start(
            cp_id="test_station_001",
            connector_id=1,
            id_tag="TAG-001",
            meter_start=0,
            timestamp="2026-01-01T00:00:00Z",
        )
        assert result == AuthorizationStatus.concurrent_tx
        mock_timescale.is_transaction_open.assert_awaited_once_with("test_station_001", 999)
        mock_timescale.lookup_id_tag.assert_not_awaited()
        mock_message_handler._push_to_main_api.assert_not_awaited()
        # The cache entry survives a confirmed ConcurrentTx so a subsequent
        # retry against the same still-open tx is also rejected.
        assert session._cp.transactions[1] == 999

    @pytest.mark.asyncio
    async def test_on_transaction_start_self_heals_when_db_says_closed(
        self, session, mock_timescale, mock_message_handler
    ) -> None:
        """In-memory cache holds a tx_id whose DB row is already closed.

        Self-heal: drop the cache entry, fall through to Authorize +
        Accept. This is the production failure mode where orphan recovery
        closed the row but the WS layer never saw it.
        """
        from ocpp.v16.enums import AuthorizationStatus

        session._cp.transactions[1] = 999
        mock_timescale.is_transaction_open = AsyncMock(return_value=False)

        result = await session._on_transaction_start(
            cp_id="test_station_001",
            connector_id=1,
            id_tag="TAG-001",
            meter_start=0,
            timestamp="2026-01-01T00:00:00Z",
        )
        await asyncio.sleep(0)

        assert result == AuthorizationStatus.accepted
        assert 1 not in session._cp.transactions
        mock_timescale.is_transaction_open.assert_awaited_once_with("test_station_001", 999)
        mock_timescale.lookup_id_tag.assert_any_await("TAG-001", station_id="test_station_001")
        assert session._pending_start is not None
        assert session._pending_start["connector_id"] == 1
        assert session._pending_start["meter_start_wh"] == 0

    @pytest.mark.asyncio
    async def test_on_transaction_start_self_heal_clears_current_transaction_id(
        self, session, mock_timescale
    ) -> None:
        """Self-heal also clears the ``current_transaction_id`` backward-compat field."""
        from ocpp.v16.enums import AuthorizationStatus

        session._cp.transactions[1] = 999
        session._cp.current_transaction_id = 999
        mock_timescale.is_transaction_open = AsyncMock(return_value=False)

        result = await session._on_transaction_start(
            cp_id="test_station_001",
            connector_id=1,
            id_tag="TAG-001",
            meter_start=0,
            timestamp="2026-01-01T00:00:00Z",
        )

        assert result == AuthorizationStatus.accepted
        assert session._cp.current_transaction_id is None

    @pytest.mark.asyncio
    async def test_on_transaction_start_self_heal_preserves_other_current_transaction_id(
        self, session, mock_timescale
    ) -> None:
        """Self-healing tx 999 must not clear current_transaction_id when it points elsewhere."""
        from ocpp.v16.enums import AuthorizationStatus

        session._cp.transactions[1] = 999
        # Connector 2 still has an active session that we should leave alone.
        session._cp.current_transaction_id = 1234
        mock_timescale.is_transaction_open = AsyncMock(return_value=False)

        result = await session._on_transaction_start(
            cp_id="test_station_001",
            connector_id=1,
            id_tag="TAG-001",
            meter_start=0,
            timestamp="2026-01-01T00:00:00Z",
        )

        assert result == AuthorizationStatus.accepted
        assert session._cp.current_transaction_id == 1234

    @pytest.mark.asyncio
    async def test_on_transaction_start_db_error_keeps_concurrent_tx_default(
        self, session, mock_timescale, mock_message_handler
    ) -> None:
        """A DB blip on ``is_transaction_open`` must fail closed (reject), not open."""
        from ocpp.v16.enums import AuthorizationStatus

        session._cp.transactions[1] = 999
        mock_timescale.is_transaction_open = AsyncMock(side_effect=RuntimeError("boom"))

        result = await session._on_transaction_start(
            cp_id="test_station_001",
            connector_id=1,
            id_tag="TAG-001",
            meter_start=0,
            timestamp="2026-01-01T00:00:00Z",
        )

        assert result == AuthorizationStatus.concurrent_tx
        mock_timescale.lookup_id_tag.assert_not_awaited()
        mock_message_handler._push_to_main_api.assert_not_awaited()
        # Cache entry preserved so subsequent retries continue to fail
        # closed until the DB recovers.
        assert session._cp.transactions[1] == 999

    @pytest.mark.asyncio
    async def test_on_transaction_start_post_authorize_race_check_blocks_sibling(
        self, session, mock_message_handler
    ) -> None:
        """Sibling StartTransaction beats us between Authorize and _pending_start stash.

        Simulates a charger retry storm: the in-memory cache is empty when
        we enter, but a concurrent task populates ``_cp.transactions[1]``
        while we ``await self._authz.authorize(...)``. The post-Authorize
        re-check must catch this and return ConcurrentTx without stashing
        ``_pending_start`` (which would otherwise produce a second open
        ``charging_sessions`` row on the same connector).
        """
        from ocpp.v16.enums import AuthorizationStatus

        from src.websocket_handler.rfid_authorization import (
            RFIDAuthDecision,
            RFIDAuthStatus,
        )

        async def _authorize_then_seed_dict(cp_id, id_tag, source):
            # A sibling racer stashed its tx in the dict while we awaited.
            session._cp.transactions[1] = 5555
            return RFIDAuthDecision(
                status=RFIDAuthStatus.ACCEPTED,
                source="test",
                reason="race-test",
                vehicle_id="vehicle-1",
                driver_id=None,
                card_id=None,
            )

        session._authz = MagicMock()
        session._authz.authorize = AsyncMock(side_effect=_authorize_then_seed_dict)

        result = await session._on_transaction_start(
            cp_id="test_station_001",
            connector_id=1,
            id_tag="TAG-001",
            meter_start=0,
            timestamp="2026-01-01T00:00:00Z",
        )

        assert result == AuthorizationStatus.concurrent_tx
        # _pending_start must NOT be populated — that would let the
        # framework call _next_transaction_id and create a duplicate row.
        assert session._pending_start is None
        mock_message_handler._push_to_main_api.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_transaction_start_rejects_unknown_id_tag(
        self, session, mock_timescale, mock_message_handler
    ) -> None:
        mock_timescale.lookup_id_tag.return_value = None
        result = await session._on_transaction_start(
            cp_id="test_station_001",
            connector_id=1,
            id_tag="UNKNOWN-TAG",
            meter_start=0,
            timestamp="2026-01-01T00:00:00Z",
        )
        await asyncio.sleep(0)
        assert result.value == "Invalid"
        mock_message_handler._push_to_main_api.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_transaction_path_rejects_unknown_id_tag(
        self, session, mock_timescale, mock_message_handler
    ) -> None:
        mock_timescale.lookup_id_tag.return_value = None
        result = await session._cp.on_start_transaction(
            connector_id=1,
            id_tag="UNKNOWN-TAG",
            meter_start=0,
            timestamp="2026-01-01T00:00:00Z",
        )
        assert result.id_tag_info["status"].value == "Invalid"
        assert result.transaction_id == 0
        assert 1 not in session._cp.transactions
        mock_timescale.next_transaction_id.assert_not_awaited()
        mock_message_handler._push_to_main_api.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_start_transaction_path_fails_closed_on_lookup_error(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.lookup_id_tag.side_effect = RuntimeError("DB down")
        result = await session._cp.on_start_transaction(
            connector_id=1,
            id_tag="ANY-TAG",
            meter_start=0,
            timestamp="2026-01-01T00:00:00Z",
        )
        assert result.id_tag_info["status"].value == "Invalid"
        assert result.transaction_id == 0
        mock_timescale.next_transaction_id.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_transaction_stop_pushes_event(self, session, mock_message_handler) -> None:
        await session._on_transaction_stop(
            cp_id="test_station_001",
            transaction_id=99,
            id_tag="TAG-001",
            meter_stop=5000,
            timestamp="2026-01-01T01:00:00Z",
            reason="Local",
        )
        await asyncio.sleep(0)
        mock_message_handler._push_to_main_api.assert_awaited()


class TestOCPP16SessionChargingProfile:
    @pytest.mark.asyncio
    async def test_send_charging_profile_delegates_to_fleet_cp(self, session) -> None:
        mock_result = True
        session._cp.set_charging_profile = AsyncMock(return_value=mock_result)

        profile = {
            "chargingSchedule": {
                "chargingSchedulePeriod": [
                    {"startPeriod": 0, "limit": 22000},
                    {"startPeriod": 3600, "limit": 11000},
                ],
                "chargingRateUnit": "W",
            },
            "chargingProfilePurpose": "TxDefaultProfile",
            "chargingProfileKind": "Absolute",
            "stackLevel": 0,
            "chargingProfileId": 7,
        }
        result = await session.send_charging_profile(evse_id=1, charging_profile=profile)

        assert result is True
        session._cp.set_charging_profile.assert_awaited_once_with(
            connector_id=1,
            charging_schedule=[
                {"startPeriod": 0, "limit": 22000},
                {"startPeriod": 3600, "limit": 11000},
            ],
            profile_purpose="TxDefaultProfile",
            profile_kind="Absolute",
            charging_rate_unit="W",
            stack_level=0,
            profile_id=7,
        )

    @pytest.mark.asyncio
    async def test_send_charging_profile_flat_format(self, session) -> None:
        """Flat chargingSchedulePeriod at top level (no nested chargingSchedule)."""
        session._cp.set_charging_profile = AsyncMock(return_value=True)

        profile = {
            "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 50000}],
            "chargingRateUnit": "A",
        }
        result = await session.send_charging_profile(evse_id=2, charging_profile=profile)
        assert result is True
        call_kwargs = session._cp.set_charging_profile.call_args.kwargs
        assert call_kwargs["charging_schedule"] == [{"startPeriod": 0, "limit": 50000}]
        assert call_kwargs["charging_rate_unit"] == "A"
        assert call_kwargs["profile_id"] == 123456


# ---------------------------------------------------------------------------
# Server routing regression test
# ---------------------------------------------------------------------------


class TestServerRoutesOCPP16Correctly:
    """Verify the server actually instantiates OCPP16Session for ocpp1.6 connections."""

    def test_ocpp16_available_flag(self) -> None:
        """Redundant guard — fail fast if the module-level flag is wrong.

        Requires the full websocket_handler dependency stack.
        Skipped in minimal environments.
        """
        pytest.importorskip("pydantic", reason="Full server stack not installed")
        from src.websocket_handler import server as srv

        assert srv.OCPP16_AVAILABLE is True

    def test_server_creates_ocpp16_session_for_v16_subprotocol(self) -> None:
        """When subprotocol==ocpp1.6, an OCPP16Session must be created, not
        EnhancedOCPPChargePoint. An EnhancedOCPPChargePoint (v201) would validate
        OCPP 1.6 payloads against the 2.0.1 schema and raise FormatViolationError.

        Requires the full websocket_handler dependency stack.
        Skipped in minimal environments.
        """
        pytest.importorskip("pydantic", reason="Full server stack not installed")
        from src.websocket_handler.ocpp16_adapter import OCPP16Session
        from src.websocket_handler.server import OCPP16_AVAILABLE

        # This assertion is the canonical check: if True, the branch that
        # creates OCPP16Session is reachable. If False, every 1.6 charger
        # gets an EnhancedOCPPChargePoint and the schema storm occurs.
        assert (
            OCPP16_AVAILABLE is True
        ), "OCPP16_AVAILABLE is False — server will misroute OCPP 1.6 chargers"
        # Confirm OCPP16Session itself is the right type (not NoneType)
        assert OCPP16Session is not None


# ---------------------------------------------------------------------------
# Cross-restart recovery (migration 013) — unit tests
# ---------------------------------------------------------------------------


class TestOCPP16SessionRecovery:
    """Cover the recovery paths that live in ``OCPP16Session``.

    The integration tests in ``tests/integration/test_websocket_recovery.py``
    drive these against a real database; these unit tests cover the in-memory
    branches and error handling that don't need DB.
    """

    @pytest.mark.asyncio
    async def test_send_charging_profile_falls_back_to_enqueue(
        self, session, mock_timescale
    ) -> None:
        """When the WS push fails, the profile must hit charging_command_queue."""
        session._cp.set_charging_profile = AsyncMock(return_value=False)
        mock_timescale.enqueue_charging_command = AsyncMock(return_value=99)

        ok = await session.send_charging_profile(
            1,
            {
                "chargingProfileId": 1,
                "stackLevel": 0,
                "chargingProfilePurpose": "TxDefaultProfile",
                "chargingProfileKind": "Absolute",
                "chargingSchedule": {
                    "chargingRateUnit": "W",
                    "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 1000}],
                },
            },
        )

        assert ok is False
        mock_timescale.enqueue_charging_command.assert_awaited_once()
        kwargs = mock_timescale.enqueue_charging_command.call_args.kwargs
        assert kwargs["charge_point_id"] == "test_station_001"
        assert kwargs["connector_id"] == 1

    @pytest.mark.asyncio
    async def test_send_charging_profile_does_not_enqueue_rejected_online_charger(
        self, session, mock_timescale
    ) -> None:
        """Explicit rejection from an online charger must not be queued."""
        session._cp.set_charging_profile = AsyncMock(return_value=False)
        mock_timescale.enqueue_charging_command = AsyncMock()
        with patch.object(session, "_is_connection_open", return_value=True):
            ok = await session.send_charging_profile(
                1,
                {
                    "chargingProfileId": 1,
                    "stackLevel": 0,
                    "chargingProfilePurpose": "TxDefaultProfile",
                    "chargingProfileKind": "Absolute",
                    "chargingSchedule": {
                        "chargingRateUnit": "W",
                        "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 1000}],
                    },
                },
            )
        assert ok is False
        mock_timescale.enqueue_charging_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_charging_profile_no_enqueue_when_disabled(
        self, session, mock_timescale
    ) -> None:
        """``allow_enqueue=False`` (the replay path) must propagate exceptions."""
        session._cp.set_charging_profile = AsyncMock(side_effect=RuntimeError("ws closed"))
        mock_timescale.enqueue_charging_command = AsyncMock()

        with pytest.raises(RuntimeError):
            await session.send_charging_profile(
                1, {"chargingSchedulePeriod": []}, allow_enqueue=False
            )
        mock_timescale.enqueue_charging_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_replay_queued_commands_marks_acked_on_success(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.fetch_pending_commands = AsyncMock(
            return_value=[
                {
                    "queue_id": 1,
                    "connector_id": 2,
                    "command_type": "set_charging_profile",
                    "payload": {
                        "chargingProfileId": 7,
                        "stackLevel": 0,
                        "chargingProfilePurpose": "TxDefaultProfile",
                        "chargingProfileKind": "Absolute",
                        "chargingSchedule": {
                            "chargingRateUnit": "W",
                            "chargingSchedulePeriod": [{"startPeriod": 0, "limit": 1000}],
                        },
                    },
                    "attempt_count": 0,
                }
            ]
        )
        mock_timescale.mark_command_acked = AsyncMock()
        mock_timescale.mark_command_failed = AsyncMock()
        session._cp.set_charging_profile = AsyncMock(return_value=True)

        sent = await session.replay_queued_commands()

        assert sent == 1
        mock_timescale.mark_command_acked.assert_awaited_once_with(1)
        mock_timescale.mark_command_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_replay_queued_commands_marks_failed_on_rejection(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.fetch_pending_commands = AsyncMock(
            return_value=[
                {
                    "queue_id": 9,
                    "connector_id": 1,
                    "command_type": "set_charging_profile",
                    "payload": {"chargingSchedulePeriod": []},
                    "attempt_count": 0,
                }
            ]
        )
        mock_timescale.mark_command_acked = AsyncMock()
        mock_timescale.mark_command_failed = AsyncMock()
        session._cp.set_charging_profile = AsyncMock(return_value=False)

        sent = await session.replay_queued_commands()

        assert sent == 0
        mock_timescale.mark_command_failed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_replay_queued_commands_continues_when_mark_failed_update_raises(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.fetch_pending_commands = AsyncMock(
            return_value=[
                {
                    "queue_id": 9,
                    "connector_id": 1,
                    "command_type": "set_charging_profile",
                    "payload": {"chargingSchedulePeriod": []},
                    "attempt_count": 0,
                },
                {
                    "queue_id": 10,
                    "connector_id": 2,
                    "command_type": "set_charging_profile",
                    "payload": {"chargingSchedulePeriod": []},
                    "attempt_count": 0,
                },
            ]
        )
        mock_timescale.mark_command_acked = AsyncMock()
        mock_timescale.mark_command_failed = AsyncMock(
            side_effect=[RuntimeError("db hiccup"), None]
        )
        session._cp.set_charging_profile = AsyncMock(return_value=False)

        sent = await session.replay_queued_commands()

        assert sent == 0
        assert mock_timescale.mark_command_failed.await_count == 2

    @pytest.mark.asyncio
    async def test_replay_queued_commands_tolerates_fetch_failure(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.fetch_pending_commands = AsyncMock(side_effect=RuntimeError("db down"))
        sent = await session.replay_queued_commands()
        assert sent == 0

    @pytest.mark.asyncio
    async def test_on_boot_reloads_transactions_from_db(self, session, mock_timescale) -> None:
        mock_timescale.clear_sessions_seen = AsyncMock()
        mock_timescale.fetch_open_sessions = AsyncMock(
            return_value=[
                {"transaction_id": 555, "connector_id": 1},
                {"transaction_id": 556, "connector_id": 2},
            ]
        )
        await session._on_boot(
            cp_id="test_station_001",
            vendor="V",
            model="M",
            serial_number="S",
            firmware_version="F",
        )
        # Background _delayed_replay schedules a task; let it run so the
        # event loop doesn't carry it into the next test.
        await asyncio.sleep(0)

        assert session._cp.transactions[1] == 555
        assert session._cp.transactions[2] == 556
        mock_timescale.clear_sessions_seen.assert_awaited_once_with("test_station_001")

    @pytest.mark.asyncio
    async def test_on_boot_cancels_previous_delayed_replay(self, session, mock_timescale) -> None:
        mock_timescale.clear_sessions_seen = AsyncMock()
        mock_timescale.fetch_open_sessions = AsyncMock(return_value=[])
        previous = MagicMock()
        previous.done.return_value = False
        previous.cancel = MagicMock()
        session._replay_task = previous

        await session._on_boot(
            cp_id="test_station_001",
            vendor="V",
            model="M",
            serial_number="S",
            firmware_version="F",
        )
        await asyncio.sleep(0)

        previous.cancel.assert_called_once()
        assert session._replay_task is not None
        if session._replay_task is not None:
            session._replay_task.cancel()

    @pytest.mark.asyncio
    async def test_on_boot_keeps_newest_transaction_for_connector(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.clear_sessions_seen = AsyncMock()
        older = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
        newer = datetime(2026, 4, 26, 12, 5, tzinfo=timezone.utc)
        mock_timescale.fetch_open_sessions = AsyncMock(
            return_value=[
                {"transaction_id": 556, "connector_id": 1, "start_time": newer},
                {"transaction_id": 555, "connector_id": 1, "start_time": older},
            ]
        )

        await session._on_boot(
            cp_id="test_station_001",
            vendor="V",
            model="M",
            serial_number="S",
            firmware_version="F",
        )
        await asyncio.sleep(0)

        assert session._cp.transactions[1] == 556
        assert session._cp.current_transaction_id == 556

    @pytest.mark.asyncio
    async def test_on_boot_tolerates_fetch_failure(self, session, mock_timescale) -> None:
        mock_timescale.clear_sessions_seen = AsyncMock()
        mock_timescale.fetch_open_sessions = AsyncMock(side_effect=RuntimeError("db down"))
        # Should not raise — failures are best-effort.
        await session._on_boot(
            cp_id="test_station_001",
            vendor="V",
            model="M",
            serial_number=None,
            firmware_version=None,
        )

    @pytest.mark.asyncio
    async def test_on_status_change_persists_to_connector_status(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.insert_connector_status = AsyncMock()

        await session._on_status_change(
            cp_id="test_station_001",
            connector_id=1,
            status="Charging",
            error_code="NoError",
            timestamp="2026-04-26T12:00:00Z",
        )

        mock_timescale.insert_connector_status.assert_awaited_once()
        payload = mock_timescale.insert_connector_status.call_args.args[0]
        assert payload["status"] == "Charging"
        assert payload["error_code"] == "NoError"

    @pytest.mark.asyncio
    async def test_on_status_change_tolerates_db_failure(self, session, mock_timescale) -> None:
        mock_timescale.insert_connector_status = AsyncMock(side_effect=RuntimeError("db down"))
        # Must not raise; status pushes still continue downstream.
        await session._on_status_change(
            cp_id="test_station_001",
            connector_id=1,
            status="Faulted",
            error_code="GroundFailure",
            timestamp=None,
        )

    @pytest.mark.asyncio
    async def test_on_transaction_stop_closes_session_row(self, session, mock_timescale) -> None:
        mock_timescale.close_open_session = AsyncMock(return_value=None)

        await session._on_transaction_stop(
            cp_id="test_station_001",
            transaction_id=4242,
            id_tag="TAG_X",
            meter_stop=12345,
            timestamp="2026-04-26T13:00:00Z",
            reason="Local",
        )

        mock_timescale.close_open_session.assert_awaited_once()
        args = mock_timescale.close_open_session.call_args.args
        assert args[0] == "test_station_001"
        assert args[1] == 4242
        # args[2] is the parsed end_time
        assert args[2].year == 2026
        # meter_stop_wh is plumbed through as the kwarg (migration 036).
        kwargs = mock_timescale.close_open_session.call_args.kwargs
        assert kwargs["meter_stop_wh"] == 12345

    @pytest.mark.asyncio
    async def test_on_transaction_start_stashes_meter_start_wh(
        self, session, mock_timescale
    ) -> None:
        """meter_start arrives in OCPP StartTransaction and must be stashed.

        Without this, _next_transaction_id has no meter_start_wh to send
        to insert_open_session and the DB row goes in with NULL — the
        whole bug we are fixing (migration 036).
        """
        await session._on_transaction_start(
            cp_id="test_station_001",
            connector_id=1,
            id_tag="TAG-001",
            meter_start=17500,
            timestamp="2026-04-26T12:00:00Z",
        )
        await asyncio.sleep(0)

        assert session._pending_start is not None
        assert session._pending_start["meter_start_wh"] == 17500

    @pytest.mark.asyncio
    async def test_next_transaction_id_persists_meter_start_wh(
        self, session, mock_timescale
    ) -> None:
        """insert_open_session must receive the stashed meter_start_wh."""
        mock_timescale.next_transaction_id = AsyncMock(return_value=1000)
        mock_timescale.insert_open_session = AsyncMock()
        session._pending_start = {
            "connector_id": 1,
            "evse_id": 1,
            "id_tag": "TAG_Z",
            "start_time": datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc),
            "meter_start_wh": 22000,
            "vehicle_id": None,
            "driver_id": None,
            "card_id": None,
        }

        await session._next_transaction_id()

        kwargs = mock_timescale.insert_open_session.call_args.kwargs
        assert kwargs["meter_start_wh"] == 22000

    @pytest.mark.asyncio
    async def test_on_transaction_stop_warns_on_anomalous_delta(
        self, session, mock_timescale, caplog
    ) -> None:
        """meter_stop < meter_start_wh (rollover/replacement) -> WARN, no kWh.

        The close UPDATE keeps energy_delivered_kwh NULL via the CASE
        gate; the adapter must surface that for operators rather than
        silently swallow it.
        """
        import logging

        # Simulates the DB-side CASE gate leaving energy_delivered_kwh NULL.
        mock_timescale.close_open_session = AsyncMock(
            return_value={"meter_start_wh": 9000, "energy_delivered_kwh": None}
        )

        with caplog.at_level(logging.WARNING, logger="src.websocket_handler.ocpp16_adapter"):
            await session._on_transaction_stop(
                cp_id="test_station_001",
                transaction_id=4243,
                id_tag="TAG_X",
                meter_stop=1000,  # < 9000
                timestamp="2026-04-26T13:00:00Z",
                reason="Local",
            )

        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            "meter_stop_lt_meter_start" in m for m in warnings
        ), f"Expected anomaly WARN with reason=meter_stop_lt_meter_start; got: {warnings}"

    @pytest.mark.asyncio
    async def test_on_transaction_stop_warns_on_missing_meter_start(
        self, session, mock_timescale, caplog
    ) -> None:
        """Legacy row inserted before mig 036 has meter_start_wh NULL.

        Close cannot compute a delta; operator gets a WARN with the
        reason tag so they can reconcile manually.
        """
        import logging

        mock_timescale.close_open_session = AsyncMock(
            return_value={"meter_start_wh": None, "energy_delivered_kwh": None}
        )

        with caplog.at_level(logging.WARNING, logger="src.websocket_handler.ocpp16_adapter"):
            await session._on_transaction_stop(
                cp_id="test_station_001",
                transaction_id=4244,
                id_tag="TAG_X",
                meter_stop=5000,
                timestamp="2026-04-26T13:00:00Z",
                reason="Local",
            )

        warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            "missing_meter_start" in m for m in warnings
        ), f"Expected missing_meter_start WARN; got: {warnings}"

    @pytest.mark.asyncio
    async def test_on_transaction_stop_no_warn_on_happy_path(
        self, session, mock_timescale, caplog
    ) -> None:
        """Successful delta write must not produce noisy WARN logs."""
        import logging

        mock_timescale.close_open_session = AsyncMock(
            return_value={"meter_start_wh": 1000, "energy_delivered_kwh": 4.0}
        )

        with caplog.at_level(logging.WARNING, logger="src.websocket_handler.ocpp16_adapter"):
            await session._on_transaction_stop(
                cp_id="test_station_001",
                transaction_id=4245,
                id_tag="TAG_X",
                meter_stop=5000,
                timestamp="2026-04-26T13:00:00Z",
                reason="Local",
            )

        anomaly_warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING" and "Anomalous meter delta" in r.getMessage()
        ]
        assert anomaly_warnings == []

    @pytest.mark.asyncio
    async def test_on_transaction_stop_silent_when_no_row_matched(
        self, session, mock_timescale, caplog
    ) -> None:
        """Idempotent retry (close returns None) is silent — not an anomaly."""
        import logging

        mock_timescale.close_open_session = AsyncMock(return_value=None)

        with caplog.at_level(logging.WARNING, logger="src.websocket_handler.ocpp16_adapter"):
            await session._on_transaction_stop(
                cp_id="test_station_001",
                transaction_id=4246,
                id_tag="TAG_X",
                meter_stop=5000,
                timestamp="2026-04-26T13:00:00Z",
                reason="Local",
            )

        anomaly_warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelname == "WARNING" and "Anomalous meter delta" in r.getMessage()
        ]
        assert anomaly_warnings == []

    @pytest.mark.asyncio
    async def test_next_transaction_id_persists_when_pending_set(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.next_transaction_id = AsyncMock(return_value=999)
        mock_timescale.insert_open_session = AsyncMock()
        session._pending_start = {
            "connector_id": 1,
            "evse_id": 1,
            "id_tag": "TAG_Y",
            "start_time": datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc),
            "vehicle_id": "660e8400-e29b-41d4-a716-446655440001",
            "driver_id": "770e8400-e29b-41d4-a716-446655440001",
            "card_id": "880e8400-e29b-41d4-a716-446655440001",
        }

        tx_id = await session._next_transaction_id()

        assert tx_id == 999
        mock_timescale.insert_open_session.assert_awaited_once()
        kwargs = mock_timescale.insert_open_session.call_args.kwargs
        assert kwargs["transaction_id"] == 999
        assert kwargs["station_id"] == "test_station_001"
        assert kwargs["vehicle_id"] == "660e8400-e29b-41d4-a716-446655440001"
        assert kwargs["driver_id"] == "770e8400-e29b-41d4-a716-446655440001"
        assert kwargs["card_id"] == "880e8400-e29b-41d4-a716-446655440001"
        # Stash must be cleared so a stray call cannot double-insert.
        assert session._pending_start is None

    @pytest.mark.asyncio
    async def test_next_transaction_id_clears_pending_when_sequence_fails(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.next_transaction_id = AsyncMock(side_effect=RuntimeError("db down"))
        session._pending_start = {
            "connector_id": 1,
            "evse_id": 1,
            "id_tag": "TAG_Y",
            "start_time": datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc),
        }

        with pytest.raises(RuntimeError):
            await session._next_transaction_id()
        assert session._pending_start is None

    @pytest.mark.asyncio
    async def test_next_transaction_id_marks_fallback_tx_as_in_memory_only_when_sequence_fails(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.next_transaction_id = AsyncMock(side_effect=RuntimeError("db down"))
        session._pending_start = {
            "connector_id": 1,
            "evse_id": 1,
            "id_tag": "TAG_Y",
            "start_time": datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc),
        }
        session._cp.transactions[1] = 9001

        with pytest.raises(RuntimeError):
            await session._next_transaction_id()

        assert 9001 in session._in_memory_only_tx_ids
        assert session._pending_start is None

    @pytest.mark.asyncio
    async def test_next_transaction_id_skips_insert_without_pending(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.next_transaction_id = AsyncMock(return_value=1)
        mock_timescale.insert_open_session = AsyncMock()
        session._pending_start = None

        tx_id = await session._next_transaction_id()

        assert tx_id == 1
        mock_timescale.insert_open_session.assert_not_called()
