"""Unit tests for the OCPP 1.6 offline-RFID local-authorization-list sync.

Covers ``src/adapters/ocpp/local_auth_sync.py`` and the BootNotification
wiring in ``src/websocket_handler/ocpp16_adapter.py`` that schedules the
sync after the queued-command replay.

The DB layer is mocked: SQL correctness for ``list_authorized_id_tags``
is validated separately by integration tests against a real TimescaleDB.
These unit tests pin the orchestration contract — version monotonicity,
first-vs-subsequent bootstrap, fallback paths, and the env kill-switch.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.ocpp.local_auth_sync import (
    SyncResult,
    _BOOTSTRAP_CONFIG_KEYS,
    _format_entries,
    sync_charger,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_db(
    *,
    station_row: Any,
    id_tag_rows: list[dict] | Exception | None = None,
) -> MagicMock:
    """Build a fake asyncpg-style db with .fetchrow / .fetch / .execute.

    ``station_row`` is what ``fetchrow`` for the ``charging_stations`` lookup
    returns (or an Exception to raise). ``id_tag_rows`` is what the
    ``fetch`` called from ``list_authorized_id_tags`` returns.
    """
    db = MagicMock()
    if isinstance(station_row, Exception):
        db.fetchrow = AsyncMock(side_effect=station_row)
    else:
        db.fetchrow = AsyncMock(return_value=station_row)

    if isinstance(id_tag_rows, Exception):
        db.fetch = AsyncMock(side_effect=id_tag_rows)
    elif id_tag_rows is None:
        db.fetch = AsyncMock(return_value=[])
    else:
        db.fetch = AsyncMock(return_value=id_tag_rows)

    db.execute = AsyncMock()
    return db


def _make_cp(
    *,
    send_status: str = "Accepted",
    send_raises: Exception | None = None,
    vendor: str | None = None,
) -> MagicMock:
    """Build a fake FleetChargePoint with the methods sync_charger calls."""
    cp = MagicMock()
    cp.vendor = vendor
    cp.change_configuration = AsyncMock(return_value="Accepted")
    if send_raises is not None:
        cp.send_local_list = AsyncMock(side_effect=send_raises)
    else:
        cp.send_local_list = AsyncMock(return_value=send_status)
    return cp


# ---------------------------------------------------------------------------
# _format_entries
# ---------------------------------------------------------------------------


class TestFormatEntries:
    def test_shapes_rows_into_ocpp_authorization_data(self) -> None:
        rows = [
            {"id_tag": "ABC", "source": "vehicle"},
            {"id_tag": "DEF", "source": "rfid_card_driver"},
        ]
        entries = _format_entries(rows)
        assert entries == [
            {"id_tag": "ABC", "id_tag_info": {"status": "Accepted"}},
            {"id_tag": "DEF", "id_tag_info": {"status": "Accepted"}},
        ]

    def test_empty_input_yields_empty_list(self) -> None:
        assert _format_entries([]) == []


# ---------------------------------------------------------------------------
# sync_charger — environment / DB skip paths
# ---------------------------------------------------------------------------


class TestSyncChargerSkipPaths:
    @pytest.mark.asyncio
    async def test_env_disabled_short_circuits(self, monkeypatch) -> None:
        monkeypatch.setenv("OCPP_DISABLE_LOCAL_AUTH_LIST", "true")
        db = _make_db(station_row={"id": "uuid-1", "local_list_version": 0})
        cp = _make_cp()

        result = await sync_charger(cp, db, "station-001")

        assert result == SyncResult(
            status="skipped", version=0, entries=0, reason="env_disabled"
        )
        # Must not touch the DB or the charger when disabled.
        db.fetchrow.assert_not_awaited()
        db.execute.assert_not_awaited()
        cp.send_local_list.assert_not_awaited()
        cp.change_configuration.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_station_skipped(self, monkeypatch) -> None:
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(station_row=None)
        cp = _make_cp()

        result = await sync_charger(cp, db, "station-missing")

        assert result.status == "skipped"
        assert result.reason == "unknown_station"
        cp.send_local_list.assert_not_awaited()
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_db_error_fetching_station_skipped(self, monkeypatch) -> None:
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(station_row=RuntimeError("connection lost"))
        cp = _make_cp()

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "skipped"
        assert result.reason == "db_error"
        cp.send_local_list.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_db_error_listing_tags_skipped_without_state_change(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={"id": "uuid-1", "local_list_version": 5},
            id_tag_rows=RuntimeError("query timeout"),
        )
        cp = _make_cp()

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "skipped"
        assert result.reason == "db_error"
        # Version reported back is the unchanged current version.
        assert result.version == 5
        cp.send_local_list.assert_not_awaited()
        db.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# sync_charger — happy paths
# ---------------------------------------------------------------------------


class TestSyncChargerFirstSync:
    @pytest.mark.asyncio
    async def test_first_sync_pushes_bootstrap_config_then_full_list(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={"id": "uuid-1", "local_list_version": 0},
            id_tag_rows=[
                {"id_tag": "VEH-1", "source": "vehicle"},
                {"id_tag": "CARD-A", "source": "rfid_card_driver"},
            ],
        )
        cp = _make_cp(send_status="Accepted")

        result = await sync_charger(cp, db, "station-001")

        assert result == SyncResult(status="Accepted", version=1, entries=2)

        # All three bootstrap config keys are pushed before SendLocalList.
        config_calls = cp.change_configuration.await_args_list
        assert [c.args for c in config_calls] == list(_BOOTSTRAP_CONFIG_KEYS)

        # Full update with version=1 (current 0 + 1).
        cp.send_local_list.assert_awaited_once_with(
            list_version=1,
            update_type="Full",
            local_authorization_list=[
                {"id_tag": "VEH-1", "id_tag_info": {"status": "Accepted"}},
                {"id_tag": "CARD-A", "id_tag_info": {"status": "Accepted"}},
            ],
        )

        # DB UPDATE with new version + status.
        update_call = db.execute.await_args_list[-1]
        assert "local_list_version = $1" in update_call.args[0]
        assert update_call.args[1] == 1  # new version
        assert update_call.args[2] == "Accepted"
        assert update_call.args[3] == "uuid-1"


class TestSyncChargerSubsequentSync:
    @pytest.mark.asyncio
    async def test_subsequent_sync_skips_bootstrap_and_bumps_version(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={"id": "uuid-1", "local_list_version": 7},
            id_tag_rows=[{"id_tag": "VEH-9", "source": "vehicle"}],
        )
        cp = _make_cp(send_status="Accepted")

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Accepted"
        assert result.version == 8
        cp.change_configuration.assert_not_awaited()
        cp.send_local_list.assert_awaited_once()
        assert cp.send_local_list.await_args.kwargs["list_version"] == 8

    @pytest.mark.asyncio
    async def test_empty_tag_list_still_pushes_full_update(self, monkeypatch) -> None:
        """A site with no authorized tags must still send a Full update —
        otherwise a tag revoked between syncs would remain on the charger."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={"id": "uuid-1", "local_list_version": 3},
            id_tag_rows=[],
        )
        cp = _make_cp(send_status="Accepted")

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Accepted"
        assert result.entries == 0
        cp.send_local_list.assert_awaited_once()
        assert cp.send_local_list.await_args.kwargs["local_authorization_list"] == []


