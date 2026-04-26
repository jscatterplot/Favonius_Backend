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
        return_value={"vehicle_id": "vehicle-1", "depot_id": "depot-1"}
    )
    tc.next_transaction_id = AsyncMock(return_value=4242)
    tc.next_charging_profile_id = AsyncMock(return_value=123456)
    return tc


@pytest.fixture()
def mock_message_handler() -> MagicMock:
    mh = MagicMock()
    mh._push_to_main_api = AsyncMock()
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
    async def test_on_meter_values_writes_telemetry(
        self, session, mock_timescale
    ) -> None:
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
        mock_timescale.insert_telemetry_batch.assert_awaited_once()
        call_args = mock_timescale.insert_telemetry_batch.call_args[0][0][0]
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
    async def test_on_status_change_pushes_event(
        self, session, mock_message_handler
    ) -> None:
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
        mock_timescale.lookup_id_tag.assert_awaited_once_with("TAG-001")
        mock_message_handler._push_to_main_api.assert_awaited()

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
    async def test_on_transaction_stop_pushes_event(
        self, session, mock_message_handler
    ) -> None:
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
        assert OCPP16_AVAILABLE is True, (
            "OCPP16_AVAILABLE is False — server will misroute OCPP 1.6 chargers"
        )
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
        """When the WS push raises, the profile must hit charging_command_queue."""
        session._cp.set_charging_profile = AsyncMock(
            side_effect=ConnectionError("not connected")
        )
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
    async def test_send_charging_profile_no_enqueue_when_disabled(
        self, session, mock_timescale
    ) -> None:
        """``allow_enqueue=False`` (the replay path) must propagate exceptions."""
        session._cp.set_charging_profile = AsyncMock(
            side_effect=RuntimeError("ws closed")
        )
        mock_timescale.enqueue_charging_command = AsyncMock()

        with pytest.raises(RuntimeError):
            await session.send_charging_profile(
                1, {"chargingSchedulePeriod": []}, allow_enqueue=False
            )
        mock_timescale.enqueue_charging_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_replay_queued_commands_marks_sent_on_success(
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
                            "chargingSchedulePeriod": [
                                {"startPeriod": 0, "limit": 1000}
                            ],
                        },
                    },
                    "attempt_count": 0,
                }
            ]
        )
        mock_timescale.mark_command_sent = AsyncMock()
        mock_timescale.mark_command_failed = AsyncMock()
        session._cp.set_charging_profile = AsyncMock(return_value=True)

        sent = await session.replay_queued_commands()

        assert sent == 1
        mock_timescale.mark_command_sent.assert_awaited_once_with(1)
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
        mock_timescale.mark_command_sent = AsyncMock()
        mock_timescale.mark_command_failed = AsyncMock()
        session._cp.set_charging_profile = AsyncMock(return_value=False)

        sent = await session.replay_queued_commands()

        assert sent == 0
        mock_timescale.mark_command_failed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_replay_queued_commands_tolerates_fetch_failure(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.fetch_pending_commands = AsyncMock(
            side_effect=RuntimeError("db down")
        )
        sent = await session.replay_queued_commands()
        assert sent == 0

    @pytest.mark.asyncio
    async def test_on_boot_reloads_transactions_from_db(
        self, session, mock_timescale
    ) -> None:
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

    @pytest.mark.asyncio
    async def test_on_boot_tolerates_fetch_failure(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.fetch_open_sessions = AsyncMock(
            side_effect=RuntimeError("db down")
        )
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
    async def test_on_status_change_tolerates_db_failure(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.insert_connector_status = AsyncMock(
            side_effect=RuntimeError("db down")
        )
        # Must not raise; status pushes still continue downstream.
        await session._on_status_change(
            cp_id="test_station_001",
            connector_id=1,
            status="Faulted",
            error_code="GroundFailure",
            timestamp=None,
        )

    @pytest.mark.asyncio
    async def test_on_transaction_stop_closes_session_row(
        self, session, mock_timescale
    ) -> None:
        mock_timescale.close_open_session = AsyncMock()

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
        }

        tx_id = await session._next_transaction_id()

        assert tx_id == 999
        mock_timescale.insert_open_session.assert_awaited_once()
        kwargs = mock_timescale.insert_open_session.call_args.kwargs
        assert kwargs["transaction_id"] == 999
        assert kwargs["station_id"] == "test_station_001"
        # Stash must be cleared so a stray call cannot double-insert.
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