# ---------------------------------------------------------------------------
# sync_charger — non-Accepted SendLocalList responses
# ---------------------------------------------------------------------------


class TestSyncChargerFailureModes:
    @pytest.mark.asyncio
    async def test_not_supported_records_status_without_bumping_version(
        self, monkeypatch
    ) -> None:
        """ABB cap or vendor-disabled charger: status recorded, version held.

        FleetChargePoint.send_local_list returns 'NotSupported' when the
        ABB 16-entry cap is exceeded; we must not advance the DB version
        because the charger never accepted the new list.
        """
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={"id": "uuid-1", "local_list_version": 4},
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp(send_status="NotSupported")

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "NotSupported"
        # Version held at current.
        assert result.version == 4
        # DB UPDATE was the "stamp last_status only" variant — does NOT mention
        # local_list_version or local_list_synced_at assignment.
        update_call = db.execute.await_args_list[-1]
        assert "local_list_version" not in update_call.args[0]
        assert "local_list_synced_at" not in update_call.args[0]
        assert "local_list_last_status" in update_call.args[0]
        assert update_call.args[1] == "NotSupported"

    @pytest.mark.asyncio
    async def test_failed_response_records_status_without_bumping_version(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={"id": "uuid-1", "local_list_version": 2},
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp(send_status="Failed")

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Failed"
        assert result.version == 2

    @pytest.mark.asyncio
    async def test_send_local_list_raises_is_caught_as_failed(
        self, monkeypatch
    ) -> None:
        """Defensive path: production FleetChargePoint already catches its own
        exceptions, but a duck-typed test fake might not. The orchestration
        must never propagate an exception out of sync_charger."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={"id": "uuid-1", "local_list_version": 0},
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp(send_raises=RuntimeError("socket closed"))

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Failed"
        # version held at current (0) since not Accepted.
        assert result.version == 0
        # DB still records the last_status.
        update_call = db.execute.await_args_list[-1]
        assert update_call.args[1] == "Failed"

    @pytest.mark.asyncio
    async def test_db_error_during_state_update_is_swallowed(
        self, monkeypatch
    ) -> None:
        """If the post-push DB UPDATE fails, we still return the SendLocalList
        outcome — the charger has the list; we just can't record we sent it."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={"id": "uuid-1", "local_list_version": 0},
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        db.execute = AsyncMock(side_effect=RuntimeError("write failed"))
        cp = _make_cp(send_status="Accepted")

        result = await sync_charger(cp, db, "station-001")

        # Still reports the wire result.
        assert result.status == "Accepted"
        assert result.version == 1


# ---------------------------------------------------------------------------
# Bootstrap config defensiveness
# ---------------------------------------------------------------------------


class TestBootstrapConfig:
    @pytest.mark.asyncio
    async def test_change_configuration_failure_does_not_block_send(
        self, monkeypatch
    ) -> None:
        """A charger that doesn't expose LocalAuthListEnabled must still get
        SendLocalList — a 'NotSupported' response on a config key cannot
        disable the offline-RFID feature for that charger."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={"id": "uuid-1", "local_list_version": 0},
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp()
        cp.change_configuration = AsyncMock(return_value="NotSupported")

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Accepted"
        cp.send_local_list.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_change_configuration_exception_does_not_block_send(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={"id": "uuid-1", "local_list_version": 0},
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp()
        cp.change_configuration = AsyncMock(side_effect=RuntimeError("boom"))

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Accepted"
        cp.send_local_list.assert_awaited_once()


# ---------------------------------------------------------------------------
# OCPP16Session BootNotification wiring
# ---------------------------------------------------------------------------


@pytest.fixture()
def _session_factory():
    from unittest.mock import MagicMock as _MM

    def make(*, pg_pool: Any = None) -> Any:
        from src.websocket_handler.ocpp16_adapter import OCPP16Session

        ws = _MM()
        ws.send = AsyncMock()
        ws.recv = AsyncMock()
        ws.close = AsyncMock()

        tc = _MM()
        tc.fetch_open_sessions = AsyncMock(return_value=[])
        tc.next_transaction_id = AsyncMock(return_value=1)
        tc.next_charging_profile_id = AsyncMock(return_value=1)
        tc.insert_telemetry_batch = AsyncMock()
        tc.store_security_event = AsyncMock()
        tc.lookup_id_tag = AsyncMock(return_value=None)
        tc.pg_pool = pg_pool

        from src.websocket_handler.rfid_authorization import RFIDAuthorizationService

        mh = _MM()
        mh._push_to_main_api = AsyncMock()
        mh.rfid_authorization = RFIDAuthorizationService(tc, _MM())

        return OCPP16Session(
            station_id="boot-wire-001",
            websocket=ws,
            timescale_client=tc,
            message_handler=mh,
        )

    return make


class TestBootSchedulesLocalAuthSync:
    @pytest.mark.asyncio
    async def test_on_boot_schedules_local_auth_sync_task(
        self, _session_factory
    ) -> None:
        session = _session_factory(pg_pool=MagicMock())
        # _resolve_tenant_context relies on _supabase_client — left None.
        # fetch_open_sessions is mocked to []; no transactions to reload.
        await session._on_boot(
            cp_id="boot-wire-001",
            vendor="ABB",
            model="Terra AC",
            serial_number="SN-1",
            firmware_version="1.8.21",
        )
        try:
            assert session._local_auth_sync_task is not None
            assert isinstance(session._local_auth_sync_task, asyncio.Task)
        finally:
            # Cancel the scheduled task so the test event loop doesn't leak it.
            if session._local_auth_sync_task is not None:
                session._local_auth_sync_task.cancel()
                try:
                    await session._local_auth_sync_task
                except (asyncio.CancelledError, Exception):
                    pass
            if session._replay_task is not None:
                session._replay_task.cancel()
                try:
                    await session._replay_task
                except (asyncio.CancelledError, Exception):
                    pass

    @pytest.mark.asyncio
    async def test_delayed_local_auth_sync_skips_when_pool_missing(
        self, _session_factory, monkeypatch
    ) -> None:
        """If the timescale client never finished initialising, the sync must
        log-and-return rather than raising — the WS handler must keep
        accepting OCPP messages on the socket even if pg_pool is None."""
        session = _session_factory(pg_pool=None)
        # Pre-empt the replay sleep so the helper proceeds immediately.
        session._replay_task = None
        # Spy on sync_charger to ensure it is NOT called when pool is None.
        called = {"count": 0}

        async def _fake_sync(*args: Any, **kwargs: Any) -> SyncResult:  # pragma: no cover - asserted via counter
            called["count"] += 1
            return SyncResult(status="Accepted", version=1, entries=0)

        monkeypatch.setattr(
            "src.websocket_handler.ocpp16_adapter.sync_local_auth_list", _fake_sync
        )
        await session._delayed_local_auth_sync()
        assert called["count"] == 0

    @pytest.mark.asyncio
    async def test_delayed_local_auth_sync_propagates_own_cancellation(
        self, _session_factory, monkeypatch
    ) -> None:
        session = _session_factory(pg_pool=MagicMock())
        replay_gate = asyncio.Event()

        async def _slow_replay() -> None:
            await replay_gate.wait()

        session._replay_task = asyncio.create_task(_slow_replay())

        sync_called = {"count": 0}

        async def _fake_sync(*args: Any, **kwargs: Any) -> SyncResult:
            sync_called["count"] += 1
            return SyncResult(status="Accepted", version=1, entries=0)

        monkeypatch.setattr("src.websocket_handler.ocpp16_adapter.sync_local_auth_list", _fake_sync)

        task = asyncio.create_task(session._delayed_local_auth_sync())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sync_called["count"] == 0
        replay_gate.set()
        with pytest.raises(asyncio.CancelledError):
            await session._replay_task
